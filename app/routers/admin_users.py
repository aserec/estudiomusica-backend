from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AfterValidator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.permissions.deps import require_site_permission
from app.permissions.types import Permission
from app.schemas.errors import ErrorResponse
from app.schemas.pagination import Paginated, PaginationQuery, pagination_query
from app.schemas.users import (
    AdminFlagUpdate,
    AdminUserIdentityUpdate,
    AdminUserStatus,
    AdminUserView,
    SuspensionUpdate,
    reject_nul,
)
from app.services.users import (
    SEARCH_Q_RAW_MAX,
    list_admin_users,
    set_user_admin,
    set_user_suspended,
    soft_delete_user,
    to_admin_user_view,
    update_user_identity,
)

router = APIRouter(prefix="/admin/users", tags=["admin"])

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Petición inválida"},
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin permiso de sitio"},
    404: {"model": ErrorResponse, "description": "Usuario no encontrado"},
    409: {"model": ErrorResponse, "description": "Conflicto de administración"},
}

_ADMIN = require_site_permission(Permission.SITE_MANAGE_ADMINS)


@router.get(
    "",
    response_model=Paginated[AdminUserView],
    summary="Listar usuarios (admin)",
    description=(
        "Requiere ``site:manage-admins``. Devuelve todos los vigentes "
        "(incluidos suspendidos) con ``page``/``pageSize``. "
        "``query`` filtra por nombre o email. ``status``: all, active, "
        "suspended, deleted. Sin status se omiten los borrados. "
        "``query`` admite como máximo 200 caracteres tras NFC "
        "(tope crudo 2000). "
        "No se usa limit/cursor (pendiente de decisión)."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
    },
)
async def get_admin_users(
    _admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
    pagination: Annotated[PaginationQuery, Depends(pagination_query)],
    query: Annotated[
        str | None,
        Query(
            max_length=SEARCH_Q_RAW_MAX,
            description=(
                "Texto libre sobre nombre o email. Máximo 200 caracteres "
                "tras NFC (tope crudo 2000 para no rechazar NFD que cabe)."
            ),
            examples=["ada"],
        ),
        AfterValidator(reject_nul),
    ] = None,
    status_filter: Annotated[
        AdminUserStatus | None,
        Query(
            alias="status",
            description=(
                "all incluye borrados. Sin valor: vigentes, incluidos suspendidos."
            ),
        ),
    ] = None,
) -> Paginated[AdminUserView]:
    return await list_admin_users(
        session, query=query, status=status_filter, pagination=pagination
    )


@router.put(
    "/{user_id}",
    response_model=AdminUserView,
    summary="Editar identidad de un usuario",
    description=(
        "Requiere ``site:manage-admins``. PUT (convención #91; el ticket "
        "pedía PATCH) de firstName, lastName y/o email. Email repetido → "
        "``email_taken``. Ignora isAdmin, password y systemRoleId."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def put_admin_user_identity(
    user_id: UUID,
    body: AdminUserIdentityUpdate,
    admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUserView:
    user = await update_user_identity(session, actor=admin, user_id=user_id, body=body)
    return to_admin_user_view(user)


@router.put(
    "/{user_id}/admin",
    response_model=AdminUserView,
    summary="Dar o quitar el rol admin",
    description=(
        "Requiere ``site:manage-admins``. ``isAdmin: true`` asigna el rol "
        "``admin``; ``false`` vuelve a ``alumno`` (vía ``assign_system_role``). "
        "Quitarse el admin a uno mismo → ``cannot_modify_self`` "
        "(aunque haya otros admins). Un admin suspendido no cuenta como "
        "vigente. No puede quedar el sitio sin un admin vigente "
        "(``last_admin``). Si el rol ``alumno`` está desactivado, quitar "
        "el admin responde ``role_not_assignable``."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def put_admin_user_flag(
    user_id: UUID,
    body: AdminFlagUpdate,
    admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUserView:
    user = await set_user_admin(
        session, actor=admin, user_id=user_id, is_admin=body.is_admin
    )
    return to_admin_user_view(user)


@router.put(
    "/{user_id}/suspension",
    response_model=AdminUserView,
    summary="Suspender o rehabilitar un usuario",
    description=(
        "Requiere ``site:manage-admins``. Suspenderse a uno mismo → "
        "``cannot_modify_self``. Un usuario suspendido con sesión recibe "
        "``account_suspended``. No se revocan ``auth_sessions`` (al "
        "reactivar, el token viejo vuelve a valer; pendiente de Danel). "
        "Suspender al último admin vigente → ``last_admin``."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def put_admin_user_suspension(
    user_id: UUID,
    body: SuspensionUpdate,
    admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUserView:
    user = await set_user_suspended(
        session, actor=admin, user_id=user_id, suspended=body.suspended
    )
    return to_admin_user_view(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar lógicamente un usuario",
    description=(
        "Requiere ``site:manage-admins``. Escribe ``deleted_at`` y revoca "
        "todas las ``auth_sessions`` vigentes. Borrarse a uno mismo → "
        "``cannot_modify_self``. Borrar al último admin vigente → "
        "``last_admin``. El email del borrado queda bloqueado."
    ),
    responses={
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def delete_admin_user(
    user_id: UUID,
    admin: Annotated[User, Depends(_ADMIN)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await soft_delete_user(session, actor=admin, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
