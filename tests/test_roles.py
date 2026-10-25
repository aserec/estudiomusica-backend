import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import AccessDenied, RoleNotAssignable
from app.models import Course, CourseMembership, SystemRoleRow, User
from app.models.course import CourseLevel, DefaultGroupMode
from app.models.user import SystemRole
from app.permissions.types import CourseRole
from app.services.roles import assert_role_assignable, assign_system_role
from tests.test_auth import PASSWORD, signup_body, unique_email

pytestmark = pytest.mark.db

_COURSE_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_COURSE_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_LEAK_MARKERS = ("password12", "argon2", "test-auth-secret", "Bearer ")

SYSTEM_ROLES = (
    SystemRole.ALUMNO,
    SystemRole.PROFESOR,
    SystemRole.PROFESOR_ADJUNTO,
    SystemRole.ADMIN,
)

ADMIN_ENDPOINTS: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", "/api/v1/admin/system-roles", None),
    ("PATCH", "/api/v1/admin/system-roles/alumno", {"name": "Alumno"}),
    ("DELETE", "/api/v1/admin/system-roles/alumno", None),
)


def _assert_no_secrets(response: Any) -> None:
    for marker in _LEAK_MARKERS:
        assert marker not in response.text


async def _signup(
    session_client: Any, *, email: str | None = None, name: str = "Ada Lovelace"
) -> dict[str, Any]:
    chosen = email or unique_email("role")
    response = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name=name, email=chosen),
    )
    assert response.status_code == 201
    user_id = uuid.UUID(response.json()["user"]["id"])
    return {
        "email": chosen,
        "id": user_id,
        "token": response.cookies["session"],
        "response": response,
    }


async def _set_role(
    db_session: AsyncSession, user_id: uuid.UUID, role: SystemRole
) -> User:
    user = await db_session.get(User, user_id)
    assert user is not None
    user.system_role_id = role
    user.is_admin = role == SystemRole.ADMIN
    await db_session.flush()
    return user


async def _ensure_course(db_session: AsyncSession, course_id: uuid.UUID) -> Course:
    """Crea la ficha mínima que exige la FK de ``course_memberships``."""

    from datetime import date

    existing = await db_session.get(Course, course_id)
    if existing is not None:
        if existing.deleted_at is not None:
            existing.deleted_at = None
            await db_session.flush()
        return existing
    row = Course(
        id=course_id,
        short_name=f"c-{course_id.hex[:12]}",
        name=f"Course {course_id.hex[:8]}",
        visible=True,
        level=CourseLevel.BEGINNER,
        default_group_mode=DefaultGroupMode.NONE,
        start_date=date(2026, 1, 1),
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _enroll(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    course_id: uuid.UUID,
    course_role: CourseRole,
) -> None:
    await _ensure_course(db_session, course_id)
    db_session.add(
        CourseMembership(
            user_id=user_id,
            course_id=course_id,
            course_role=course_role,
        )
    )
    await db_session.flush()


async def _request(
    session_client: Any,
    method: str,
    path: str,
    json_body: dict[str, object] | None = None,
) -> Any:
    return await session_client.request(method, path, json=json_body)


@pytest.mark.parametrize("role", SYSTEM_ROLES)
@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS)
async def test_admin_endpoints_role_matrix(
    session_client: Any,
    db_session: AsyncSession,
    role: SystemRole,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    created = await _signup(session_client, email=unique_email(role.value))
    await _set_role(db_session, created["id"], role)
    response = await _request(session_client, method, path, body)
    _assert_no_secrets(response)
    if role == SystemRole.ADMIN:
        if method == "DELETE":
            assert response.status_code == 409
            assert response.json() == {"error": "system_role_protected"}
        else:
            assert response.status_code == 200
    else:
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}
        assert created["email"] not in response.text


@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS)
async def test_admin_endpoints_without_session_are_unauthorized(
    session_client: Any,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    response = await _request(session_client, method, path, body)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    _assert_no_secrets(response)


@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS)
async def test_admin_endpoints_reject_invalid_token(
    session_client: Any,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    created = await _signup(session_client)
    token = created["token"]
    tampered = token[:-2] + ("A" if token[-1] != "A" else "B")
    session_client.cookies.clear()
    response = await session_client.request(
        method,
        path,
        json=body,
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert tampered not in response.text
    _assert_no_secrets(response)


async def test_enrolled_student_receives_only_course_view(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _enroll(db_session, created["id"], _COURSE_A, CourseRole.STUDENT)
    response = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert response.status_code == 200
    body = response.json()
    assert body == {"courseRole": "student", "permissions": ["course:view"]}
    _assert_no_secrets(response)


async def test_assistant_receives_view_and_grade_not_teach(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.PROFESOR_ADJUNTO)
    await _enroll(db_session, created["id"], _COURSE_A, CourseRole.ASSISTANT)
    response = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert response.status_code == 200
    body = response.json()
    assert body["courseRole"] == "assistant"
    assert body["permissions"] == ["course:view", "course:grade"]
    assert "course:teach" not in body["permissions"]


async def test_teacher_receives_view_grade_teach(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.PROFESOR)
    await _enroll(db_session, created["id"], _COURSE_A, CourseRole.TEACHER)
    response = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert response.status_code == 200
    assert response.json() == {
        "courseRole": "teacher",
        "permissions": ["course:view", "course:grade", "course:teach"],
    }


async def test_admin_gets_site_permissions_without_enrollment(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    response = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert response.status_code == 200
    body = response.json()
    assert body["courseRole"] is None
    assert body["permissions"] == [
        "course:view",
        "course:grade",
        "course:teach",
        "site:manage-admins",
        "site:manage-courses",
    ]


async def test_unenrolled_non_admin_is_forbidden(
    session_client: Any,
) -> None:
    await _signup(session_client)
    response = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    _assert_no_secrets(response)


async def test_course_permissions_without_session_are_unauthorized(
    session_client: Any,
) -> None:
    response = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_idor_does_not_reveal_other_course_membership(
    session_client: Any, db_session: AsyncSession
) -> None:
    owner = await _signup(session_client, email=unique_email("owner"))
    await _enroll(db_session, owner["id"], _COURSE_B, CourseRole.TEACHER)
    session_client.cookies.clear()
    other = await _signup(session_client, email=unique_email("other"))
    await _enroll(db_session, other["id"], _COURSE_A, CourseRole.STUDENT)
    response = await session_client.get(f"/api/v1/courses/{_COURSE_B}/me/permissions")
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert owner["email"] not in response.text
    assert str(owner["id"]) not in response.text
    _assert_no_secrets(response)


async def test_permissions_are_reevaluated_after_role_change(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _enroll(db_session, created["id"], _COURSE_A, CourseRole.STUDENT)
    first = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert first.json()["permissions"] == ["course:view"]
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    second = await session_client.get(f"/api/v1/courses/{_COURSE_A}/me/permissions")
    assert "site:manage-admins" in second.json()["permissions"]
    assert "course:teach" in second.json()["permissions"]


async def test_admin_can_list_and_deactivate_role(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    listed = await session_client.get("/api/v1/admin/system-roles")
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()]
    assert ids == ["alumno", "profesor_adjunto", "profesor", "admin"]
    for item in listed.json():
        assert set(item) == {
            "id",
            "name",
            "sortOrder",
            "isSystem",
            "active",
            "createdAt",
            "updatedAt",
        }
        assert item["isSystem"] is True
        assert item["active"] is True

    patched = await session_client.patch(
        "/api/v1/admin/system-roles/profesor",
        json={"active": False, "name": "Profesor", "sortOrder": 30},
    )
    assert patched.status_code == 200
    assert patched.json()["active"] is False
    assert patched.json()["name"] == "Profesor"

    existing = await _signup(session_client, email=unique_email("prof"))
    session_client.cookies.clear()
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": created["email"], "password": PASSWORD},
    )
    await _set_role(db_session, existing["id"], SystemRole.PROFESOR)
    existing_user = await db_session.get(User, existing["id"])
    assert existing_user is not None
    assert existing_user.system_role_id == SystemRole.PROFESOR

    with pytest.raises(RoleNotAssignable):
        await assert_role_assignable(db_session, SystemRole.PROFESOR)

    admin_user = await db_session.get(User, created["id"])
    target = await db_session.get(User, existing["id"])
    assert admin_user is not None and target is not None
    with pytest.raises(RoleNotAssignable):
        await assign_system_role(
            db_session,
            actor=admin_user,
            target=target,
            role_id=SystemRole.PROFESOR,
        )


async def test_patch_ignores_privilege_escalation_fields(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    response = await session_client.patch(
        "/api/v1/admin/system-roles/alumno",
        json={
            "name": "Alumno",
            "isSystem": False,
            "id": "admin",
            "permissions": ["site:manage-admins"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "alumno"
    assert body["isSystem"] is True
    assert "permissions" not in body


async def test_non_admin_cannot_assign_admin_role(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    actor = await db_session.get(User, created["id"])
    assert actor is not None
    with pytest.raises(AccessDenied):
        await assign_system_role(
            db_session,
            actor=actor,
            target=actor,
            role_id=SystemRole.ADMIN,
        )
    await db_session.refresh(actor)
    assert actor.system_role_id == SystemRole.ALUMNO
    assert actor.is_admin is False


async def test_delete_system_role_conflict_and_custom_role(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    protected = await session_client.delete("/api/v1/admin/system-roles/admin")
    assert protected.status_code == 409
    assert protected.json() == {"error": "system_role_protected"}

    custom = SystemRoleRow(
        id="custom_coach",
        name="Coach",
        sort_order=90,
        is_system=False,
        active=True,
    )
    db_session.add(custom)
    await db_session.flush()
    holder = User(
        first_name="Cora",
        last_name="Coach",
        email=unique_email("coach"),
        password_hash="x",
        system_role_id="custom_coach",
    )
    db_session.add(holder)
    await db_session.flush()

    in_use = await session_client.delete("/api/v1/admin/system-roles/custom_coach")
    assert in_use.status_code == 409
    assert in_use.json() == {"error": "role_in_use"}
    assert holder.email not in in_use.text
    assert str(holder.id) not in in_use.text

    holder.system_role_id = SystemRole.ALUMNO
    await db_session.flush()
    deleted = await session_client.delete("/api/v1/admin/system-roles/custom_coach")
    assert deleted.status_code == 204
    assert deleted.content == b""
    missing = await session_client.delete("/api/v1/admin/system-roles/custom_coach")
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found"}


async def test_patch_unknown_role_is_not_found(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    response = await session_client.patch(
        "/api/v1/admin/system-roles/does-not-exist",
        json={"name": "Nadie"},
    )
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


async def test_patch_validation_rejects_blank_name(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    response = await session_client.patch(
        "/api/v1/admin/system-roles/alumno",
        json={"name": "   "},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "validation"
    assert "name" in response.json()["fields"]


async def test_require_site_and_course_permission_probes(
    session_client: Any, db_session: AsyncSession
) -> None:
    student = await _signup(session_client, email=unique_email("stu"))
    await _enroll(db_session, student["id"], _COURSE_A, CourseRole.STUDENT)
    denied_site = await session_client.get("/api/v1/_test/site-manage-admins")
    assert denied_site.status_code == 403
    denied_teach = await session_client.get(f"/api/v1/_test/courses/{_COURSE_A}/teach")
    assert denied_teach.status_code == 403

    session_client.cookies.clear()
    teacher = await _signup(session_client, email=unique_email("tea"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE_A, CourseRole.TEACHER)
    allowed_teach = await session_client.get(f"/api/v1/_test/courses/{_COURSE_A}/teach")
    assert allowed_teach.status_code == 200
    assert allowed_teach.json() == {"ok": True}
    denied_admin = await session_client.get("/api/v1/_test/site-manage-admins")
    assert denied_admin.status_code == 403

    session_client.cookies.clear()
    admin = await _signup(session_client, email=unique_email("adm"))
    await _set_role(db_session, admin["id"], SystemRole.ADMIN)
    allowed_site = await session_client.get("/api/v1/_test/site-manage-admins")
    assert allowed_site.status_code == 200
    allowed_admin_teach = await session_client.get(
        f"/api/v1/_test/courses/{_COURSE_A}/teach"
    )
    assert allowed_admin_teach.status_code == 200


async def test_deactivated_alumno_blocks_signup(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await _signup(session_client)
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    await session_client.patch(
        "/api/v1/admin/system-roles/alumno",
        json={"active": False},
    )
    blocked = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(email=unique_email("blocked")),
    )
    assert blocked.status_code == 409
    assert blocked.json() == {"error": "role_not_assignable"}
    _assert_no_secrets(blocked)


def test_assistant_media_setting_is_configurable() -> None:
    settings = get_settings()
    assert settings.assistant_media_and_sm3_visible is False
    enabled = settings.model_copy(update={"assistant_media_and_sm3_visible": True})
    assert enabled.assistant_media_and_sm3_visible is True
