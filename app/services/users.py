"""Administración de usuarios: listar, identidad, admin, suspensión y borrado."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.exceptions import (
    AccessDenied,
    CannotModifySelf,
    EmailTaken,
    LastAdmin,
    NotFound,
    Validation,
)
from app.models import CourseMembership, User
from app.models.user import SystemRole
from app.permissions.matrix import has_site_permission
from app.permissions.types import CourseRole, Permission
from app.schemas.pagination import Paginated, PaginationQuery
from app.schemas.users import (
    AdminUserIdentityUpdate,
    AdminUserStatus,
    AdminUserView,
    UserSearchHit,
)
from app.services.auth import to_public_user
from app.services.roles import assign_system_role

_SEARCH_Q_MAX = 200


def to_admin_user_view(user: User) -> AdminUserView:
    public = to_public_user(user)
    return AdminUserView(
        id=public.id,
        name=public.name,
        email=public.email,
        first_name=public.first_name,
        last_name=public.last_name,
        avatar_file_id=public.avatar_file_id,
        email_notifications_enabled=public.email_notifications_enabled,
        suspended_at=user.suspended_at,
        deleted_at=user.deleted_at,
        system_role_id=user.system_role_id,
    )


def to_search_hit(user: User) -> UserSearchHit:
    return UserSearchHit(
        id=user.id,
        name=user.display_name,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
    )


def _escape_like(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _text_match(term: str) -> ColumnElement[bool]:
    pattern = f"%{_escape_like(term)}%"
    display_name = func.concat(User.first_name, " ", User.last_name)
    return or_(
        User.email.ilike(pattern, escape="\\"),
        User.first_name.ilike(pattern, escape="\\"),
        User.last_name.ilike(pattern, escape="\\"),
        display_name.ilike(pattern, escape="\\"),
    )


def _status_filters(status: AdminUserStatus | None) -> list[ColumnElement[bool]]:
    if status is None:
        return [User.deleted_at.is_(None)]
    if status is AdminUserStatus.ALL:
        return []
    if status is AdminUserStatus.ACTIVE:
        return [User.deleted_at.is_(None), User.suspended_at.is_(None)]
    if status is AdminUserStatus.SUSPENDED:
        return [User.deleted_at.is_(None), User.suspended_at.is_not(None)]
    return [User.deleted_at.is_not(None)]


def _active_admin_clause() -> ColumnElement[bool]:
    return and_(
        or_(User.is_admin.is_(True), User.system_role_id == SystemRole.ADMIN),
        User.deleted_at.is_(None),
        User.suspended_at.is_(None),
    )


def _is_active_admin(user: User) -> bool:
    return (
        (user.is_admin or user.system_role_id == SystemRole.ADMIN)
        and user.deleted_at is None
        and user.suspended_at is None
    )


def _ensure_admin_remains(locked: list[User], *, actor: User, target: User) -> None:
    others = [
        user for user in locked if user.id != target.id and _is_active_admin(user)
    ]
    if others:
        return
    if actor.id == target.id:
        raise CannotModifySelf()
    raise LastAdmin()


async def _lock_target(session: AsyncSession, user_id: UUID) -> User:
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None or user.deleted_at is not None:
        raise NotFound()
    return user


async def _lock_target_and_active_admins(
    session: AsyncSession, user_id: UUID
) -> tuple[User, list[User]]:
    """Un solo SELECT ... FOR UPDATE ordenado por id (sin deadlock)."""

    rows = list(
        await session.scalars(
            select(User)
            .where(or_(User.id == user_id, _active_admin_clause()))
            .order_by(User.id)
            .with_for_update()
        )
    )
    target = next((user for user in rows if user.id == user_id), None)
    if target is None or target.deleted_at is not None:
        raise NotFound()
    admins = [user for user in rows if _is_active_admin(user)]
    return target, admins


async def list_admin_users(
    session: AsyncSession,
    *,
    query: str | None,
    status: AdminUserStatus | None,
    pagination: PaginationQuery,
) -> Paginated[AdminUserView]:
    filters = list(_status_filters(status))
    term = query.strip() if query else ""
    if term:
        filters.append(_text_match(term))

    count_stmt = select(func.count()).select_from(User)
    list_stmt = select(User)
    if filters:
        count_stmt = count_stmt.where(*filters)
        list_stmt = list_stmt.where(*filters)

    total = int(await session.scalar(count_stmt) or 0)
    offset = (pagination.page - 1) * pagination.page_size
    rows = await session.scalars(
        list_stmt.order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(pagination.page_size)
    )
    return Paginated(
        items=[to_admin_user_view(user) for user in rows],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


async def update_user_identity(
    session: AsyncSession,
    *,
    actor: User,
    user_id: UUID,
    body: AdminUserIdentityUpdate,
) -> User:
    if not has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS):
        raise AccessDenied()
    target = await _lock_target(session, user_id)
    if body.first_name is not None:
        target.first_name = body.first_name
    if body.last_name is not None:
        target.last_name = body.last_name
    if body.email is not None:
        email = str(body.email)
        taken = await session.scalar(
            select(User.id).where(User.email == email, User.id != target.id)
        )
        if taken is not None:
            raise EmailTaken()
        target.email = email
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise EmailTaken() from exc
    return target


async def set_user_admin(
    session: AsyncSession,
    *,
    actor: User,
    user_id: UUID,
    is_admin: bool,
) -> User:
    if not has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS):
        raise AccessDenied()
    if not is_admin and actor.id == user_id:
        await _lock_target(session, user_id)
        raise CannotModifySelf()
    if is_admin:
        target = await _lock_target(session, user_id)
        if target.is_admin and target.system_role_id == SystemRole.ADMIN:
            return target
        return await assign_system_role(
            session, actor=actor, target=target, role_id=SystemRole.ADMIN
        )

    target, locked = await _lock_target_and_active_admins(session, user_id)
    already_admin = target.is_admin or target.system_role_id == SystemRole.ADMIN
    if not already_admin:
        return target
    if _is_active_admin(target):
        _ensure_admin_remains(locked, actor=actor, target=target)
    return await assign_system_role(
        session, actor=actor, target=target, role_id=SystemRole.ALUMNO
    )


async def set_user_suspended(
    session: AsyncSession,
    *,
    actor: User,
    user_id: UUID,
    suspended: bool,
) -> User:
    if not has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS):
        raise AccessDenied()
    if suspended:
        target, locked = await _lock_target_and_active_admins(session, user_id)
        if target.suspended_at is None and _is_active_admin(target):
            _ensure_admin_remains(locked, actor=actor, target=target)
        if target.suspended_at is None:
            target.suspended_at = datetime.now(UTC)
        await session.flush()
        return target

    target = await _lock_target(session, user_id)
    target.suspended_at = None
    await session.flush()
    return target


async def soft_delete_user(
    session: AsyncSession, *, actor: User, user_id: UUID
) -> None:
    if not has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS):
        raise AccessDenied()
    if actor.id == user_id:
        await _lock_target(session, user_id)
        raise CannotModifySelf()
    target, locked = await _lock_target_and_active_admins(session, user_id)
    if _is_active_admin(target):
        _ensure_admin_remains(locked, actor=actor, target=target)
    target.deleted_at = datetime.now(UTC)
    await session.flush()


async def actor_can_search_users(session: AsyncSession, actor: User) -> bool:
    if has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS):
        return True
    if has_site_permission(actor.system_role, Permission.SITE_MANAGE_COURSES):
        return True
    teach = await session.scalar(
        select(CourseMembership.id)
        .where(
            CourseMembership.user_id == actor.id,
            CourseMembership.course_role == CourseRole.TEACHER,
            CourseMembership.deleted_at.is_(None),
        )
        .limit(1)
    )
    return teach is not None


async def search_users(
    session: AsyncSession,
    *,
    actor: User,
    q: str,
    pagination: PaginationQuery,
) -> Paginated[UserSearchHit]:
    if not await actor_can_search_users(session, actor):
        raise AccessDenied()
    term = q.strip()
    if len(term) > _SEARCH_Q_MAX:
        raise Validation(fields=["q"])
    if not term:
        return Paginated(
            items=[],
            page=pagination.page,
            page_size=pagination.page_size,
            total=0,
        )

    filters: list[ColumnElement[bool]] = [
        User.deleted_at.is_(None),
        User.suspended_at.is_(None),
        _text_match(term),
    ]
    total = int(
        await session.scalar(select(func.count()).select_from(User).where(*filters))
        or 0
    )
    offset = (pagination.page - 1) * pagination.page_size
    rows = await session.scalars(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(pagination.page_size)
    )
    return Paginated(
        items=[to_search_hit(user) for user in rows],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )
