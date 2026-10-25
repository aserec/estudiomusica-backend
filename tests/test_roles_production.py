"""Roles y permisos con APP_ENV=production contra la base *_test."""

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import RedactedDsn
from tests.conftest import PRODUCTION_AUTH_SECRET
from tests.test_auth import set_cookie_header, signup_body, unique_email

pytestmark = pytest.mark.db

_COURSE = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _run_sql(dsn: RedactedDsn, sql: str, params: dict[str, object]) -> None:
    async def run() -> None:
        engine = create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(run())


def _promote_to_admin(dsn: RedactedDsn, email: str) -> None:
    _run_sql(
        dsn,
        "UPDATE users SET system_role_id = 'admin', is_admin = true "
        "WHERE email = :email",
        {"email": email},
    )


@contextmanager
def _temporarily_deactivate_role(dsn: RedactedDsn, role_id: str) -> Iterator[None]:
    """Cambia ``active`` en la base compartida y siempre lo restaura."""

    _run_sql(
        dsn,
        "UPDATE system_roles SET active = false WHERE id = :id",
        {"id": role_id},
    )
    try:
        yield
    finally:
        _run_sql(
            dsn,
            "UPDATE system_roles SET active = true WHERE id = :id",
            {"id": role_id},
        )


def _assert_production_session(response: object, client: TestClient) -> None:
    """Aserciones que fallan si el cliente corre con APP_ENV=development."""

    settings = get_settings()
    assert settings.app_env == "production"
    assert settings.auth_cookie_secure() is True
    cookie = set_cookie_header(response)
    assert "session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "samesite=lax" in cookie.lower()
    assert PRODUCTION_AUTH_SECRET not in getattr(response, "text", "")
    boom = client.get("/api/v1/_test/boom")
    assert boom.status_code == 500
    assert boom.json() == {"error": "internal_error"}
    assert "message" not in boom.json()
    assert "Traceback" not in boom.text
    assert PRODUCTION_AUTH_SECRET not in boom.text
    assert client.get("/api/v1/examples").status_code == 404


def test_production_signup_assigns_alumno(production_client: TestClient) -> None:
    email = unique_email("prod-alumno")
    created = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert created.status_code == 201
    _assert_production_session(created, production_client)
    me = production_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["systemRole"] == "alumno"
    assert me.json()["isAdmin"] is False


def test_production_denies_admin_and_course_by_default(
    production_client: TestClient,
) -> None:
    email = unique_email("prod-denied")
    created = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert created.status_code == 201
    _assert_production_session(created, production_client)
    admin = production_client.get("/api/v1/admin/system-roles")
    assert admin.status_code == 403
    assert admin.json() == {"error": "forbidden"}
    assert "message" not in admin.json()
    course = production_client.get(f"/api/v1/courses/{_COURSE}/me/permissions")
    assert course.status_code == 403
    assert course.json() == {"error": "forbidden"}
    assert "message" not in course.json()
    assert email not in admin.text


def test_production_inactive_alumno_blocks_signup_with_409(
    production_client: TestClient,
    production_test_database_url: RedactedDsn,
) -> None:
    admin_email = unique_email("prod-admin")
    created = production_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Prod Admin", email=admin_email),
    )
    assert created.status_code == 201
    _assert_production_session(created, production_client)
    _promote_to_admin(production_test_database_url, admin_email)

    listed = production_client.get("/api/v1/admin/system-roles")
    assert listed.status_code == 200

    with _temporarily_deactivate_role(production_test_database_url, "alumno"):
        blocked = production_client.post(
            "/api/v1/auth/signup",
            json=signup_body(email=unique_email("prod-blocked")),
        )
        assert blocked.status_code == 409
        assert blocked.json() == {"error": "role_not_assignable"}
        assert "message" not in blocked.json()
        assert "password12" not in blocked.text
        assert PRODUCTION_AUTH_SECRET not in blocked.text
