import asyncio
import unicodedata
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CategoryNameTaken
from app.models import Category, Course
from app.models.course import CourseLevel, DefaultGroupMode
from app.models.user import SystemRole
from app.permissions.types import Permission
from app.schemas.categories import (
    CategoryCreate,
    CategoryUpdate,
    category_name_key,
    normalize_category_description,
    normalize_category_name,
)
from app.services.categories import (
    _CATEGORY_TREE_LOCK_KEY,
    _integrity_name,
    _raise_from_integrity,
    create_category,
    delete_category,
    update_category,
)
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
    assert isinstance(created["sortOrder"], int)

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


async def test_put_omitted_fields_do_not_change(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    parent = await _create(session_client, name="Contenedor")
    child = await _create(
        session_client,
        name="Movida",
        parent_id=parent["id"],
        description="conservar",
    )
    response = await session_client.put(
        f"{_ADMIN}/{child['id']}",
        json={"name": "Movida 2"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Movida 2"
    assert response.json()["parentId"] == parent["id"]
    assert response.json()["description"] == "conservar"


async def test_put_explicit_null_clears_parent_and_description(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    parent = await _create(session_client, name="Contenedor")
    child = await _create(
        session_client,
        name="Movida",
        parent_id=parent["id"],
        description="borrar",
    )
    response = await session_client.put(
        f"{_ADMIN}/{child['id']}",
        json={"parentId": None, "description": None},
    )
    assert response.status_code == 200
    assert response.json()["parentId"] is None
    assert response.json()["description"] is None
    assert response.json()["name"] == "Movida"


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


async def test_single_level_rejects_grandchildren_and_non_root_parent(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    root = await _create(session_client, name="Raíz")
    child = await _create(session_client, name="Hija", parent_id=root["id"])
    other = await _create(session_client, name="Otra")

    grandchild = await session_client.post(
        _ADMIN, json={"name": "Nieta", "parentId": child["id"]}
    )
    assert grandchild.status_code == 400
    assert grandchild.json()["error"] == "validation"
    assert "parentId" in grandchild.json()["fields"]

    self_parent = await session_client.put(
        f"{_ADMIN}/{root['id']}",
        json={"parentId": root["id"]},
    )
    assert self_parent.status_code == 400
    assert self_parent.json()["error"] == "validation"
    assert "parentId" in self_parent.json()["fields"]

    parent_under_child = await session_client.put(
        f"{_ADMIN}/{root['id']}",
        json={"parentId": child["id"]},
    )
    assert parent_under_child.status_code == 400
    assert "parentId" in parent_under_child.json()["fields"]

    root_with_children = await session_client.put(
        f"{_ADMIN}/{root['id']}",
        json={"parentId": other["id"]},
    )
    assert root_with_children.status_code == 400
    assert "parentId" in root_with_children.json()["fields"]

    moved_leaf = await session_client.put(
        f"{_ADMIN}/{child['id']}",
        json={"parentId": other["id"]},
    )
    assert moved_leaf.status_code == 200
    assert moved_leaf.json()["parentId"] == other["id"]


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


async def _occupy_with_course(db_session: AsyncSession, category_id: str) -> Course:
    course = Course(
        short_name=f"bloqueante-{uuid.uuid4().hex[:8]}",
        name="Curso que ocupa la categoría",
        category_id=uuid.UUID(category_id),
        start_date=date(2026, 1, 1),
        visible=True,
        level=CourseLevel.BEGINNER,
        default_group_mode=DefaultGroupMode.NONE,
    )
    db_session.add(course)
    await db_session.flush()
    return course


async def _soft_delete_course(db_session: AsyncSession, course: Course) -> None:
    stored = await db_session.scalar(select(Course).where(Course.id == course.id))
    assert stored is not None
    stored.deleted_at = datetime.now(UTC)
    await db_session.flush()


async def test_delete_with_courses_is_category_not_empty(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    category = await _create(session_client, name="Con cursos")
    course = await _occupy_with_course(db_session, category["id"])

    blocked = await session_client.delete(f"{_ADMIN}/{category['id']}")
    assert blocked.status_code == 409
    assert blocked.json() == {"error": "category_not_empty"}

    await _soft_delete_course(db_session, course)
    allowed = await session_client.delete(f"{_ADMIN}/{category['id']}")
    assert allowed.status_code == 204


async def test_move_with_courses_is_category_not_empty(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    parent = await _create(session_client, name="Raíz origen")
    other = await _create(session_client, name="Raíz destino")
    leaf = await _create(session_client, name="Hoja con cursos", parent_id=parent["id"])
    course = await _occupy_with_course(db_session, leaf["id"])

    blocked = await session_client.put(
        f"{_ADMIN}/{leaf['id']}",
        json={"parentId": other["id"]},
    )
    assert blocked.status_code == 409
    assert blocked.json() == {"error": "category_not_empty"}
    listed = await session_client.get(_LIST)
    current = next(item for item in listed.json() if item["id"] == leaf["id"])
    assert current["parentId"] == parent["id"]

    await _soft_delete_course(db_session, course)
    allowed = await session_client.put(
        f"{_ADMIN}/{leaf['id']}",
        json={"parentId": other["id"]},
    )
    assert allowed.status_code == 200
    assert allowed.json()["parentId"] == other["id"]


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


_LEGITIMATE_CF_NAMES: tuple[str, ...] = (
    "👨‍👩‍👧",
    "🌈\ufe0f",
    "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "שלום\u200f",
    "سلام\u061c",
    "क्‍ष",
    "എന്‍റെ",
    "ব্যক্তি",
    "می‌خواهم",
)


def test_normalize_category_name_nfc_spaces_and_invisibles() -> None:
    nfc = "Café"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert normalize_category_name(nfd) == nfc
    assert normalize_category_name("Solfeo  I") == "Solfeo I"
    assert normalize_category_name("é" * 100) == "é" * 100
    assert normalize_category_name(unicodedata.normalize("NFD", "é" * 100)) == "é" * 100
    assert normalize_category_name("Piano\u200bI") == "Piano\u200bI"
    assert normalize_category_name("Piano\u200dI") == "Piano\u200dI"
    assert normalize_category_name("\u200bPiano\u200b") == "Piano"
    for name in _LEGITIMATE_CF_NAMES:
        got = normalize_category_name(name)
        assert got
        assert any(unicodedata.category(char)[0] not in {"C", "Z", "M"} for char in got)
    with pytest.raises(ValueError):
        normalize_category_name("\u200b")
    with pytest.raises(ValueError):
        normalize_category_name("\u3164")
    with pytest.raises(ValueError):
        normalize_category_name("\u2800")
    with pytest.raises(ValueError):
        normalize_category_name("\u115f")
    with pytest.raises(ValueError):
        normalize_category_name("\u1160")
    with pytest.raises(ValueError):
        normalize_category_name("\u0301")
    with pytest.raises(ValueError):
        normalize_category_name("\x00ok")
    with pytest.raises(ValueError):
        normalize_category_name("   ")
    with pytest.raises(ValueError):
        normalize_category_name("a\udc00")
    with pytest.raises(ValueError):
        normalize_category_description("a\udc00")
    with pytest.raises(ValueError):
        normalize_category_description("ok\x00")
    assert category_name_key("a\u200bb") == category_name_key("ab")
    assert len("x" * 121) == 121
    with pytest.raises(ValueError):
        normalize_category_name("x" * 121)
    assert normalize_category_name("x" * 120) == "x" * 120


async def test_name_nfc_nfd_and_double_space_collide(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    nfc = "Café"
    created = await _create(session_client, name=unicodedata.normalize("NFD", nfc))
    assert created["name"] == nfc
    again = await session_client.post(_ADMIN, json={"name": nfc})
    assert again.status_code == 409
    assert again.json() == {"error": "category_name_taken"}

    spaced = await _create(session_client, name="Solfeo  I")
    assert spaced["name"] == "Solfeo I"
    dup = await session_client.post(_ADMIN, json={"name": "Solfeo I"})
    assert dup.status_code == 409


async def test_name_rejects_zero_width_and_nul(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    zwsp = await session_client.post(_ADMIN, json={"name": "\u200b"})
    assert zwsp.status_code == 400
    assert zwsp.json()["error"] == "validation"
    assert "name" in zwsp.json()["fields"]

    nul = await session_client.post(_ADMIN, json={"name": "a\x00b"})
    assert nul.status_code in {400, 422}
    assert nul.status_code != 500
    if nul.status_code == 400:
        assert nul.json()["error"] == "validation"

    for payload in (
        "\u3164",
        "\u2800",
        "\u115f",
        "\u1160",
        "\u0301",
    ):
        response = await session_client.post(_ADMIN, json={"name": payload})
        assert response.status_code == 400
        assert response.json()["error"] == "validation"
        assert "name" in response.json()["fields"]

    for name in _LEGITIMATE_CF_NAMES:
        created = await _create(session_client, name=name)
        assert created["name"] == normalize_category_name(name)

    inside = await _create(session_client, name="Piano\u200bI")
    assert inside["name"] == "Piano\u200bI"


async def test_nfd_length_and_nfc_expansion(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    nfd_hundred = unicodedata.normalize("NFD", "é" * 100)
    created = await _create(session_client, name=nfd_hundred)
    assert created["name"] == "é" * 100

    huge = await session_client.post(
        _ADMIN,
        json={"name": "Ok", "description": "\u0344" * 2000},
    )
    assert huge.status_code in {400, 422}
    assert huge.status_code != 500

    too_long = await session_client.post(_ADMIN, json={"name": "n" * 121})
    assert too_long.status_code in {400, 422}
    assert too_long.status_code != 500
    created_max = await _create(session_client, name="n" * 120)
    assert created_max["name"] == "n" * 120


async def test_surrogates_are_validation_not_500(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="Base")
    headers = {"Content-Type": "application/json"}
    desc = await session_client.post(
        _ADMIN,
        content='{"name":"Con surrogate","description":"a\\udc00"}',
        headers=headers,
    )
    assert desc.status_code in {400, 422}
    assert desc.status_code != 500
    put_desc = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        content='{"description":"b\\udc00"}',
        headers=headers,
    )
    assert put_desc.status_code in {400, 422}
    assert put_desc.status_code != 500
    put_name = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        content='{"name":"c\\udc00"}',
        headers=headers,
    )
    assert put_name.status_code in {400, 422}
    assert put_name.status_code != 500


async def test_nul_description_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    response = await session_client.post(
        _ADMIN,
        json={"name": "Nulo", "description": "ok\x00no"},
    )
    assert response.status_code in {400, 422}
    assert response.status_code != 500


async def test_zwsp_name_collides_with_plain(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="ab")
    dup = await session_client.post(_ADMIN, json={"name": "a\u200bb"})
    assert dup.status_code == 409
    assert dup.json() == {"error": "category_name_taken"}
    put = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        json={"name": "a\u200bb"},
    )
    assert put.status_code == 200
    assert put.json()["name"] == "a\u200bb"
    other = await _create(session_client, name="cd")
    clash = await session_client.put(
        f"{_ADMIN}/{other['id']}",
        json={"name": "a\u200bb"},
    )
    assert clash.status_code == 409


async def test_put_name_is_normalized(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="Original")
    nfd = unicodedata.normalize("NFD", "Café")
    response = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        json={"name": nfd},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Café"


async def test_put_duplicate_name_is_conflict_not_500(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    first = await _create(session_client, name="Armonía")
    second = await _create(session_client, name="Solfeo")
    response = await session_client.put(
        f"{_ADMIN}/{second['id']}",
        json={"name": "armonía"},
    )
    assert response.status_code == 409
    assert response.json() == {"error": "category_name_taken"}
    assert first["id"] not in response.text


async def test_put_null_name_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="Nombre fijo")
    response = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        json={"name": None},
    )
    assert response.status_code in {400, 422}
    assert response.status_code != 500
    if response.status_code == 400:
        assert response.json()["error"] == "validation"
        assert "name" in response.json()["fields"]
    listed = await session_client.get(_LIST)
    match = next(item for item in listed.json() if item["id"] == created["id"])
    assert match["name"] == created["name"]


async def test_put_null_sort_order_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="Compás")
    response = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        json={"sortOrder": None},
    )
    assert response.status_code in {400, 422}
    assert response.status_code != 500
    if response.status_code == 400:
        assert response.json()["error"] == "validation"
        assert "sortOrder" in response.json()["fields"]
    listed = await session_client.get(_LIST)
    match = next(item for item in listed.json() if item["id"] == created["id"])
    assert match["sortOrder"] == created["sortOrder"]


async def test_put_null_payload_is_validated_before_lookup(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    missing = str(uuid.uuid4())
    sort_null = await session_client.put(
        f"{_ADMIN}/{missing}",
        json={"sortOrder": None},
    )
    assert sort_null.status_code in {400, 422}
    assert sort_null.status_code != 404
    name_null = await session_client.put(
        f"{_ADMIN}/{missing}",
        json={"name": None},
    )
    assert name_null.status_code in {400, 422}
    assert name_null.status_code != 404


def test_unknown_integrity_error_is_reraised() -> None:
    class _Orig(Exception):
        constraint_name = "ck_unrelated"

    exc = IntegrityError("INSERT", {}, _Orig())
    with pytest.raises(IntegrityError):
        _raise_from_integrity(exc)


def test_name_unique_integrity_error_is_name_taken() -> None:
    class _Orig(Exception):
        constraint_name = "uq_categories_root_name_active"

    exc = IntegrityError("INSERT", {}, _Orig())
    with pytest.raises(CategoryNameTaken):
        _raise_from_integrity(exc)

    sibling = IntegrityError(
        "INSERT",
        {},
        type(
            "Orig",
            (Exception,),
            {"constraint_name": "uq_categories_parent_id_name_key"},
        )(),
    )
    with pytest.raises(CategoryNameTaken):
        _raise_from_integrity(sibling)

    parent = IntegrityError(
        "INSERT",
        {},
        type("Orig", (Exception,), {"constraint_name": "parent_not_self"})(),
    )
    with pytest.raises(IntegrityError):
        _raise_from_integrity(parent)


def test_integrity_name_reads_asyncpg_constraint() -> None:
    class AsyncpgError(Exception):
        constraint_name = "uq_categories_root_name_active"

    class Wrapper(Exception):
        pass

    wrapper = Wrapper()
    wrapper.__cause__ = AsyncpgError()
    exc = IntegrityError("INSERT", {}, wrapper)
    assert _integrity_name(exc) == "uq_categories_root_name_active"

    direct = IntegrityError("INSERT", {}, AsyncpgError())
    assert _integrity_name(direct) == "uq_categories_root_name_active"

    class DeadDiag(Exception):
        diag = type("Diag", (), {"constraint_name": "psycopg_only"})()

    dead = IntegrityError("INSERT", {}, DeadDiag())
    assert _integrity_name(dead) == ""
    empty = IntegrityError("INSERT", {}, Exception())
    empty.orig = None
    assert _integrity_name(empty) == ""


async def test_parent_not_self_check_exists(db_session: AsyncSession) -> None:
    name = await db_session.scalar(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conname = 'ck_categories_parent_not_self'"
        )
    )
    assert name == "ck_categories_parent_not_self"


async def test_unique_indexes_filter_deleted_and_name_is_citext(
    db_session: AsyncSession,
) -> None:
    root_idx = await db_session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_categories_root_name_active'"
        )
    )
    parent_idx = await db_session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_categories_parent_id_name_key'"
        )
    )
    assert root_idx is not None and "deleted_at IS NULL" in root_idx
    assert parent_idx is not None and "deleted_at IS NULL" in parent_idx
    assert "name_key" in root_idx
    assert "name_key" in parent_idx
    udt = await db_session.scalar(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'categories' "
            "AND column_name = 'name'"
        )
    )
    assert udt == "citext"
    key_udt = await db_session.scalar(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'categories' "
            "AND column_name = 'name_key'"
        )
    )
    assert key_udt == "citext"
    samples = ("ab", "a\u200bb", "می‌خواهم", "👨‍👩‍👧", "Café")
    for sample in samples:
        db_key = await db_session.scalar(
            text("SELECT category_name_key(CAST(:n AS citext))"),
            {"n": sample},
        )
        assert db_key == category_name_key(sample)


def test_tree_lock_is_transactional_not_session(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    """Tras commit/rollback no queda advisory lock; otra sesión puede tomarlo."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []
    classid, objid = _CATEGORY_TREE_LOCK_KEY

    async def try_tree_lock(session: Any) -> bool:
        got = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:classid, :objid)"),
            {"classid": classid, "objid": objid},
        )
        return bool(got)

    async def leftover_locks(session: Any) -> int:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                )
            )
            or 0
        )

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=2, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                parent = await create_category(
                    setup, CategoryCreate(name=f"lockp-{suffix}")
                )
                tracked.append(parent.id)
                child = await create_category(
                    setup,
                    CategoryCreate(name=f"lockc-{suffix}", parent_id=parent.id),
                )
                tracked.append(child.id)
                other = await create_category(
                    setup, CategoryCreate(name=f"locko-{suffix}")
                )
                tracked.append(other.id)
                await setup.commit()

            async with factory() as holder:
                born = await create_category(
                    holder,
                    CategoryCreate(name=f"lockn-{suffix}", parent_id=parent.id),
                )
                tracked.append(born.id)

                async def blocked_create() -> str:
                    async with factory() as waiter:
                        row = await create_category(
                            waiter,
                            CategoryCreate(name=f"lockw-{suffix}", parent_id=parent.id),
                        )
                        tracked.append(row.id)
                        await waiter.commit()
                    return "ok"

                pending = asyncio.create_task(blocked_create())
                await asyncio.sleep(0.4)
                assert pending.done() is False
                await holder.commit()
                assert await leftover_locks(holder) == 0
                assert await pending == "ok"

            async with factory() as free:
                assert await try_tree_lock(free) is True
                await free.rollback()
                assert await leftover_locks(free) == 0

            async with factory() as holder:
                await update_category(
                    holder, child.id, CategoryUpdate(parent_id=other.id)
                )
                async with factory() as waiter:
                    assert await try_tree_lock(waiter) is False
                    await waiter.rollback()
                await holder.commit()
                assert await leftover_locks(holder) == 0

            async with factory() as holder:
                await delete_category(holder, child.id)
                async with factory() as waiter:
                    assert await try_tree_lock(waiter) is False
                    await waiter.rollback()
                await holder.commit()
                assert await leftover_locks(holder) == 0
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_duplicate_create_keeps_session_usable(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=1, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                first = await create_category(
                    session, CategoryCreate(name=f"DupSess-{suffix}")
                )
                tracked.append(first.id)

                async def never_taken(*_args: object, **_kwargs: object) -> bool:
                    return False

                from app.services import categories as categories_svc

                original = categories_svc._name_taken
                categories_svc._name_taken = never_taken
                try:
                    with pytest.raises(CategoryNameTaken):
                        await create_category(
                            session, CategoryCreate(name=f"DupSess-{suffix}")
                        )
                finally:
                    categories_svc._name_taken = original

                second = await create_category(
                    session, CategoryCreate(name=f"DupSess2-{suffix}")
                )
                tracked.append(second.id)
                await session.commit()
                assert second.id != first.id
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_cleanup_production_deletes_prod_categories(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn
    from tests.conftest import _cleanup_production_test_rows

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)

    async def run() -> None:
        engine = create_async_engine(dsn.secret())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                row = await create_category(
                    session, CategoryCreate(name="Prod-cleanup-marker")
                )
                await session.commit()
                marker = row.id
            await _cleanup_production_test_rows(dsn)
            async with factory() as session:
                left = await session.scalar(
                    select(Category.id).where(Category.id == marker)
                )
                assert left is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_category_mutations_require_manage_courses() -> None:
    from app.routers.categories import _MANAGE

    names = _MANAGE.__code__.co_freevars
    assert _MANAGE.__closure__ is not None
    captured = {
        name: cell.cell_contents
        for name, cell in zip(names, _MANAGE.__closure__, strict=True)
    }
    assert captured["permissions"] == (Permission.SITE_MANAGE_COURSES,)


def _tree_depth_and_cycles(items: list[dict[str, Any]]) -> tuple[int, bool]:
    by_id = {item["id"]: item for item in items}

    def walk(item: dict[str, Any]) -> int:
        seen: set[str] = set()
        depth = 0
        current = item
        while current["parentId"]:
            if current["id"] in seen:
                return 999
            seen.add(current["id"])
            parent = by_id.get(current["parentId"])
            if parent is None:
                break
            depth += 1
            current = parent
        return depth

    depths = [walk(item) for item in items]
    return max(depths, default=0), any(depth >= 999 for depth in depths)


def _run_in_thread(database_url: str, barrier: Any, work: Any) -> str:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.exceptions import (
        CategoryNameTaken,
        CategoryNotEmpty,
        NotFound,
        Validation,
    )

    async def run() -> str:
        engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                barrier.wait(timeout=5)
                try:
                    result = await work(session)
                    await session.commit()
                    assert isinstance(result, str)
                    return result
                except Validation:
                    await session.rollback()
                    return "validation"
                except CategoryNameTaken:
                    await session.rollback()
                    return "name_taken"
                except CategoryNotEmpty:
                    await session.rollback()
                    return "not_empty"
                except NotFound:
                    await session.rollback()
                    return "not_found"
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _race(database_url: str, left: Any, right: Any) -> list[str]:
    import threading

    barrier = threading.Barrier(2)
    results: list[str] = []

    def run(work: Any) -> None:
        results.append(_run_in_thread(database_url, barrier, work))

    t1 = threading.Thread(target=run, args=(left,))
    t2 = threading.Thread(target=run, args=(right,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    return results


async def _cleanup_ids(database_url: str, ids: list[uuid.UUID]) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            if ids:
                await session.execute(delete(Category).where(Category.id.in_(ids)))
            leftover = await session.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            )
            assert leftover == 0
            await session.commit()
    finally:
        await engine.dispose()


async def _orphans(session: Any) -> int:
    return int(
        await session.scalar(
            text(
                "SELECT count(*) FROM categories child "
                "JOIN categories parent ON parent.id = child.parent_id "
                "WHERE child.deleted_at IS NULL AND parent.deleted_at IS NOT NULL"
            )
        )
        or 0
    )


def test_concurrent_tree_mutations(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn
    from app.schemas.categories import CategoryCreate, CategoryUpdate
    from app.services.categories import (
        create_category,
        delete_category,
        list_categories,
        update_category,
    )

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []

    async def seed() -> dict[str, uuid.UUID]:
        engine = create_async_engine(dsn.secret())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                created: dict[str, uuid.UUID] = {}
                for key in ("root", "x", "leaf", "a", "b", "parent", "mover"):
                    row = await create_category(
                        session, CategoryCreate(name=f"{key}-{suffix}")
                    )
                    created[key] = row.id
                    tracked.append(row.id)
                named = await create_category(
                    session, CategoryCreate(name=f"keep-{suffix}")
                )
                other = await create_category(
                    session, CategoryCreate(name=f"rename-{suffix}")
                )
                created["keep"] = named.id
                created["rename"] = other.id
                tracked.extend([named.id, other.id])
                await session.commit()
                return created
        finally:
            await engine.dispose()

    try:
        ids = asyncio.run(seed())

        async def reparent_leaf(session: Any) -> str:
            await update_category(
                session, ids["leaf"], CategoryUpdate(parent_id=ids["x"])
            )
            return "ok"

        async def reparent_x(session: Any) -> str:
            await update_category(
                session, ids["x"], CategoryUpdate(parent_id=ids["root"])
            )
            return "ok"

        first = _race(dsn.secret(), reparent_leaf, reparent_x)
        assert sorted(first) == ["ok", "validation"]

        async def a_to_b(session: Any) -> str:
            await update_category(session, ids["a"], CategoryUpdate(parent_id=ids["b"]))
            return "ok"

        async def b_to_a(session: Any) -> str:
            await update_category(session, ids["b"], CategoryUpdate(parent_id=ids["a"]))
            return "ok"

        second = _race(dsn.secret(), a_to_b, b_to_a)
        assert sorted(second) == ["ok", "validation"]

        async def rename_keep(session: Any) -> str:
            await update_category(
                session, ids["keep"], CategoryUpdate(name=f"same-{suffix}")
            )
            return "ok"

        async def rename_other(session: Any) -> str:
            await update_category(
                session, ids["rename"], CategoryUpdate(name=f"same-{suffix}")
            )
            return "ok"

        renamed = _race(dsn.secret(), rename_keep, rename_other)
        assert "ok" in renamed
        assert "name_taken" in renamed
        assert "error" not in renamed

        async def post_child(session: Any) -> str:
            row = await create_category(
                session,
                CategoryCreate(name=f"born-{suffix}", parent_id=ids["parent"]),
            )
            tracked.append(row.id)
            return "ok"

        async def delete_parent(session: Any) -> str:
            await delete_category(session, ids["parent"])
            return "ok"

        delete_vs_post = _race(dsn.secret(), post_child, delete_parent)
        assert "ok" in delete_vs_post
        assert set(delete_vs_post) <= {"ok", "validation", "not_empty"}

        delete_parent_id = seed_one(dsn.secret(), f"delp-{suffix}", tracked)
        mover2 = seed_one(dsn.secret(), f"mov2-{suffix}", tracked)

        async def put_under_fresh(session: Any) -> str:
            await update_category(
                session, mover2, CategoryUpdate(parent_id=delete_parent_id)
            )
            return "ok"

        async def delete_fresh_parent(session: Any) -> str:
            await delete_category(session, delete_parent_id)
            return "ok"

        delete_vs_put = _race(dsn.secret(), put_under_fresh, delete_fresh_parent)
        assert "ok" in delete_vs_put
        assert set(delete_vs_put) <= {"ok", "validation", "not_empty"}

        extra_parent = seed_one(dsn.secret(), f"p2-{suffix}", tracked)
        extra_root = seed_one(dsn.secret(), f"r2-{suffix}", tracked)

        async def create_under_extra(session: Any) -> str:
            row = await create_category(
                session,
                CategoryCreate(name=f"c2-{suffix}", parent_id=extra_parent),
            )
            tracked.append(row.id)
            return "ok"

        async def move_extra_under_root(session: Any) -> str:
            await update_category(
                session, extra_parent, CategoryUpdate(parent_id=extra_root)
            )
            return "ok"

        create_vs_update = _race(
            dsn.secret(), create_under_extra, move_extra_under_root
        )
        assert sorted(create_vs_update) == ["ok", "validation"]

        sibling_parent = seed_one(dsn.secret(), f"p3-{suffix}", tracked)

        async def create_one(session: Any) -> str:
            row = await create_category(
                session,
                CategoryCreate(name=f"s1-{suffix}", parent_id=sibling_parent),
            )
            tracked.append(row.id)
            return "ok"

        async def create_two(session: Any) -> str:
            row = await create_category(
                session,
                CategoryCreate(name=f"s2-{suffix}", parent_id=sibling_parent),
            )
            tracked.append(row.id)
            return "ok"

        create_vs_create = _race(dsn.secret(), create_one, create_two)
        assert create_vs_create == ["ok", "ok"]

        async def inspect() -> None:
            engine = create_async_engine(dsn.secret())
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session:
                    rows = await list_categories(session)
                    ours = [
                        {
                            "id": str(row.id),
                            "parentId": (str(row.parent_id) if row.parent_id else None),
                        }
                        for row in rows
                        if row.id in set(tracked)
                    ]
                    depth, has_cycle = _tree_depth_and_cycles(ours)
                    assert has_cycle is False
                    assert depth <= 1
                    assert await _orphans(session) == 0
            finally:
                await engine.dispose()

        asyncio.run(inspect())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def seed_one(database_url: str, name: str, tracked: list[uuid.UUID]) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.schemas.categories import CategoryCreate
    from app.services.categories import create_category

    async def run() -> uuid.UUID:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                row = await create_category(session, CategoryCreate(name=name))
                await session.commit()
                tracked.append(row.id)
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(run())
