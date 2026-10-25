import pytest

from app.config import Settings
from app.models.user import SystemRole
from app.permissions.deps import require_course_permission, require_site_permission
from app.permissions.matrix import (
    COURSE_ROLE_PERMISSIONS,
    PERMISSION_MATRIX_VERSION,
    effective_permissions,
    extra_assistant_permissions,
    has_course_permission,
    has_site_permission,
    ordered_permissions,
)
from app.permissions.types import CourseRole, Permission


def test_permission_matrix_version_is_pinned() -> None:
    assert PERMISSION_MATRIX_VERSION == 1


def test_student_matrix_is_view_only() -> None:
    granted = effective_permissions(SystemRole.ALUMNO, CourseRole.STUDENT)
    assert set(granted) == {Permission.COURSE_VIEW}
    assert not has_course_permission(
        SystemRole.ALUMNO, CourseRole.STUDENT, Permission.COURSE_TEACH
    )


def test_assistant_matrix_excludes_teach() -> None:
    granted = effective_permissions(SystemRole.PROFESOR_ADJUNTO, CourseRole.ASSISTANT)
    assert granted == COURSE_ROLE_PERMISSIONS[CourseRole.ASSISTANT]
    assert Permission.COURSE_TEACH not in granted


def test_teacher_matrix_includes_teach() -> None:
    granted = effective_permissions(SystemRole.PROFESOR, CourseRole.TEACHER)
    assert Permission.COURSE_TEACH in granted
    assert not has_site_permission(SystemRole.PROFESOR, Permission.SITE_MANAGE_ADMINS)


def test_admin_has_site_and_course_permissions_without_enrollment() -> None:
    granted = effective_permissions(SystemRole.ADMIN, None)
    assert set(granted) == set(Permission)
    assert has_site_permission(SystemRole.ADMIN, Permission.SITE_MANAGE_COURSES)
    assert has_course_permission(SystemRole.ADMIN, None, Permission.COURSE_TEACH)


def test_non_admin_without_enrollment_has_no_course_permissions() -> None:
    for role in (
        SystemRole.ALUMNO,
        SystemRole.PROFESOR,
        SystemRole.PROFESOR_ADJUNTO,
    ):
        assert effective_permissions(role, None) == frozenset()


def test_ordered_permissions_follow_matrix_order() -> None:
    assert ordered_permissions(set(Permission)) == list(Permission)


def test_require_permission_factories_need_at_least_one() -> None:
    with pytest.raises(ValueError, match="al menos un permiso"):
        require_site_permission()
    with pytest.raises(ValueError, match="al menos un permiso"):
        require_course_permission()


def test_todo_306_flag_does_not_invent_a_permission() -> None:
    settings = Settings(assistant_media_and_sm3_visible=True, _env_file=None)
    assert extra_assistant_permissions(settings) == frozenset()
    assert extra_assistant_permissions(None) == frozenset()
    granted = effective_permissions(
        SystemRole.PROFESOR_ADJUNTO,
        CourseRole.ASSISTANT,
        settings=settings,
    )
    assert granted == {Permission.COURSE_VIEW, Permission.COURSE_GRADE}
