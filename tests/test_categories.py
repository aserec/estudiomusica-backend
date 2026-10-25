import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course
from app.models.course import CourseLevel, DefaultGroupMode
from app.models.user import SystemRole
from tests.test_auth import unique_email
from tests.test_roles import (
    SYSTEM_ROLES,
    _assert_no_secrets,
    _request,
    _set_role,
    _signup,
)

pytestmark = pytest.mark.db

_LIST = "/api/v1/categories"
_ADMIN = "/api/v1/admin/categories"

ADMIN_MUTATIONS: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("POST", _ADMIN, {"name": "Teoría"}),
    ("PUT", f"{_ADMIN}/{uuid.UUID(int=1)}", {"name": "Teoría"}),
    ("DELETE", f"{_ADMIN}/{uuid.UUID(int=1)}", None),
)

PROTECTED: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", _LIST, None),
    *ADMIN_MUTATIONS,
)


def _category_keys(body: dict[str, Any]) -> None:
    assert set(body) == {
        "id",
        "name",
        "parentId",
        "description",
        "sortOrder",
        "createdAt",
        "updatedAt",
    }


async def _admin(session_client: Any, db_session: AsyncSession) -> dict[str, Any]:
    created = await _signup(session_client, email=unique_email("cat-admin"))
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    return created


async def _create(
    session_client: Any,
    *,
    name: str,
    parent_id: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, object] = {"name": name}
    if parent_id is not None:
        payload["parentId"] = parent_id
    if description is not None:
        payload["description"] = description
    if sort_order is not None:
        payload["sortOrder"] = sort_order
    response = await session_client.post(_ADMIN, json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    _category_keys(body)
    return body


@pytest.mark.parametrize("role", SYSTEM_ROLES)
async def test_authenticated_roles_can_list_categories(
    session_client: Any,
    db_session: AsyncSession,
    role: SystemRole,
) -> None:
    created = await _signup(session_client, email=unique_email(role.value))
    await _set_role(db_session, created["id"], role)
    response = await session_client.get(_LIST)
    _assert_no_secrets(response)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.parametrize("role", SYSTEM_ROLES)
@pytest.mark.parametrize("method,path,body", ADMIN_MUTATIONS)
async def test_non_admin_cannot_mutate_categories(
    session_client: Any,
    db_session: AsyncSession,
    role: SystemRole,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    created = await _signup(session_client, email=unique_email(f"mut-{role.value}"))
    await _set_role(db_session, created["id"], role)
    response = await _request(session_client, method, path, body)
    _assert_no_secrets(response)
    if role == SystemRole.ADMIN:
        assert response.status_code in {201, 404, 204}
    else:
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}
        assert created["email"] not in response.text


@pytest.mark.parametrize("method,path,body", PROTECTED)
async def test_category_endpoints_without_session_are_unauthorized(
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
async def test_category_endpoints_reject_invalid_token(
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


async def test_admin_creates_category_and_it_appears_in_list(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(
        session_client,
        name="  Armonía  ",
        description="  tonal  ",
    )
    assert created["name"] == "Armonía"
    assert created["parentId"] is None
    assert created["description"] == "tonal"
    assert created["sortOrder"] == 0

    listed = await session_client.get(_LIST)
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()]
    assert created["id"] in ids
    match = next(item for item in listed.json() if item["id"] == created["id"])
    assert match["name"] == "Armonía"
    _assert_no_secrets(listed)


async def test_create_child_and_unique_name_per_parent(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    root = await _create(session_client, name="Instrumentos")
    other_root = await _create(session_client, name="Teoría")
    child = await _create(
        session_client,
        name="Piano",
        parent_id=root["id"],
        description="teclado",
    )
    assert child["parentId"] == root["id"]
    same_other_parent = await _create(
        session_client, name="Piano", parent_id=other_root["id"]
    )
    assert same_other_parent["parentId"] == other_root["id"]

    duplicate_root = await session_client.post(_ADMIN, json={"name": "instrumentos"})
    assert duplicate_root.status_code == 409
    assert duplicate_root.json() == {"error": "category_name_taken"}

    duplicate_child = await session_client.post(
        _ADMIN, json={"name": "PIANO", "parentId": root["id"]}
    )
    assert duplicate_child.status_code == 409
    assert duplicate_child.json() == {"error": "category_name_taken"}


async def test_create_rejects_missing_or_deleted_parent(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    missing = await session_client.post(
        _ADMIN,
        json={"name": "Huérfana", "parentId": str(uuid.uuid4())},
    )
    assert missing.status_code == 400
    assert missing.json()["error"] == "validation"
    assert "parentId" in missing.json()["fields"]

    parent = await _create(session_client, name="Temporal")
    deleted = await session_client.delete(f"{_ADMIN}/{parent['id']}")
    assert deleted.status_code == 204
    orphan = await session_client.post(
        _ADMIN,
        json={"name": "Hija", "parentId": parent["id"]},
    )
    assert orphan.status_code == 400
    assert orphan.json()["error"] == "validation"
    assert "parentId" in orphan.json()["fields"]


async def test_create_validation_rejects_blank_name(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    response = await session_client.post(_ADMIN, json={"name": "   "})
    assert response.status_code == 400
    assert response.json()["error"] == "validation"
    assert "name" in response.json()["fields"]


async def test_put_updates_fields_and_ignores_unknown(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="Solfeo", description="rítmico")
    parent = await _create(session_client, name="Lenguaje")
    response = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        json={
            "name": "Solfeo I",
            "parentId": parent["id"],
            "description": "actualizado",
            "sortOrder": 7,
            "id": str(uuid.uuid4()),
            "deletedAt": "2020-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["name"] == "Solfeo I"
    assert body["parentId"] == parent["id"]
    assert body["description"] == "actualizado"
    assert body["sortOrder"] == 7
    assert "deletedAt" not in body


async def test_put_unknown_category_is_not_found(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    response = await session_client.put(
        f"{_ADMIN}/{uuid.uuid4()}",
        json={"name": "Nadie"},
    )
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


async def test_put_without_parent_id_moves_to_root(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    parent = await _create(session_client, name="Contenedor")
    child = await _create(session_client, name="Movida", parent_id=parent["id"])
    response = await session_client.put(
        f"{_ADMIN}/{child['id']}",
        json={"name": "Movida", "description": child["description"]},
    )
    assert response.status_code == 200
    assert response.json()["parentId"] is None


async def test_put_same_name_on_self_is_allowed(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="Historia")
    response = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        json={"name": "Historia", "description": "occidental"},
    )
    assert response.status_code == 200
    assert response.json()["description"] == "occidental"


async def test_hierarchy_cycle_is_conflict(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    root = await _create(session_client, name="Raíz")
    child = await _create(session_client, name="Hija", parent_id=root["id"])
    grand = await _create(session_client, name="Nieta", parent_id=child["id"])

    self_parent = await session_client.put(
        f"{_ADMIN}/{root['id']}",
        json={"name": "Raíz", "parentId": root["id"]},
    )
    assert self_parent.status_code == 409
    assert self_parent.json() == {"error": "category_cycle"}

    parent_to_child = await session_client.put(
        f"{_ADMIN}/{root['id']}",
        json={"name": "Raíz", "parentId": child["id"]},
    )
    assert parent_to_child.status_code == 409
    assert parent_to_child.json() == {"error": "category_cycle"}

    parent_to_grand = await session_client.put(
        f"{_ADMIN}/{root['id']}",
        json={"name": "Raíz", "parentId": grand["id"]},
    )
    assert parent_to_grand.status_code == 409
    assert parent_to_grand.json() == {"error": "category_cycle"}

    child_to_self_chain = await session_client.put(
        f"{_ADMIN}/{child['id']}",
        json={"name": "Hija", "parentId": grand["id"]},
    )
    assert child_to_self_chain.status_code == 409
    assert child_to_self_chain.json() == {"error": "category_cycle"}


async def test_delete_with_children_is_category_not_empty(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    parent = await _create(session_client, name="Con hijas")
    child = await _create(session_client, name="Hija viva", parent_id=parent["id"])
    blocked = await session_client.delete(f"{_ADMIN}/{parent['id']}")
    assert blocked.status_code == 409
    assert blocked.json() == {"error": "category_not_empty"}
    assert child["id"] not in blocked.text

    removed_child = await session_client.delete(f"{_ADMIN}/{child['id']}")
    assert removed_child.status_code == 204
    allowed = await session_client.delete(f"{_ADMIN}/{parent['id']}")
    assert allowed.status_code == 204
    assert allowed.content == b""

    missing = await session_client.delete(f"{_ADMIN}/{parent['id']}")
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found"}

    listed = await session_client.get(_LIST)
    ids = [item["id"] for item in listed.json()]
    assert parent["id"] not in ids
    assert child["id"] not in ids


async def test_delete_with_courses_is_category_not_empty(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    category = await _create(session_client, name="Con cursos")
    course = Course(
        short_name="bloqueante",
        name="Curso que ocupa la categoría",
        category_id=uuid.UUID(category["id"]),
        start_date=date(2026, 1, 1),
        visible=True,
        level=CourseLevel.BEGINNER,
        default_group_mode=DefaultGroupMode.NONE,
    )
    db_session.add(course)
    await db_session.flush()

    blocked = await session_client.delete(f"{_ADMIN}/{category['id']}")
    assert blocked.status_code == 409
    assert blocked.json() == {"error": "category_not_empty"}

    stored = await db_session.scalar(select(Course).where(Course.id == course.id))
    assert stored is not None
    stored.deleted_at = datetime.now(UTC)
    await db_session.flush()
    allowed = await session_client.delete(f"{_ADMIN}/{category['id']}")
    assert allowed.status_code == 204


async def test_soft_deleted_name_can_be_reused(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    first = await _create(session_client, name="Reutilizable")
    deleted = await session_client.delete(f"{_ADMIN}/{first['id']}")
    assert deleted.status_code == 204
    reused = await _create(session_client, name="reutilizable")
    assert reused["id"] != first["id"]
    assert reused["name"] == "reutilizable"


async def test_list_orders_by_sort_order_then_name(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    zeta = await _create(session_client, name="Zeta", sort_order=2)
    alpha = await _create(session_client, name="Alpha", sort_order=2)
    first = await _create(session_client, name="Primera", sort_order=1)
    listed = await session_client.get(_LIST)
    ids = [item["id"] for item in listed.json()]
    assert ids.index(first["id"]) < ids.index(alpha["id"]) < ids.index(zeta["id"])


async def test_sort_order_autoincrement_among_siblings(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    parent = await _create(session_client, name="Padre")
    first = await _create(session_client, name="Uno", parent_id=parent["id"])
    second = await _create(session_client, name="Dos", parent_id=parent["id"])
    assert first["sortOrder"] == 0
    assert second["sortOrder"] == 1
    other_root = await _create(session_client, name="Otra raíz")
    assert other_root["sortOrder"] == parent["sortOrder"] + 1


async def test_student_can_list_but_not_create(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _admin(session_client, db_session)
    created = await _create(session_client, name="Visible")
    session_client.cookies.clear()
    student = await _signup(session_client, email=unique_email("cat-stu"))
    await _set_role(db_session, student["id"], SystemRole.ALUMNO)
    listed = await session_client.get(_LIST)
    assert listed.status_code == 200
    assert created["id"] in [item["id"] for item in listed.json()]
    denied = await session_client.post(_ADMIN, json={"name": "No"})
    assert denied.status_code == 403
    assert denied.json() == {"error": "forbidden"}
    assert admin["email"] not in denied.text
