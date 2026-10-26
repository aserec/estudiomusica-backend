"""Administración de usuarios: listar, identidad, admin, suspensión y borrado."""

import unicodedata
from datetime import UTC, datetime
from uuid import UUID

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import and_, func, or_, select, update
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
from app.models import AuthSession, CourseMembership, User
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
_TEACHER_Q_MIN_EFFECTIVE = 3
_TEACHER_SEARCH_CAP = 10
_LIKE_WILDCARDS = frozenset("%_")
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


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


def to_search_hit(user: User, *, include_email: bool) -> UserSearchHit:
    return UserSearchHit(
        id=user.id,
        name=user.display_name,
        email=user.email if include_email else None,
        first_name=user.first_name,
        last_name=user.last_name,
    )


def _escape_like(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _effective_search_len(term: str) -> int:
    return sum(1 for char in term if char not in _LIKE_WILDCARDS)


def _name_match(term: str) -> ColumnElement[bool]:
    pattern = f"%{_escape_like(term)}%"
    display_name = func.concat(User.first_name, " ", User.last_name)
    return or_(
        User.first_name.ilike(pattern, escape="\\"),
        User.last_name.ilike(pattern, escape="\\"),
        display_name.ilike(pattern, escape="\\"),
    )


def _email_term(term: str) -> str:
    """Misma normalización que el login: ``EmailStr`` + ``lower`` (IDNA).

    Si el término no es un email válido, ``strip`` + ``lower`` + NFC.
    """

    try:
        validated = _EMAIL_ADAPTER.validate_python(term)
    except ValidationError:
        return unicodedata.normalize("NFC", term.strip().lower())
    return str(validated).lower()


def _visible_user_filters() -> list[ColumnElement[bool]]:
    return [User.deleted_at.is_(None), User.suspended_at.is_(None)]


def _exact_email_clause(email_term: str) -> ColumnElement[bool]:
    return User.email == email_term


def _text_match(term: str) -> ColumnElement[bool]:
    pattern = f"%{_escape_like(_email_term(term))}%"
    return or_(
        User.email.ilike(pattern, escape="\\"),
        _name_match(term),
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


def _admin_identity_clause() -> ColumnElement[bool]:
    return and_(
        User.system_role_id == SystemRole.ADMIN,
        User.deleted_at.is_(None),
    )


def _active_admin_clause() -> ColumnElement[bool]:
    return and_(_admin_identity_clause(), User.suspended_at.is_(None))


def _is_active_admin(user: User) -> bool:
    return user.is_site_admin and user.deleted_at is None and user.suspended_at is None


def _actor_is_site_admin(user: User) -> bool:
    return _is_active_admin(user) and has_site_permission(
        user.system_role, Permission.SITE_MANAGE_ADMINS
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


def _require_active_admin_actor(actor: User) -> None:
    if not _actor_is_site_admin(actor):
        raise AccessDenied()


async def _lock_actor_and_target(
    session: AsyncSession, *, actor_id: UUID, target_id: UUID
) -> tuple[User, User]:
    rows = list(
        await session.scalars(
            select(User)
            .where(or_(User.id == actor_id, User.id == target_id))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    by_id = {user.id: user for user in rows}
    actor = by_id.get(actor_id)
    target = by_id.get(target_id)
    if actor is None:
        raise AccessDenied()
    _require_active_admin_actor(actor)
    if target is None or target.deleted_at is not None:
        raise NotFound()
    return actor, target


async def _lock_actor_target_and_active_admins(
    session: AsyncSession, *, actor_id: UUID, target_id: UUID
) -> tuple[User, User, list[User]]:
    """SELECT … FOR UPDATE de actor, destino y admins vigentes, ordenado por id.

    No incluye admins suspendidos: no cuentan como vigentes y bloquearlos
    no cambia ``last_admin`` ni la revalidación del actor.
    """

    rows = list(
        await session.scalars(
            select(User)
            .where(
                or_(
                    User.id == actor_id,
                    User.id == target_id,
                    _active_admin_clause(),
                )
            )
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    by_id = {user.id: user for user in rows}
    actor = by_id.get(actor_id)
    target = by_id.get(target_id)
    if actor is None:
        raise AccessDenied()
    _require_active_admin_actor(actor)
    if target is None or target.deleted_at is not None:
        raise NotFound()
    return actor, target, rows


async def _revoke_user_sessions(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.deleted_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def list_admin_users(
    session: AsyncSession,
    *,
    query: str | None,
    status: AdminUserStatus | None,
    pagination: PaginationQuery,
) -> Paginated[AdminUserView]:
    filters = list(_status_filters(status))
    term = query.strip() if query else ""
    if len(term) > _SEARCH_Q_MAX:
        raise Validation(fields=["query"])
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
    actor_row, target = await _lock_actor_and_target(
        session, actor_id=actor.id, target_id=user_id
    )
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
    if is_admin:
        actor_row, target = await _lock_actor_and_target(
            session, actor_id=actor.id, target_id=user_id
        )
        if target.is_site_admin:
            return target
        return await assign_system_role(
            session, actor=actor_row, target=target, role_id=SystemRole.ADMIN
        )

    actor_row, target, locked = await _lock_actor_target_and_active_admins(
        session, actor_id=actor.id, target_id=user_id
    )
    if actor_row.id == target.id:
        raise CannotModifySelf()
    already_admin = target.is_site_admin
    if already_admin and _is_active_admin(target):
        _ensure_admin_remains(locked, actor=actor_row, target=target)
    if not already_admin:
        return target
    return await assign_system_role(
        session, actor=actor_row, target=target, role_id=SystemRole.ALUMNO
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
    if not suspended:
        actor_row, target = await _lock_actor_and_target(
            session, actor_id=actor.id, target_id=user_id
        )
        target.suspended_at = None
        await session.flush()
        return target

    actor_row, target, locked = await _lock_actor_target_and_active_admins(
        session, actor_id=actor.id, target_id=user_id
    )
    if actor_row.id == target.id:
        raise CannotModifySelf()
    if target.suspended_at is None and _is_active_admin(target):
        _ensure_admin_remains(locked, actor=actor_row, target=target)
    if target.suspended_at is None:
        target.suspended_at = datetime.now(UTC)
    await session.flush()
    return target


async def soft_delete_user(
    session: AsyncSession, *, actor: User, user_id: UUID
) -> None:
    if not has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS):
        raise AccessDenied()
    actor_row, target, locked = await _lock_actor_target_and_active_admins(
        session, actor_id=actor.id, target_id=user_id
    )
    if actor_row.id == target.id:
        raise CannotModifySelf()
    if _is_active_admin(target):
        _ensure_admin_remains(locked, actor=actor_row, target=target)
    target.deleted_at = datetime.now(UTC)
    await _revoke_user_sessions(session, target.id)
    await session.flush()


async def actor_can_search_users(session: AsyncSession, actor: User) -> bool:
    if has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS):
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


def _actor_has_full_search(actor: User) -> bool:
    return has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS)


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
    email_term = _email_term(term)
    full_search = _actor_has_full_search(actor)
    if not term:
        return Paginated(
            items=[],
            page=pagination.page,
            page_size=pagination.page_size,
            total=0,
        )
    if not full_search and _effective_search_len(term) < _TEACHER_Q_MIN_EFFECTIVE:
        raise Validation(fields=["q"])

    visible = _visible_user_filters()
    if full_search:
        filters = [*visible, _text_match(term)]
        total = int(
            await session.scalar(select(func.count()).select_from(User).where(*filters))
            or 0
        )
        offset = (pagination.page - 1) * pagination.page_size
        rows = list(
            await session.scalars(
                select(User)
                .where(*filters)
                .order_by(User.created_at.desc(), User.id.desc())
                .offset(offset)
                .limit(pagination.page_size)
            )
        )
    else:
        exact = await session.scalar(
            select(User).where(*visible, _exact_email_clause(email_term))
        )
        name_rows = list(
            await session.scalars(
                select(User)
                .where(*visible, _name_match(term))
                .order_by(User.created_at.desc(), User.id.desc())
                .limit(_TEACHER_SEARCH_CAP)
            )
        )
        fetched: list[User] = []
        if exact is not None:
            fetched.append(exact)
        for user in name_rows:
            if exact is not None and user.id == exact.id:
                continue
            fetched.append(user)
            if len(fetched) >= _TEACHER_SEARCH_CAP:
                break
        total = len(fetched)
        offset = (pagination.page - 1) * pagination.page_size
        rows = fetched[offset : offset + pagination.page_size]

    items = [
        to_search_hit(
            user,
            include_email=full_search or _email_term(user.email) == email_term,
        )
        for user in rows
    ]
    return Paginated(
        items=items,
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )
