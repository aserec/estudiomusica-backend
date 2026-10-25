"""Matriz rol → permiso (constante versionada; PR #306 del frontend)."""

from app.config import Settings
from app.models.user import SystemRole
from app.permissions.types import CourseRole, Permission

PERMISSION_MATRIX_VERSION = 1

PERMISSION_ORDER: tuple[Permission, ...] = (
    Permission.COURSE_VIEW,
    Permission.COURSE_GRADE,
    Permission.COURSE_TEACH,
    Permission.SITE_MANAGE_ADMINS,
    Permission.SITE_MANAGE_COURSES,
)

COURSE_ROLE_PERMISSIONS: dict[CourseRole, frozenset[Permission]] = {
    CourseRole.STUDENT: frozenset({Permission.COURSE_VIEW}),
    CourseRole.ASSISTANT: frozenset(
        {Permission.COURSE_VIEW, Permission.COURSE_GRADE}
    ),
    CourseRole.TEACHER: frozenset(
        {
            Permission.COURSE_VIEW,
            Permission.COURSE_GRADE,
            Permission.COURSE_TEACH,
        }
    ),
}

# El admin tiene todos los permisos de curso más los de sitio.
SITE_ROLE_PERMISSIONS: dict[SystemRole, frozenset[Permission]] = {
    SystemRole.ALUMNO: frozenset(),
    SystemRole.PROFESOR: frozenset(),
    SystemRole.PROFESOR_ADJUNTO: frozenset(),
    SystemRole.ADMIN: frozenset(PERMISSION_ORDER),
}


def ordered_permissions(permissions: set[Permission]) -> list[Permission]:
    return [item for item in PERMISSION_ORDER if item in permissions]


def extra_assistant_permissions(settings: Settings | None) -> frozenset[Permission]:
    """Hueco para el TODO(#306) de visibilidad multimedia/SM3.

    El flag ``assistant_media_and_sm3_visible`` queda configurable. Hasta
    que Danel decida el permiso concreto, no se añade ninguno a la matriz
    publicada para no inventar un slug que el frontend aún no tiene.
    """

    if settings is None or not settings.assistant_media_and_sm3_visible:
        return frozenset()
    return frozenset()


def effective_permissions(
    system_role: SystemRole,
    course_role: CourseRole | None,
    *,
    settings: Settings | None = None,
) -> frozenset[Permission]:
    granted = set(SITE_ROLE_PERMISSIONS.get(system_role, frozenset()))
    if course_role is not None:
        granted.update(COURSE_ROLE_PERMISSIONS[course_role])
        if course_role == CourseRole.ASSISTANT:
            granted.update(extra_assistant_permissions(settings))
    return frozenset(granted)


def has_site_permission(system_role: SystemRole, permission: Permission) -> bool:
    return permission in SITE_ROLE_PERMISSIONS.get(system_role, frozenset())


def has_course_permission(
    system_role: SystemRole,
    course_role: CourseRole | None,
    permission: Permission,
    *,
    settings: Settings | None = None,
) -> bool:
    return permission in effective_permissions(
        system_role, course_role, settings=settings
    )
