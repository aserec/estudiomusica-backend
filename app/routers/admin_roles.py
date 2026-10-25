from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.permissions.deps import require_site_permission
from app.permissions.types import Permission
from app.schemas.errors import ErrorResponse
from app.schemas.roles import SystemRoleResponse, SystemRoleUpdate
from app.services.roles import (
    delete_system_role,
    list_system_roles,
    to_system_role_response,
    update_system_role,
)

router = APIRouter(prefix="/admin/system-roles", tags=["admin"])

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Petición inválida"},
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin permiso de sitio"},
    404: {"model": ErrorResponse, "description": "Rol no encontrado"},
    409: {"model": ErrorResponse, "description": "Rol de sistema o en uso"},
}

_ADMIN = require_site_permission(Permission.SITE_MANAGE_ADMINS)


@router.get(
    "",
    response_model=list[SystemRoleResponse],
    summary="Listar roles de sistema",
    description=(
        "Requiere ``site:manage-admins``. Devuelve todos los roles vigentes "
        "(incluidos los inactivos) ordenados por sortOrder."
    ),
    responses={401: ERROR_RESPONSES[401], 403: ERROR_RESPONSES[403]},
)
async def get_system_roles(
    _admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SystemRoleResponse]:
    rows = await list_system_roles(session)
    return [to_system_role_response(row) for row in rows]


@router.patch(
    "/{role_id}",
    response_model=SystemRoleResponse,
    summary="Actualizar un rol de sistema",
    description=(
        "Requiere ``site:manage-admins``. Acepta name, active y sortOrder. "
        "Desactivar un rol impide asignarlo a usuarios nuevos; los que ya "
        "lo tienen lo conservan. No cambia isSystem ni el id."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
    },
)
async def patch_system_role(
    role_id: str,
    body: SystemRoleUpdate,
    _admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SystemRoleResponse:
    row = await update_system_role(session, role_id, body)
    return to_system_role_response(row)


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar un rol de sistema",
    description=(
        "Requiere ``site:manage-admins``. 409 si el rol es de sistema "
        "(isSystem) o está asignado a algún usuario. No expone quién lo usa."
    ),
    responses={
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def remove_system_role(
    role_id: str,
    _admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await delete_system_role(session, role_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
