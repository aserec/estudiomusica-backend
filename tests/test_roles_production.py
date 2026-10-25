"""Roles y permisos con APP_ENV=production contra la base *_test."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import RedactedDsn
from tests.test_auth import signup_body, unique_email

pytestmark = pytest.mark.db

_COURSE = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _promote_to_admin(dsn: RedactedDsn, email: str) -> None:
    async def run() -> None:
        engine = create_async_engine(dsn.secret())
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

    asyncio.run(run())


def test_production_signup_assigns_alumno(production_client: TestClient) -> None:
    email = unique_email("prod-alumno")
    created = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert created.status_code == 201
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
    admin = production_client.get("/api/v1/admin/system-roles")
    assert admin.status_code == 403
    assert admin.json() == {"error": "forbidden"}
    course = production_client.get(f"/api/v1/courses/{_COURSE}/me/permissions")
    assert course.status_code == 403
    assert course.json() == {"error": "forbidden"}
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
    _promote_to_admin(production_test_database_url, admin_email)

    listed = production_client.get("/api/v1/admin/system-roles")
    assert listed.status_code == 200

    deactivated = production_client.patch(
        "/api/v1/admin/system-roles/alumno",
        json={"active": False},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["active"] is False
    try:
        blocked = production_client.post(
            "/api/v1/auth/signup",
            json=signup_body(email=unique_email("prod-blocked")),
        )
        assert blocked.status_code == 409
        assert blocked.json() == {"error": "role_not_assignable"}
        assert "password12" not in blocked.text
    finally:
        restored = production_client.patch(
            "/api/v1/admin/system-roles/alumno",
            json={"active": True},
        )
        assert restored.status_code == 200
        assert restored.json()["active"] is True
