import asyncio
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.tokens import encode_session_token
from app.config import get_settings
from app.db import RedactedDsn
from app.exceptions import AccessDenied, LastAdmin, NotFound
from app.models import AuthSession, CourseMembership, SystemRoleRow, User
from app.models.user import SystemRole
from app.permissions.types import CourseRole
from app.schemas.auth import normalize_email
from app.schemas.users import AdminUserIdentityUpdate
from app.services.roles import assign_system_role
from app.services.users import (
    _active_admin_clause,
    _email_term,
    _ensure_admin_remains,
    _exact_email_clause,
    _visible_user_filters,
    set_user_admin,
    set_user_suspended,
    soft_delete_user,
    update_user_identity,
)
from tests.conftest import resolve_test_database_url
from tests.test_auth import PASSWORD, unique_email
from tests.test_roles import (
    SYSTEM_ROLES,
    _enroll,
    _set_role,
    _signup,
)

pytestmark = pytest.mark.db

_COURSE = uuid.UUID("44444444-4444-4444-4444-444444444444")
_LEAK_MARKERS = (
    "password12",
    "argon2",
    "password_hash",
    "passwordHash",
    "test-auth-secret",
    "Bearer ",
)
_ADMIN_VIEW_KEYS = {
    "id",
    "name",
    "email",
    "firstName",
    "lastName",
    "avatarFileId",
    "emailNotificationsEnabled",
    "suspendedAt",
    "deletedAt",
    "systemRoleId",
}
_SEARCH_KEYS = {"id", "name", "email", "firstName", "lastName"}

ADMIN_USER_ENDPOINTS: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", "/api/v1/admin/users", None),
    ("PUT", "/api/v1/admin/users/{user_id}", {"firstName": "Ada"}),
    ("PUT", "/api/v1/admin/users/{user_id}/admin", {"isAdmin": True}),
    ("PUT", "/api/v1/admin/users/{user_id}/suspension", {"suspended": True}),
    ("DELETE", "/api/v1/admin/users/{user_id}", None),
)


def _assert_no_secrets(response: Any) -> None:
    for marker in _LEAK_MARKERS:
        assert marker not in response.text


def _assert_admin_view(payload: dict[str, Any], *, email: str | None = None) -> None:
    assert set(payload) == _ADMIN_VIEW_KEYS
    uuid.UUID(payload["id"])
    if email is not None:
        assert payload["email"] == email
    for marker in _LEAK_MARKERS:
        assert marker not in str(payload)


def _path(template: str, user_id: uuid.UUID) -> str:
    return template.format(user_id=user_id)


async def _signup_role(
    session_client: Any,
    db_session: AsyncSession,
    role: SystemRole,
    *,
    prefix: str | None = None,
    name: str = "Ada Lovelace",
) -> dict[str, Any]:
    created = await _signup(
        session_client,
        email=unique_email(prefix or role.value),
        name=name,
    )
    await _set_role(db_session, created["id"], role)
    return created


async def _demote_foreign_admins(
    db_session: AsyncSession, keep: set[uuid.UUID]
) -> None:
    rows = await db_session.scalars(
        select(User).where(User.system_role_id == SystemRole.ADMIN)
    )
    for user in rows:
        if user.id not in keep:
            user.is_admin = False
            user.system_role_id = SystemRole.ALUMNO
    await db_session.flush()


@pytest.mark.parametrize("role", SYSTEM_ROLES)
@pytest.mark.parametrize("method,template,body", ADMIN_USER_ENDPOINTS)
async def test_admin_users_role_matrix(
    session_client: Any,
    db_session: AsyncSession,
    role: SystemRole,
    method: str,
    template: str,
    body: dict[str, object] | None,
) -> None:
    created = await _signup_role(session_client, db_session, role)
    path = _path(template, created["id"])
    response = await session_client.request(method, path, json=body)
    _assert_no_secrets(response)
    if role == SystemRole.ADMIN:
        if method == "DELETE":
            assert response.status_code == 409
            assert response.json() == {"error": "cannot_modify_self"}
        elif method == "GET":
            assert response.status_code == 200
            assert set(response.json()) == {"items", "page", "pageSize", "total"}
        elif method == "PUT" and template.endswith("/suspension"):
            assert response.status_code == 409
            assert response.json() == {"error": "cannot_modify_self"}
        else:
            assert response.status_code == 200
            _assert_admin_view(response.json(), email=created["email"])
    else:
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}
        assert created["email"] not in response.text


@pytest.mark.parametrize("method,template,body", ADMIN_USER_ENDPOINTS)
async def test_admin_users_without_session_are_unauthorized(
    session_client: Any,
    method: str,
    template: str,
    body: dict[str, object] | None,
) -> None:
    response = await session_client.request(
        method, _path(template, uuid.uuid4()), json=body
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    _assert_no_secrets(response)


@pytest.mark.parametrize("method,template,body", ADMIN_USER_ENDPOINTS)
async def test_admin_users_reject_invalid_and_expired_token(
    session_client: Any,
    method: str,
    template: str,
    body: dict[str, object] | None,
) -> None:
    created = await _signup(session_client)
    token = created["token"]
    tampered = token[:-2] + ("A" if token[-1] != "A" else "B")
    expired = encode_session_token(
        secret=get_settings().resolved_auth_secret(),
        user_id=created["id"],
        session_id=uuid.uuid4(),
        issued_at=datetime.now(UTC) - timedelta(hours=2),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session_client.cookies.clear()
    path = _path(template, created["id"])
    for bad in (tampered, expired, "not-a-jwt"):
        response = await session_client.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {bad}"},
        )
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        assert bad not in response.text
        _assert_no_secrets(response)


async def test_admin_lists_paginated_and_filterable_including_suspended(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="list-admin"
    )
    marker = uuid.uuid4().hex[:12]
    active = await _signup(
        session_client,
        email=unique_email(f"{marker}-act"),
        name="Ada Active",
    )
    session_client.cookies.clear()
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    suspended = await _signup(
        session_client,
        email=unique_email(f"{marker}-sus"),
        name="Sue Spendida",
    )
    deleted = await _signup(
        session_client,
        email=unique_email(f"{marker}-del"),
        name="Del Eted",
    )
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    suspend = await session_client.put(
        f"/api/v1/admin/users/{suspended['id']}/suspension",
        json={"suspended": True},
    )
    assert suspend.status_code == 200
    assert suspend.json()["suspendedAt"] is not None
    gone = await session_client.delete(f"/api/v1/admin/users/{deleted['id']}")
    assert gone.status_code == 204

    listed = await session_client.get(
        "/api/v1/admin/users",
        params={"query": marker, "page": 1, "pageSize": 1},
    )
    assert listed.status_code == 200
    body = listed.json()
    assert set(body) == {"items", "page", "pageSize", "total"}
    assert "nextCursor" not in body
    assert "limit" not in body
    assert body["page"] == 1
    assert body["pageSize"] == 1
    assert body["total"] == 2
    assert len(body["items"]) == 1
    _assert_admin_view(body["items"][0])
    emails = {body["items"][0]["email"]}
    page2 = await session_client.get(
        "/api/v1/admin/users",
        params={"query": marker, "page": 2, "pageSize": 1},
    )
    emails.add(page2.json()["items"][0]["email"])
    assert emails == {active["email"], suspended["email"]}
    assert deleted["email"] not in emails

    suspended_only = await session_client.get(
        "/api/v1/admin/users",
        params={"query": marker, "status": "suspended"},
    )
    assert {item["email"] for item in suspended_only.json()["items"]} == {
        suspended["email"]
    }

    all_status = await session_client.get(
        "/api/v1/admin/users",
        params={"query": marker, "status": "all"},
    )
    assert {item["email"] for item in all_status.json()["items"]} == {
        active["email"],
        suspended["email"],
        deleted["email"],
    }
    deleted_row = next(
        item for item in all_status.json()["items"] if item["email"] == deleted["email"]
    )
    assert deleted_row["deletedAt"] is not None
    _assert_no_secrets(listed)
    _assert_no_secrets(all_status)


async def test_self_demote_and_self_delete_are_cannot_modify_self(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="self-admin"
    )
    other = await _signup(session_client, email=unique_email("self-other"))
    await _set_role(db_session, other["id"], SystemRole.ADMIN)
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    demote = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}/admin",
        json={"isAdmin": False},
    )
    assert demote.status_code == 409
    assert demote.json() == {"error": "cannot_modify_self"}
    still = await db_session.get(User, admin["id"])
    assert still is not None
    assert still.is_admin is True
    assert still.system_role_id == SystemRole.ADMIN

    deleted = await session_client.delete(f"/api/v1/admin/users/{admin['id']}")
    assert deleted.status_code == 409
    assert deleted.json() == {"error": "cannot_modify_self"}
    still = await db_session.get(User, admin["id"])
    assert still is not None
    assert still.deleted_at is None
    peer = await db_session.get(User, other["id"])
    assert peer is not None
    assert peer.is_admin is True
    _assert_no_secrets(demote)
    _assert_no_secrets(deleted)


async def test_cannot_leave_site_without_active_admin(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="solo-admin"
    )
    await _demote_foreign_admins(db_session, {admin["id"]})
    suspended = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}/suspension",
        json={"suspended": True},
    )
    assert suspended.status_code == 409
    assert suspended.json() == {"error": "cannot_modify_self"}
    user = await db_session.get(User, admin["id"])
    assert user is not None
    assert user.suspended_at is None


async def test_edit_identity_rejects_taken_email(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="id-admin"
    )
    other = await _signup(session_client, email=unique_email("taken"))
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    taken = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}",
        json={"email": other["email"].upper()},
    )
    assert taken.status_code == 409
    assert taken.json() == {"error": "email_taken"}
    assert str(other["id"]) not in taken.text
    _assert_no_secrets(taken)

    same = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}",
        json={"email": admin["email"], "firstName": "Ada", "lastName": "Admin"},
    )
    assert same.status_code == 200
    _assert_admin_view(same.json(), email=admin["email"])
    assert same.json()["firstName"] == "Ada"
    assert same.json()["lastName"] == "Admin"
    assert same.json()["name"] == "Ada Admin"


async def test_identity_put_ignores_privilege_escalation_fields(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="esc-admin"
    )
    target = await _signup(session_client, email=unique_email("esc-target"))
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    response = await session_client.put(
        f"/api/v1/admin/users/{target['id']}",
        json={
            "firstName": "Neo",
            "isAdmin": True,
            "systemRoleId": "admin",
            "password": "password12",
            "passwordHash": "$argon2id$v=19$m=8,t=1,p=1$aaaaaaaaaaa$bbbb",
        },
    )
    assert response.status_code == 200
    body = response.json()
    _assert_admin_view(body)
    assert body["firstName"] == "Neo"
    assert body["systemRoleId"] == "alumno"
    assert "password" not in body
    user = await db_session.get(User, target["id"])
    assert user is not None
    assert user.is_admin is False
    assert user.system_role_id == SystemRole.ALUMNO
    assert user.password_hash.startswith("$argon2")
    _assert_no_secrets(response)


async def test_grant_and_revoke_admin_on_another_user(
    session_client: Any,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assigned: list[str] = []

    async def _spy(
        session: AsyncSession,
        *,
        actor: User,
        target: User,
        role_id: str,
    ) -> User:
        assigned.append(role_id)
        return await assign_system_role(
            session, actor=actor, target=target, role_id=role_id
        )

    monkeypatch.setattr("app.services.users.assign_system_role", _spy)
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="grant-admin"
    )
    target = await _signup(session_client, email=unique_email("grant-target"))
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    granted = await session_client.put(
        f"/api/v1/admin/users/{target['id']}/admin",
        json={"isAdmin": True},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["systemRoleId"] == "admin"
    promoted = await db_session.get(User, target["id"])
    assert promoted is not None
    assert promoted.is_admin is True
    assert promoted.system_role_id == SystemRole.ADMIN
    revoked = await session_client.put(
        f"/api/v1/admin/users/{target['id']}/admin",
        json={"isAdmin": False},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["systemRoleId"] == "alumno"
    assert assigned == [SystemRole.ADMIN, SystemRole.ALUMNO]
    user = await db_session.get(User, target["id"])
    assert user is not None
    assert user.is_admin is False
    assert user.system_role_id == SystemRole.ALUMNO
    assert user.is_admin == (user.system_role_id == SystemRole.ADMIN)


async def test_suspend_existing_session_is_account_suspended(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="sus-admin"
    )
    victim = await _signup(session_client, email=unique_email("sus-victim"))
    victim_token = victim["token"]
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    suspended = await session_client.put(
        f"/api/v1/admin/users/{victim['id']}/suspension",
        json={"suspended": True},
    )
    assert suspended.status_code == 200
    session_client.cookies.clear()
    me = await session_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {victim_token}"},
    )
    assert me.status_code == 403
    assert me.json() == {"error": "account_suspended"}
    assert victim_token not in me.text
    _assert_no_secrets(me)


async def test_unknown_user_is_not_found(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _signup_role(session_client, db_session, SystemRole.ADMIN, prefix="nf-admin")
    missing = await session_client.put(
        f"/api/v1/admin/users/{uuid.uuid4()}",
        json={"firstName": "Nadie"},
    )
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found"}


async def test_mutations_on_deleted_user_are_not_found(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="del-admin"
    )
    target = await _signup(session_client, email=unique_email("already-del"))
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    deleted = await session_client.delete(f"/api/v1/admin/users/{target['id']}")
    assert deleted.status_code == 204
    again = await session_client.delete(f"/api/v1/admin/users/{target['id']}")
    assert again.status_code == 404
    edit = await session_client.put(
        f"/api/v1/admin/users/{target['id']}",
        json={"firstName": "Ghost"},
    )
    assert edit.status_code == 404
    _assert_no_secrets(again)


async def test_empty_identity_update_is_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="val-admin"
    )
    response = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}",
        json={},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "validation"


async def test_search_hides_deleted_and_suspended_and_requires_teacher_or_admin(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:12]
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="sr-admin"
    )
    visible = await _signup(
        session_client,
        email=unique_email(f"{marker}-ok"),
        name=f"Vz{marker} Visible",
    )
    suspended = await _signup(
        session_client,
        email=unique_email(f"{marker}-hid"),
        name="Hidden Suspended",
    )
    deleted = await _signup(
        session_client,
        email=unique_email(f"{marker}-gone"),
        name="Hidden Deleted",
    )
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    await session_client.put(
        f"/api/v1/admin/users/{suspended['id']}/suspension",
        json={"suspended": True},
    )
    await session_client.delete(f"/api/v1/admin/users/{deleted['id']}")

    found = await session_client.get(
        "/api/v1/users/search", params={"q": marker, "pageSize": 20}
    )
    assert found.status_code == 200
    body = found.json()
    assert set(body) == {"items", "page", "pageSize", "total"}
    emails = {item["email"] for item in body["items"]}
    assert emails == {visible["email"]}
    assert set(body["items"][0]) == _SEARCH_KEYS
    assert deleted["email"] not in found.text
    _assert_no_secrets(found)

    empty = await session_client.get("/api/v1/users/search", params={"q": ""})
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0

    session_client.cookies.clear()
    student = await _signup(session_client, email=unique_email("sr-stu"))
    denied = await session_client.get("/api/v1/users/search", params={"q": marker})
    assert denied.status_code == 403
    assert denied.json() == {"error": "forbidden"}
    assert visible["email"] not in denied.text

    session_client.cookies.clear()
    teacher = await _signup(session_client, email=unique_email("sr-tea"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    bare = await session_client.get("/api/v1/users/search", params={"q": marker})
    assert bare.status_code == 403

    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    allowed = await session_client.get(
        "/api/v1/users/search", params={"q": f"Vz{marker}"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["total"] == 1
    hit = allowed.json()["items"][0]
    assert hit["id"] == str(visible["id"])
    assert "email" not in hit

    session_client.cookies.clear()
    assistant = await _signup(session_client, email=unique_email("sr-ast"))
    await _set_role(db_session, assistant["id"], SystemRole.PROFESOR_ADJUNTO)
    await _enroll(db_session, assistant["id"], _COURSE, CourseRole.ASSISTANT)
    asst = await session_client.get("/api/v1/users/search", params={"q": marker})
    assert asst.status_code == 403
    assert student["email"] not in asst.text


async def test_search_without_session_is_unauthorized(session_client: Any) -> None:
    response = await session_client.get("/api/v1/users/search", params={"q": "ada"})
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_query_wildcard_escape_does_not_match_everyone(
    session_client: Any, db_session: AsyncSession
) -> None:
    await _signup_role(session_client, db_session, SystemRole.ADMIN, prefix="wc-admin")
    marker = uuid.uuid4().hex[:8]
    special = User(
        first_name=f"{marker}%Special",
        last_name="Match",
        email=unique_email(f"{marker}-pct"),
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
    )
    db_session.add(special)
    for index in range(25):
        db_session.add(
            User(
                first_name=marker,
                last_name=f"Other{index:02d}",
                email=unique_email(f"{marker}-o{index}"),
                password_hash="x",
                system_role_id=SystemRole.ALUMNO,
            )
        )
    await db_session.flush()
    wild = await session_client.get(
        "/api/v1/admin/users",
        params={"query": f"{marker}%", "pageSize": 20},
    )
    assert wild.status_code == 200
    body = wild.json()
    assert body["total"] == 1
    assert [item["email"] for item in body["items"]] == [special.email]


async def test_identity_patch_is_not_allowed(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="patch-admin"
    )
    response = await session_client.patch(
        f"/api/v1/admin/users/{admin['id']}",
        json={"firstName": "Nope"},
    )
    assert response.status_code == 405


async def test_last_admin_delete_of_other_admin_when_actor_remains(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="keep-admin"
    )
    other = await _signup(session_client, email=unique_email("other-admin"))
    await _set_role(db_session, other["id"], SystemRole.ADMIN)
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    await _demote_foreign_admins(db_session, {admin["id"], other["id"]})
    deleted = await session_client.delete(f"/api/v1/admin/users/{other['id']}")
    assert deleted.status_code == 204
    leftover = await db_session.get(User, admin["id"])
    assert leftover is not None
    assert leftover.is_admin is True


async def _park_existing_admins(
    session: AsyncSession,
) -> list[tuple[UUID, str, bool]]:
    existing = list(
        await session.scalars(
            select(User).where(User.system_role_id == SystemRole.ADMIN)
        )
    )
    extras = [(user.id, user.system_role_id, user.is_admin) for user in existing]
    for user in existing:
        user.is_admin = False
        user.system_role_id = SystemRole.ALUMNO
    return extras


async def _restore_parked_admins(
    session: AsyncSession,
    extras: list[tuple[UUID, str, bool]],
    created_ids: list[UUID],
) -> None:
    for user_id in created_ids:
        created = await session.get(User, user_id)
        if created is not None:
            await session.delete(created)
    for extra_id, role_id, is_admin in extras:
        extra = await session.get(User, extra_id)
        if extra is not None:
            extra.system_role_id = role_id
            extra.is_admin = is_admin


async def _insert_admin(session: AsyncSession, prefix: str) -> UUID:
    user = User(
        first_name=prefix,
        last_name="Admin",
        email=unique_email(prefix),
        password_hash="x",
        system_role_id=SystemRole.ADMIN,
        is_admin=True,
    )
    session.add(user)
    await session.flush()
    return user.id


async def _has_waiting_lock(engine: Any) -> bool:
    async with engine.connect() as conn:
        waiting = await conn.scalar(
            text("SELECT count(*) FROM pg_locks WHERE NOT granted")
        )
    return int(waiting or 0) > 0


async def _held_lock_degrade_actor(
    engine: Any,
    *,
    actor_id: UUID,
    hold_ids: tuple[UUID, ...],
    act: Any,
) -> None:
    """Carga el actor con session.get, otra conexión retiene FOR UPDATE y lo degrada."""

    actor_loaded = asyncio.Event()
    lock_ready = asyncio.Event()
    actor_started = asyncio.Event()
    act_task: asyncio.Task[None] | None = None

    async def _hold() -> None:
        await actor_loaded.wait()
        async with AsyncSession(engine) as session, session.begin():
            await session.scalars(
                select(User)
                .where(User.id.in_(hold_ids))
                .order_by(User.id)
                .with_for_update()
            )
            row = await session.get(User, actor_id)
            assert row is not None
            row.is_admin = False
            row.system_role_id = SystemRole.ALUMNO
            await session.flush()
            lock_ready.set()
            await actor_started.wait()
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if act_task is not None and act_task.done():
                    return
                if await _has_waiting_lock(engine):
                    return
                await asyncio.sleep(0.05)

    async def _run() -> None:
        async with AsyncSession(engine) as session, session.begin():
            actor = await session.get(User, actor_id)
            assert actor is not None
            actor_loaded.set()
            await lock_ready.wait()
            actor_started.set()
            await act(session, actor)

    hold_task = asyncio.create_task(_hold())
    act_task = asyncio.create_task(_run())
    try:
        await asyncio.wait_for(asyncio.shield(act_task), timeout=8)
        pytest.fail("expected AccessDenied")
    except AccessDenied:
        pass
    finally:
        await hold_task


def _apply_fate(user: User, fate: str) -> None:
    if fate == "demote":
        user.is_admin = False
        user.system_role_id = SystemRole.ALUMNO
        return
    if fate == "suspend":
        user.suspended_at = datetime.now(UTC)
        return
    user.deleted_at = datetime.now(UTC)


async def test_ensure_admin_remains_raises_last_admin_when_target_is_alone() -> None:
    target = User(
        first_name="Only",
        last_name="Admin",
        email=unique_email("alone-tgt"),
        password_hash="x",
        system_role_id=SystemRole.ADMIN,
        is_admin=True,
    )
    target.id = uuid.uuid4()
    actor = User(
        first_name="Other",
        last_name="Actor",
        email=unique_email("alone-act"),
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
        is_admin=False,
    )
    actor.id = uuid.uuid4()
    with pytest.raises(LastAdmin):
        _ensure_admin_remains([target], actor=actor, target=target)


async def test_held_lock_refreshes_same_session_actor(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """A se carga con session.get; otra conexión degrada A y C bajo lock.

    Sin ``populate_existing`` el actor caducado cuenta como admin y B cae.
    Mata el mutante del lock y K3 (sin FOR UPDATE no espera).
    """

    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            actor_id = await _insert_admin(session, "lock-actor")
            target_id = await _insert_admin(session, "lock-target")
            peer_id = await _insert_admin(session, "lock-peer")
            ids = [actor_id, target_id, peer_id]

        actor_loaded = asyncio.Event()
        lock_ready = asyncio.Event()
        actor_started = asyncio.Event()
        demote_task: asyncio.Task[None] | None = None

        async def _hold_peer_and_target() -> None:
            await actor_loaded.wait()
            async with AsyncSession(engine) as session, session.begin():
                await session.scalars(
                    select(User)
                    .where(User.id.in_((target_id, peer_id)))
                    .order_by(User.id)
                    .with_for_update()
                )
                for user_id in (actor_id, peer_id):
                    row = await session.get(User, user_id)
                    assert row is not None
                    row.is_admin = False
                    row.system_role_id = SystemRole.ALUMNO
                await session.flush()
                lock_ready.set()
                await actor_started.wait()
                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    if demote_task is not None and demote_task.done():
                        return
                    if await _has_waiting_lock(engine):
                        return
                    await asyncio.sleep(0.05)

        async def _demote_target() -> None:
            async with AsyncSession(engine) as session, session.begin():
                actor = await session.get(User, actor_id)
                assert actor is not None
                assert actor.is_site_admin
                actor_loaded.set()
                await lock_ready.wait()
                actor_started.set()
                await set_user_admin(
                    session,
                    actor=actor,
                    user_id=target_id,
                    is_admin=False,
                )

        hold_task = asyncio.create_task(_hold_peer_and_target())
        demote_task = asyncio.create_task(_demote_target())
        try:
            await asyncio.wait_for(asyncio.shield(demote_task), timeout=8)
            pytest.fail("expected AccessDenied")
        except AccessDenied:
            pass
        finally:
            await hold_task

        async with AsyncSession(engine) as session:
            target = await session.get(User, target_id)
            peer = await session.get(User, peer_id)
            actor = await session.get(User, actor_id)
            assert target is not None
            assert peer is not None
            assert actor is not None
            assert target.is_admin is True
            assert target.system_role_id == SystemRole.ADMIN
            assert peer.is_admin is False
            assert actor.is_admin is False
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


@pytest.mark.parametrize("fate", ("demote", "suspend", "delete"))
async def test_actor_revalidated_after_lock_when_degraded(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    fate: str,
) -> None:
    """Tres admins: B se carga en la sesión y se degrada desde otra conexión."""

    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            admin_a = await _insert_admin(session, "toc-a")
            admin_b = await _insert_admin(session, "toc-b")
            admin_c = await _insert_admin(session, "toc-c")
            ids = [admin_a, admin_b, admin_c]

        actor_loaded = asyncio.Event()
        lock_ready = asyncio.Event()
        actor_started = asyncio.Event()
        act_task: asyncio.Task[None] | None = None

        async def _degrade_b() -> None:
            await actor_loaded.wait()
            async with AsyncSession(engine) as session, session.begin():
                await session.scalars(
                    select(User)
                    .where(User.id.in_((admin_a, admin_b, admin_c)))
                    .order_by(User.id)
                    .with_for_update()
                )
                actor = await session.get(User, admin_b)
                assert actor is not None
                _apply_fate(actor, fate)
                await session.flush()
                lock_ready.set()
                await actor_started.wait()
                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    if act_task is not None and act_task.done():
                        return
                    if await _has_waiting_lock(engine):
                        return
                    await asyncio.sleep(0.05)

        async def _act_on_c() -> None:
            async with AsyncSession(engine) as session, session.begin():
                actor = await session.get(User, admin_b)
                assert actor is not None
                actor_loaded.set()
                await lock_ready.wait()
                actor_started.set()
                if fate == "delete":
                    await soft_delete_user(session, actor=actor, user_id=admin_c)
                elif fate == "suspend":
                    await set_user_suspended(
                        session, actor=actor, user_id=admin_c, suspended=True
                    )
                else:
                    await set_user_admin(
                        session, actor=actor, user_id=admin_c, is_admin=False
                    )

        hold_task = asyncio.create_task(_degrade_b())
        act_task = asyncio.create_task(_act_on_c())
        try:
            await asyncio.wait_for(asyncio.shield(act_task), timeout=8)
            pytest.fail("expected AccessDenied")
        except AccessDenied:
            pass
        finally:
            await hold_task

        async with AsyncSession(engine) as session:
            target = await session.get(User, admin_c)
            leftover = await session.get(User, admin_a)
            assert target is not None
            assert leftover is not None
            assert target.is_admin is True
            assert target.suspended_at is None
            assert target.deleted_at is None
            assert leftover.is_admin is True
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


@pytest.mark.parametrize("action", ("identity", "unsuspend"))
async def test_stale_same_session_actor_cannot_edit_or_unsuspend(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    action: str,
) -> None:
    """session.get en la misma sesión; otra conexión degrada al actor.

    Mata N4 (identidad) y N6 (reactivar) si el lock no refresca.
    """

    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            actor_id = await _insert_admin(session, f"{action}-act")
            target_id = await _insert_admin(session, f"{action}-tgt")
            ids = [actor_id, target_id]
            target = await session.get(User, target_id)
            assert target is not None
            target.suspended_at = datetime.now(UTC)

        async with AsyncSession(engine) as session, session.begin():
            actor = await session.get(User, actor_id)
            assert actor is not None
            async with AsyncSession(engine) as other, other.begin():
                row = await other.get(User, actor_id)
                assert row is not None
                row.is_admin = False
                row.system_role_id = SystemRole.ALUMNO
            with pytest.raises(AccessDenied):
                if action == "identity":
                    await update_user_identity(
                        session,
                        actor=actor,
                        user_id=target_id,
                        body=AdminUserIdentityUpdate(first_name="N4"),
                    )
                else:
                    await set_user_suspended(
                        session, actor=actor, user_id=target_id, suspended=False
                    )

        async with AsyncSession(engine) as session:
            target = await session.get(User, target_id)
            assert target is not None
            if action == "identity":
                assert target.first_name != "N4"
            else:
                assert target.suspended_at is not None
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


@pytest.mark.parametrize("fate", ("demote", "suspend", "delete"))
async def test_concurrent_mutual_admin_ops_leave_one_admin(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    fate: str,
) -> None:
    """Dos conexiones cargan al actor con session.get y se degradan a la vez."""

    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            admin_a = await _insert_admin(session, f"race-a-{fate}")
            admin_b = await _insert_admin(session, f"race-b-{fate}")
            ids = [admin_a, admin_b]

        loaded = 0
        gate = asyncio.Lock()
        both_loaded = asyncio.Event()

        async def _act(actor_id: UUID, target_id: UUID) -> None:
            # La barrera corre siempre: sin ella el test no mata el mutante.
            nonlocal loaded
            async with AsyncSession(engine) as session, session.begin():
                actor = await session.get(User, actor_id)
                assert actor is not None
                async with gate:
                    loaded += 1
                    if loaded == 2:
                        both_loaded.set()
                await both_loaded.wait()
                if fate == "delete":
                    await soft_delete_user(session, actor=actor, user_id=target_id)
                elif fate == "suspend":
                    await set_user_suspended(
                        session, actor=actor, user_id=target_id, suspended=True
                    )
                else:
                    await set_user_admin(
                        session, actor=actor, user_id=target_id, is_admin=False
                    )

        results = await asyncio.gather(
            _act(admin_a, admin_b),
            _act(admin_b, admin_a),
            return_exceptions=True,
        )
        assert both_loaded.is_set()
        denied = [item for item in results if isinstance(item, AccessDenied)]
        failures = [
            item
            for item in results
            if isinstance(item, BaseException) and not isinstance(item, AccessDenied)
        ]
        assert not failures, failures
        assert len(denied) == 1
        assert results.count(None) == 1

        async with AsyncSession(engine) as session:
            remaining = [
                user
                for user_id in ids
                if (user := await session.get(User, user_id)) is not None
                and user.is_site_admin
                and user.deleted_at is None
                and user.suspended_at is None
            ]
            assert len(remaining) == 1
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


async def test_stale_same_session_actor_cannot_remove_last_active_admin(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            actor_id = await _insert_admin(session, "seq-stale")
            target_id = await _insert_admin(session, "seq-last")
            ids = [actor_id, target_id]

        async with AsyncSession(engine) as session, session.begin():
            actor = await session.get(User, actor_id)
            assert actor is not None
            async with AsyncSession(engine) as other, other.begin():
                row = await other.get(User, actor_id)
                assert row is not None
                row.is_admin = False
                row.system_role_id = SystemRole.ALUMNO
            with pytest.raises(AccessDenied):
                await set_user_admin(
                    session, actor=actor, user_id=target_id, is_admin=False
                )

        async with AsyncSession(engine) as session:
            leftover = await session.get(User, target_id)
            assert leftover is not None
            assert leftover.is_admin is True
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


@pytest.mark.parametrize(
    "action",
    ("promote_existing", "promote_new", "identity", "unsuspend"),
)
async def test_held_lock_actor_target_kills_k3a_and_n5(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    action: str,
) -> None:
    """K3a: sin FOR UPDATE en actor+destino no espera. N5: promover sin
    revalidar devolvería el destino si ya es admin.
    """

    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            actor_id = await _insert_admin(session, f"k3a-{action}-a")
            if action == "promote_new":
                new_user = User(
                    first_name="K3a",
                    last_name="Alumno",
                    email=unique_email(f"k3a-{action}-t"),
                    password_hash="x",
                    system_role_id=SystemRole.ALUMNO,
                    is_admin=False,
                )
                session.add(new_user)
                await session.flush()
                target_id = new_user.id
            else:
                target_id = await _insert_admin(session, f"k3a-{action}-t")
            ids = [actor_id, target_id]
            if action == "unsuspend":
                row = await session.get(User, target_id)
                assert row is not None
                row.suspended_at = datetime.now(UTC)

        async def _act(session: AsyncSession, actor: User) -> None:
            if action in {"promote_existing", "promote_new"}:
                await set_user_admin(
                    session, actor=actor, user_id=target_id, is_admin=True
                )
            elif action == "identity":
                await update_user_identity(
                    session,
                    actor=actor,
                    user_id=target_id,
                    body=AdminUserIdentityUpdate(first_name="K3a"),
                )
            else:
                await set_user_suspended(
                    session, actor=actor, user_id=target_id, suspended=False
                )

        await _held_lock_degrade_actor(
            engine,
            actor_id=actor_id,
            hold_ids=(actor_id, target_id),
            act=_act,
        )

        async with AsyncSession(engine) as session:
            target = await session.get(User, target_id)
            actor = await session.get(User, actor_id)
            assert target is not None
            assert actor is not None
            assert actor.is_admin is False
            if action == "promote_new":
                assert target.is_admin is False
                assert target.system_role_id == SystemRole.ALUMNO
            elif action == "identity":
                assert target.first_name != "K3a"
            elif action == "unsuspend":
                assert target.suspended_at is not None
            else:
                assert target.is_admin is True
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


@pytest.mark.parametrize("action", ("demote", "suspend", "delete"))
async def test_stale_actor_on_self_is_403_not_409(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    action: str,
) -> None:
    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            actor_id = await _insert_admin(session, f"self403-{action}")
            peer_id = await _insert_admin(session, f"self403-peer-{action}")
            ids = [actor_id, peer_id]

        async with AsyncSession(engine) as session, session.begin():
            actor = await session.get(User, actor_id)
            assert actor is not None
            async with AsyncSession(engine) as other, other.begin():
                row = await other.get(User, actor_id)
                assert row is not None
                row.is_admin = False
                row.system_role_id = SystemRole.ALUMNO
            with pytest.raises(AccessDenied):
                if action == "delete":
                    await soft_delete_user(session, actor=actor, user_id=actor_id)
                elif action == "suspend":
                    await set_user_suspended(
                        session, actor=actor, user_id=actor_id, suspended=True
                    )
                else:
                    await set_user_admin(
                        session, actor=actor, user_id=actor_id, is_admin=False
                    )

        async with AsyncSession(engine) as session:
            leftover = await session.get(User, actor_id)
            assert leftover is not None
            assert leftover.deleted_at is None
            assert leftover.suspended_at is None
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


@pytest.mark.parametrize("action", ("identity", "unsuspend", "delete"))
async def test_stale_actor_on_deleted_target_is_403_not_404(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    action: str,
) -> None:
    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret(), pool_size=4, max_overflow=0)
    extras: list[tuple[UUID, str, bool]] = []
    ids: list[UUID] = []
    try:
        async with AsyncSession(engine) as session, session.begin():
            extras = await _park_existing_admins(session)
            actor_id = await _insert_admin(session, f"del403-{action}")
            target_id = await _insert_admin(session, f"del403-t-{action}")
            ids = [actor_id, target_id]
            target = await session.get(User, target_id)
            assert target is not None
            target.deleted_at = datetime.now(UTC)
            if action == "unsuspend":
                target.suspended_at = datetime.now(UTC)

        async with AsyncSession(engine) as session, session.begin():
            actor = await session.get(User, actor_id)
            assert actor is not None
            async with AsyncSession(engine) as other, other.begin():
                row = await other.get(User, actor_id)
                assert row is not None
                row.is_admin = False
                row.system_role_id = SystemRole.ALUMNO
            with pytest.raises(AccessDenied) as caught:
                if action == "delete":
                    await soft_delete_user(session, actor=actor, user_id=target_id)
                elif action == "unsuspend":
                    await set_user_suspended(
                        session, actor=actor, user_id=target_id, suspended=False
                    )
                else:
                    await update_user_identity(
                        session,
                        actor=actor,
                        user_id=target_id,
                        body=AdminUserIdentityUpdate(first_name="Gone"),
                    )
            assert not isinstance(caught.value, NotFound)
    finally:
        async with AsyncSession(engine) as session, session.begin():
            await _restore_parked_admins(session, extras, ids)
        await engine.dispose()


async def test_suspended_admin_does_not_count_as_active(
    session_client: Any, db_session: AsyncSession
) -> None:
    actor = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="k11-actor"
    )
    target = await _signup(session_client, email=unique_email("k11-target"))
    suspended = await _signup(session_client, email=unique_email("k11-sus"))
    await _set_role(db_session, target["id"], SystemRole.ADMIN)
    await _set_role(db_session, suspended["id"], SystemRole.ADMIN)
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": actor["email"], "password": PASSWORD},
    )
    parked = await session_client.put(
        f"/api/v1/admin/users/{suspended['id']}/suspension",
        json={"suspended": True},
    )
    assert parked.status_code == 200
    await _demote_foreign_admins(
        db_session, {actor["id"], target["id"], suspended["id"]}
    )
    demoted = await session_client.put(
        f"/api/v1/admin/users/{target['id']}/admin",
        json={"isAdmin": False},
    )
    assert demoted.status_code == 200
    still_actor = await db_session.get(User, actor["id"])
    still_target = await db_session.get(User, target["id"])
    assert still_actor is not None
    assert still_target is not None
    assert still_actor.is_admin is True
    assert still_target.is_admin is False
    active_ids = set(
        await db_session.scalars(
            select(User.id).where(
                _active_admin_clause(),
                User.id.in_((actor["id"], target["id"], suspended["id"])),
            )
        )
    )
    assert active_ids == {actor["id"]}


async def test_self_suspend_is_cannot_modify_self_even_with_other_admins(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="ss-admin"
    )
    other = await _signup(session_client, email=unique_email("ss-other"))
    await _set_role(db_session, other["id"], SystemRole.ADMIN)
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    response = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}/suspension",
        json={"suspended": True},
    )
    assert response.status_code == 409
    assert response.json() == {"error": "cannot_modify_self"}
    user = await db_session.get(User, admin["id"])
    assert user is not None
    assert user.suspended_at is None


async def test_delete_revokes_sessions_so_restore_is_unauthorized(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="rev-admin"
    )
    victim = await _signup(session_client, email=unique_email("rev-victim"))
    victim_token = victim["token"]
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    deleted = await session_client.delete(f"/api/v1/admin/users/{victim['id']}")
    assert deleted.status_code == 204
    sessions = list(
        await db_session.scalars(
            select(AuthSession).where(AuthSession.user_id == victim["id"])
        )
    )
    assert sessions
    assert all(row.revoked_at is not None for row in sessions)
    user = await db_session.get(User, victim["id"])
    assert user is not None
    user.deleted_at = None
    await db_session.flush()
    session_client.cookies.clear()
    me = await session_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {victim_token}"},
    )
    assert me.status_code == 401
    assert me.json() == {"error": "unauthorized"}
    assert victim_token not in me.text


async def test_unsuspend_keeps_old_token_valid(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="tok-admin"
    )
    victim = await _signup(session_client, email=unique_email("tok-victim"))
    victim_token = victim["token"]
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    suspended = await session_client.put(
        f"/api/v1/admin/users/{victim['id']}/suspension",
        json={"suspended": True},
    )
    assert suspended.status_code == 200
    restored = await session_client.put(
        f"/api/v1/admin/users/{victim['id']}/suspension",
        json={"suspended": False},
    )
    assert restored.status_code == 200
    session_client.cookies.clear()
    me = await session_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {victim_token}"},
    )
    assert me.status_code == 200
    assert me.json()["user"]["id"] == str(victim["id"])


async def test_teacher_search_limits_and_hides_email_except_exact(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    teacher = await _signup(session_client, email=unique_email("tea-lim"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    emails: list[str] = []
    for index in range(12):
        row = User(
            first_name=f"Tq{marker}",
            last_name=f"Hit{index:02d}",
            email=unique_email(f"{marker}-t{index}"),
            password_hash="x",
            system_role_id=SystemRole.ALUMNO,
        )
        db_session.add(row)
        emails.append(row.email)
    await db_session.flush()

    short = await session_client.get("/api/v1/users/search", params={"q": "ab"})
    assert short.status_code == 400
    assert short.json()["error"] == "validation"
    combining = await session_client.get(
        "/api/v1/users/search", params={"q": "e\u0323\u0302"}
    )
    assert combining.status_code == 400
    assert combining.json()["error"] == "validation"
    wild = await session_client.get("/api/v1/users/search", params={"q": "%%%"})
    assert wild.status_code == 400
    at_sign = await session_client.get("/api/v1/users/search", params={"q": "@"})
    assert at_sign.status_code == 400

    by_name = await session_client.get(
        "/api/v1/users/search", params={"q": f"Tq{marker}", "pageSize": 20}
    )
    assert by_name.status_code == 200
    body = by_name.json()
    assert body["total"] == 10
    assert len(body["items"]) == 10
    assert all("email" not in item for item in body["items"])

    exact = emails[0]
    by_email = await session_client.get(
        "/api/v1/users/search", params={"q": exact.upper()}
    )
    assert by_email.status_code == 200
    found = by_email.json()["items"]
    assert len(found) == 1
    assert found[0]["email"] == exact
    partial = exact.split("@")[0]
    hidden = await session_client.get("/api/v1/users/search", params={"q": partial})
    assert hidden.status_code == 200
    assert hidden.json()["items"] == []


async def test_deleted_teacher_membership_cannot_search(
    session_client: Any, db_session: AsyncSession
) -> None:
    teacher = await _signup(session_client, email=unique_email("tea-del"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    membership = await db_session.scalar(
        select(CourseMembership).where(CourseMembership.user_id == teacher["id"])
    )
    assert membership is not None
    ok = await session_client.get("/api/v1/users/search", params={"q": "ada"})
    assert ok.status_code == 200
    membership.deleted_at = datetime.now(UTC)
    await db_session.flush()
    denied = await session_client.get("/api/v1/users/search", params={"q": "ada"})
    assert denied.status_code == 403
    assert denied.json() == {"error": "forbidden"}


async def test_admin_and_search_query_reject_overlong_and_strict_bool(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="len-admin"
    )
    too_long = "a" * 201
    listed = await session_client.get("/api/v1/admin/users", params={"query": too_long})
    assert listed.status_code == 400
    assert listed.json()["error"] == "validation"
    search = await session_client.get("/api/v1/users/search", params={"q": too_long})
    assert search.status_code == 400
    assert search.json()["error"] == "validation"
    fitted = await session_client.get(
        "/api/v1/admin/users", params={"query": "a" * 200}
    )
    assert fitted.status_code == 200
    yes_admin = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}/admin",
        json={"isAdmin": "yes"},
    )
    assert yes_admin.status_code == 400
    assert yes_admin.json()["error"] == "validation"
    yes_sus = await session_client.put(
        f"/api/v1/admin/users/{admin['id']}/suspension",
        json={"suspended": "yes"},
    )
    assert yes_sus.status_code == 400
    assert yes_sus.json()["error"] == "validation"


async def test_nul_bytes_in_user_text_fields_are_validation(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="nul-admin"
    )
    listed = await session_client.get(
        "/api/v1/admin/users", params={"query": "ada\x00"}
    )
    assert listed.status_code == 400
    assert listed.json()["error"] == "validation"
    search = await session_client.get("/api/v1/users/search", params={"q": "ada\x00"})
    assert search.status_code == 400
    assert search.json()["error"] == "validation"
    for field, value in (
        ("firstName", "Ada\x00"),
        ("lastName", "Lovelace\x00"),
        ("email", "ada\x00@example.com"),
    ):
        response = await session_client.put(
            f"/api/v1/admin/users/{admin['id']}",
            json={field: value},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "validation"


async def test_teacher_exact_email_uses_lower_not_casefold(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    teacher = await _signup(session_client, email=unique_email("tea-esz"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    email = f"{marker}ßname@example.com"
    db_session.add(
        User(
            first_name="Eszett",
            last_name=marker,
            email=email,
            password_hash="x",
            system_role_id=SystemRole.ALUMNO,
        )
    )
    await db_session.flush()
    found = await session_client.get("/api/v1/users/search", params={"q": email})
    assert found.status_code == 200
    items = found.json()["items"]
    assert len(items) == 1
    assert items[0]["email"].lower() == email.lower()
    folded = await session_client.get(
        "/api/v1/users/search", params={"q": email.casefold()}
    )
    assert folded.status_code == 200
    assert folded.json()["items"] == []


async def test_teacher_search_normalizes_istanbul_email_like_login(
    session_client: Any, db_session: AsyncSession
) -> None:
    typed = "\u0130stanbul"
    assert typed[0] == "\u0130"
    assert "\u0307" not in typed
    marker = uuid.uuid4().hex[:8]
    typed_email = f"{typed}-{marker}@example.com"
    stored = normalize_email(typed_email)
    assert isinstance(stored, str)
    teacher = await _signup(session_client, email=unique_email("tea-ist"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    db_session.add(
        User(
            first_name="Istanbul",
            last_name=marker,
            email=stored,
            password_hash="x",
            system_role_id=SystemRole.ALUMNO,
        )
    )
    await db_session.flush()
    assert _email_term(typed_email) == stored
    found = await session_client.get("/api/v1/users/search", params={"q": typed_email})
    assert found.status_code == 200
    items = found.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == stored

    session_client.cookies.clear()
    await _signup_role(session_client, db_session, SystemRole.ADMIN, prefix="ist-admin")
    admin_search = await session_client.get(
        "/api/v1/users/search", params={"q": typed_email}
    )
    assert admin_search.status_code == 200
    admin_items = admin_search.json()["items"]
    assert len(admin_items) == 1
    assert admin_items[0]["email"] == stored
    listed = await session_client.get(
        "/api/v1/admin/users", params={"query": typed_email}
    )
    assert listed.status_code == 200
    listed_items = listed.json()["items"]
    assert len(listed_items) == 1
    assert listed_items[0]["email"] == stored


@pytest.mark.parametrize("local_part", ("ñandú", "σοφία", "émile"))
async def test_search_nfd_email_matches_nfc_stored(
    session_client: Any, db_session: AsyncSession, local_part: str
) -> None:
    nfc_local = unicodedata.normalize("NFC", local_part)
    nfd_local = unicodedata.normalize("NFD", local_part)
    assert nfc_local != nfd_local
    marker = uuid.uuid4().hex[:8]
    raw_stored = normalize_email(f"{nfc_local}-{marker}@example.com")
    assert isinstance(raw_stored, str)
    stored = unicodedata.normalize("NFC", raw_stored)
    typed_nfd = unicodedata.normalize("NFD", stored)
    assert typed_nfd != stored
    assert _email_term(typed_nfd) == stored

    teacher = await _signup(session_client, email=unique_email("tea-nfd"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    db_session.add(
        User(
            first_name="Nfd",
            last_name=marker,
            email=stored,
            password_hash="x",
            system_role_id=SystemRole.ALUMNO,
        )
    )
    await db_session.flush()

    found = await session_client.get("/api/v1/users/search", params={"q": typed_nfd})
    assert found.status_code == 200
    items = found.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == stored

    session_client.cookies.clear()
    await _signup_role(session_client, db_session, SystemRole.ADMIN, prefix="nfd-admin")
    admin_search = await session_client.get(
        "/api/v1/users/search", params={"q": typed_nfd}
    )
    assert admin_search.status_code == 200
    assert admin_search.json()["items"][0]["email"] == stored
    listed = await session_client.get(
        "/api/v1/admin/users", params={"query": typed_nfd}
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["email"] == stored


@pytest.mark.parametrize("domain", ("ﬁle-r95.es", "ｒ９５.es"))
async def test_search_idna_domain_matches_login_typed(
    session_client: Any, db_session: AsyncSession, domain: str
) -> None:
    marker = uuid.uuid4().hex[:8]
    typed = f"{marker}@{domain}"
    created = await _signup(session_client, email=typed, name=f"Idna {marker}")
    stored = created["response"].json()["user"]["email"]
    expected = f"{marker}@file-r95.es" if domain == "ﬁle-r95.es" else f"{marker}@r95.es"
    assert stored == expected
    assert stored != typed
    session_client.cookies.clear()

    teacher = await _signup(session_client, email=unique_email("tea-idna"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    found = await session_client.get("/api/v1/users/search", params={"q": typed})
    assert found.status_code == 200
    items = found.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == stored
    assert items[0]["id"] == str(created["id"])

    session_client.cookies.clear()
    await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="idna-admin"
    )
    admin_search = await session_client.get("/api/v1/users/search", params={"q": typed})
    assert admin_search.status_code == 200
    assert admin_search.json()["items"][0]["email"] == stored
    listed = await session_client.get("/api/v1/admin/users", params={"query": typed})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["email"] == stored


async def test_search_j_caron_email_matches_login_nfc(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    typed = f"J\u030cX-{marker}@example.com"
    stored = f"\u01f0x-{marker}@example.com"
    created = await _signup(session_client, email=typed, name=f"Jcaron {marker}")
    assert created["response"].json()["user"]["email"] == stored
    session_client.cookies.clear()

    teacher = await _signup(session_client, email=unique_email("tea-jcaron"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    found = await session_client.get("/api/v1/users/search", params={"q": typed})
    assert found.status_code == 200
    items = found.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == stored
    assert items[0]["id"] == str(created["id"])


async def test_accented_names_nfc_and_nfd_appear_in_search(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    nfc_first = f"Émile{marker}"
    nfd_first = unicodedata.normalize("NFD", nfc_first)
    assert nfd_first != nfc_first

    signup_nfd = await _signup(
        session_client,
        email=unique_email("nm-nfd"),
        name=f"{nfd_first} Nfd",
    )
    assert signup_nfd["response"].json()["user"]["firstName"] == nfc_first
    stored_nfd = await db_session.get(User, signup_nfd["id"])
    assert stored_nfd is not None
    assert stored_nfd.first_name == nfc_first

    signup_nfc = await _signup(
        session_client,
        email=unique_email("nm-nfc"),
        name=f"{nfc_first} Nfc",
    )
    assert signup_nfc["response"].json()["user"]["firstName"] == nfc_first

    session_client.cookies.clear()
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="nm-admin"
    )
    put_nfd = await _signup(session_client, email=unique_email("nm-put-nfd"))
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    edited = await session_client.put(
        f"/api/v1/admin/users/{put_nfd['id']}",
        json={"firstName": nfd_first, "lastName": "PutNfd"},
    )
    assert edited.status_code == 200
    assert edited.json()["firstName"] == nfc_first
    db_session.expire_all()
    put_row = await db_session.get(User, put_nfd["id"])
    assert put_row is not None
    assert put_row.first_name == nfc_first

    put_nfc = await _signup(session_client, email=unique_email("nm-put-nfc"))
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    edited_nfc = await session_client.put(
        f"/api/v1/admin/users/{put_nfc['id']}",
        json={"firstName": nfc_first, "lastName": "PutNfc"},
    )
    assert edited_nfc.status_code == 200
    assert edited_nfc.json()["firstName"] == nfc_first

    leftover = User(
        first_name=nfd_first,
        last_name=f"Legacy{marker}",
        email=unique_email("nm-legacy"),
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
    )
    db_session.add(leftover)
    await db_session.flush()
    assert leftover.first_name == nfd_first

    for query in (nfc_first, nfd_first):
        found = await session_client.get(
            "/api/v1/users/search", params={"q": query, "pageSize": 20}
        )
        assert found.status_code == 200
        ids = {item["id"] for item in found.json()["items"]}
        assert str(signup_nfd["id"]) in ids
        assert str(signup_nfc["id"]) in ids
        assert str(put_nfd["id"]) in ids
        assert str(put_nfc["id"]) in ids
        assert str(leftover.id) in ids
        listed = await session_client.get(
            "/api/v1/admin/users", params={"query": query, "pageSize": 20}
        )
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert str(leftover.id) in listed_ids


def test_email_term_fallback_applies_nfc_after_strip_lower() -> None:
    assert _email_term("  J\u030cX  ") == "\u01f0x"
    assert _email_term("J\u030cX") == "\u01f0x"
    assert _email_term("j\u030cx") == "\u01f0x"


async def test_admin_search_fallback_nfd_local_finds_nfc_email(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    typed = f"J\u030cX-{marker}@example.com"
    stored = f"\u01f0x-{marker}@example.com"
    created = await _signup(session_client, email=typed, name=f"Fb {marker}")
    assert created["response"].json()["user"]["email"] == stored
    await _set_role(db_session, created["id"], SystemRole.ADMIN)
    fragment = f"J\u030cX-{marker}"
    found = await session_client.get("/api/v1/users/search", params={"q": fragment})
    assert found.status_code == 200
    items = found.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == stored
    listed = await session_client.get("/api/v1/admin/users", params={"query": fragment})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["email"] == stored


async def test_teacher_exact_email_query_uses_unique_index(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    dsn = RedactedDsn(resolve_test_database_url())
    engine = create_async_engine(dsn.secret())
    email = _email_term("Explain-Unique@example.com")
    stmt = select(User).where(*_visible_user_filters(), _exact_email_clause(email))
    compiled = stmt.compile(
        dialect=engine.sync_engine.dialect,
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "users.email =" in sql
    assert "ILIKE" not in sql.upper()
    assert "deleted_at IS NULL" in sql
    assert "suspended_at IS NULL" in sql
    try:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SET LOCAL enable_seqscan = off"))
            rows = await conn.execute(text("EXPLAIN " + sql))
            plan = "\n".join(str(row[0]) for row in rows)
        assert "Index Scan using uq_users_email" in plan
        assert "Seq Scan" not in plan
    finally:
        await engine.dispose()


async def test_teacher_exact_email_hides_suspended_and_deleted(
    session_client: Any, db_session: AsyncSession
) -> None:
    teacher = await _signup(session_client, email=unique_email("tea-e7"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    suspended = User(
        first_name="E7",
        last_name="Suspended",
        email=unique_email("e7-sus"),
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
        suspended_at=datetime.now(UTC),
    )
    deleted = User(
        first_name="E7",
        last_name="Deleted",
        email=unique_email("e7-gone"),
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
        deleted_at=datetime.now(UTC),
    )
    db_session.add_all([suspended, deleted])
    await db_session.flush()
    for hidden in (suspended, deleted):
        found = await session_client.get(
            "/api/v1/users/search", params={"q": hidden.email}
        )
        assert found.status_code == 200
        assert found.json()["items"] == []
        assert found.json()["total"] == 0
        assert hidden.email not in found.text


async def test_teacher_merged_search_caps_at_ten(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    query = f"e6merge-{marker}@example.com"
    teacher = await _signup(session_client, email=unique_email("tea-e6"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    exact = User(
        first_name="NopeExact",
        last_name="E6",
        email=query,
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
    )
    db_session.add(exact)
    await db_session.flush()
    for index in range(12):
        db_session.add(
            User(
                first_name=query,
                last_name=f"N{index:02d}",
                email=unique_email(f"e6-n{index}"),
                password_hash="x",
                system_role_id=SystemRole.ALUMNO,
            )
        )
        await db_session.flush()
    found = await session_client.get(
        "/api/v1/users/search", params={"q": query, "pageSize": 20}
    )
    assert found.status_code == 200
    body = found.json()
    assert body["total"] == 10
    assert len(body["items"]) == 10
    assert body["items"][0]["id"] == str(exact.id)
    assert body["items"][0]["email"] == query


async def test_teacher_merged_search_order(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    query = f"e9ord-{marker}@example.com"
    teacher = await _signup(session_client, email=unique_email("tea-e9"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    exact = User(
        first_name="NopeExact",
        last_name="E9",
        email=query,
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
    )
    db_session.add(exact)
    await db_session.flush()
    name_ids: list[str] = []
    for index in range(3):
        row = User(
            first_name=query,
            last_name=f"N{index}",
            email=unique_email(f"e9-n{index}"),
            password_hash="x",
            system_role_id=SystemRole.ALUMNO,
        )
        db_session.add(row)
        await db_session.flush()
        name_ids.append(str(row.id))
    found = await session_client.get("/api/v1/users/search", params={"q": query})
    assert found.status_code == 200
    body = found.json()
    assert body["total"] == 4
    ids = [item["id"] for item in body["items"]]
    assert ids == [str(exact.id), *reversed(name_ids)]


async def test_teacher_exact_also_matching_name_appears_once(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    query = f"e11both-{marker}@example.com"
    teacher = await _signup(session_client, email=unique_email("tea-e11"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    exact = User(
        first_name=query,
        last_name="Both",
        email=query,
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
    )
    other = User(
        first_name=query,
        last_name="NameOnly",
        email=unique_email("e11-name"),
        password_hash="x",
        system_role_id=SystemRole.ALUMNO,
    )
    db_session.add_all([exact, other])
    await db_session.flush()
    found = await session_client.get("/api/v1/users/search", params={"q": query})
    assert found.status_code == 200
    body = found.json()
    ids = [item["id"] for item in body["items"]]
    assert ids.count(str(exact.id)) == 1
    assert ids[0] == str(exact.id)
    assert str(other.id) in ids
    assert body["total"] == 2
    assert len(body["items"]) == 2


async def test_search_compatibility_local_part_matches_login(
    session_client: Any, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:8]
    typed = f"ﬁle-{marker}@example.com"
    assert "\ufb01" in typed
    created = await _signup(session_client, email=typed, name=f"Nfc3 {marker}")
    stored = created["response"].json()["user"]["email"]
    assert stored == typed
    assert stored == f"ﬁle-{marker}@example.com"
    session_client.cookies.clear()

    teacher = await _signup(session_client, email=unique_email("tea-nfc3"))
    await _set_role(db_session, teacher["id"], SystemRole.PROFESOR)
    await _enroll(db_session, teacher["id"], _COURSE, CourseRole.TEACHER)
    found = await session_client.get("/api/v1/users/search", params={"q": typed})
    assert found.status_code == 200
    items = found.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == stored
    assert items[0]["id"] == str(created["id"])


async def test_deactivated_alumno_cannot_demote_admin(
    session_client: Any,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quitar admin pasa por assign_system_role: 409 role_not_assignable, no 500."""

    assigned: list[str] = []

    async def _spy(
        session: AsyncSession,
        *,
        actor: User,
        target: User,
        role_id: str,
    ) -> User:
        assigned.append(role_id)
        return await assign_system_role(
            session, actor=actor, target=target, role_id=role_id
        )

    monkeypatch.setattr("app.services.users.assign_system_role", _spy)
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="rol-admin"
    )
    target = await _signup(session_client, email=unique_email("rol-target"))
    await _set_role(db_session, target["id"], SystemRole.ADMIN)
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    alumno = await db_session.get(SystemRoleRow, SystemRole.ALUMNO)
    assert alumno is not None
    alumno.active = False
    await db_session.flush()
    response = await session_client.put(
        f"/api/v1/admin/users/{target['id']}/admin",
        json={"isAdmin": False},
    )
    assert response.status_code != 500
    assert response.status_code == 409
    assert response.json() == {"error": "role_not_assignable"}
    assert assigned == [SystemRole.ALUMNO]
    still = await db_session.get(User, target["id"])
    assert still is not None
    assert still.system_role_id == SystemRole.ADMIN
    assert still.is_admin is True


async def test_deleted_email_stays_blocked(
    session_client: Any, db_session: AsyncSession
) -> None:
    admin = await _signup_role(
        session_client, db_session, SystemRole.ADMIN, prefix="blk-admin"
    )
    victim = await _signup(session_client, email=unique_email("blk-victim"))
    victim_email = victim["email"]
    await session_client.post(
        "/api/v1/auth/login",
        json={"email": admin["email"], "password": PASSWORD},
    )
    gone = await session_client.delete(f"/api/v1/admin/users/{victim['id']}")
    assert gone.status_code == 204
    session_client.cookies.clear()
    again = await session_client.post(
        "/api/v1/auth/signup",
        json={
            "name": "Reuse Email",
            "email": victim_email,
            "password": PASSWORD,
        },
    )
    assert again.status_code == 409
    assert again.json() == {"error": "email_taken"}
