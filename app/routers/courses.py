from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.exceptions import AccessDenied
from app.models import User
from app.models.user import SystemRole
from app.permissions.deps import get_current_user, require_site_permission
from app.permissions.matrix import effective_permissions, ordered_permissions
from app.permissions.types import Permission
from app.schemas.courses import (
    CourseArchive,
    CourseCreate,
    CourseDuplicate,
    CourseResponse,
    CourseUpdate,
)
from app.schemas.errors import ErrorResponse
from app.schemas.pagination import Paginated, PaginationQuery, pagination_query
from app.schemas.roles import CoursePermissionsResponse
from app.services.courses import (
    create_course,
    delete_course,
    duplicate_course,
    get_course_for_view,
    list_admin_courses,
    set_course_archived,
    to_course_response,
    update_course,
)
from app.services.permissions import load_course_role

router = APIRouter(prefix="/courses", tags=["courses"])
admin_router = APIRouter(prefix="/admin/courses", tags=["courses"])

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Petición inválida"},
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin permiso"},
    404: {"model": ErrorResponse, "description": "Curso no encontrado"},
    409: {"model": ErrorResponse, "description": "Conflicto de curso"},
}

_MANAGE = require_site_permission(Permission.SITE_MANAGE_COURSES)


@router.get(
    "/{course_id}/me/permissions",
    response_model=CoursePermissionsResponse,
    summary="Permisos del usuario actual en un curso",
    description=(
        "Reevalúa la membresía y el rol de sistema en cada petición. "
        "Un alumno no matriculado recibe 403. Un admin recibe todos los "
        "permisos de curso y de sitio aunque no esté matriculado."
    ),
    responses={401: ERROR_RESPONSES[401], 403: ERROR_RESPONSES[403]},
)
async def get_my_course_permissions(
    course_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CoursePermissionsResponse:
    course_role = await load_course_role(session, user.id, course_id)
    if course_role is None and user.system_role != SystemRole.ADMIN:
        raise AccessDenied()
    granted = effective_permissions(
        user.system_role, course_role, settings=get_settings()
    )
    return CoursePermissionsResponse(
        course_role=course_role,
        permissions=ordered_permissions(set(granted)),
    )


@router.get(
    "/{course_id}",
    response_model=CourseResponse,
    summary="Obtener un curso",
    description=(
        "Requiere ``course:view`` (matrícula o admin). Un curso oculto "
        "sin ese permiso responde 404 para no filtrar su existencia. "
        "Un curso visible sin permiso responde 403. Borrado lógico: 404."
    ),
    responses={
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
    },
)
async def get_course(
    course_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CourseResponse:
    row = await get_course_for_view(session, user, course_id)
    return to_course_response(row)


@router.put(
    "/{course_id}",
    response_model=CourseResponse,
    summary="Actualizar un curso",
    description=(
        "Requiere ``course:teach`` (profesor del curso o admin). "
        "PUT (convención #91; el ticket pedía PATCH). Un profesor "
        "adjunto o alumno recibe 403. Curso archivado: 409 "
        "``course_archived``. Curso oculto sin ``course:view``: 404."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def put_course(
    course_id: UUID,
    body: CourseUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CourseResponse:
    row = await update_course(session, user, course_id, body)
    return to_course_response(row)


@router.put(
    "/{course_id}/archived",
    response_model=CourseResponse,
    summary="Archivar o desarchivar un curso",
    description=(
        "Requiere ``course:teach``. No es una modificación de contenido: "
        "un curso archivado se puede desarchivar. Oculto sin "
        "``course:view``: 404."
    ),
    responses={
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
    },
)
async def put_course_archived(
    course_id: UUID,
    body: CourseArchive,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CourseResponse:
    row = await set_course_archived(session, user, course_id, body)
    return to_course_response(row)


@admin_router.get(
    "",
    response_model=Paginated[CourseResponse],
    summary="Listar cursos (admin)",
    description=(
        "Requiere ``site:manage-courses``. Paginación ``page``/``pageSize`` "
        "(helper de #91; no se usa limit/cursor). Filtros opcionales "
        "``query`` (nombre o shortName) y ``archived``."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
    },
)
async def get_admin_courses(
    _admin: Annotated[User, Depends(_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_session)],
    pagination: Annotated[PaginationQuery, Depends(pagination_query)],
    query: Annotated[str | None, Query(description="Nombre o shortName")] = None,
    archived: Annotated[bool | None, Query()] = None,
) -> Paginated[CourseResponse]:
    return await list_admin_courses(session, pagination, query=query, archived=archived)


@admin_router.post(
    "",
    response_model=CourseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un curso",
    description=(
        "Requiere ``site:manage-courses``. ``shortName`` único entre "
        "cursos vigentes (citext). 409 ``duplicate_short_name`` si ya "
        "existe. ``endDate`` anterior a ``startDate``: 400."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        409: ERROR_RESPONSES[409],
    },
)
async def post_course(
    body: CourseCreate,
    _admin: Annotated[User, Depends(_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CourseResponse:
    row = await create_course(session, body)
    return to_course_response(row)


@admin_router.post(
    "/{course_id}/duplicate",
    response_model=CourseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicar un curso",
    description=(
        "Requiere ``site:manage-courses``. Copia la ficha (sin "
        "matrículas ni notas). La copia de secciones y actividades "
        "queda en ``copy_course_contents`` (#402 y siguientes). "
        "El curso nuevo no hereda ``archivedAt``."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
        409: ERROR_RESPONSES[409],
    },
)
async def post_duplicate_course(
    course_id: UUID,
    body: CourseDuplicate,
    _admin: Annotated[User, Depends(_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CourseResponse:
    row = await duplicate_course(session, course_id, body)
    return to_course_response(row)


@admin_router.delete(
    "/{course_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar un curso",
    description=(
        "Requiere ``site:manage-courses``. Borrado lógico. Libera "
        "``shortName`` para reutilizarlo."
    ),
    responses={
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
        404: ERROR_RESPONSES[404],
    },
)
async def remove_course(
    course_id: UUID,
    _admin: Annotated[User, Depends(_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await delete_course(session, course_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
