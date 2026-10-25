"""Administración de usuarios con APP_ENV=production contra la base *_test."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import RedactedDsn
from tests.test_auth import PASSWORD, signup_body, unique_email
from tests.test_roles_production import _promote_to_admin

pytestmark = pytest.mark.db

_LEAK_MARKERS = ("password12", "argon2", "password_hash", "passwordHash")


def test_production_alumno_is_forbidden_on_admin_users(
    production_client: TestClient,
) -> None:
    email = unique_email("prod-user-denied")
    created = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert created.status_code == 201
    listed = production_client.get("/api/v1/admin/users")
    assert listed.status_code == 403
    assert listed.json() == {"error": "forbidden"}
    assert email not in listed.text
    for marker in _LEAK_MARKERS:
        assert marker not in listed.text


def test_production_admin_lists_and_cannot_modify_self(
    production_client: TestClient,
    production_test_database_url: RedactedDsn,
) -> None:
    admin_email = unique_email("prod-user-admin")
    created = production_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Prod Admin", email=admin_email),
    )
    assert created.status_code == 201
    admin_id = created.json()["user"]["id"]
    _promote_to_admin(production_test_database_url, admin_email)

    listed = production_client.get(
        "/api/v1/admin/users",
        params={"query": admin_email, "page": 1, "pageSize": 20},
    )
    assert listed.status_code == 200
    body = listed.json()
    assert set(body) == {"items", "page", "pageSize", "total"}
    assert body["total"] >= 1
    assert any(item["email"] == admin_email for item in body["items"])
    assert "passwordHash" not in body["items"][0]
    for marker in _LEAK_MARKERS:
        assert marker not in listed.text

    demote = production_client.put(
        f"/api/v1/admin/users/{admin_id}/admin",
        json={"isAdmin": False},
    )
    assert demote.status_code == 409
    assert demote.json() == {"error": "cannot_modify_self"}

    deleted = production_client.delete(f"/api/v1/admin/users/{admin_id}")
    assert deleted.status_code == 409
    assert deleted.json() == {"error": "cannot_modify_self"}


def test_production_email_taken_and_suspended_session(
    production_client: TestClient,
    production_test_database_url: RedactedDsn,
) -> None:
    admin_email = unique_email("prod-user-taken-admin")
    admin = production_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Prod Admin", email=admin_email),
    )
    assert admin.status_code == 201
    _promote_to_admin(production_test_database_url, admin_email)

    other_email = unique_email("prod-user-taken")
    other = production_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Other User", email=other_email),
    )
    assert other.status_code == 201
    other_id = other.json()["user"]["id"]
    other_token = other.cookies["session"]

    production_client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": PASSWORD},
    )
    taken = production_client.put(
        f"/api/v1/admin/users/{admin.json()['user']['id']}",
        json={"email": other_email},
    )
    assert taken.status_code == 409
    assert taken.json() == {"error": "email_taken"}
    assert other_id not in taken.text

    suspended = production_client.put(
        f"/api/v1/admin/users/{other_id}/suspension",
        json={"suspended": True},
    )
    assert suspended.status_code == 200
    assert suspended.json()["suspendedAt"] is not None
    try:
        me = production_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert me.status_code == 403
        assert me.json() == {"error": "account_suspended"}
        assert other_token not in me.text
        for marker in _LEAK_MARKERS:
            assert marker not in me.text
    finally:
        restored = production_client.put(
            f"/api/v1/admin/users/{other_id}/suspension",
            json={"suspended": False},
        )
        assert restored.status_code == 200


def test_production_unauthorized_without_session(
    production_client: TestClient,
) -> None:
    production_client.cookies.clear()
    listed = production_client.get("/api/v1/admin/users")
    assert listed.status_code == 401
    assert listed.json() == {"error": "unauthorized"}
    search = production_client.get("/api/v1/users/search", params={"q": "ada"})
    assert search.status_code == 401
    assert search.json() == {"error": "unauthorized"}


def test_production_search_does_not_list_deleted(
    production_client: TestClient,
    production_test_database_url: RedactedDsn,
) -> None:
    admin_email = unique_email("prod-search-admin")
    admin = production_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Prod Admin", email=admin_email),
    )
    assert admin.status_code == 201
    _promote_to_admin(production_test_database_url, admin_email)

    marker = uuid.uuid4().hex[:12]
    visible_email = unique_email(f"{marker}-ok")
    deleted_email = unique_email(f"{marker}-gone")
    visible = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=visible_email)
    )
    deleted = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=deleted_email)
    )
    assert visible.status_code == 201
    assert deleted.status_code == 201
    production_client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": PASSWORD},
    )
    gone = production_client.delete(
        f"/api/v1/admin/users/{deleted.json()['user']['id']}"
    )
    assert gone.status_code == 204
    found = production_client.get("/api/v1/users/search", params={"q": marker})
    assert found.status_code == 200
    emails = {item["email"] for item in found.json()["items"]}
    assert emails == {visible_email}
    assert deleted_email not in found.text
