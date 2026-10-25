"""Matriz de permisos y dependencias de autorización."""

from app.permissions.deps import (
    get_current_user,
    require_course_permission,
    require_site_permission,
)
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
    "get_current_user",
    "has_course_permission",
    "has_site_permission",
    "require_course_permission",
    "require_site_permission",
]
