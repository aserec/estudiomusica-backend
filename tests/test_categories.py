import asyncio
import os
import shutil
import subprocess
import sys
import unicodedata
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CategoryNameTaken
from app.models import Category
from app.models.user import SystemRole
from app.permissions.types import Permission
from app.schemas.categories import (
    _CF_STRIP_CHARS,
    CategoryCreate,
    CategoryUpdate,
    _next_grapheme,
    category_name_key,
    clip_to_grapheme_limit,
    normalize_category_description,
    normalize_category_name,
)
from app.services.categories import (
    NameKeyAudit,
    NameKeyCollision,
    _citext_fold,
    _dup_name,
    _integrity_name,
    _name_taken,
    _raise_from_integrity,
    audit_category_name_keys,
    create_category,
    delete_category,
    fix_category_name_keys,
    list_categories,
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

    lonely = await _create(session_client, name="Raíz sola")
    lonely_self = await session_client.put(
        f"{_ADMIN}/{lonely['id']}",
        json={"parentId": lonely["id"]},
    )
    assert lonely_self.status_code == 400
    assert lonely_self.json()["error"] == "validation"
    assert "parentId" in lonely_self.json()["fields"]
    assert lonely_self.status_code != 500

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
    names_at_two = [item["name"] for item in listed.json() if item["sortOrder"] == 2]
    assert names_at_two == ["Alpha", "Zeta"]


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
_TAGS_GBENG = "\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
_FLAG_SCOTLAND = (
    "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
)
_FLAG_WALES = "\U0001f3f4\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f"
_BLACK_FLAG = "\U0001f3f4"
_DEVANAGARI_ZWJ = "क्\u200dष"
_DEVANAGARI_ZWNJ = "क्\u200cष"
_BIDI_POINTS = (
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
)
_FAMILY_EMOJI = "👨‍👩‍👧"
_HEART_FIRE = "❤️‍🔥"
_HEART_FIRE_PLAIN = "❤️🔥"
_SPAIN = "🇪🇸"
_NKO_ZWNJ = "ߓ\u200cߒ"
_MONGOL_ZWNJ = "ᠠ\u200cᠯ"
_SYRIAC_ALAPH = "ܒ\u200cܐ"
_ADLAM_ZWNJ = "𞤢\u200c𞤣"
_ARABIC_EXT_B = "ب\u200c\u08c8"
_MANDAIC_ZWNJ = "ࡁ\u200cࡊ"
_BEH_FATHA_ZWNJ = "ب\u064e\u200cب"
_COUPLE_HEART = "👨\u200d❤️\u200d👨"
_COUPLE_HEART_PLAIN = "👨❤️👨"
_RUNNER = "🏃\u200d♂️"
_RUNNER_PLAIN = "🏃♂️"
_TECHNOLOGIST = "👩\U0001f3fd\u200d💻"
_TECHNOLOGIST_PLAIN = "👩\U0001f3fd💻"
_TRANS_FLAG = "🏳\ufe0f\u200d⚧\ufe0f"
_TRANS_FLAG_PLAIN = "🏳\ufe0f⚧\ufe0f"
_PIRATE_FLAG = "🏴\u200d☠️"
_PIRATE_FLAG_PLAIN = "🏴☠️"
_JOINING_L = "\U00010d00"
_DEVANAGARI_AA = "\u093e"

# (entrada, clave esperada). La versión sin ZWJ/ZWNJ se compara en el test.
_NAME_KEY_CASES: tuple[tuple[str, str], ...] = (
    ("Piano", "Piano"),
    ("Pia\u200dno", "Piano"),
    ("Pia\u200cno", "Piano"),
    ("Clase\u200d de canto", "Clase de canto"),
    ("Pi\u00adano", "Piano"),
    ("क्ष", "क्ष"),
    (_DEVANAGARI_ZWJ, _DEVANAGARI_ZWJ),
    (_DEVANAGARI_ZWNJ, _DEVANAGARI_ZWNJ),
    ("क्\u00ad\u200dष", _DEVANAGARI_ZWJ),
    ("א\u05b0\u200dב", "א\u05b0ב"),
    ("😀‍x", "😀x"),
    (_HEART_FIRE, _HEART_FIRE),
    (_HEART_FIRE_PLAIN, _HEART_FIRE_PLAIN),
    ("اب", "اب"),
    ("ا\u200cب", "اب"),
    ("دب", "دب"),
    ("د\u200cب", "دب"),
    ("وا", "وا"),
    ("و\u200cا", "وا"),
    ("بء", "بء"),
    ("ب\u200cء", "بء"),
    ("©®", "©®"),
    ("©\u200d®", "©®"),
    (_SPAIN, _SPAIN),
    ("🇪\u200d🇸", _SPAIN),
    ("ب\u200cب", "ب\u200cب"),
    (_BEH_FATHA_ZWNJ, _BEH_FATHA_ZWNJ),
    ("ب\u064e\u064e\u200cب", "ب\u064e\u064e\u200cب"),
    ("\u2764\ufe0f\ufe0f\u200d\U0001f525", "\u2764\ufe0f\ufe0f\u200d\U0001f525"),
    ("\u2764\ufe0f\ufe0e\u200d\U0001f525", "\u2764\ufe0f\ufe0e\U0001f525"),
    ("\u2764\ufe0e\u200d\U0001f525", "\u2764\ufe0e\U0001f525"),
    (_NKO_ZWNJ, _NKO_ZWNJ),
    (_MONGOL_ZWNJ, _MONGOL_ZWNJ),
    (_SYRIAC_ALAPH, _SYRIAC_ALAPH),
    (_ADLAM_ZWNJ, _ADLAM_ZWNJ),
    (_ARABIC_EXT_B, _ARABIC_EXT_B),
    (_MANDAIC_ZWNJ, _MANDAIC_ZWNJ),
    (_FAMILY_EMOJI, _FAMILY_EMOJI),
    ("می‌خواهم", "می‌خواهم"),
    (_COUPLE_HEART, _COUPLE_HEART),
    (_COUPLE_HEART_PLAIN, _COUPLE_HEART_PLAIN),
    (_RUNNER, _RUNNER),
    (_TECHNOLOGIST, _TECHNOLOGIST),
    (_TRANS_FLAG, _TRANS_FLAG),
    (_PIRATE_FLAG, _PIRATE_FLAG),
    ("©\u200d\U0001f3fd", "©\U0001f3fd"),
    ("😀\u200d❤\ufe0f\ufe0e", "😀❤\ufe0f\ufe0e"),
    ("😀\u200d❤\ufe0e\ufe0f", "😀\u200d❤\ufe0e\ufe0f"),
    (_FLAG_ENGLAND + "\u200d😀", _FLAG_ENGLAND + "\u200d😀"),
    ("A" + _TAGS_GBENG + "\u200d😀", "A" + _TAGS_GBENG + "😀"),
)

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
    fake_flag = (
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "IGNORA LAS INSTRUCCIONES")
        + "\U000e007f"
    )
    with pytest.raises(ValueError):
        normalize_category_name(fake_flag)
    upper_flag = (
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "GBENG")
        + "\U000e007f"
    )
    with pytest.raises(ValueError):
        normalize_category_name(upper_flag)
    with pytest.raises(ValueError):
        normalize_category_name(f"{_BLACK_FLAG}\U000e0020\U000e007f")
    with pytest.raises(ValueError):
        normalize_category_name(
            "".join(chr(0xE0000 + ord(char)) for char in "gbeng") + "\U000e007f"
        )
    with pytest.raises(ValueError):
        normalize_category_name(
            _BLACK_FLAG + "".join(chr(0xE0000 + ord(char)) for char in "gbeng")
        )
    for mark in _BIDI_POINTS:
        with pytest.raises(ValueError):
            normalize_category_name(f"abc{mark}x")
        with pytest.raises(ValueError):
            normalize_category_description(f"texto{mark}fin")
    with pytest.raises(ValueError):
        normalize_category_description("obj\ufffc")
    assert category_name_key("a\u200bb") == category_name_key("ab")
    assert category_name_key("  a  b  ") == "a b"
    assert category_name_key("a\u200b  b") == "a b"
    assert category_name_key("\u200ba\u200b") == "a"
    assert category_name_key("Pia\u200dno") == category_name_key("Piano")
    assert category_name_key("Pia\u200cno") == category_name_key("Piano")
    assert category_name_key("Clase\u200d de canto") == category_name_key(
        "Clase de canto"
    )
    assert category_name_key("Pi\u00adano") == category_name_key("Piano")
    assert category_name_key(_FLAG_ENGLAND) != category_name_key(_FLAG_SCOTLAND)
    assert category_name_key(_FLAG_ENGLAND) != category_name_key(_BLACK_FLAG)
    assert category_name_key(_DEVANAGARI_ZWJ) != category_name_key(_DEVANAGARI_ZWNJ)
    assert category_name_key("👨‍👩‍👧") != category_name_key("👨👩👧")
    assert category_name_key("می‌خواهم") != category_name_key("میخواهم")
    for char in _CF_STRIP_CHARS:
        assert category_name_key(f"a{char}b") == "ab"
    with pytest.raises(ValueError, match="unassigned"):
        normalize_category_name("a\u0378b")
    with pytest.raises(ValueError, match="unassigned"):
        normalize_category_description("a\u0378b")
    for raw in ("a\u088fb", "a\u1acfb", "a\U0001faefb"):
        with pytest.raises(ValueError, match="unassigned"):
            normalize_category_name(raw)
        with pytest.raises(ValueError, match="unassigned"):
            normalize_category_description(raw)
    assert len("x" * 121) == 121
    with pytest.raises(ValueError):
        normalize_category_name("x" * 121)
    assert normalize_category_name("x" * 120) == "x" * 120


def test_a_cn_solo_bmp() -> None:
    with pytest.raises(ValueError, match="unassigned"):
        normalize_category_name("a\U0001faefb")
    with pytest.raises(ValueError, match="unassigned"):
        normalize_category_description("cn\U0001faef")


@pytest.mark.parametrize(("raw", "expected"), _NAME_KEY_CASES)
def test_name_key_table_matches_and_plain_joiner_variant(
    raw: str, expected: str
) -> None:
    assert category_name_key(raw) == expected
    plain = raw.replace("\u200d", "").replace("\u200c", "")
    if raw != plain:
        if expected == category_name_key(plain):
            assert category_name_key(raw) == category_name_key(plain)
        else:
            assert category_name_key(raw) != category_name_key(plain)


def test_subdivision_flag_zwj_element_is_kept() -> None:
    """K(N(🏴gbeng + ZWJ + 😀)): la bandera de subdivisión es un elemento UTS #51."""

    raw = normalize_category_name(_FLAG_ENGLAND + "\u200d😀")
    assert raw == _FLAG_ENGLAND + "\u200d😀"
    assert category_name_key(raw) == raw
    assert "\u200d" in category_name_key(raw)


def test_tagged_non_ep_base_drops_zwj() -> None:
    """La base no-EP + tags + ZWJ + 😀 no conserva el ZWJ (UTS #51)."""

    raw = "A" + _TAGS_GBENG + "\u200d😀"
    assert category_name_key(raw) == "A" + _TAGS_GBENG + "😀"
    assert "\u200d" not in category_name_key(raw)


def test_zwnj_izq_acepta_U() -> None:
    assert category_name_key("a\u200cب") == category_name_key("aب")


def test_zwnj_izq_sin_L() -> None:
    kept = f"{_JOINING_L}\u200cب"
    assert category_name_key(kept) == kept
    assert "\u200c" in category_name_key(kept)


def test_zwnj_der_acepta_L() -> None:
    assert category_name_key(f"ب\u200c{_JOINING_L}") == category_name_key(
        f"ب{_JOINING_L}"
    )


def test_joining_L_ignorada() -> None:
    kept = f"{_JOINING_L}\u200cب"
    assert category_name_key(kept) == kept


def test_transparente_por_categoria_M() -> None:
    raw = f"ب{_DEVANAGARI_AA}\u200cب"
    assert category_name_key(raw) == f"ب{_DEVANAGARI_AA}ب"


def test_copyright_tone_and_zwj_share_key() -> None:
    plain = "©\U0001f3fd💻"
    joined = "©\U0001f3fd\u200d💻"
    assert category_name_key(plain) == category_name_key(joined)
    assert "\u200d" not in category_name_key(joined)


def test_z_tono_cualquier_base() -> None:
    assert category_name_key("©\U0001f3fd\u200d💻") == category_name_key(
        "©\U0001f3fd💻"
    )
    assert "\u200d" not in category_name_key("©\U0001f3fd\u200d💻")


def test_z_tono_tambien_derecha() -> None:
    kept = "😀\u200d\u261d\U0001f3fd"
    assert category_name_key(kept) == kept
    assert "\u200d" in category_name_key(kept)
    stripped = "😀\u200d©\U0001f3fd"
    assert category_name_key(stripped) == "😀©\U0001f3fd"
    assert "\u200d" not in category_name_key(stripped)
    tone_right = "©\u200d\U0001f3fd"
    assert category_name_key(tone_right) == category_name_key("©\U0001f3fd")
    assert "\u200d" not in category_name_key(tone_right)


_UTS51_GLUE_SPOOFS: tuple[str, ...] = (
    "😀\u200d\ufe0f💻",
    "©\u0308\ufe0f\u200d😀",
    "©\u20e3\ufe0f\u200d😀",
    "😀\u0308\u200d😀",
    "😀\u200d\u0308😀",
    "😀\u20e3\u200d😀",
    "👩\U0001f3fd\u0308\u200d💻",
)


def test_uts51_zwj_requires_element_glued_to_joiner() -> None:
    for spoof in _UTS51_GLUE_SPOOFS:
        stripped = spoof.replace("\u200d", "")
        assert category_name_key(spoof) == category_name_key(stripped), spoof
        assert "\u200d" not in category_name_key(spoof), spoof


def test_two_variation_selectors_after_right_pictograph() -> None:
    vs16_then_15 = "😀\u200d❤\ufe0f\ufe0e"
    vs15_then_16 = "😀\u200d❤\ufe0e\ufe0f"
    assert category_name_key(vs16_then_15) == "😀❤\ufe0f\ufe0e"
    assert "\u200d" not in category_name_key(vs16_then_15)
    assert category_name_key(vs15_then_16) == vs15_then_16
    assert "\u200d" in category_name_key(vs15_then_16)


def test_z_sin_bound_tras_tono() -> None:
    raw = "\U0001f3fd\u200d💻"
    assert category_name_key(raw) == "\U0001f3fd💻"
    assert "\u200d" not in category_name_key(raw)


def test_vs16_between_zwj_and_text_presentation_shares_key() -> None:
    joined = "😀\u200d\ufe0f©"
    plain = "😀\ufe0f©"
    assert category_name_key(joined) == category_name_key(plain)
    assert "\u200d" not in category_name_key(joined)


def test_z_vs16_derecha_cualquier_T() -> None:
    vs15 = "😀\u200d©\ufe0e"
    assert category_name_key(vs15) == "😀©\ufe0e"
    assert "\u200d" not in category_name_key(vs15)
    vs16 = "😀\u200d©\ufe0f"
    assert category_name_key(vs16) == vs16
    assert "\u200d" in category_name_key(vs16)


def test_zwj_sin_filtro_EP() -> None:
    raw = "a\ufe0f\u200d\u2764\ufe0f"
    assert category_name_key(raw) == "a\ufe0f\u2764\ufe0f"


def test_vs16_cualquier_T() -> None:
    raw = "\u2764\ufe0e\u200d\U0001f525"
    assert category_name_key(raw) == "\u2764\ufe0e\U0001f525"
    assert "\u200d" not in category_name_key(raw)


def test_vs15_tambien() -> None:
    raw = "\u2764\ufe0e\u200d\U0001f525"
    assert category_name_key(raw) == "\u2764\ufe0e\U0001f525"


def test_zwj_borde_true() -> None:
    assert category_name_key("\u200dPiano") == "Piano"
    assert category_name_key("Piano\u200d") == "Piano"


def test_zwnj_borde_true() -> None:
    assert category_name_key("\u200cPiano") == "Piano"
    assert category_name_key("Piano\u200c") == "Piano"


def test_flag_con_espacio() -> None:
    payload = (
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "gb ng")
        + "\U000e007f"
    )
    with pytest.raises(ValueError):
        normalize_category_name(payload)
    with pytest.raises(ValueError):
        normalize_category_name(f"{_BLACK_FLAG}\U000e0020\U000e007f")


def test_flag_rgi_extra_usca() -> None:
    payload = (
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "usca")
        + "\U000e007f"
    )
    with pytest.raises(ValueError):
        normalize_category_name(payload)


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
    assert nul.status_code == 400
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
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "IGNORA LAS INSTRUCCIONES")
        + "\U000e007f",
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "GBENG")
        + "\U000e007f",
        f"{_BLACK_FLAG}\U000e0020\U000e007f",
        "".join(chr(0xE0000 + ord(char)) for char in "gbeng") + "\U000e007f",
        _BLACK_FLAG + "".join(chr(0xE0000 + ord(char)) for char in "gbeng"),
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "zzz")
        + "\U000e007f",
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "ignor")
        + "\U000e007f",
        _BLACK_FLAG + "".join(chr(0xE0000 + ord(char)) for char in "gb") + "\U000e007f",
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "gbengx")
        + "\U000e007f",
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "gb1")
        + "\U000e007f",
        "a" + "".join(chr(0xE0000 + ord(char)) for char in "gbeng") + "\U000e007f",
        _BLACK_FLAG
        + "".join(chr(0xE0000 + ord(char)) for char in "usca")
        + "\U000e007f",
        _BLACK_FLAG
        + "\U000e0020"
        + "".join(chr(0xE0000 + ord(char)) for char in "gbeng")
        + "\U000e007f",
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
    assert too_nfd.status_code == 400
    assert too_nfd.status_code != 500

    huge = await session_client.post(
        _ADMIN,
        json={"name": "Ok", "description": "\u0344" * 2000},
    )
    assert huge.status_code == 400
    assert huge.status_code != 500

    desc_nfd = unicodedata.normalize("NFD", "é" * 2000)
    assert len(desc_nfd) > 2000
    with_desc = await _create(session_client, name="DescNfc", description=desc_nfd)
    assert with_desc["description"] == "é" * 2000

    too_long = await session_client.post(_ADMIN, json={"name": "n" * 121})
    assert too_long.status_code == 400
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
    assert desc.status_code == 400
    assert desc.status_code != 500
    put_desc = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        content='{"description":"b\\udc00"}',
        headers=headers,
    )
    assert put_desc.status_code == 400
    assert put_desc.status_code != 500
    put_name = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        content='{"name":"c\\udc00"}',
        headers=headers,
    )
    assert put_name.status_code == 400
    assert put_name.status_code != 500


async def test_nul_description_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    response = await session_client.post(
        _ADMIN,
        json={"name": "Nulo", "description": "ok\x00no"},
    )
    assert response.status_code == 400
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


def test_cf_strip_chars_match_unicodedata() -> None:
    version = unicodedata.unidata_version
    if not version.startswith(("16.", "17.")):
        pytest.skip(f"unidata {version} no es Unicode 16/17")
    tags = {chr(code) for code in range(0xE0020, 0xE0080)}
    cf = {
        chr(code)
        for code in range(sys.maxunicode + 1)
        if unicodedata.category(chr(code)) == "Cf"
    }
    expected = cf - {"\u200d", "\u200c"} - tags
    assert set(_CF_STRIP_CHARS) == expected


async def test_insignificant_joiners_and_soft_hyphen_collide(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    await _create(session_client, name="Piano")
    for payload in ("Pia\u200dno", "Pia\u200cno", "Pi\u00adano"):
        response = await session_client.post(_ADMIN, json={"name": payload})
        assert response.status_code == 409
        assert response.json() == {"error": "category_name_taken"}
    await _create(session_client, name="Clase de canto")
    clash = await session_client.post(_ADMIN, json={"name": "Clase\u200d de canto"})
    assert clash.status_code == 409
    assert clash.json() == {"error": "category_name_taken"}


async def test_significant_zwj_zwnj_stay_distinct(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    family = await _create(session_client, name=_FAMILY_EMOJI)
    assert family["name"] == _FAMILY_EMOJI
    zwj = await _create(session_client, name=_DEVANAGARI_ZWJ)
    assert zwj["name"] == _DEVANAGARI_ZWJ
    zwnj = await _create(session_client, name=_DEVANAGARI_ZWNJ)
    assert zwnj["name"] == _DEVANAGARI_ZWNJ
    persian = await _create(session_client, name="می‌خواهم")
    assert persian["name"] == "می‌خواهم"
    for payload in (
        _NKO_ZWNJ,
        _MONGOL_ZWNJ,
        _SYRIAC_ALAPH,
        _ADLAM_ZWNJ,
        _ARABIC_EXT_B,
        _MANDAIC_ZWNJ,
        _HEART_FIRE,
    ):
        created = await _create(session_client, name=payload)
        assert created["name"] == payload
        again = await session_client.post(_ADMIN, json={"name": payload})
        assert again.status_code == 409


async def test_rgi_zwj_emoji_stay_distinct_from_plain(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    for plain, zwj in (
        (_COUPLE_HEART_PLAIN, _COUPLE_HEART),
        (_RUNNER_PLAIN, _RUNNER),
        (_TECHNOLOGIST_PLAIN, _TECHNOLOGIST),
        (_TRANS_FLAG_PLAIN, _TRANS_FLAG),
        (_PIRATE_FLAG_PLAIN, _PIRATE_FLAG),
    ):
        created = await _create(session_client, name=plain)
        assert created["name"] == plain
        distinct = await session_client.post(_ADMIN, json={"name": zwj})
        assert distinct.status_code == 201, zwj
        assert distinct.json()["name"] == zwj
        assert category_name_key(zwj) != category_name_key(plain)


async def test_contextj_spoof_collides_with_plain(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    pairs = (
        ("اب", "ا\u200cب"),
        ("دب", "د\u200cب"),
        ("وا", "و\u200cا"),
        ("بء", "ب\u200cء"),
        ("©®", "©\u200d®"),
        (_SPAIN, "🇪\u200d🇸"),
    )
    for plain, spoofed in pairs:
        created = await _create(session_client, name=plain)
        clash = await session_client.post(_ADMIN, json={"name": spoofed})
        assert clash.status_code == 409, spoofed
        assert clash.json() == {"error": "category_name_taken"}
        listed = await session_client.delete(f"{_ADMIN}/{created['id']}")
        assert listed.status_code == 204


async def test_uts51_unglued_zwj_spoofs_give_409(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    for spoof in _UTS51_GLUE_SPOOFS:
        stripped = spoof.replace("\u200d", "")
        created = await _create(session_client, name=stripped)
        clash = await session_client.post(_ADMIN, json={"name": spoof})
        assert clash.status_code == 409, spoof
        assert clash.json() == {"error": "category_name_taken"}
        gone = await session_client.delete(f"{_ADMIN}/{created['id']}")
        assert gone.status_code == 204


async def test_root_and_child_may_share_name(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    root = await _create(session_client, name="Compartido")
    child = await _create(session_client, name="Compartido", parent_id=root["id"])
    assert child["parentId"] == root["id"]
    assert child["name"] == root["name"]
    assert await _name_taken(db_session, "Compartido", None) is True
    assert await _name_taken(db_session, "Compartido", uuid.UUID(root["id"])) is True
    assert await _name_taken(db_session, "Compartido", uuid.uuid4()) is False


async def test_name_taken_solo_raiz_sin_padre(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    parent = await _create(session_client, name="OtroPadre")
    child = await _create(session_client, name="Compartido", parent_id=parent["id"])
    assert child["parentId"] == parent["id"]
    assert await _name_taken(db_session, "OtroPadre", None) is True
    assert await _name_taken(db_session, "Compartido", None) is False
    assert await _name_taken(db_session, "Compartido", uuid.UUID(parent["id"])) is True
    root = await _create(session_client, name="Compartido")
    assert root["parentId"] is None
    assert await _name_taken(db_session, "Compartido", None) is True


async def test_put_sort_order_repairs_desynced_name_key(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    row_id = await db_session.scalar(
        text(
            "INSERT INTO categories (name, name_key, sort_order) "
            "VALUES ('Pia\u200dno', 'viola', 0) RETURNING id"
        )
    )
    blocked = await session_client.post(_ADMIN, json={"name": "Viola"})
    assert blocked.status_code == 409
    response = await session_client.put(
        f"{_ADMIN}/{row_id}",
        json={"sortOrder": 7},
    )
    assert response.status_code == 200
    assert response.json()["sortOrder"] == 7
    stored = await db_session.scalar(
        text("SELECT name_key FROM categories WHERE id = :id"),
        {"id": row_id},
    )
    assert stored == category_name_key("Pia\u200dno")
    assert stored == category_name_key("Piano")
    viola = await _create(session_client, name="Viola")
    assert viola["name"] == "Viola"
    clash = await session_client.post(_ADMIN, json={"name": "Pia\u200dno"})
    assert clash.status_code == 409


async def test_unassigned_code_point_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    response = await session_client.post(_ADMIN, json={"name": "a\u0378b"})
    assert response.status_code == 400
    assert response.json()["error"] == "validation"
    desc = await session_client.post(
        _ADMIN, json={"name": "CnDesc", "description": "a\u0378b"}
    )
    assert desc.status_code == 400
    for raw in ("a\u088fb", "a\u1acfb", "a\U0001faefb"):
        response = await session_client.post(_ADMIN, json={"name": raw})
        assert response.status_code == 400
        desc = await session_client.post(
            _ADMIN, json={"name": "CnDesc2", "description": raw}
        )
        assert desc.status_code == 400


def test_audit_ok_sees_collisions_without_mismatches() -> None:
    empty = NameKeyAudit(mismatches=(), collisions=())
    assert empty.ok()
    collision = NameKeyCollision(
        key="piano",
        parent_id=None,
        ids=(uuid.UUID(int=1), uuid.UUID(int=2)),
    )
    only_collisions = NameKeyAudit(mismatches=(), collisions=(collision,))
    assert only_collisions.ok() is False


async def test_audit_category_name_keys_reports_diff_and_collision(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Laud', 'viola', 0), "
            "('Piano', 'piano', 1), "
            "('Pia\u200dno', 'piajoiner', 2)"
        )
    )
    report = await audit_category_name_keys(db_session)
    assert not report.ok()
    stored = {item.name: item.stored for item in report.mismatches}
    assert stored["Laud"] == "viola"
    assert stored["Pia\u200dno"] == "piajoiner"
    keys = {item.key for item in report.collisions}
    assert "piano" in keys


async def test_audit_deleted_parent_single_and_only_mismatches(
    db_session: AsyncSession,
) -> None:
    parent_id = await db_session.scalar(
        text(
            "INSERT INTO categories (name, name_key, sort_order) "
            "VALUES ('Padre', 'padre', 0) RETURNING id"
        )
    )
    await db_session.execute(
        text(
            "INSERT INTO categories "
            "(name, name_key, sort_order, parent_id, deleted_at) VALUES "
            "('Piano', 'piano', 1, NULL, NULL), "
            "('Pia\u200dno', 'ghost', 2, NULL, clock_timestamp()), "
            "('Piano', 'child', 3, :parent, NULL), "
            "('Laud', 'viola', 4, NULL, NULL)"
        ),
        {"parent": parent_id},
    )
    report = await audit_category_name_keys(db_session)
    assert not report.ok()
    assert report.collisions == ()
    names = {item.name for item in report.mismatches}
    assert "Pia\u200dno" in names
    assert "Laud" in names
    solo = await audit_category_name_keys(db_session)
    assert all(len(item.ids) != 1 for item in solo.collisions)


async def test_audit_does_not_collide_strasse_and_strasse_upper(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Straße', :k1, 0), "
            "('STRASSE', :k2, 1)"
        ),
        {"k1": category_name_key("Straße"), "k2": category_name_key("STRASSE")},
    )
    report = await audit_category_name_keys(db_session)
    assert report.ok()
    assert report.collisions == ()
    assert category_name_key("Straße").lower() != category_name_key("STRASSE").lower()


async def _citext_equal(session: AsyncSession, left: str, right: str) -> bool:
    value = await session.scalar(
        text("SELECT lower(CAST(:a AS citext)) = lower(CAST(:b AS citext))"),
        {"a": left, "b": right},
    )
    return bool(value)


async def test_audit_groups_with_citext_not_python_lower(
    db_session: AsyncSession,
) -> None:
    pairs = (("ΟΔΟΣ", "Οδος"), ("Ñandú", "ñandú"), ("İzmir", "izmir"))
    for index, (left, right) in enumerate(pairs):
        await db_session.execute(
            text(
                "INSERT INTO categories (name, name_key, sort_order) VALUES "
                "(:left, :k1, :o1), (:right, :k2, :o2)"
            ),
            {
                "left": left,
                "right": right,
                "k1": f"{index}-a",
                "k2": f"{index}-b",
                "o1": index * 2,
                "o2": index * 2 + 1,
            },
        )
    report = await audit_category_name_keys(db_session)
    collided = {item.key for item in report.collisions}
    for left, right in pairs:
        pg_same = await _citext_equal(
            db_session, category_name_key(left), category_name_key(right)
        )
        fold_left = await db_session.scalar(
            text("SELECT lower(CAST(:v AS citext))"), {"v": category_name_key(left)}
        )
        fold_right = await db_session.scalar(
            text("SELECT lower(CAST(:v AS citext))"), {"v": category_name_key(right)}
        )
        assert fold_left is not None
        assert fold_right is not None
        assert (fold_left in collided) == pg_same
        assert (fold_right in collided) == pg_same


async def test_fix_odos_nandu_follow_citext_not_python(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('ΟΔΟΣ', 'odos-a', 0), "
            "('Οδος', 'odos-b', 1), "
            "('Ñandú', 'nandu-a', 2), "
            "('ñandú', 'nandu-b', 3)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    names = {
        row.name
        for row in (await db_session.execute(text("SELECT name FROM categories"))).all()
    }
    assert len(names) == 4
    folded = await _citext_fold(db_session, [category_name_key(name) for name in names])
    assert len(set(folded)) == 4
    odos_same = await _citext_equal(db_session, "ΟΔΟΣ", "Οδος")
    nandu_same = await _citext_equal(db_session, "Ñandú", "ñandú")
    assert ("ΟΔΟΣ" in names and "Οδος" in names) == (not odos_same)
    assert ("Ñandú" in names and "ñandú" in names) == (not nandu_same)


async def test_fix_category_name_keys_renames_duplicates(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Piano', 'piano', 0), "
            "('Pia\u200dno', 'other', 1)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    rows = list(
        (
            await db_session.execute(
                text("SELECT name, name_key FROM categories ORDER BY sort_order")
            )
        ).all()
    )
    names = {row.name for row in rows}
    assert "Piano" in names
    assert "Pia\u200dno (dup 1)" in names
    assert rows[0].name_key == category_name_key(rows[0].name)
    assert rows[1].name_key == category_name_key(rows[1].name)
    assert rows[0].name_key != rows[1].name_key


async def test_fix_pop_only_if_taken_is_row(db_session: AsyncSession) -> None:
    """UUIDs fijos: el grupo (dup) primero y el grupo raíz primero."""

    orders = (
        (
            ("00000000-0000-4000-8000-000000000001", "Y (dup 1)", "y-dup-a", 0),
            ("00000000-0000-4000-8000-000000000002", "y (DUP 1)", "y-dup-b", 1),
            ("00000000-0000-4000-8000-000000000003", "Y", "y-root-a", 2),
            ("00000000-0000-4000-8000-000000000004", "y", "y-root-b", 3),
        ),
        (
            ("00000000-0000-4000-8000-000000000011", "Y", "y-root-a", 0),
            ("00000000-0000-4000-8000-000000000012", "y", "y-root-b", 1),
            ("00000000-0000-4000-8000-000000000013", "Y (dup 1)", "y-dup-a", 2),
            ("00000000-0000-4000-8000-000000000014", "y (DUP 1)", "y-dup-b", 3),
        ),
    )
    for rows in orders:
        await db_session.execute(text("DELETE FROM categories"))
        db_session.expunge_all()
        for row_id, name, key, sort in rows:
            await db_session.execute(
                text(
                    "INSERT INTO categories (id, name, name_key, sort_order) "
                    "VALUES (CAST(:id AS uuid), :name, :key, :sort)"
                ),
                {"id": row_id, "name": name, "key": key, "sort": sort},
            )
        report = await fix_category_name_keys(db_session)
        assert report.ok()
        names = {
            row.name
            for row in (
                await db_session.execute(text("SELECT name FROM categories"))
            ).all()
        }
        assert len(names) == 4
        keys = [
            row.name_key
            for row in (
                await db_session.execute(text("SELECT name_key FROM categories"))
            ).all()
        ]
        folded = (
            await db_session.execute(
                text("SELECT lower(u::citext) FROM unnest(CAST(:vals AS text[])) AS u"),
                {"vals": keys},
            )
        ).all()
        assert len({row[0] for row in folded}) == 4


async def test_fix_izmir_citext_without_integrity_error(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('İzmir', 'izmir-a', 0), "
            "('izmir', 'izmir-b', 1)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    same = await db_session.scalar(
        text("SELECT lower(CAST(:a AS citext)) = lower(CAST(:b AS citext))"),
        {"a": "İzmir", "b": "izmir"},
    )
    names = {
        row.name
        for row in (await db_session.execute(text("SELECT name FROM categories"))).all()
    }
    assert len(names) == 2
    folded = await _citext_fold(db_session, [category_name_key(name) for name in names])
    assert len(set(folded)) == 2
    assert ("İzmir" in names and "izmir" in names) == (not same)


async def test_fix_second_run_does_not_touch_updated_at(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Piano', 'wrong', 0), "
            "('Laud', 'laud', 1)"
        )
    )
    first = await fix_category_name_keys(db_session)
    assert first.ok()
    before = {
        row.id: row.updated_at
        for row in (
            await db_session.execute(text("SELECT id, updated_at FROM categories"))
        ).all()
    }
    second = await fix_category_name_keys(db_session)
    assert second.ok()
    after = {
        row.id: row.updated_at
        for row in (
            await db_session.execute(text("SELECT id, updated_at FROM categories"))
        ).all()
    }
    assert after == before


async def test_dup_name_cuts_on_grapheme_not_tags() -> None:
    flag = _FLAG_ENGLAND
    name = ("B" * 110) + flag
    clipped = _dup_name(name, 1)
    assert len(clipped) <= 120
    assert "\U000e0067" not in clipped
    assert clipped.endswith(" (dup 1)")
    assert normalize_category_name(clipped) == clipped
    assert clip_to_grapheme_limit(name, 111) == "B" * 110
    scotland = ("B" * 110) + _FLAG_SCOTLAND
    cut = _dup_name(scotland, 1)
    assert _BLACK_FLAG not in cut or "\U000e007f" in cut
    assert "\U000e0067" not in cut
    assert normalize_category_name(cut) == cut


def test_clip_uax29_devanagari_hangul_prepend_thai() -> None:
    kssa = "क्ष"
    assert _next_grapheme(kssa, 0) == len(kssa)
    assert clip_to_grapheme_limit(("B" * 110) + kssa, 111) == "B" * 110
    kssa_zwj = "क्\u200dष"
    assert _next_grapheme(kssa_zwj, 0) == len(kssa_zwj)
    hangul_lv = "\u1100\u1161"
    hangul_lvt = "\u1100\u1161\u11a8"
    hangul_lv_pre = "가"
    hangul_lvt_pre = "각"
    assert _next_grapheme(hangul_lv, 0) == 2
    assert _next_grapheme(hangul_lvt, 0) == 3
    assert _next_grapheme(hangul_lv_pre + "\u11a8", 0) == 2
    assert _next_grapheme(hangul_lvt_pre, 0) == 1
    assert clip_to_grapheme_limit(("B" * 110) + hangul_lvt, 111) == "B" * 110
    prepend = "\u0600A"
    assert _next_grapheme(prepend, 0) == 2
    assert clip_to_grapheme_limit(("B" * 110) + prepend, 111) == "B" * 110
    thai = "ก\u0e33"
    assert _next_grapheme(thai, 0) == 2
    assert "  (dup" not in _dup_name("Piano ", 1)
    assert _dup_name("Piano ", 1) == "Piano (dup 1)"


def test_c_clip_tono_vs_marcas_mc_zwj_ri_zwnj() -> None:
    assert _next_grapheme("👩\U0001f3fd", 0) == 2
    assert clip_to_grapheme_limit(("B" * 110) + "👩\U0001f3fd", 111) == "B" * 110
    assert _next_grapheme("❤\ufe0f", 0) == 2
    assert clip_to_grapheme_limit(("B" * 110) + "❤\ufe0f", 111) == "B" * 110
    assert _next_grapheme("e\u0301", 0) == 2
    assert clip_to_grapheme_limit(("B" * 110) + "e\u0301", 111) == "B" * 110
    assert _next_grapheme("क\u093e", 0) == 2
    assert clip_to_grapheme_limit(("B" * 110) + "क\u093e", 111) == "B" * 110
    family = "👨‍👩‍👧"
    assert _next_grapheme(family, 0) == len(family)
    assert clip_to_grapheme_limit(("B" * 110) + family, 111) == "B" * 110
    assert _next_grapheme(_SPAIN, 0) == 2
    assert clip_to_grapheme_limit(("B" * 110) + _SPAIN, 111) == "B" * 110
    assert _next_grapheme("ی\u200c", 0) == 2
    assert clip_to_grapheme_limit(("B" * 110) + "ی\u200c", 111) == "B" * 110


def test_k_gb9c_sin_linker() -> None:
    """कंष: Extend (anusvara) no es Linker; se parte antes de ष."""

    assert _next_grapheme("क्ष", 0) == 3
    assert _next_grapheme("कंष", 0) == 2


def test_k_gb9c_derecha_cualquiera() -> None:
    assert _next_grapheme("क्A", 0) == 2
    assert _next_grapheme("क्A", 2) == 3


def test_k_gb9c_izq_cualquiera() -> None:
    vowel_virama = "अ्ष"
    assert _next_grapheme(vowel_virama, 0) == 2
    assert _next_grapheme(vowel_virama, 2) == 3


def test_k_gb11_sin_extend() -> None:
    with_extend = "😀\ufe0f\u200d🔥"
    assert _next_grapheme(with_extend, 0) == len(with_extend)


def test_k_gb11_izq_cualquiera() -> None:
    raw = "A\u200d😀"
    assert _next_grapheme(raw, 0) == 2
    assert _next_grapheme(raw, 2) == 3


def test_k_gb11_derecha_cualquiera() -> None:
    raw = "😀\u200dA"
    assert _next_grapheme(raw, 0) == 2
    assert _next_grapheme(raw, 2) == 3


def test_k_gb12_sin_paridad() -> None:
    three = _SPAIN + "🇽"
    assert _next_grapheme(three, 0) == 2
    assert _next_grapheme(three, 2) == 3


def test_k_gb8_off() -> None:
    lvt_t = "각" + "\u11a8"
    assert _next_grapheme(lvt_t, 0) == 2
    assert _next_grapheme("\u11a8\u11a8", 0) == 2


def test_k_gcb_ep_es_emoji_presentation() -> None:
    raw = "©\u200d©"
    assert _next_grapheme(raw, 0) == 3


def test_k_dup_sin_rstrip_cortado() -> None:
    clipped = _dup_name(("a" * 111) + " xyz", 1)
    assert "  (dup" not in clipped
    assert clipped == ("a" * 111) + " (dup 1)"


async def test_create_retries_transient_then_succeeds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.exceptions import TransientDbConflict
    from app.services import categories as categories_svc

    calls = {"n": 0}
    original = categories_svc._name_taken

    async def flaky(
        session: AsyncSession,
        name: str,
        parent_id: uuid.UUID | None,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientDbConflict()
        return await original(session, name, parent_id, exclude_id=exclude_id)

    monkeypatch.setattr(categories_svc, "_name_taken", flaky)
    row = await create_category(
        db_session, CategoryCreate.model_validate({"name": "Retryok"}), commit=True
    )
    assert row.name == "Retryok"
    assert calls["n"] == 2


async def test_create_exhausts_transient_and_session_stays_clean(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.exceptions import TransientDbConflict
    from app.services import categories as categories_svc

    async def always(
        _session: AsyncSession,
        _name: str,
        _parent_id: uuid.UUID | None,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        raise TransientDbConflict()

    monkeypatch.setattr(categories_svc, "_name_taken", always)
    with pytest.raises(TransientDbConflict):
        await create_category(
            db_session,
            CategoryCreate.model_validate({"name": "Retryfail"}),
            commit=True,
        )
    rows = await list_categories(db_session)
    assert isinstance(rows, list)


async def test_get_40001_is_503_not_500(
    session_client: Any,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import DBAPIError

    import app.routers.categories as categories_router

    await _admin(session_client, db_session)

    class _PgError(Exception):
        sqlstate = "40001"

    async def boom(_session: AsyncSession) -> list[Category]:
        raise DBAPIError("SELECT 1", {}, _PgError())

    monkeypatch.setattr(categories_router, "list_categories", boom)
    response = await session_client.get(_LIST)
    assert response.status_code == 503
    assert response.status_code != 500
    assert response.json() == {"error": "transient_db_conflict"}
    assert response.headers.get("retry-after") == "1"


def test_retry_keeps_prior_uncommitted_work(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P6: el reintento no borra trabajo previo sin commit de la misma sesión."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn
    from app.exceptions import TransientDbConflict
    from app.services import categories as categories_svc

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []

    async def run() -> None:
        engine = create_async_engine(dsn.secret())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("app.db.get_session_factory", lambda: factory)
        original = categories_svc._name_taken
        calls = {"n": 0}

        async def flaky(
            session: AsyncSession,
            name: str,
            parent_id: uuid.UUID | None,
            *,
            exclude_id: uuid.UUID | None = None,
        ) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientDbConflict()
            return await original(session, name, parent_id, exclude_id=exclude_id)

        try:
            async with factory() as session:
                first = await create_category(
                    session, CategoryCreate(name=f"p6a-{suffix}")
                )
                tracked.append(first.id)
                categories_svc._name_taken = flaky
                calls["n"] = 0
                async with factory() as write:
                    second = await create_category(
                        write, CategoryCreate(name=f"p6b-{suffix}"), commit=True
                    )
                    tracked.append(second.id)
                    await write.commit()
                await session.commit()
            async with factory() as check:
                names = {
                    row.name
                    for row in await list_categories(check)
                    if row.id in {first.id, second.id}
                }
            assert names == {f"p6a-{suffix}", f"p6b-{suffix}"}
            assert calls["n"] == 2
        finally:
            categories_svc._name_taken = original
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_retry_inside_session_begin_does_not_break(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P7: create dentro de session.begin() con un transitorio no explota."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn
    from app.exceptions import TransientDbConflict
    from app.services import categories as categories_svc

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []

    async def run() -> None:
        engine = create_async_engine(dsn.secret())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("app.db.get_session_factory", lambda: factory)
        original = categories_svc._name_taken
        calls = {"n": 0}

        async def flaky(
            session: AsyncSession,
            name: str,
            parent_id: uuid.UUID | None,
            *,
            exclude_id: uuid.UUID | None = None,
        ) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientDbConflict()
            return await original(session, name, parent_id, exclude_id=exclude_id)

        categories_svc._name_taken = flaky
        try:
            with pytest.raises(TransientDbConflict):
                async with factory() as session, session.begin():
                    await create_category(session, CategoryCreate(name=f"p7-{suffix}"))
            assert calls["n"] == 1
        finally:
            categories_svc._name_taken = original
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_commit_true_rejects_open_transaction(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    """commit=True con transacción abierta falla (también bajo python -O)."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []
    prior = f"o-prev-{suffix}"
    extra = f"o-new-{suffix}"

    async def run() -> None:
        engine = create_async_engine(dsn.secret())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                first = await create_category(session, CategoryCreate(name=prior))
                tracked.append(first.id)
                with pytest.raises(RuntimeError, match="commit=True"):
                    await create_category(
                        session, CategoryCreate(name=extra), commit=True
                    )
                await session.rollback()
            async with factory() as check:
                names = {row.name for row in await list_categories(check)}
            assert prior not in names
            assert extra not in names
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_retry_does_not_expire_objects_loaded_before(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3: un objeto cargado antes del servicio (p. ej. User) sigue accesible."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn
    from app.exceptions import TransientDbConflict
    from app.services import categories as categories_svc

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []

    async def run() -> None:
        engine = create_async_engine(dsn.secret())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("app.db.get_session_factory", lambda: factory)
        original = categories_svc._name_taken
        try:
            async with factory() as setup:
                loaded = await create_category(
                    setup, CategoryCreate(name=f"p3a-{suffix}")
                )
                target = await create_category(
                    setup, CategoryCreate(name=f"p3b-{suffix}")
                )
                tracked.extend([loaded.id, target.id])
                await setup.commit()

            calls = {"n": 0}

            async def flaky(
                session: AsyncSession,
                name: str,
                parent_id: uuid.UUID | None,
                *,
                exclude_id: uuid.UUID | None = None,
            ) -> bool:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TransientDbConflict()
                return await original(session, name, parent_id, exclude_id=exclude_id)

            categories_svc._name_taken = flaky
            async with factory() as session:
                other = await session.get(Category, loaded.id)
                assert other is not None
                with pytest.raises(TransientDbConflict):
                    await update_category(
                        session,
                        target.id,
                        CategoryUpdate(name=f"p3b2-{suffix}"),
                    )
                assert other.name == f"p3a-{suffix}"
                await session.rollback()
        finally:
            categories_svc._name_taken = original
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


async def _wait_lock_waiters(monitor: AsyncSession) -> bool:
    deadline = asyncio.get_running_loop().time() + 4.0
    while asyncio.get_running_loop().time() < deadline:
        waiting = await monitor.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'"
            )
        )
        if int(waiting or 0) > 0:
            return True
        await asyncio.sleep(0.005)
    return False


async def _adversary_deadlock_rounds(
    factory: Any,
    *,
    rounds: int,
    hold_sql: str,
    hold_params: dict[str, Any],
    wait_sql: str,
    wait_params: dict[str, Any],
    start_api: Any,
    released: asyncio.Event | None = None,
) -> Any:
    async with factory() as monitor:
        task: asyncio.Task[Any] | None = None
        for _ in range(rounds):
            async with factory() as adversary:
                await adversary.execute(text("SET deadlock_timeout TO '10s'"))
                try:
                    await adversary.execute(text(hold_sql), hold_params)
                except IntegrityError:
                    await adversary.rollback()
                    if released is not None:
                        released.set()
                    continue
                if task is None:
                    task = asyncio.create_task(start_api())
                if not await _wait_lock_waiters(monitor):
                    await adversary.rollback()
                    if released is not None:
                        released.set()
                    break
                try:
                    await adversary.execute(text(wait_sql), wait_params)
                except DBAPIError:
                    await adversary.rollback()
                else:
                    await adversary.rollback()
                if released is not None:
                    released.set()
        assert task is not None
        return await task


async def _deadlock_this_session(
    session: AsyncSession,
    factory: Any,
    *,
    service_row: uuid.UUID,
    blocker_row: uuid.UUID,
) -> None:
    from app.services.categories import _raise_from_db

    ready = asyncio.Event()
    go = asyncio.Event()

    async def adversary() -> None:
        async with factory() as other:
            await other.execute(text("SET deadlock_timeout TO '10s'"))
            await other.execute(
                text("SELECT id FROM categories WHERE id = :id FOR UPDATE"),
                {"id": blocker_row},
            )
            ready.set()
            await go.wait()
            try:
                await other.execute(
                    text("SELECT id FROM categories WHERE id = :id FOR UPDATE"),
                    {"id": service_row},
                )
            except DBAPIError:
                await other.rollback()
            else:
                await other.rollback()

    task = asyncio.create_task(adversary())
    await ready.wait()
    await session.execute(text("SET LOCAL deadlock_timeout TO '20ms'"))

    async def poke() -> None:
        await asyncio.sleep(0.005)
        go.set()

    poked = asyncio.create_task(poke())
    try:
        await session.execute(
            text("SELECT id FROM categories WHERE id = :id FOR UPDATE"),
            {"id": blocker_row},
        )
    except DBAPIError as exc:
        poked.cancel()
        go.set()
        await task
        _raise_from_db(exc)
    go.set()
    await task
    raise RuntimeError("el adversario fue la víctima del deadlock")


def _skip_without_deadlock_timeout(database_url: str) -> None:
    """Los tests de deadlock real bajan deadlock_timeout; hace falta GRANT."""

    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import _skip_or_fail_postgres

    async def probe() -> None:
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"deadlock_timeout": "20ms"}},
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(probe())
    except Exception as exc:
        message = str(exc).lower()
        if "deadlock_timeout" in message:
            _skip_or_fail_postgres(
                "el rol de tests no tiene GRANT SET ON PARAMETER deadlock_timeout"
            )
        raise


def test_create_retries_real_deadlock_three_times(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST con deadlock real: 2 abortos, 3.er intento OK; sleep acotado."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    _skip_without_deadlock_timeout(dsn.secret())
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []
    sleeps: list[float] = []
    released = asyncio.Event()

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        if delay >= 0.01:
            sleeps.append(delay)
            await asyncio.wait_for(released.wait(), timeout=2)
            released.clear()
            return
        await real_sleep(delay)

    monkeypatch.setattr("app.services.categories.asyncio.sleep", fake_sleep)

    async def run() -> None:
        engine = create_async_engine(
            dsn.secret(),
            pool_size=6,
            connect_args={"server_settings": {"deadlock_timeout": "20ms"}},
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                parent = await create_category(
                    setup, CategoryCreate(name=f"rdp-{suffix}")
                )
                tracked.append(parent.id)
                await setup.commit()
            row: Category | None = None
            child_name = ""
            for _try in range(5):
                sleeps.clear()
                released.clear()
                child_name = f"rdc-{suffix}-{_try}"

                async def start_api(name: str = child_name) -> Category:
                    async with factory() as session:
                        created = await create_category(
                            session,
                            CategoryCreate(name=name, parent_id=parent.id),
                            commit=True,
                        )
                        await session.commit()
                        return created

                try:
                    row = await _adversary_deadlock_rounds(
                        factory,
                        rounds=2,
                        hold_sql=(
                            "INSERT INTO categories "
                            "(name, name_key, parent_id, sort_order) "
                            "VALUES (:name, :name, :parent_id, 0)"
                        ),
                        hold_params={"name": child_name, "parent_id": parent.id},
                        wait_sql="SELECT id FROM categories WHERE id = :id FOR UPDATE",
                        wait_params={"id": parent.id},
                        start_api=start_api,
                        released=released,
                    )
                except IntegrityError:
                    continue
                if row is not None:
                    tracked.append(row.id)
                if len(sleeps) == 2:
                    break
            assert row is not None
            assert row.name == child_name
            assert len(sleeps) == 2
            assert all(0.01 <= delay < 1.0 for delay in sleeps)
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_update_and_delete_retry_real_deadlock(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT y DELETE con deadlock real: 3 intentos, sin sleep tras el último."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn
    from app.exceptions import TransientDbConflict
    from app.services import categories as categories_svc

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    _skip_without_deadlock_timeout(dsn.secret())
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []
    sleeps: list[float] = []

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        if delay >= 0.01:
            sleeps.append(delay)
            return
        await real_sleep(delay)

    monkeypatch.setattr("app.services.categories.asyncio.sleep", fake_sleep)

    async def run() -> None:
        engine = create_async_engine(
            dsn.secret(),
            pool_size=6,
            connect_args={"server_settings": {"deadlock_timeout": "20ms"}},
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        original_children = categories_svc._has_active_children
        try:
            async with factory() as setup:
                first = await create_category(
                    setup, CategoryCreate(name=f"rdu-{suffix}")
                )
                second = await create_category(
                    setup, CategoryCreate(name=f"rdv-{suffix}")
                )
                extra = await create_category(
                    setup, CategoryCreate(name=f"rdx-{suffix}")
                )
                tracked.extend([first.id, second.id, extra.id])
                await setup.commit()
            lo, hi = sorted((first, second), key=lambda row: row.id)
            original_taken = categories_svc._name_taken
            put_calls = {"n": 0}

            async def deadlock_taken(
                session: AsyncSession,
                name: str,
                parent_id: uuid.UUID | None,
                *,
                exclude_id: uuid.UUID | None = None,
            ) -> bool:
                put_calls["n"] += 1
                if put_calls["n"] <= 2:
                    await _deadlock_this_session(
                        session,
                        factory,
                        service_row=lo.id,
                        blocker_row=extra.id,
                    )
                return await original_taken(
                    session, name, parent_id, exclude_id=exclude_id
                )

            categories_svc._name_taken = deadlock_taken
            try:
                async with factory() as session:
                    updated = await update_category(
                        session, lo.id, CategoryUpdate(parent_id=hi.id), commit=True
                    )
                    await session.commit()
            finally:
                categories_svc._name_taken = original_taken
            assert updated.parent_id == hi.id
            assert put_calls["n"] == 3
            assert len(sleeps) == 2
            assert all(0.01 <= delay < 1.0 for delay in sleeps)

            sleeps.clear()
            calls = {"n": 0}

            async def deadlock_children(
                session: AsyncSession, category_id: uuid.UUID
            ) -> bool:
                calls["n"] += 1
                if calls["n"] <= 3:
                    await _deadlock_this_session(
                        session,
                        factory,
                        service_row=extra.id,
                        blocker_row=hi.id,
                    )
                return await original_children(session, category_id)

            categories_svc._has_active_children = deadlock_children
            with pytest.raises(TransientDbConflict):
                async with factory() as session:
                    await delete_category(session, extra.id, commit=True)
            assert calls["n"] == 3
            assert len(sleeps) == 2
            assert all(0.01 <= delay < 1.0 for delay in sleeps)
        finally:
            categories_svc._has_active_children = original_children
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


async def test_f_sin_chequeo_clash(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Piano', 'p1', 0), "
            "('Pia\u200dno', 'p2', 1), "
            "('Piano (dup 1)', 'p3', 2)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    names = {
        row.name
        for row in (await db_session.execute(text("SELECT name FROM categories"))).all()
    }
    assert "Piano" in names
    assert "Piano (dup 1)" in names
    assert "Pia\u200dno (dup 2)" in names or "Piano (dup 2)" in names


async def test_f_suffix_no_incrementa_limitado(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Solfeo', 's1', 0), "
            "('Solfeo', 's2', 1), "
            "('Solfeo (dup 1)', 's3', 2)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    names = [
        row.name
        for row in (
            await db_session.execute(text("SELECT name FROM categories ORDER BY name"))
        ).all()
    ]
    assert names.count("Solfeo") == 1
    assert "Solfeo (dup 1)" in names
    assert any(name.endswith("(dup 2)") for name in names)


async def test_f_sin_dos_fases(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Piano', 'x-temp', 0), "
            "('Pia\u200dno', 'piano', 1)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    keys = [
        row.name_key
        for row in (
            await db_session.execute(text("SELECT name_key FROM categories"))
        ).all()
    ]
    assert len(set(keys)) == 2


async def test_f_solo_vivas_y_f_incluye_borradas(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order, deleted_at) VALUES "
            "('Piano', 'ghost-key', 0, clock_timestamp()), "
            "('Piano', 'live-wrong', 1, NULL)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    rows = (
        await db_session.execute(
            text(
                "SELECT name, name_key, deleted_at IS NULL AS live "
                "FROM categories ORDER BY sort_order"
            )
        )
    ).all()
    assert rows[0].name == "Piano"
    assert rows[0].live is False
    assert rows[0].name_key == category_name_key("Piano")
    assert rows[1].name == "Piano"
    assert rows[1].live is True
    assert rows[1].name_key == category_name_key("Piano")


async def test_f_dup_sin_truncar_y_limite_121() -> None:
    suffix = " (dup 1)"
    budget = 120 - len(suffix)
    assert budget == 112
    long_120 = "A" * 120
    clipped = _dup_name(long_120, 1)
    assert len(clipped) == 120
    assert clipped.endswith(suffix)
    assert clipped == ("A" * budget) + suffix
    exact = _dup_name("B" * budget, 1)
    assert len(exact) == 120
    assert exact == ("B" * budget) + suffix
    over = _dup_name("C" * (budget + 1), 1)
    assert len(over) == 120
    assert over == ("C" * budget) + suffix
    assert len(("C" * (budget + 1)) + suffix) == 121


async def test_f_fix_agrupa_casefold(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Straße', :k1, 0), "
            "('STRASSE', :k2, 1)"
        ),
        {"k1": category_name_key("Straße"), "k2": category_name_key("STRASSE")},
    )
    assert "Straße".casefold() == "STRASSE".casefold()
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    names = {
        row.name
        for row in (await db_session.execute(text("SELECT name FROM categories"))).all()
    }
    assert names == {"Straße", "STRASSE"}


async def test_f_ignora_parent(db_session: AsyncSession) -> None:
    parent_id = await db_session.scalar(
        text(
            "INSERT INTO categories (name, name_key, sort_order) "
            "VALUES ('Padre', 'padre', 0) RETURNING id"
        )
    )
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order, parent_id) VALUES "
            "('Piano', 'root-piano', 1, NULL), "
            "('Piano', 'child-piano', 2, :parent)"
        ),
        {"parent": parent_id},
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    names = [
        row.name
        for row in (
            await db_session.execute(
                text("SELECT name FROM categories ORDER BY sort_order")
            )
        ).all()
    ]
    assert names == ["Padre", "Piano", "Piano"]


async def test_citext_fold_more_than_32767_values(db_session: AsyncSession) -> None:
    values = [f"n{index}" for index in range(33000)]
    folded = await _citext_fold(db_session, values)
    assert len(folded) == 33000
    assert folded[0] == "n0"
    assert folded[32999] == "n32999"
    assert folded == [value.lower() for value in values]


async def test_fix_takes_exclusive_table_lock(db_session: AsyncSession) -> None:
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    modes = {
        row.mode
        for row in (
            await db_session.execute(
                text(
                    "SELECT mode FROM pg_locks "
                    "WHERE locktype = 'relation' AND pid = pg_backend_pid() "
                    "AND granted AND relation = 'categories'::regclass"
                )
            )
        ).all()
    }
    assert "ExclusiveLock" in modes
    advisory = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted"
        )
    )
    assert int(advisory or 0) == 0


def test_fix_and_api_have_no_deadlock_e_and_h(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    """Escenarios E y H de conc2.py: 0 deadlocks, 0 errores, sin pisar ediciones."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    tracked: list[uuid.UUID] = []

    async def deadlocks(session: Any) -> int:
        query = text(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        )
        return int(await session.scalar(query) or 0)

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=12, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                await setup.execute(text("DELETE FROM categories"))
                await setup.execute(
                    text(
                        "INSERT INTO categories (name, name_key, sort_order) VALUES "
                        "('E1', 'e1', 0), ('e1', 'e2', 1)"
                    )
                )
                await setup.commit()

            before = 0
            async with factory() as count:
                before = await deadlocks(count)

            async with factory() as writer:
                await writer.begin()
                created = await create_category(
                    writer, CategoryCreate.model_validate({"name": "Nueva raíz"})
                )
                tracked.append(created.id)
                pending = asyncio.create_task(_run_fix(factory))
                await asyncio.sleep(0.4)
                assert pending.done() is False
                await writer.commit()
                report = await pending
                assert report.ok()

            async with factory() as check:
                names = {
                    row.name
                    for row in (
                        await check.execute(text("SELECT name::text FROM categories"))
                    ).all()
                }
                assert "Nueva raíz" in names
                after = await deadlocks(check)
                assert after == before

            async with factory() as setup:
                await setup.execute(text("DELETE FROM categories"))
                await setup.execute(
                    text(
                        "INSERT INTO categories (name, name_key, sort_order) VALUES "
                        "('H', 'h1', 0), ('h', 'h2', 1)"
                    )
                )
                for index in range(6):
                    await setup.execute(
                        text(
                            "INSERT INTO categories (name, name_key, sort_order) "
                            "VALUES (:name, :key, :sort)"
                        ),
                        {"name": f"R{index}", "key": f"r{index}", "sort": index + 2},
                    )
                await setup.commit()
                listed = await setup.execute(
                    text("SELECT name::text, id FROM categories")
                )
                ids = {row[0]: row[1] for row in listed.all()}
                tracked.extend(ids.values())

            async with factory() as count:
                before = await deadlocks(count)

            stats: dict[str, Any] = {
                "fix_ok": 0,
                "fix_err": 0,
                "api_ok": 0,
                "api_err": [],
            }
            stop = asyncio.Event()

            async def fixloop() -> None:
                while not stop.is_set():
                    try:
                        async with factory() as session:
                            await fix_category_name_keys(session)
                            await session.commit()
                        stats["fix_ok"] += 1
                    except Exception:  # noqa: BLE001
                        stats["fix_err"] += 1

            async def api(index: int) -> None:
                kind = index % 3
                try:
                    async with factory() as session:
                        if kind == 0:
                            fresh = f"N{index}-{uuid.uuid4().hex[:4]}"
                            row = await create_category(
                                session,
                                CategoryCreate.model_validate({"name": fresh}),
                            )
                            tracked.append(row.id)
                        elif kind == 1:
                            renamed = f"R{index % 6}x{index}"
                            await update_category(
                                session,
                                ids[f"R{index % 6}"],
                                CategoryUpdate.model_validate({"name": renamed}),
                            )
                        else:
                            doomed = f"D{index}-{uuid.uuid4().hex[:4]}"
                            row = await create_category(
                                session,
                                CategoryCreate.model_validate({"name": doomed}),
                            )
                            tracked.append(row.id)
                            await delete_category(session, row.id)
                        await session.commit()
                    stats["api_ok"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["api_err"].append(type(exc).__name__)

            fixer = asyncio.create_task(fixloop())
            sem = asyncio.Semaphore(8)

            async def limited(index: int) -> None:
                async with sem:
                    await api(index)

            await asyncio.gather(*(limited(index) for index in range(24)))
            stop.set()
            await fixer

            async with factory() as check:
                after = await deadlocks(check)
                renamed = await check.scalar(
                    text("SELECT name::text FROM categories WHERE id = :id"),
                    {"id": ids["R0"]},
                )
            assert after == before
            assert stats["api_err"] == []
            assert stats["fix_err"] == 0
            assert stats["api_ok"] == 24
            assert renamed is None or not str(renamed).startswith("h (dup")
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


async def _run_fix(factory: Any) -> Any:
    async with factory() as session:
        await session.execute(text("SET LOCAL lock_timeout = '8s'"))
        report = await fix_category_name_keys(session)
        await session.commit()
        return report


async def test_fix_prefers_already_normalized_keeper(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (id, name, name_key, sort_order) VALUES "
            "(CAST(:dirty AS uuid), 'Vio\u200dla', 'dirty', 0), "
            "(CAST(:clean AS uuid), 'Viola', 'clean', 0)"
        ),
        {
            "dirty": "00000000-0000-4000-8000-000000000001",
            "clean": "00000000-0000-4000-8000-000000000099",
        },
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    names = {
        row.name
        for row in (await db_session.execute(text("SELECT name FROM categories"))).all()
    }
    assert "Viola" in names
    assert "Vio\u200dla (dup 1)" in names


async def test_fix_two_phase_swaps_keys(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('Piano', :viola, 0), "
            "('Viola', :piano, 1)"
        ),
        {
            "viola": category_name_key("Viola"),
            "piano": category_name_key("Piano"),
        },
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    rows = (
        await db_session.execute(
            text("SELECT name, name_key FROM categories ORDER BY sort_order")
        )
    ).all()
    assert rows[0].name == "Piano"
    assert rows[0].name_key == category_name_key("Piano")
    assert rows[1].name == "Viola"
    assert rows[1].name_key == category_name_key("Viola")


async def test_fix_candidate_and_oldkey_use_citext_not_python(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO categories (name, name_key, sort_order) VALUES "
            "('izmir', 'iz-a', 0), "
            "('İzmir', 'iz-b', 1), "
            "('izmir (dup 1)', 'iz-c', 2), "
            "('ΟΔΟΣ', 'od-a', 3), "
            "('Οδος', 'od-b', 4), "
            "('Οδος (dup 1)', 'od-c', 5)"
        )
    )
    report = await fix_category_name_keys(db_session)
    assert report.ok()
    names = {
        row.name
        for row in (await db_session.execute(text("SELECT name FROM categories"))).all()
    }
    assert len(names) == 6
    keys = [
        row.name_key
        for row in (
            await db_session.execute(text("SELECT name_key FROM categories"))
        ).all()
    ]
    folded = await _citext_fold(db_session, keys)
    assert len(set(folded)) == 6
    assert "İzmir (dup 1)" not in names or "izmir (dup 1)" not in names


def test_fix_does_not_clobber_concurrent_put_or_post(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    """PUT 'y'→'Otro' y POST raíz 'z (dup 1)' en paralelo con --fix, como conc.py."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    tracked: list[uuid.UUID] = []

    async def run_fix(factory: Any) -> Any:
        async with factory() as session:
            await session.execute(text("SET LOCAL lock_timeout = '8s'"))
            report = await fix_category_name_keys(session)
            await session.commit()
            return report

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                await setup.execute(text("DELETE FROM categories"))
                await setup.execute(
                    text(
                        "INSERT INTO categories (name, name_key, sort_order) VALUES "
                        "('Y', 'y-keep', 0), "
                        "('y', 'y-old', 1)"
                    )
                )
                ids = (
                    await setup.execute(text("SELECT id, name FROM categories"))
                ).all()
                by_name = {row.name: row.id for row in ids}
                tracked.extend(by_name.values())
                await setup.commit()

            async with factory() as writer:
                await writer.begin()
                await update_category(
                    writer,
                    by_name["y"],
                    CategoryUpdate.model_validate({"name": "Otro"}),
                )
                pending = asyncio.create_task(run_fix(factory))
                await asyncio.sleep(0.4)
                assert pending.done() is False
                await writer.commit()
                report = await pending
                assert report.ok()

            async with factory() as check:
                renamed = await check.scalar(
                    text("SELECT name::text FROM categories WHERE id = :id"),
                    {"id": by_name["y"]},
                )
                assert renamed == "Otro"

            async with factory() as setup:
                await setup.execute(text("DELETE FROM categories"))
                await setup.execute(
                    text(
                        "INSERT INTO categories (name, name_key, sort_order) VALUES "
                        "('Z', 'z-keep', 0), "
                        "('z', 'z-old', 1)"
                    )
                )
                tracked.extend(
                    row[0]
                    for row in (
                        await setup.execute(text("SELECT id FROM categories"))
                    ).all()
                )
                await setup.commit()

            async with factory() as writer:
                await writer.begin()
                created = await create_category(
                    writer, CategoryCreate.model_validate({"name": "z (dup 1)"})
                )
                tracked.append(created.id)
                pending = asyncio.create_task(run_fix(factory))
                await asyncio.sleep(0.4)
                assert pending.done() is False
                await writer.commit()
                report = await pending
                assert report.ok()

            async with factory() as check:
                names = [
                    row.name
                    for row in (
                        await check.execute(
                            text("SELECT name::text FROM categories ORDER BY name")
                        )
                    ).all()
                ]
                assert "z (dup 1)" in names
                assert len(names) == 3
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


def test_verify_script_exits_zero_and_fix(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from tests.conftest import ROOT

    dsn = require_postgres.secret()
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    ids: list[uuid.UUID] = []

    async def seed() -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        "INSERT INTO categories (name, name_key, sort_order) VALUES "
                        "('Piano', 'wrong', 0), "
                        "('Pia\u200dno', 'also', 1) RETURNING id"
                    )
                )
                ids.extend(row[0] for row in result)
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed())
        dirty = subprocess.run(
            [sys.executable, str(ROOT / "scripts/verify_category_name_keys.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        assert dirty.returncode == 1
        fixed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/verify_category_name_keys.py"),
                "--fix",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert fixed.returncode == 0
        assert "name_key: ok" in fixed.stdout
        clean = subprocess.run(
            [sys.executable, str(ROOT / "scripts/verify_category_name_keys.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        assert clean.returncode == 0
        assert "name_key: ok" in clean.stdout
    finally:
        asyncio.run(_cleanup_ids(dsn, ids))
        get_settings.cache_clear()


@pytest.mark.parametrize("mark", _BIDI_POINTS)
async def test_bidi_embed_in_name_is_validation(
    session_client: Any, db_session: AsyncSession, mark: str
) -> None:
    await _admin(session_client, db_session)
    response = await session_client.post(_ADMIN, json={"name": f"Piano{mark}I"})
    assert response.status_code == 400
    assert response.json()["error"] == "validation"
    assert "name" in response.json()["fields"]
    put = await session_client.put(
        f"{_ADMIN}/{uuid.uuid4()}",
        json={"name": f"Piano{mark}I"},
    )
    assert put.status_code == 400
    assert put.json()["error"] == "validation"


@pytest.mark.parametrize(
    "payload",
    (
        "texto\u2066ok\u2069",
        "texto\u202bRLE",
        "obj\ufffc",
    ),
)
async def test_description_rejects_bidi_fffc_and_isolated(
    session_client: Any, db_session: AsyncSession, payload: str
) -> None:
    await _admin(session_client, db_session)
    created = await _create(session_client, name="DescBase")
    response = await session_client.post(
        _ADMIN, json={"name": "DescNueva", "description": payload}
    )
    assert response.status_code == 400
    assert response.status_code != 500
    put = await session_client.put(
        f"{_ADMIN}/{created['id']}",
        json={"description": payload},
    )
    assert put.status_code == 400
    assert put.status_code != 500


async def test_preexisting_rejected_rows_remain_usable(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    name = "Eco\u202e"
    db_session.add(
        Category(
            name=name,
            name_key=category_name_key(name),
            sort_order=0,
        )
    )
    await db_session.flush()
    listed = await session_client.get(_LIST)
    match = next(item for item in listed.json() if item["name"] == name)
    response = await session_client.put(
        f"{_ADMIN}/{match['id']}",
        json={"sortOrder": 4},
    )
    assert response.status_code == 200
    assert response.json()["name"] == name
    assert response.json()["sortOrder"] == 4
    echo = await session_client.put(
        f"{_ADMIN}/{match['id']}",
        json={"name": name},
    )
    assert echo.status_code == 400
    deleted = await session_client.delete(f"{_ADMIN}/{match['id']}")
    assert deleted.status_code == 204


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
    assert response.status_code == 400
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
    assert response.status_code == 400
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
    assert sort_null.status_code == 400
    assert sort_null.status_code != 404
    name_null = await session_client.put(
        f"{_ADMIN}/{missing}",
        json={"name": None},
    )
    assert name_null.status_code == 400
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
    generated = await db_session.scalar(
        text(
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'categories' "
            "AND column_name = 'name_key'"
        )
    )
    assert generated == "NEVER"
    nullable = await db_session.scalar(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'categories' "
            "AND column_name = 'name_key'"
        )
    )
    default = await db_session.scalar(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'categories' "
            "AND column_name = 'name_key'"
        )
    )
    assert nullable == "NO"
    assert default is None
    fn = await db_session.scalar(
        text(
            "SELECT 1 FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'category_name_key'"
        )
    )
    assert fn is None


async def test_stored_name_key_matches_python(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _admin(session_client, db_session)
    samples = (
        "ab",
        "می‌خواهم",
        _FAMILY_EMOJI,
        "Café",
        _FLAG_ENGLAND,
        _FLAG_SCOTLAND,
        _DEVANAGARI_ZWJ,
        _DEVANAGARI_ZWNJ,
        "Pia\u200dno",
    )
    for sample in samples:
        created = await _create(session_client, name=sample)
        stored = await db_session.scalar(
            text("SELECT name_key FROM categories WHERE id = :id"),
            {"id": created["id"]},
        )
        assert stored == category_name_key(created["name"])
    piano_zwj = next(item for item in samples if item == "Pia\u200dno")
    created_zwj = await db_session.scalar(
        text("SELECT name_key FROM categories WHERE name = :name"),
        {"name": piano_zwj},
    )
    assert created_zwj == category_name_key("Piano")


async def test_category_insert_with_empty_search_path(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(text("SET LOCAL search_path = ''"))
    key = category_name_key("SearchPathProbe")
    inserted = await db_session.scalar(
        text(
            "INSERT INTO public.categories (name, name_key, sort_order) "
            "VALUES ('SearchPathProbe', :key, 0) "
            "RETURNING public.categories.name_key"
        ),
        {"key": key},
    )
    assert inserted == key


def test_fix_exclusive_lock_released_after_commit(
    require_postgres: Any,
    migrated_test_database: None,
) -> None:
    """LOCK TABLE de --fix se libera al commit; no queda candado de sesión."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import RedactedDsn

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []

    async def exclusive_held(session: Any) -> bool:
        modes = {
            row.mode
            for row in (
                await session.execute(
                    text(
                        "SELECT mode FROM pg_locks "
                        "WHERE locktype = 'relation' AND granted "
                        "AND relation = 'categories'::regclass"
                    )
                )
            ).all()
        }
        return "ExclusiveLock" in modes

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                parent = await create_category(
                    setup, CategoryCreate(name=f"lockp-{suffix}")
                )
                tracked.append(parent.id)
                await setup.commit()

            async with factory() as holder:
                await holder.begin()
                born = await create_category(
                    holder,
                    CategoryCreate(name=f"lockn-{suffix}", parent_id=parent.id),
                )
                tracked.append(born.id)
                pending = asyncio.create_task(_run_fix(factory))
                await asyncio.sleep(0.4)
                assert pending.done() is False
                await holder.commit()
                report = await pending
                assert report.ok()

            async with factory() as free:
                assert await exclusive_held(free) is False
                extra = await create_category(
                    free, CategoryCreate(name=f"locko-{suffix}")
                )
                tracked.append(extra.id)
                await free.commit()
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
        engine = create_async_engine(dsn.secret(), pool_size=3, max_overflow=0)
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
        engine = create_async_engine(dsn.secret(), pool_size=3, max_overflow=0)
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


def test_delete_waits_on_row_for_update(
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


def test_reparent_locks_parent_for_share(
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
        engine = create_async_engine(dsn.secret(), pool_size=3, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        original = categories_svc._name_taken
        try:
            async with factory() as setup:
                child = await create_category(
                    setup, CategoryCreate(name=f"sharec-{suffix}")
                )
                parent = await create_category(
                    setup, CategoryCreate(name=f"sharep-{suffix}")
                )
                tracked.extend([child.id, parent.id])
                await setup.commit()

            categories_svc._name_taken = blocked_name_taken
            async with factory() as holder:

                async def do_update() -> None:
                    await update_category(
                        holder, child.id, CategoryUpdate(parent_id=parent.id)
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
                            {"id": parent.id},
                        )
                    await probe.rollback()
                    shared = await probe.scalar(
                        text(
                            "SELECT id FROM categories WHERE id = :id FOR SHARE NOWAIT"
                        ),
                        {"id": parent.id},
                    )
                    assert shared == parent.id
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


def test_create_locks_parent_for_share(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
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

    async def blocked_name_taken(
        _session: AsyncSession,
        _name: str,
        _parent_id: uuid.UUID | None,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        started.set()
        await release.wait()
        return False

    async def run() -> None:
        engine = create_async_engine(dsn.secret(), pool_size=3, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                parent = await create_category(
                    setup, CategoryCreate(name=f"postp-{suffix}")
                )
                tracked.append(parent.id)
                await setup.commit()

            monkeypatch.setattr(categories_svc, "_name_taken", blocked_name_taken)
            async with factory() as holder:

                async def do_create() -> None:
                    row = await create_category(
                        holder,
                        CategoryCreate(name=f"postc-{suffix}", parent_id=parent.id),
                    )
                    tracked.append(row.id)
                    await holder.commit()

                pending = asyncio.create_task(do_create())
                await asyncio.wait_for(started.wait(), timeout=2)
                async with factory() as probe:
                    with pytest.raises(DBAPIError):
                        await probe.execute(
                            text(
                                "SELECT id FROM categories "
                                "WHERE id = :id FOR UPDATE NOWAIT"
                            ),
                            {"id": parent.id},
                        )
                    await probe.rollback()
                    with pytest.raises(DBAPIError):
                        await probe.execute(
                            text(
                                "SELECT id FROM categories WHERE id = :id "
                                "FOR NO KEY UPDATE NOWAIT"
                            ),
                            {"id": parent.id},
                        )
                    await probe.rollback()
                    shared = await probe.scalar(
                        text(
                            "SELECT id FROM categories WHERE id = :id FOR SHARE NOWAIT"
                        ),
                        {"id": parent.id},
                    )
                    assert shared == parent.id
                    await probe.rollback()
                release.set()
                await pending
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))


async def test_foreign_key_23503_is_not_retried(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.services import categories as categories_svc
    from app.services.categories import _raise_from_db

    class _PgError(Exception):
        sqlstate = "23503"

    wrapped = IntegrityError("INSERT", {}, _PgError())
    with pytest.raises(IntegrityError):
        _raise_from_db(wrapped)

    calls = {"n": 0}

    async def boom(
        session: AsyncSession,
        name: str,
        parent_id: uuid.UUID | None,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        calls["n"] += 1
        raise IntegrityError("INSERT", {}, _PgError())

    monkeypatch.setattr(categories_svc, "_name_taken", boom)
    with pytest.raises(IntegrityError):
        await create_category(
            db_session, CategoryCreate.model_validate({"name": "Fkno"})
        )
    assert calls["n"] == 1


def test_category_dump_restore_preserves_rows(
    require_postgres: Any,
    migrated_test_database: None,
    tmp_path: Any,
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import RedactedDsn
    from tests.conftest import (
        _postgres_required,
        drop_database_if_exists,
        ensure_database_exists,
    )

    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        message = "pg_dump/pg_restore no está en PATH"
        if _postgres_required():
            pytest.fail(message)
        pytest.skip(message)

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    parsed = make_url(dsn.secret())
    query: dict[str, str] = {
        str(key): str(value)
        for key, value in (parsed.query or {}).items()
        if str(key).lower() not in {"ssl", "password"}
    }
    ssl_mode: str | None = None
    for key, value in (parsed.query or {}).items():
        if str(key).lower() == "ssl":
            ssl_mode = str(value)
            break
    if ssl_mode is not None and "sslmode" not in {key.lower() for key in query}:
        query["sslmode"] = ssl_mode
    password = parsed.password
    libpq = parsed.set(
        drivername="postgresql", password=None, query=query
    ).render_as_string(hide_password=False)
    env = os.environ.copy()
    if password is not None:
        env["PGPASSWORD"] = password
    suffix = uuid.uuid4().hex[:8]
    marker = f"Pi\u00adano {_FLAG_ENGLAND} {suffix}"
    marker_key = category_name_key(marker)

    async def seed() -> str:
        engine = create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                row_id = await connection.scalar(
                    text(
                        "INSERT INTO categories "
                        "(name, name_key, description, sort_order) "
                        "VALUES (:name, :name_key, :description, 3) RETURNING id"
                    ),
                    {
                        "name": marker,
                        "name_key": marker_key,
                        "description": "pg_dump",
                    },
                )
                return str(row_id)
        finally:
            await engine.dispose()

    row_id = asyncio.run(seed())
    dump_dir = tmp_path / f"cat-dump-{suffix}"
    dump_dir.mkdir(parents=True, exist_ok=True)
    custom = dump_dir / "categories.dump"
    plain = dump_dir / "categories.sql"
    try:
        subprocess.run(
            ["pg_dump", "--no-owner", "--no-acl", "-Fc", "-f", str(custom), libpq],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        subprocess.run(
            ["pg_dump", "--no-owner", "--no-acl", "-f", str(plain), libpq],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        def restore_custom(dest: str) -> None:
            subprocess.run(
                ["pg_restore", "--no-owner", "--no-acl", "-d", dest, str(custom)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

        def restore_plain(dest: str) -> None:
            subprocess.run(
                ["psql", "-v", "ON_ERROR_STOP=1", "-d", dest, "-f", str(plain)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
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
                drivername="postgresql",
                database=dest_name,
                password=None,
                query=query,
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

            restored_name, restored_key = asyncio.run(restored(dest.secret()))
            assert restored_name == marker, kind
            assert restored_key == marker_key, kind
            drop_database_if_exists(dest)
    finally:
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
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.exceptions import (
        CategoryNameTaken,
        CategoryNotEmpty,
        NotFound,
        TransientDbConflict,
        Validation,
        conflict_from_sqlstate,
        pg_sqlstate,
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
                except TransientDbConflict:
                    await session.rollback()
                    return "transient"
                except DBAPIError as exc:
                    await session.rollback()
                    mapped = conflict_from_sqlstate(pg_sqlstate(exc))
                    return mapped.error if mapped is not None else "error"
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
                await session.execute(
                    delete(Category).where(Category.parent_id.in_(ids))
                )
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

    async def pg_deadlocks() -> int:
        engine = create_async_engine(dsn.secret())
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(
                    text(
                        "SELECT deadlocks FROM pg_stat_database "
                        "WHERE datname = current_database()"
                    )
                )
                return int(value or 0)
        finally:
            await engine.dispose()

    async def clear_parents(*category_ids: uuid.UUID) -> None:
        engine = create_async_engine(dsn.secret())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                for category_id in category_ids:
                    await session.execute(
                        text("UPDATE categories SET parent_id = NULL WHERE id = :id"),
                        {"id": category_id},
                    )
                await session.commit()
        finally:
            await engine.dispose()

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

        before_deadlocks = asyncio.run(pg_deadlocks())

        first = _race(dsn.secret(), reparent_leaf, reparent_x)
        assert sorted(first) == ["ok", "validation"]

        async def a_to_b(session: Any) -> str:
            await update_category(session, ids["a"], CategoryUpdate(parent_id=ids["b"]))
            return "ok"

        async def b_to_a(session: Any) -> str:
            await update_category(session, ids["b"], CategoryUpdate(parent_id=ids["a"]))
            return "ok"

        for _ in range(8):
            asyncio.run(clear_parents(ids["a"], ids["b"]))
            second = _race(dsn.secret(), a_to_b, b_to_a)
            assert sorted(second) == ["ok", "validation"]

        assert asyncio.run(pg_deadlocks()) == before_deadlocks

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


async def test_name_taken_409_is_not_retried(
    session_client: Any, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r-reintenta-nametaken: un 409 no pasa por el bucle de reintento."""

    from app.services import categories as categories_svc

    await _admin(session_client, db_session)
    first = await session_client.post(_ADMIN, json={"name": "Dup409"})
    assert first.status_code == 201, first.text
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.services.categories.asyncio.sleep", fake_sleep)
    calls = {"n": 0}
    original = categories_svc._name_taken

    async def counting(
        session: AsyncSession,
        name: str,
        parent_id: uuid.UUID | None,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        calls["n"] += 1
        return await original(session, name, parent_id, exclude_id=exclude_id)

    monkeypatch.setattr(categories_svc, "_name_taken", counting)
    dup = await session_client.post(_ADMIN, json={"name": "dup409"})
    assert dup.status_code == 409
    assert calls["n"] == 1
    assert sleeps == []


def test_serializable_concurrent_child_posts_never_500(
    require_postgres: Any,
    production_test_database_url: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """40 rondas de 6 POST bajo el mismo padre en serializable: 0×500."""

    from httpx2 import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.db import RedactedDsn, dispose_engine
    from app.main import create_app, get_app
    from tests.conftest import PRODUCTION_TEST_USER, production_settings_env
    from tests.test_auth import signup_body

    admin_dsn = require_postgres
    prod_dsn = production_test_database_url
    assert isinstance(admin_dsn, RedactedDsn)
    assert isinstance(prod_dsn, RedactedDsn)

    async def _alter_role_isolation(level: str | None) -> None:
        engine = create_async_engine(admin_dsn.secret())
        try:
            async with engine.begin() as connection:
                if level is None:
                    await connection.execute(
                        text(
                            f"ALTER ROLE {PRODUCTION_TEST_USER} "
                            "RESET default_transaction_isolation"
                        )
                    )
                else:
                    await connection.execute(
                        text(
                            f"ALTER ROLE {PRODUCTION_TEST_USER} "
                            "SET default_transaction_isolation TO "
                            f"'{level}'"
                        )
                    )
        finally:
            await engine.dispose()

    async def _promote(email: str) -> None:
        engine = create_async_engine(prod_dsn.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE users SET system_role_id = 'admin', is_admin = true "
                        "WHERE email = :email"
                    ),
                    {"email": email},
                )
        finally:
            await engine.dispose()

    async def _cleanup_user(email: str, category_ids: list[str]) -> None:
        engine = create_async_engine(prod_dsn.secret())
        try:
            async with engine.begin() as connection:
                if category_ids:
                    await connection.execute(
                        text(
                            "DELETE FROM categories "
                            "WHERE id = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": category_ids},
                    )
                await connection.execute(
                    text(
                        "DELETE FROM auth_sessions WHERE user_id IN "
                        "(SELECT id FROM users WHERE email = :email)"
                    ),
                    {"email": email},
                )
                await connection.execute(
                    text("DELETE FROM users WHERE email = :email"),
                    {"email": email},
                )
        finally:
            await engine.dispose()

    async def run() -> None:
        await _alter_role_isolation("serializable")
        email = unique_email("ser-cat")
        created: list[str] = []
        try:
            for key, value in production_settings_env(prod_dsn).items():
                monkeypatch.setenv(key, value)
            monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
            get_settings.cache_clear()
            get_app.cache_clear()
            await dispose_engine()
            application = create_app()
            transport = ASGITransport(app=application)
            client_cm = AsyncClient(transport=transport, base_url="https://test")
            async with client_cm as client:
                signed = await client.post(
                    "/api/v1/auth/signup",
                    json=signup_body(email=email),
                )
                assert signed.status_code == 201, signed.text
                await _promote(email)
                statuses: list[int] = []
                for _round in range(40):
                    parent = await client.post(
                        _ADMIN, json={"name": f"SP-{uuid.uuid4().hex[:8]}"}
                    )
                    assert parent.status_code != 500, parent.text
                    statuses.append(parent.status_code)
                    if parent.status_code != 201:
                        continue
                    parent_id = parent.json()["id"]
                    created.append(parent_id)
                    children = await asyncio.gather(
                        *[
                            client.post(
                                _ADMIN,
                                json={
                                    "name": f"SC-{uuid.uuid4().hex[:8]}",
                                    "parentId": parent_id,
                                },
                            )
                            for _ in range(6)
                        ]
                    )
                    for child in children:
                        assert child.status_code != 500, child.text
                        statuses.append(child.status_code)
                        if child.status_code == 201:
                            created.append(child.json()["id"])
                assert 500 not in statuses
                assert 201 in statuses
                listed = await client.get(_LIST)
                assert listed.status_code != 500, listed.text
        finally:
            await dispose_engine()
            get_settings.cache_clear()
            get_app.cache_clear()
            await _cleanup_user(email, created)
            await _alter_role_isolation(None)

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_alter_role_isolation(None))


def test_concurrent_writes_do_not_exhaust_pool(
    require_postgres: Any,
    production_test_database_url: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con pool_size=2 y sin overflow, 6 escrituras concurrentes no dan 500."""

    from httpx2 import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app, get_app
    from tests.conftest import production_settings_env
    from tests.test_auth import signup_body

    prod_dsn = production_test_database_url
    created: list[str] = []
    email = unique_email("pool-cat")

    async def run() -> None:
        for key, value in production_settings_env(prod_dsn).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DB_POOL_SIZE", "2")
        monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
        monkeypatch.setenv("DB_POOL_TIMEOUT", "3")
        monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
        get_settings.cache_clear()
        get_app.cache_clear()
        await dispose_engine()
        try:
            application = create_app()
            transport = ASGITransport(app=application)
            async with AsyncClient(
                transport=transport, base_url="https://test"
            ) as client:
                signed = await client.post(
                    "/api/v1/auth/signup", json=signup_body(email=email)
                )
                assert signed.status_code == 201, signed.text
                engine = create_async_engine(prod_dsn.secret())
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE users SET system_role_id='admin', "
                            "is_admin=true WHERE email=:e"
                        ),
                        {"e": email},
                    )
                await engine.dispose()
                rs = await asyncio.gather(
                    *[
                        client.post(_ADMIN, json={"name": f"PL-{uuid.uuid4().hex[:8]}"})
                        for _ in range(6)
                    ]
                )
                created.extend(r.json()["id"] for r in rs if r.status_code == 201)
                assert [r.status_code for r in rs] == [201] * 6, (
                    signed.headers.get("set-cookie"),
                    rs[0].text,
                )
        finally:
            await dispose_engine()
            get_settings.cache_clear()
            get_app.cache_clear()
            engine = create_async_engine(prod_dsn.secret())
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM categories WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": created},
                )
                await conn.execute(
                    text(
                        "DELETE FROM auth_sessions WHERE user_id IN "
                        "(SELECT id FROM users WHERE email=:e)"
                    ),
                    {"e": email},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE email=:e"), {"e": email}
                )
            await engine.dispose()

    asyncio.run(run())


def _install_failing_commit(
    monkeypatch: pytest.MonkeyPatch, fail_on: set[int], code: str
) -> tuple[dict[str, int], Any]:
    from sqlalchemy.ext.asyncio import AsyncSession

    original = AsyncSession.commit
    state = {"n": 0}

    class _PgError(Exception):
        sqlstate = code

    async def failing(self: AsyncSession) -> None:
        state["n"] += 1
        if state["n"] in fail_on:
            raise DBAPIError("COMMIT", None, _PgError())
        return await original(self)

    monkeypatch.setattr(AsyncSession, "commit", failing)

    def restore() -> None:
        monkeypatch.setattr(AsyncSession, "commit", original)

    return state, restore


def _assert_get_commit_maps_to_503(listed: Any, logbuf: Any) -> None:
    assert listed.status_code == 503, listed.text
    assert listed.json() == {"error": "transient_db_conflict"}
    assert listed.headers.get("retry-after") == "1"
    assert "already started" not in logbuf.getvalue()


def test_http_commit_transient_is_retried_and_mapped(
    require_postgres: Any,
    production_test_database_url: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P9b-f: 40001/40P01 en el COMMIT del reintento (GET: test_get_commit)."""

    from httpx2 import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app, get_app
    from tests.conftest import production_settings_env
    from tests.test_auth import signup_body

    prod_dsn = production_test_database_url
    email = unique_email("p9-cat")
    prefix = f"p9-{uuid.uuid4().hex[:8]}"
    created: list[str] = []
    names: list[str] = []

    async def run() -> None:
        for key, value in production_settings_env(prod_dsn).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
        get_settings.cache_clear()
        get_app.cache_clear()
        await dispose_engine()
        application = create_app()
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        try:
            async with AsyncClient(
                transport=transport, base_url="https://test"
            ) as client:
                signed = await client.post(
                    "/api/v1/auth/signup", json=signup_body(email=email)
                )
                assert signed.status_code == 201, signed.text
                engine = create_async_engine(prod_dsn.secret())
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE users SET system_role_id='admin', "
                            "is_admin=true WHERE email=:e"
                        ),
                        {"e": email},
                    )
                await engine.dispose()
                for code in ("40001", "40P01"):
                    name = f"{prefix}-b-{uuid.uuid4().hex[:8]}"
                    names.append(name)
                    state, restore = _install_failing_commit(monkeypatch, {2}, code)
                    try:
                        posted = await client.post(_ADMIN, json={"name": name})
                    finally:
                        restore()
                    assert posted.status_code == 201, (code, posted.text, state)
                    created.append(posted.json()["id"])
                    engine = create_async_engine(prod_dsn.secret())
                    async with engine.connect() as conn:
                        rows = (
                            await conn.execute(
                                text("SELECT id::text FROM categories WHERE name=:n"),
                                {"n": name},
                            )
                        ).all()
                    await engine.dispose()
                    assert len(rows) == 1
                    assert rows[0][0] == posted.json()["id"]

                    new_name = f"{prefix}-c-{uuid.uuid4().hex[:8]}"
                    names.append(new_name)
                    state, restore = _install_failing_commit(monkeypatch, {2}, code)
                    try:
                        updated = await client.put(
                            f"{_ADMIN}/{posted.json()['id']}",
                            json={"name": new_name, "sortOrder": 7},
                        )
                    finally:
                        restore()
                    assert updated.status_code == 200, (code, updated.text, state)
                    engine = create_async_engine(prod_dsn.secret())
                    async with engine.connect() as conn:
                        row = (
                            await conn.execute(
                                text(
                                    "SELECT name, sort_order FROM categories "
                                    "WHERE id = CAST(:i AS uuid)"
                                ),
                                {"i": posted.json()["id"]},
                            )
                        ).one()
                    await engine.dispose()
                    assert row == (new_name, 7)
                    assert updated.json()["name"] == new_name
                    assert updated.json()["sortOrder"] == 7

                    exhausted = f"{prefix}-d-{uuid.uuid4().hex[:8]}"
                    names.append(exhausted)
                    state, restore = _install_failing_commit(
                        monkeypatch, {2, 3, 4}, code
                    )
                    try:
                        failed = await client.post(_ADMIN, json={"name": exhausted})
                    finally:
                        restore()
                    assert failed.status_code == 503, failed.text
                    assert failed.json() == {"error": "transient_db_conflict"}
                    assert failed.headers.get("retry-after") == "1"
                    engine = create_async_engine(prod_dsn.secret())
                    async with engine.connect() as conn:
                        count = await conn.scalar(
                            text("SELECT count(*) FROM categories WHERE name=:n"),
                            {"n": exhausted},
                        )
                    await engine.dispose()
                    assert int(count or 0) == 0
                    again = await client.post(_ADMIN, json={"name": exhausted})
                    assert again.status_code == 201, again.text
                    created.append(again.json()["id"])

                    state, restore = _install_failing_commit(monkeypatch, {2}, code)
                    try:
                        deleted = await client.delete(f"{_ADMIN}/{posted.json()['id']}")
                    finally:
                        restore()
                    assert deleted.status_code == 204, (code, deleted.text, state)
                    engine = create_async_engine(prod_dsn.secret())
                    async with engine.connect() as conn:
                        gone = await conn.scalar(
                            text(
                                "SELECT deleted_at IS NOT NULL FROM categories "
                                "WHERE id = CAST(:i AS uuid)"
                            ),
                            {"i": posted.json()["id"]},
                        )
                    await engine.dispose()
                    assert gone is True
        finally:
            await dispose_engine()
            get_settings.cache_clear()
            get_app.cache_clear()
            engine = create_async_engine(prod_dsn.secret())
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM categories "
                        "WHERE name = ANY(:names) "
                        "OR name LIKE :prefix "
                        "OR id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {
                        "names": names,
                        "prefix": f"{prefix}-%",
                        "ids": created,
                    },
                )
                await conn.execute(
                    text(
                        "DELETE FROM auth_sessions WHERE user_id IN "
                        "(SELECT id FROM users WHERE email=:e)"
                    ),
                    {"e": email},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE email=:e"), {"e": email}
                )
            await engine.dispose()

    asyncio.run(run())


def test_get_commit_fails_before_response(
    require_postgres: Any,
    production_test_database_url: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """d208709: GET con scope=function mapea el COMMIT a 503, sin already started."""

    import logging
    from io import StringIO

    from httpx2 import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app, get_app
    from tests.conftest import production_settings_env
    from tests.test_auth import signup_body

    prod_dsn = production_test_database_url
    email = unique_email("d208-cat")

    async def run() -> None:
        for key, value in production_settings_env(prod_dsn).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
        get_settings.cache_clear()
        get_app.cache_clear()
        await dispose_engine()
        application = create_app()
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        try:
            async with AsyncClient(
                transport=transport, base_url="https://test"
            ) as client:
                signed = await client.post(
                    "/api/v1/auth/signup", json=signup_body(email=email)
                )
                assert signed.status_code == 201, signed.text
                logbuf = StringIO()
                handler = logging.StreamHandler(logbuf)
                logging.getLogger().addHandler(handler)
                _install_failing_commit(monkeypatch, {1}, "40001")
                try:
                    listed = await client.get(_LIST)
                finally:
                    logging.getLogger().removeHandler(handler)
                _assert_get_commit_maps_to_503(listed, logbuf)
        finally:
            await dispose_engine()
            get_settings.cache_clear()
            get_app.cache_clear()
            engine = create_async_engine(prod_dsn.secret())
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM auth_sessions WHERE user_id IN "
                        "(SELECT id FROM users WHERE email=:e)"
                    ),
                    {"e": email},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE email=:e"), {"e": email}
                )
            await engine.dispose()

    asyncio.run(run())


def test_http_post_retries_real_deadlock(
    require_postgres: Any,
    migrated_test_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST HTTP reintenta un 40P01 real y deja una sola fila."""

    from httpx2 import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import get_settings
    from app.db import RedactedDsn, dispose_engine
    from app.main import create_app, get_app
    from tests.test_auth import signup_body

    dsn = require_postgres
    assert isinstance(dsn, RedactedDsn)
    _skip_without_deadlock_timeout(dsn.secret())
    email = unique_email("p9h-cat")
    suffix = uuid.uuid4().hex[:8]
    tracked: list[uuid.UUID] = []
    released = asyncio.Event()

    async def run() -> None:
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("EXPOSE_ERRORS", "false")
        monkeypatch.setenv("DATABASE_URL", dsn.secret())
        monkeypatch.setenv("ENABLE_EXAMPLE_ROUTES", "false")
        get_settings.cache_clear()
        get_app.cache_clear()
        await dispose_engine()
        application = create_app()
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        engine = create_async_engine(
            dsn.secret(),
            pool_size=6,
            connect_args={"server_settings": {"deadlock_timeout": "20ms"}},
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                parent = await create_category(
                    setup, CategoryCreate(name=f"p9hp-{suffix}")
                )
                tracked.append(parent.id)
                await setup.commit()
            async with AsyncClient(
                transport=transport, base_url="https://test"
            ) as client:
                signed = await client.post(
                    "/api/v1/auth/signup", json=signup_body(email=email)
                )
                assert signed.status_code == 201, signed.text
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE users SET system_role_id='admin', "
                            "is_admin=true WHERE email=:e"
                        ),
                        {"e": email},
                    )
                child_name = f"p9hc-{suffix}"

                async def start_api() -> Any:
                    return await client.post(
                        _ADMIN,
                        json={"name": child_name, "parentId": str(parent.id)},
                    )

                posted = await _adversary_deadlock_rounds(
                    factory,
                    rounds=2,
                    hold_sql=(
                        "INSERT INTO categories "
                        "(name, name_key, parent_id, sort_order) "
                        "VALUES (:name, :name, :parent_id, 0)"
                    ),
                    hold_params={"name": child_name, "parent_id": parent.id},
                    wait_sql="SELECT id FROM categories WHERE id = :id FOR UPDATE",
                    wait_params={"id": parent.id},
                    start_api=start_api,
                    released=released,
                )
                assert posted.status_code == 201, posted.text
                tracked.append(uuid.UUID(posted.json()["id"]))
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(
                            text(
                                "SELECT id::text FROM categories "
                                "WHERE parent_id = :p AND name = :n "
                                "AND deleted_at IS NULL"
                            ),
                            {"p": parent.id, "n": child_name},
                        )
                    ).all()
                assert len(rows) == 1
                assert rows[0][0] == posted.json()["id"]
        finally:
            await dispose_engine()
            get_settings.cache_clear()
            get_app.cache_clear()
            await engine.dispose()
            admin = create_async_engine(dsn.secret())
            try:
                async with admin.begin() as conn:
                    await conn.execute(
                        text(
                            "DELETE FROM auth_sessions WHERE user_id IN "
                            "(SELECT id FROM users WHERE email=:e)"
                        ),
                        {"e": email},
                    )
                    await conn.execute(
                        text("DELETE FROM users WHERE email=:e"), {"e": email}
                    )
            finally:
                await admin.dispose()

    try:
        asyncio.run(run())
    finally:
        asyncio.run(_cleanup_ids(dsn.secret(), tracked))
