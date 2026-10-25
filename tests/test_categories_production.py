"""Categorías con APP_ENV=production contra la base *_test."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import RedactedDsn
from tests.test_auth import signup_body, unique_email

pytestmark = pytest.mark.db

_LIST = "/api/v1/categories"
_ADMIN = "/api/v1/admin/categories"


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


def test_production_alumno_can_list_but_not_create(
    production_client: TestClient,
) -> None:
    email = unique_email("prod-cat-alumno")
    created = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert created.status_code == 201
    listed = production_client.get(_LIST)
    assert listed.status_code == 200
    assert isinstance(listed.json(), list)
    denied = production_client.post(_ADMIN, json={"name": f"No-{uuid.uuid4()}"})
    assert denied.status_code == 403
    assert denied.json() == {"error": "forbidden"}
    assert email not in denied.text


def test_production_category_endpoints_without_session_are_unauthorized(
    production_client: TestClient,
) -> None:
    production_client.cookies.clear()
    listed = production_client.get(_LIST)
    assert listed.status_code == 401
    assert listed.json() == {"error": "unauthorized"}
    created = production_client.post(_ADMIN, json={"name": "X"})
    assert created.status_code == 401
    assert created.json() == {"error": "unauthorized"}


def test_production_admin_category_crud(
    production_client: TestClient,
    production_test_database_url: RedactedDsn,
) -> None:
    admin_email = unique_email("prod-cat-admin")
    created = production_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Prod Cat Admin", email=admin_email),
    )
    assert created.status_code == 201
    _promote_to_admin(production_test_database_url, admin_email)

    name = f"Prod-{uuid.uuid4()}"
    posted = production_client.post(_ADMIN, json={"name": name, "description": "demo"})
    assert posted.status_code == 201
    body = posted.json()
    assert body["name"] == name
    category_id = body["id"]
    try:
        listed = production_client.get(_LIST)
        assert listed.status_code == 200
        assert category_id in [item["id"] for item in listed.json()]

        updated = production_client.put(
            f"{_ADMIN}/{category_id}",
            json={"name": f"{name}-b", "description": "editado"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == f"{name}-b"
        assert updated.json()["description"] == "editado"
    finally:
        deleted = production_client.delete(f"{_ADMIN}/{category_id}")
        assert deleted.status_code == 204
