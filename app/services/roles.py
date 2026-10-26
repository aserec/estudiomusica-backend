"""Administración de roles de sistema y asignación controlada en servidor."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    AccessDenied,
    NotFound,
    RoleInUse,
    RoleNotAssignable,
    SystemRoleProtected,
)
from app.models import SystemRoleRow, User
from app.models.user import SystemRole
from app.permissions.matrix import has_site_permission
from app.permissions.types import Permission
from app.schemas.roles import SystemRoleResponse, SystemRoleUpdate


def to_system_role_response(row: SystemRoleRow) -> SystemRoleResponse:
    return SystemRoleResponse(
        id=row.id,
        name=row.name,
        sort_order=row.sort_order,
        is_system=row.is_system,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def get_system_role_row(
    session: AsyncSession, role_id: str
) -> SystemRoleRow | None:
    return await _load_system_role(session, role_id)


async def _load_system_role(
    session: AsyncSession,
    role_id: str,
    *,
    with_for_update: bool | dict[str, bool] | None = None,
) -> SystemRoleRow | None:
    """Carga el rol. ``with_for_update`` refresca y retiene el lock hasta el commit."""

    if with_for_update is None:
        row = await session.get(SystemRoleRow, role_id)
    else:
        row = await session.get(
            SystemRoleRow,
            role_id,
            with_for_update=with_for_update,
            populate_existing=True,
        )
    if row is None or row.deleted_at is not None:
        return None
    return row


async def list_system_roles(session: AsyncSession) -> list[SystemRoleRow]:
    result = await session.scalars(
        select(SystemRoleRow)
        .where(SystemRoleRow.deleted_at.is_(None))
        .order_by(SystemRoleRow.sort_order, SystemRoleRow.id)
    )
    return list(result)


async def assert_role_assignable(
    session: AsyncSession, role_id: str, *, for_share: bool = False
) -> SystemRoleRow:
    """Un rol inactivo no se puede asignar a usuarios nuevos.

    Con ``for_share`` bloquea ``SELECT … FOR SHARE`` y vuelve a leer
    ``active`` tras el lock (``populate_existing``) para no asignar un
    rol que otro PATCH está desactivando.
    """

    row = await _load_system_role(
        session,
        role_id,
        with_for_update={"read": True} if for_share else None,
    )
    if row is None:
        raise NotFound()
    if not row.active:
        raise RoleNotAssignable()
    return row


def _actor_manages_admins(actor: User) -> bool:
    return has_site_permission(actor.system_role, Permission.SITE_MANAGE_ADMINS)


async def assign_system_role(
    session: AsyncSession,
    *,
    actor: User,
    target: User,
    role_id: str,
) -> User:
    """Asigna un rol. Solo un admin. Deniega por defecto. No filtra datos ajenos.

    Lo usa la API de usuarios (webapp #395) al dar o quitar admin.
    """

    if not _actor_manages_admins(actor):
        raise AccessDenied()
    row = await assert_role_assignable(session, role_id, for_share=True)
    target.system_role_id = row.id
    target.is_admin = row.id == SystemRole.ADMIN
    await session.flush()
    return target


async def update_system_role(
    session: AsyncSession, role_id: str, body: SystemRoleUpdate
) -> SystemRoleRow:
    """Actualiza el rol. Desactivar con usuarios es válido: lo conservan.

    ``FOR UPDATE`` serializa con ``assign_system_role`` (``FOR SHARE``):
    no se puede asignar un rol a la vez que se desactiva.
    """

    row = await _load_system_role(session, role_id, with_for_update=True)
    if row is None:
        raise NotFound()
    if body.active is False and row.id == SystemRole.ADMIN:
        raise SystemRoleProtected()
    if body.name is not None:
        row.name = body.name
    if body.active is not None:
        row.active = body.active
    if body.sort_order is not None:
        row.sort_order = body.sort_order
    await session.flush()
    await session.refresh(row)
    return row


async def delete_system_role(session: AsyncSession, role_id: str) -> None:
    row = await get_system_role_row(session, role_id)
    if row is None:
        raise NotFound()
    if row.is_system:
        raise SystemRoleProtected()
    in_use = await session.scalar(
        select(User.id).where(User.system_role_id == row.id).limit(1)
    )
    if in_use is not None:
        raise RoleInUse()
    row.deleted_at = datetime.now(UTC)
    await session.flush()
