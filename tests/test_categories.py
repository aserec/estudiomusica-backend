import asyncio
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CategoryNameTaken
from app.models import Category
from app.models.user import SystemRole
from app.permissions.types import Permission
from app.schemas.categories import (
    _CF_STRIP_CHARS,
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

    leaf_self = await session_client.put(
        f"{_ADMIN}/{child['id']}",
        json={"parentId": child["id"]},
    )
    assert leaf_self.status_code == 400
    assert leaf_self.json()["error"] == "validation"
    assert "parentId" in leaf_self.json()["fields"]
    assert leaf_self.status_code != 500

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


_FLAG_ENGLAND = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
_FLAG_SCOTLAND = (
    "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
)
_FLAG_WALES = "\U0001f3f4\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f"
_BLACK_FLAG = "\U0001f3f4"
_DEVANAGARI_ZWJ = "क्\u200dष"
_DEVANAGARI_ZWNJ = "क्\u200cष"

_LEGITIMATE_CF_NAMES: tuple[tuple[str, str], ...] = (
    ("👨‍👩‍👧", "👨‍👩‍👧"),
    ("🌈\ufe0f", "🌈\ufe0f"),
    (_FLAG_ENGLAND, _FLAG_ENGLAND),
    ("שלום\u200f", "שלום"),
    ("سلام\u061c", "سلام"),
    ("क्‍ष", "क्‍ष"),
    ("എന്‍റെ", "എന്‍റെ"),
    ("ব্যক্তি", "ব্যক্তি"),
    ("می‌خواهم", "می‌خواهم"),
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
    for raw, expected in _LEGITIMATE_CF_NAMES:
        assert normalize_category_name(raw) == expected
    assert normalize_category_name(f"  {_FLAG_ENGLAND}  ") == _FLAG_ENGLAND
    assert normalize_category_name(f"{_FLAG_ENGLAND} piano") == f"{_FLAG_ENGLAND} piano"
    assert normalize_category_name(f"piano {_FLAG_ENGLAND}") == f"piano {_FLAG_ENGLAND}"
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
    with pytest.raises(ValueError):
        normalize_category_name("ok\ufffc")
    with pytest.raises(ValueError):
        normalize_category_name("abc\u202e")
    with pytest.raises(ValueError):
        normalize_category_name("abc\u2066def")
    smuggled = "Hola" + "".join(chr(0xE0000 + ord(char)) for char in "IGNORA")
    with pytest.raises(ValueError):
        normalize_category_name(smuggled)
    with pytest.raises(ValueError):
        normalize_category_name("x\U000e0001")
    with pytest.raises(ValueError):
        normalize_category_name(f"{_BLACK_FLAG}\U000e007f")
    assert category_name_key("a\u200bb") == category_name_key("ab")
    assert category_name_key("  a  b  ") == "a b"
    assert category_name_key("a\u200b  b") == "a b"
    assert category_name_key("\u200ba\u200b") == "a"
    assert category_name_key(_FLAG_ENGLAND) != category_name_key(_FLAG_SCOTLAND)
    assert category_name_key(_FLAG_ENGLAND) != category_name_key(_BLACK_FLAG)
    assert category_name_key(_DEVANAGARI_ZWJ) != category_name_key(_DEVANAGARI_ZWNJ)
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

    for raw, expected in _LEGITIMATE_CF_NAMES:
        created = await _create(session_client, name=raw)
        assert created["name"] == expected

    inside = await _create(session_client, name="Piano\u200bI")
    assert inside["name"] == "Piano\u200bI"

    scotland = await _create(session_client, name=_FLAG_SCOTLAND)
    assert scotland["name"] == _FLAG_SCOTLAND
    wales = await _create(session_client, name=_FLAG_WALES)
    assert wales["name"] == _FLAG_WALES
    black = await _create(session_client, name=_BLACK_FLAG)
    assert black["name"] == _BLACK_FLAG
    zwnj = await _create(session_client, name=_DEVANAGARI_ZWNJ)
    assert zwnj["name"] == _DEVANAGARI_ZWNJ

    for payload in (
        "ok\ufffc",
        "abc\u202e",
        "abc\u2066x",
        "Hola" + "".join(chr(0xE0000 + ord(char)) for char in "IGNORA"),
        f"{_BLACK_FLAG}\U000e007f",
    ):
        response = await session_client.post(_ADMIN, json={"name": payload})
        assert response.status_code == 400
        assert response.json()["error"] == "validation"
        assert "name" in response.json()["fields"]


async def test_nfd_length_and_nfc_expansion(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    nfd_hundred = unicodedata.normalize("NFD", "é" * 100)
    created = await _create(session_client, name=nfd_hundred)
    assert created["name"] == "é" * 100

    nfd_max = unicodedata.normalize("NFD", "é" * 120)
    assert len(nfd_max) > 120
    created_nfd_max = await _create(session_client, name=nfd_max)
    assert created_nfd_max["name"] == "é" * 120

    padded = await _create(session_client, name=f"  {'z' * 120}  ")
    assert padded["name"] == "z" * 120

    nfd_over = unicodedata.normalize("NFD", "é" * 121)
    too_nfd = await session_client.post(_ADMIN, json={"name": nfd_over})
    assert too_nfd.status_code in {400, 422}
    assert too_nfd.status_code != 500

    huge = await session_client.post(
        _ADMIN,
        json={"name": "Ok", "description": "\u0344" * 2000},
    )
    assert huge.status_code in {400, 422}
    assert huge.status_code != 500

    desc_nfd = unicodedata.normalize("NFD", "é" * 2000)
    assert len(desc_nfd) > 2000
    with_desc = await _create(session_client, name="DescNfc", description=desc_nfd)
    assert with_desc["description"] == "é" * 2000

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
    samples = (
        "ab",
        "a\u200bb",
        "  a  b  ",
        "\u200ba\u200b",
        "می‌خواهم",
        "👨‍👩‍👧",
        "Café",
        _FLAG_ENGLAND,
        _FLAG_SCOTLAND,
        _DEVANAGARI_ZWJ,
        _DEVANAGARI_ZWNJ,
        f"  {_FLAG_ENGLAND}  ",
    )
    for sample in samples:
        db_key = await db_session.scalar(
            text("SELECT public.category_name_key(CAST(:n AS public.citext))"),
            {"n": sample},
        )
        assert db_key == category_name_key(sample)
    for char in _CF_STRIP_CHARS:
        for sample in (f"a{char} b", f"{char} a", f"a {char}"):
            db_key = await db_session.scalar(
                text("SELECT public.category_name_key(CAST(:n AS public.citext))"),
                {"n": sample},
            )
            assert db_key == category_name_key(sample)
    meta = await db_session.execute(
        text(
            "SELECT p.provolatile, p.proisstrict, p.proconfig "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'category_name_key'"
        )
    )
    row = meta.one()
    assert row.provolatile in {"i", b"i"}
    assert bool(row.proisstrict) is True
    config = row.proconfig or []
    assert any("search_path" in item and "pg_catalog" in item for item in config)


async def test_category_name_key_works_with_empty_search_path(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(text("SET LOCAL search_path = ''"))
    key = await db_session.scalar(
        text("SELECT public.category_name_key(CAST(:n AS public.citext))"),
        {"n": "a\u200bb"},
    )
    assert key == category_name_key("ab")
    inserted = await db_session.scalar(
        text(
            "INSERT INTO public.categories (name, sort_order) "
            "VALUES ('SearchPathProbe', 0) "
            "RETURNING public.categories.name_key"
        )
    )
    assert inserted == category_name_key("SearchPathProbe")


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
                    "WHERE locktype = 'advisory' "
                    "AND classid = :classid AND objid = :objid"
                ),
                {"classid": classid, "objid": objid},
            )
            or 0
        )

    async def assert_lock_released(factory: Any) -> None:
        async with factory() as probe:
            assert await leftover_locks(probe) == 0
            assert await try_tree_lock(probe) is True
            await probe.rollback()
            assert await leftover_locks(probe) == 0

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
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
                async with factory() as probe:
                    assert await try_tree_lock(probe) is False
                    other_key = await probe.scalar(
                        text("SELECT pg_try_advisory_xact_lock(:classid, :objid)"),
                        {"classid": classid, "objid": 99},
                    )
                    assert bool(other_key) is True
                    await probe.rollback()

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
                assert await pending == "ok"
                await assert_lock_released(factory)

            async with factory() as free:
                assert await try_tree_lock(free) is True
                await free.rollback()
                await assert_lock_released(factory)

            async with factory() as holder:
                await update_category(
                    holder, child.id, CategoryUpdate(parent_id=other.id)
                )
                async with factory() as waiter:
                    assert await try_tree_lock(waiter) is False
                    await waiter.rollback()
                await holder.commit()
                await assert_lock_released(factory)

            async with factory() as holder:
                await delete_category(holder, child.id)
                async with factory() as waiter:
                    assert await try_tree_lock(waiter) is False
                    await waiter.rollback()
                await holder.commit()
                await assert_lock_released(factory)
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


def test_duplicate_update_maps_integrity_error(
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
                    session, CategoryCreate(name=f"DupUpd-{suffix}")
                )
                second = await create_category(
                    session, CategoryCreate(name=f"DupUpd2-{suffix}")
                )
                tracked.extend((first.id, second.id))

                async def never_taken(*_args: object, **_kwargs: object) -> bool:
                    return False

                from app.services import categories as categories_svc

                original = categories_svc._name_taken
                categories_svc._name_taken = never_taken
                try:
                    with pytest.raises(CategoryNameTaken):
                        await update_category(
                            session,
                            second.id,
                            CategoryUpdate(name=f"DupUpd-{suffix}"),
                        )
                finally:
                    categories_svc._name_taken = original
                await session.rollback()
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_delete_takes_tree_lock_before_row(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []
    classid, objid = _CATEGORY_TREE_LOCK_KEY

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=3, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                row = await create_category(
                    setup, CategoryCreate(name=f"dellock-{suffix}")
                )
                tracked.append(row.id)
                await setup.commit()

            async with factory() as holder:
                locked = await holder.scalar(
                    text("SELECT id FROM categories WHERE id = :id FOR UPDATE"),
                    {"id": row.id},
                )
                assert locked == row.id

                async def do_delete() -> str:
                    async with factory() as waiter:
                        await delete_category(waiter, row.id)
                        await waiter.commit()
                    return "ok"

                pending = asyncio.create_task(do_delete())
                await asyncio.sleep(0.4)
                assert pending.done() is False
                async with factory() as probe:
                    got = await probe.scalar(
                        text("SELECT pg_try_advisory_xact_lock(:classid, :objid)"),
                        {"classid": classid, "objid": objid},
                    )
                    assert bool(got) is False
                    await probe.rollback()
                await holder.rollback()
                assert await pending == "ok"
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_update_locks_row_before_name_check(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn
    from app.services import categories as categories_svc

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_name_taken(*_args: object, **_kwargs: object) -> bool:
        started.set()
        await release.wait()
        return False

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=2, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        original = categories_svc._name_taken
        try:
            async with factory() as setup:
                row = await create_category(
                    setup, CategoryCreate(name=f"forupd-{suffix}")
                )
                tracked.append(row.id)
                await setup.commit()

            categories_svc._name_taken = blocked_name_taken
            async with factory() as holder:

                async def do_update() -> None:
                    await update_category(
                        holder, row.id, CategoryUpdate(name=f"forupd2-{suffix}")
                    )
                    await holder.commit()

                pending = asyncio.create_task(do_update())
                await asyncio.wait_for(started.wait(), timeout=2)
                async with factory() as probe:
                    with pytest.raises(DBAPIError):
                        await probe.execute(
                            text(
                                "SELECT id FROM categories "
                                "WHERE id = :id FOR UPDATE NOWAIT"
                            ),
                            {"id": row.id},
                        )
                    await probe.rollback()
                release.set()
                await pending
        finally:
            categories_svc._name_taken = original
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_category_dump_restore_preserves_rows(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import RedactedDsn
    from tests.conftest import drop_database_if_exists, ensure_database_exists

    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        pytest.skip("pg_dump/pg_restore no está en PATH")

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    parsed = make_url(dsn.secret())
    libpq = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    suffix = uuid.uuid4().hex[:8]
    marker = f"DumpRestore-{suffix}"

    async def seed() -> str:
        engine = create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                row_id = await connection.scalar(
                    text(
                        "INSERT INTO categories (name, description, sort_order) "
                        "VALUES (:name, :description, 3) RETURNING id"
                    ),
                    {"name": marker, "description": "pg_dump"},
                )
                return str(row_id)
        finally:
            await engine.dispose()

    row_id = asyncio.run(seed())
    dump_dir = Path("/tmp") / f"cat-dump-{suffix}"
    dump_dir.mkdir(parents=True, exist_ok=True)
    custom = dump_dir / "categories.dump"
    plain = dump_dir / "categories.sql"
    try:
        subprocess.run(
            ["pg_dump", "--no-owner", "--no-acl", "-Fc", "-f", str(custom), libpq],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["pg_dump", "--no-owner", "--no-acl", "-f", str(plain), libpq],
            check=True,
            capture_output=True,
            text=True,
        )

        def restore_custom(dest: str) -> None:
            subprocess.run(
                ["pg_restore", "--no-owner", "--no-acl", "-d", dest, str(custom)],
                check=True,
                capture_output=True,
                text=True,
            )

        def restore_plain(dest: str) -> None:
            subprocess.run(
                ["psql", "-v", "ON_ERROR_STOP=1", "-d", dest, "-f", str(plain)],
                check=True,
                capture_output=True,
                text=True,
            )

        for kind, dest_name, restore in (
            ("custom", "name_key_restore_custom_test", restore_custom),
            ("plain", "name_key_restore_plain_test", restore_plain),
        ):
            dest = RedactedDsn(
                parsed.set(database=dest_name).render_as_string(hide_password=False)
            )
            drop_database_if_exists(dest)
            ensure_database_exists(dest)
            dest_libpq = parsed.set(
                drivername="postgresql", database=dest_name
            ).render_as_string(hide_password=False)
            restore(dest_libpq)

            async def restored(url: str) -> tuple[object, object]:
                engine = create_async_engine(url)
                try:
                    async with engine.connect() as connection:
                        name = await connection.scalar(
                            text("SELECT name FROM categories WHERE id = :id"),
                            {"id": row_id},
                        )
                        key = await connection.scalar(
                            text("SELECT name_key FROM categories WHERE id = :id"),
                            {"id": row_id},
                        )
                        return name, key
                finally:
                    await engine.dispose()

            name, key = asyncio.run(restored(dest.secret()))
            assert name == marker, kind
            assert key == category_name_key(marker), kind
            drop_database_if_exists(dest)
    finally:
        shutil.rmtree(dump_dir, ignore_errors=True)
        asyncio.run(_cleanup_ids(dsn.secret(), [uuid.UUID(row_id)]))


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
