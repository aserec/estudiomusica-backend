"""Matriz de permisos y tipos. Las dependencias FastAPI viven en ``deps``."""

from app.permissions.matrix import (
    COURSE_ROLE_PERMISSIONS,
    PERMISSION_MATRIX_VERSION,
    SITE_ROLE_PERMISSIONS,
    effective_permissions,
    has_course_permission,
    has_site_permission,
)
from app.permissions.types import CourseRole, Permission

__all__ = [
    "COURSE_ROLE_PERMISSIONS",
    "PERMISSION_MATRIX_VERSION",
    "SITE_ROLE_PERMISSIONS",
    "CourseRole",
    "Permission",
    "effective_permissions",
    "has_course_permission",
    "has_site_permission",
]
