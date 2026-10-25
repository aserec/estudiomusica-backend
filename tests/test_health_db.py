import asyncio
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.services import health as health_service

SECRET_PASSWORD = "supersecret-password-do-not-leak"


def test_health_db_ok_when_postgres_is_up(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/api/v1/health/db")

    assert response.status_code == 200
    assert response.json() == {"db": "ok"}
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-api-version"] == "v1"


def test_root_health_db_alias(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"db": "ok"}


def test_health_db_ok_in_production_without_examples(
    production_client: TestClient,
) -> None:
    """health/db se monta siempre; /examples no.

    En production Settings rechaza bases ``*_test`` y la contraseña del
    compose, así que el cliente usa un DSN dummy: 503 es la sonda, no un
    sobre de error.
    """

    response = production_client.get("/api/v1/health/db")

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert production_client.get("/api/v1/examples").status_code == 404


def test_health_db_down_without_credentials(
    down_db_client: TestClient,
) -> None:
    response = down_db_client.get("/api/v1/health/db")

    assert response.status_code == 503
    body = response.json()
    assert body == {"db": "down"}
    assert "error" not in body
    text = response.text
    assert SECRET_PASSWORD not in text
    assert "leaky_user" not in text
    assert "DATABASE_URL" not in text
    assert "postgresql+asyncpg" not in text


def test_health_db_down_is_not_business_error_envelope(
    down_db_production_client: TestClient,
) -> None:
    response = down_db_production_client.get("/api/v1/health/db")

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert list(response.json().keys()) == ["db"]


def test_health_db_down_does_not_leak_with_expose_errors(
    down_db_expose_errors_client: TestClient,
) -> None:
    response = down_db_expose_errors_client.get("/api/v1/health/db")

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert SECRET_PASSWORD not in response.text
    assert "message" not in response.json()


class _HangingConnect:
    async def __aenter__(self) -> _HangingConnect:
        await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _HangingEngine:
    def connect(self) -> _HangingConnect:
        return _HangingConnect()


@pytest.fixture
def hang_timeout_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from tests.conftest import _build_client

    monkeypatch.setattr(health_service, "get_engine", lambda: _HangingEngine())
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_TIMEOUT="0.3",
    )


def test_health_db_returns_503_if_probe_hangs(
    hang_timeout_client: TestClient,
) -> None:
    started = time.monotonic()
    response = hang_timeout_client.get("/api/v1/health/db")
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert elapsed < 2.0


class _HangingClose:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def close(self) -> None:
        await asyncio.Event().wait()


class _HangingCloseEngine:
    async def connect(self) -> _HangingClose:
        return _HangingClose()


@pytest.fixture
def hang_close_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from tests.conftest import _build_client

    monkeypatch.setattr(health_service, "get_engine", lambda: _HangingCloseEngine())
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_TIMEOUT="0.3",
    )


def test_health_db_returns_503_if_close_hangs(
    hang_close_client: TestClient,
) -> None:
    started = time.monotonic()
    response = hang_close_client.get("/api/v1/health/db")
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert elapsed < 2.0


def test_health_db_ok_body_has_only_db_key(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/api/v1/health/db")
    assert response.status_code == 200
    assert list(response.json().keys()) == ["db"]
    assert response.json()["db"] == "ok"
