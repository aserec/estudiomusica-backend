from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.permissions.deps import get_current_user
from app.schemas.errors import ErrorResponse
from app.schemas.pagination import Paginated, PaginationQuery, pagination_query
from app.schemas.users import UserSearchHit
from app.services.users import search_users

router = APIRouter(prefix="/users", tags=["users"])

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Petición inválida"},
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin permiso para buscar"},
}


@router.get(
    "/search",
    response_model=Paginated[UserSearchHit],
    summary="Buscar usuarios para matricular",
    description=(
        "Admin (``site:manage-admins`` o ``site:manage-courses``) o "
        "profesor de un curso (membresía ``teacher``). No devuelve "
        "suspendidos, borrados ni el hash de contraseña. ``q`` vacío "
        "responde lista vacía (no vuelca el directorio). Paginación "
        "``page``/``pageSize``."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
    },
)
async def get_users_search(
    actor: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    pagination: Annotated[PaginationQuery, Depends(pagination_query)],
    q: Annotated[
        str,
        Query(description="Nombre o email. Vacío no lista a nadie.", examples=["ada"]),
    ] = "",
) -> Paginated[UserSearchHit]:
    return await search_users(session, actor=actor, q=q, pagination=pagination)
