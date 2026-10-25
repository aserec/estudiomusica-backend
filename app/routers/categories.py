from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.permissions.deps import get_current_user, require_site_permission
from app.permissions.types import Permission
from app.schemas.categories import CategoryCreate, CategoryResponse, CategoryUpdate
from app.schemas.errors import ErrorResponse
from app.services.categories import (
    create_category,
    delete_category,
    list_categories,
    to_category_response,
    update_category,
)

router = APIRouter(prefix="/categories", tags=["categories"])
admin_router = APIRouter(prefix="/admin/categories", tags=["categories"])

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Petición inválida"},
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin permiso de sitio"},
    404: {"model": ErrorResponse, "description": "Categoría no encontrada"},
    409: {"model": ErrorResponse, "description": "Conflicto de categoría"},
}

_MANAGE = require_site_permission(Permission.SITE_MANAGE_COURSES)


@router.get(
    "",
    response_model=list[CategoryResponse],
    summary="Listar categorías de curso",
    description=(
        "Requiere sesión. Cualquier rol autenticado puede leer el catálogo. "
        "Devuelve las categorías vigentes (sin paginar: el árbol es el "
        "catálogo completo) ordenadas por sortOrder y name."
    ),
    responses={401: ERROR_RESPONSES[401]},
)
async def get_categories(
    _user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CategoryResponse]:
    rows = await list_categories(session)
    return [to_category_response(row) for row in rows]


@admin_router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una categoría",
    description=(
        "Requiere ``site:manage-courses``. Acepta name, parentId, "
        "description y sortOrder. Sin sortOrder se asigna el siguiente "
        "entre hermanos. Nombre único (sin distinguir mayúsculas) por padre."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        409: ERROR_RESPONSES[409],
    },
)
async def post_category(
    body: CategoryCreate,
    _admin: Annotated[User, Depends(_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryResponse:
    row = await create_category(session, body)
    return to_category_response(row)


@admin_router.put(
    "/{category_id}",
    response_model=CategoryResponse,
    summary="Actualizar una categoría",
    description=(
        "Requiere ``site:manage-courses``. PUT (convención #91; el ticket "
        "pedía PATCH). Sustituye name, parentId y description. sortOrder "
        "opcional: si no viene, se conserva. 409 si el nuevo padre formaría "
        "un ciclo o el nombre ya existe entre hermanos."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def put_category(
    category_id: UUID,
    body: CategoryUpdate,
    _admin: Annotated[User, Depends(_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryResponse:
    row = await update_category(session, category_id, body)
    return to_category_response(row)


@admin_router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar una categoría",
    description=(
        "Requiere ``site:manage-courses``. Borrado lógico. 409 "
        "``category_not_empty`` si tiene hijas vigentes o cursos vigentes "
        "(cuando exista la tabla ``courses`` de #401)."
    ),
    responses={
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def remove_category(
    category_id: UUID,
    _admin: Annotated[User, Depends(_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await delete_category(session, category_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
