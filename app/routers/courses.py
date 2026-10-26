from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.exceptions import AccessDenied
from app.models import User
from app.models.user import SystemRole
from app.permissions.deps import get_current_user
from app.permissions.matrix import effective_permissions, ordered_permissions
from app.schemas.errors import ErrorResponse
from app.schemas.roles import CoursePermissionsResponse
from app.services.permissions import load_course_role

router = APIRouter(prefix="/courses", tags=["courses"])

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin acceso al curso"},
}


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
