import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import clear_engine
from app.main import create_app

SECRET_PASSWORD = "supersecret-password-do-not-leak"


def test_health_db_ok_when_postgres_is_up(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/api/v1/health/db")

    assert response.status_code == 200
    assert response.json() == {"db": "ok"}
    assert response.headers["content-type"].startswith("application/json")


def test_root_health_db_alias(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"db": "ok"}


def test_health_db_down_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://leaky_user:{SECRET_PASSWORD}@127.0.0.1:1/no_such_db",
    )
    get_settings.cache_clear()
    clear_engine()
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/db")
    get_settings.cache_clear()
    clear_engine()

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://leaky_user:{SECRET_PASSWORD}@127.0.0.1:1/no_such_db",
    )
    get_settings.cache_clear()
    clear_engine()
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/db")
    get_settings.cache_clear()
    clear_engine()

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert list(response.json().keys()) == ["db"]
