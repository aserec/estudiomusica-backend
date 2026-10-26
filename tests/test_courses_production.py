"""Cursos con APP_ENV=production contra la base *_test."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import RedactedDsn
from tests.test_auth import signup_body, unique_email

pytestmark = pytest.mark.db

_ADMIN = "/api/v1/admin/courses"
_COURSES = "/api/v1/courses"


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


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "shortName": f"p-{uuid.uuid4().hex[:10]}",
        "name": "Prod curso",
        "startDate": "2026-09-01",
        "visible": True,
        "level": "beginner",
        "defaultGroupMode": "none",
    }
    body.update(overrides)
    return body


def test_production_course_endpoints_without_session_are_unauthorized(
    production_client: TestClient,
) -> None:
    production_client.cookies.clear()
    listed = production_client.get(_ADMIN)
    assert listed.status_code == 401
    assert listed.json() == {"error": "unauthorized"}
    created = production_client.post(_ADMIN, json=_payload())
    assert created.status_code == 401
    assert created.json() == {"error": "unauthorized"}
    viewed = production_client.get(f"{_COURSES}/{uuid.uuid4()}")
    assert viewed.status_code == 401
    assert viewed.json() == {"error": "unauthorized"}


def test_production_alumno_cannot_create_course(
    production_client: TestClient,
) -> None:
    email = unique_email("prod-crs-alumno")
    created = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert created.status_code == 201
    denied = production_client.post(_ADMIN, json=_payload())
    assert denied.status_code == 403
    assert denied.json() == {"error": "forbidden"}
    assert email not in denied.text


def test_production_admin_course_crud(
    production_client: TestClient,
    production_test_database_url: RedactedDsn,
) -> None:
    admin_email = unique_email("prod-crs-admin")
    created = production_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Prod Course Admin", email=admin_email),
    )
    assert created.status_code == 201
    _promote_to_admin(production_test_database_url, admin_email)

    posted = production_client.post(
        _ADMIN,
        json=_payload(name="Prod Armonía", shortName=f"pa-{uuid.uuid4().hex[:8]}"),
    )
    assert posted.status_code == 201
    body = posted.json()
    course_id = body["id"]
    try:
        viewed = production_client.get(f"{_COURSES}/{course_id}")
        assert viewed.status_code == 200
        assert viewed.json()["name"] == "Prod Armonía"

        listed = production_client.get(_ADMIN, params={"page": 1, "pageSize": 20})
        assert listed.status_code == 200
        assert listed.json()["page"] == 1
        assert listed.json()["pageSize"] == 20
        assert course_id in [item["id"] for item in listed.json()["items"]]

        updated = production_client.put(
            f"{_COURSES}/{course_id}",
            json={
                "shortName": body["shortName"],
                "name": "Prod Armonía II",
                "summary": body["summary"],
                "categoryId": body["categoryId"],
                "startDate": body["startDate"],
                "endDate": body["endDate"],
                "visible": True,
                "level": "intermediate",
                "defaultGroupMode": "none",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Prod Armonía II"
        assert updated.json()["level"] == "intermediate"

        archived = production_client.put(
            f"{_COURSES}/{course_id}/archived", json={"archived": True}
        )
        assert archived.status_code == 200
        assert archived.json()["archivedAt"] is not None
        blocked = production_client.put(
            f"{_COURSES}/{course_id}",
            json={
                "shortName": body["shortName"],
                "name": "No",
                "visible": True,
                "level": "beginner",
                "defaultGroupMode": "none",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json() == {"error": "course_archived"}

        duplicated = production_client.post(
            f"{_ADMIN}/{course_id}/duplicate",
            json={
                "shortName": f"pd-{uuid.uuid4().hex[:8]}",
                "name": "Copia prod",
                "startDate": "2026-10-01",
            },
        )
        assert duplicated.status_code == 201
        copy_id = duplicated.json()["id"]
        copy_deleted = production_client.delete(f"{_ADMIN}/{copy_id}")
        assert copy_deleted.status_code == 204
    finally:
        deleted = production_client.delete(f"{_ADMIN}/{course_id}")
        assert deleted.status_code == 204
