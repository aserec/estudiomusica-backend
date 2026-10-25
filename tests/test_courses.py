import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course, CourseMembership
from app.models.user import SystemRole
from app.permissions.types import CourseRole
from tests.test_auth import unique_email
from tests.test_roles import (
    SYSTEM_ROLES,
    _assert_no_secrets,
    _enroll,
    _request,
    _set_role,
    _signup,
)

pytestmark = pytest.mark.db

_ADMIN = "/api/v1/admin/courses"
_COURSES = "/api/v1/courses"

ADMIN_MUTATIONS: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", _ADMIN, None),
    ("POST", _ADMIN, {"shortName": "nuevo", "name": "Nuevo"}),
    (
        "POST",
        f"{_ADMIN}/{uuid.UUID(int=1)}/duplicate",
        {"shortName": "x", "name": "X", "startDate": "2026-09-01"},
    ),
    ("DELETE", f"{_ADMIN}/{uuid.UUID(int=1)}", None),
)

COURSE_MUTATIONS: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    (
        "PUT",
        f"{_COURSES}/{uuid.UUID(int=1)}",
        {
            "shortName": "x",
            "name": "X",
            "visible": True,
            "level": "beginner",
            "defaultGroupMode": "none",
        },
    ),
    ("PUT", f"{_COURSES}/{uuid.UUID(int=1)}/archived", {"archived": True}),
)

PROTECTED: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", f"{_COURSES}/{uuid.UUID(int=1)}", None),
    *ADMIN_MUTATIONS,
    *COURSE_MUTATIONS,
)


def _course_keys(body: dict[str, Any]) -> None:
    assert set(body) == {
        "id",
        "shortName",
        "name",
        "summary",
        "categoryId",
        "startDate",
        "endDate",
        "visible",
        "level",
        "defaultGroupMode",
        "deletedAt",
        "archivedAt",
        "createdAt",
        "updatedAt",
    }


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "shortName": f"sn-{uuid.uuid4().hex[:10]}",
        "name": "Armonía I",
        "summary": "tonal",
        "startDate": "2026-09-01",
        "endDate": "2027-06-30",
        "visible": True,
        "level": "beginner",
        "defaultGroupMode": "none",
    }
    payload.update(overrides)
    return payload


def _update_payload(course: dict[str, Any], **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "shortName": course["shortName"],
        "name": course["name"],
        "summary": course["summary"],
        "categoryId": course["categoryId"],
        "startDate": course["startDate"],
        "endDate": course["endDate"],
        "visible": course["visible"],
        "level": course["level"],
        "defaultGroupMode": course["defaultGroupMode"],
    }
    payload.update(overrides)
    return payload


async def _admin(session_client: Any, db_session: AsyncSession) -> dict[str, Any]:
    created = await _signup(session_client, email=unique_email("crs-admin"))
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    return created


async def _create_course(session_client: Any, **overrides: object) -> dict[str, Any]:
    response = await session_client.post(_ADMIN, json=_create_payload(**overrides))
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    _course_keys(body)
    return body


@pytest.mark.parametrize("method,path,body", PROTECTED)
async def test_course_endpoints_without_session_are_unauthorized(
    session_client: Any,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    response = await _request(session_client, method, path, body)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    _assert_no_secrets(response)


@pytest.mark.parametrize("method,path,body", PROTECTED)
async def test_course_endpoints_reject_invalid_token(
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


@pytest.mark.parametrize("role", SYSTEM_ROLES)
@pytest.mark.parametrize("method,path,body", ADMIN_MUTATIONS)
async def test_non_admin_cannot_use_admin_course_endpoints(
    session_client: Any,
    db_session: AsyncSession,
    role: SystemRole,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    created = await _signup(session_client, email=unique_email(f"adm-{role.value}"))
    await _set_role(db_session, created["id"], role)
    response = await _request(session_client, method, path, body)
    _assert_no_secrets(response)
    if role == SystemRole.ADMIN:
        assert response.status_code in {200, 201, 404, 204}
    else:
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}
        assert created["email"] not in response.text


async def test_admin_creates_unique_short_name_and_duplicate_conflicts(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create_course(
        session_client, shortName="  Harm1  ", name="  Armonía  "
    )
    assert created["shortName"] == "Harm1"
    assert created["name"] == "Armonía"
    assert created["deletedAt"] is None
    assert created["archivedAt"] is None

    listed = await session_client.get(_ADMIN)
    assert listed.status_code == 200
    body = listed.json()
    assert body["page"] == 1
    assert body["pageSize"] == 20
    assert created["id"] in [item["id"] for item in body["items"]]

    duplicate = await session_client.post(
        _ADMIN, json=_create_payload(shortName="harm1", name="Otro")
    )
    assert duplicate.status_code == 409
    assert duplicate.json() == {"error": "duplicate_short_name"}


async def test_teacher_can_edit_and_assistant_or_student_cannot(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    course = await _create_course(session_client, name="Edición")
    course_id = uuid.UUID(course["id"])

    session_client.cookies.clear()
    teacher = await _signup(session_client, email=unique_email("crs-tea"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], course_id, CourseRole.TEACHER)
    updated = await session_client.put(
        f"{_COURSES}/{course['id']}",
        json=_update_payload(
            course,
            name="Nombre nuevo",
            summary="resumen",
            startDate="2026-10-01",
            endDate="2027-01-15",
        ),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Nombre nuevo"
    assert updated.json()["summary"] == "resumen"
    assert updated.json()["startDate"] == "2026-10-01"

    session_client.cookies.clear()
    assistant = await _signup(session_client, email=unique_email("crs-ast"))
    await _set_role(db_session, assistant["id"], SystemRole.PROFESOR_ADJUNTO)
    await _enroll(db_session, assistant["id"], course_id, CourseRole.ASSISTANT)
    denied_assistant = await session_client.put(
        f"{_COURSES}/{course['id']}",
        json=_update_payload(course, name="No"),
    )
    assert denied_assistant.status_code == 403
    assert denied_assistant.json() == {"error": "forbidden"}

    session_client.cookies.clear()
    student = await _signup(session_client, email=unique_email("crs-stu"))
    await _set_role(db_session, student["id"], SystemRole.ALUMNO)
    await _enroll(db_session, student["id"], course_id, CourseRole.STUDENT)
    denied_student = await session_client.put(
        f"{_COURSES}/{course['id']}",
        json=_update_payload(course, name="Tampoco"),
    )
    assert denied_student.status_code == 403
    assert denied_student.json() == {"error": "forbidden"}
    _assert_no_secrets(denied_student)


async def test_archived_course_blocks_content_until_unarchived(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    course = await _create_course(session_client, name="Archivable")
    course_id = uuid.UUID(course["id"])

    session_client.cookies.clear()
    teacher = await _signup(session_client, email=unique_email("crs-arch"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], course_id, CourseRole.TEACHER)

    archived = await session_client.put(
        f"{_COURSES}/{course['id']}/archived", json={"archived": True}
    )
    assert archived.status_code == 200
    assert archived.json()["archivedAt"] is not None

    blocked = await session_client.put(
        f"{_COURSES}/{course['id']}",
        json=_update_payload(course, name="No se puede"),
    )
    assert blocked.status_code == 409
    assert blocked.json() == {"error": "course_archived"}

    opened = await session_client.put(
        f"{_COURSES}/{course['id']}/archived", json={"archived": False}
    )
    assert opened.status_code == 200
    assert opened.json()["archivedAt"] is None
    allowed = await session_client.put(
        f"{_COURSES}/{course['id']}",
        json=_update_payload(course, name="Ya se puede"),
    )
    assert allowed.status_code == 200
    assert allowed.json()["name"] == "Ya se puede"


async def test_duplicate_copies_ficha_without_memberships(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    source = await _create_course(
        session_client,
        shortName="orig-dup",
        name="Original",
        summary="copiar",
        visible=False,
        level="advanced",
        defaultGroupMode="separate",
        startDate="2025-01-01",
        endDate="2025-06-01",
    )
    source_id = uuid.UUID(source["id"])
    teacher = await _signup(session_client, email=unique_email("dup-tea"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], source_id, CourseRole.TEACHER)
    session_client.cookies.clear()
    await _admin(session_client, db_session)

    duplicated = await session_client.post(
        f"{_ADMIN}/{source['id']}/duplicate",
        json={
            "shortName": "copia-dup",
            "name": "Copia",
            "startDate": "2026-09-01",
        },
    )
    assert duplicated.status_code == 201
    body = duplicated.json()
    _course_keys(body)
    assert body["id"] != source["id"]
    assert body["shortName"] == "copia-dup"
    assert body["name"] == "Copia"
    assert body["summary"] == "copiar"
    assert body["visible"] is False
    assert body["level"] == "advanced"
    assert body["defaultGroupMode"] == "separate"
    assert body["startDate"] == "2026-09-01"
    assert body["endDate"] is None
    assert body["archivedAt"] is None
    assert body["deletedAt"] is None

    memberships = list(
        await db_session.scalars(
            select(CourseMembership).where(
                CourseMembership.course_id == uuid.UUID(body["id"]),
                CourseMembership.deleted_at.is_(None),
            )
        )
    )
    assert memberships == []

    collision = await session_client.post(
        f"{_ADMIN}/{source['id']}/duplicate",
        json={
            "shortName": "COPIA-DUP",
            "name": "Otra",
            "startDate": "2026-10-01",
        },
    )
    assert collision.status_code == 409
    assert collision.json() == {"error": "duplicate_short_name"}


async def test_unenrolled_student_gets_404_on_hidden_course(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    hidden = await _create_course(session_client, name="Oculto", visible=False)
    visible = await _create_course(session_client, name="Visible", visible=True)
    session_client.cookies.clear()
    student = await _signup(session_client, email=unique_email("hid-stu"))
    await _set_role(db_session, student["id"], SystemRole.ALUMNO)

    hidden_get = await session_client.get(f"{_COURSES}/{hidden['id']}")
    assert hidden_get.status_code == 404
    assert hidden_get.json() == {"error": "not_found"}
    assert hidden["id"] not in hidden_get.text

    visible_get = await session_client.get(f"{_COURSES}/{visible['id']}")
    assert visible_get.status_code == 403
    assert visible_get.json() == {"error": "forbidden"}

    hidden_edit = await session_client.put(
        f"{_COURSES}/{hidden['id']}",
        json=_update_payload(hidden, name="Hack"),
    )
    assert hidden_edit.status_code == 404
    assert hidden_edit.json() == {"error": "not_found"}


async def test_enrolled_student_can_view_hidden_but_not_edit(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    hidden = await _create_course(session_client, name="Oculto mat", visible=False)
    session_client.cookies.clear()
    student = await _signup(session_client, email=unique_email("hid-enr"))
    await _set_role(db_session, student["id"], SystemRole.ALUMNO)
    await _enroll(
        db_session, student["id"], uuid.UUID(hidden["id"]), CourseRole.STUDENT
    )
    viewed = await session_client.get(f"{_COURSES}/{hidden['id']}")
    assert viewed.status_code == 200
    assert viewed.json()["id"] == hidden["id"]
    denied = await session_client.put(
        f"{_COURSES}/{hidden['id']}",
        json=_update_payload(hidden, name="No"),
    )
    assert denied.status_code == 403


async def test_teacher_cannot_edit_another_course(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    own = await _create_course(session_client, name="Propio")
    other = await _create_course(session_client, name="Ajeno", visible=False)
    session_client.cookies.clear()
    teacher = await _signup(session_client, email=unique_email("idor-tea"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], uuid.UUID(own["id"]), CourseRole.TEACHER)
    leaked = await session_client.put(
        f"{_COURSES}/{other['id']}",
        json=_update_payload(other, name="Escalada"),
    )
    assert leaked.status_code == 404
    assert leaked.json() == {"error": "not_found"}
    assert other["shortName"] not in leaked.text


async def test_end_date_before_start_date_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await session_client.post(
        _ADMIN,
        json=_create_payload(startDate="2026-09-01", endDate="2026-08-01"),
    )
    assert created.status_code == 400
    assert created.json()["error"] == "validation"
    assert "endDate" in created.json()["fields"]

    course = await _create_course(session_client)
    updated = await session_client.put(
        f"{_COURSES}/{course['id']}",
        json=_update_payload(course, startDate="2026-09-01", endDate="2026-01-01"),
    )
    assert updated.status_code == 400
    assert updated.json()["error"] == "validation"
    assert "endDate" in updated.json()["fields"]


async def test_unknown_or_deleted_category_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    missing = await session_client.post(
        _ADMIN,
        json=_create_payload(categoryId=str(uuid.uuid4())),
    )
    assert missing.status_code == 400
    assert missing.json()["error"] == "validation"
    assert "categoryId" in missing.json()["fields"]

    category = await session_client.post(
        "/api/v1/admin/categories", json={"name": "Temporal-curso"}
    )
    assert category.status_code == 201
    deleted = await session_client.delete(
        f"/api/v1/admin/categories/{category.json()['id']}"
    )
    assert deleted.status_code == 204
    orphan = await session_client.post(
        _ADMIN,
        json=_create_payload(categoryId=category.json()["id"]),
    )
    assert orphan.status_code == 400
    assert "categoryId" in orphan.json()["fields"]


async def test_soft_deleted_short_name_can_be_reused(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    first = await _create_course(session_client, shortName="reutilizable")
    deleted = await session_client.delete(f"{_ADMIN}/{first['id']}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    missing = await session_client.get(f"{_COURSES}/{first['id']}")
    assert missing.status_code == 404
    listed = await session_client.get(_ADMIN)
    assert first["id"] not in [item["id"] for item in listed.json()["items"]]

    reused = await _create_course(session_client, shortName="REUTILIZABLE")
    assert reused["id"] != first["id"]
    assert reused["shortName"] == "REUTILIZABLE"

    stored = await db_session.get(Course, uuid.UUID(first["id"]))
    assert stored is not None
    assert stored.deleted_at is not None


async def test_put_ignores_unknown_and_cannot_set_deleted_at(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    course = await _create_course(session_client, name="Inmutable")
    response = await session_client.put(
        f"{_COURSES}/{course['id']}",
        json=_update_payload(
            course,
            name="Sigue vivo",
            id=str(uuid.uuid4()),
            deletedAt="2020-01-01T00:00:00Z",
            archivedAt="2020-01-01T00:00:00Z",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == course["id"]
    assert body["name"] == "Sigue vivo"
    assert body["deletedAt"] is None
    assert body["archivedAt"] is None


async def test_admin_list_paginates_and_filters(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    alpha = await _create_course(
        session_client, shortName="pg-alpha", name="Alpha piano"
    )
    beta = await _create_course(session_client, shortName="pg-beta", name="Beta solfeo")
    archived = await _create_course(
        session_client, shortName="pg-arch", name="Archivado piano"
    )
    await session_client.put(
        f"{_COURSES}/{archived['id']}/archived", json={"archived": True}
    )

    first_page = await session_client.get(_ADMIN, params={"page": 1, "pageSize": 1})
    assert first_page.status_code == 200
    assert first_page.json()["page"] == 1
    assert first_page.json()["pageSize"] == 1
    assert first_page.json()["total"] >= 3
    assert len(first_page.json()["items"]) == 1
    assert "limit" not in first_page.json()
    assert "nextCursor" not in first_page.json()

    queried = await session_client.get(_ADMIN, params={"query": "piano"})
    names = [item["name"] for item in queried.json()["items"]]
    assert alpha["name"] in names
    assert archived["name"] in names
    assert beta["name"] not in names

    only_archived = await session_client.get(_ADMIN, params={"archived": "true"})
    archived_ids = [item["id"] for item in only_archived.json()["items"]]
    assert archived["id"] in archived_ids
    assert alpha["id"] not in archived_ids

    only_active = await session_client.get(_ADMIN, params={"archived": "false"})
    active_ids = [item["id"] for item in only_active.json()["items"]]
    assert alpha["id"] in active_ids
    assert archived["id"] not in active_ids


async def test_unknown_course_is_not_found(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    missing = str(uuid.uuid4())
    assert (await session_client.get(f"{_COURSES}/{missing}")).status_code == 404
    assert (await session_client.delete(f"{_ADMIN}/{missing}")).status_code == 404
    duplicated = await session_client.post(
        f"{_ADMIN}/{missing}/duplicate",
        json={"shortName": "no-hay", "name": "Nadie", "startDate": "2026-01-01"},
    )
    assert duplicated.status_code == 404


async def test_create_validation_rejects_blank_short_name(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    response = await session_client.post(_ADMIN, json=_create_payload(shortName="   "))
    assert response.status_code == 400
    assert response.json()["error"] == "validation"
    assert "shortName" in response.json()["fields"]
