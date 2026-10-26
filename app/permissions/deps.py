"""Dependencias FastAPI: usuario actual y autorización por permiso."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.exceptions import AccessDenied
from app.models import User
from app.permissions.matrix import effective_permissions, has_site_permission
from app.permissions.types import Permission
from app.services.auth import resolve_current_user
from app.services.permissions import load_course_role


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Usuario de la cookie ``session`` o ``Authorization: Bearer``."""

    return await resolve_current_user(request, session, get_settings())


def require_site_permission(
    *permissions: Permission,
) -> Callable[..., Awaitable[User]]:
    """Deniega por defecto. Exige todos los permisos de sitio indicados."""

    if not permissions:
        raise ValueError("require_site_permission requiere al menos un permiso")

    async def dependency(
        user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if not all(has_site_permission(user.system_role, item) for item in permissions):
            raise AccessDenied()
        return user

    return dependency


def require_course_permission(
    *permissions: Permission,
) -> Callable[..., Awaitable[User]]:
    """Deniega por defecto. Lee ``course_id`` del path y reevalúa en cada petición."""

    if not permissions:
        raise ValueError("require_course_permission requiere al menos un permiso")

    async def dependency(
        course_id: UUID,
        user: Annotated[User, Depends(get_current_user)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> User:
        course_role = await load_course_role(session, user.id, course_id)
        granted = effective_permissions(
            user.system_role, course_role, settings=get_settings()
        )
        if not set(permissions) <= granted:
            raise AccessDenied()
        return user

    return dependency
