import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_app_env_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prodd")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings()
    get_settings.cache_clear()


def test_app_env_accepts_allowed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    settings = Settings()
    assert settings.app_env == "staging"
    get_settings.cache_clear()


def test_expose_errors_defaults_to_false() -> None:
    settings = Settings()
    assert settings.expose_errors is False


def test_cors_wildcard_rejected_with_credentials() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        Settings(cors_origins="*", cors_allow_credentials=True)


def test_cors_non_http_origin_rejected_with_credentials() -> None:
    with pytest.raises(ValidationError, match="http"):
        Settings(cors_origins="ftp://evil.example", cors_allow_credentials=True)


def test_cors_origin_without_scheme_rejected_with_credentials() -> None:
    with pytest.raises(ValidationError, match="http"):
        Settings(cors_origins="localhost:3000", cors_allow_credentials=True)


def test_cors_wildcard_allowed_without_credentials() -> None:
    settings = Settings(cors_origins="*", cors_allow_credentials=False)
    assert settings.cors_origin_list() == ["*"]


def test_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:secret@db.example:5432/app",
    )
    get_settings.cache_clear()
    settings = Settings()
    assert settings.database_url == (
        "postgresql+asyncpg://user:secret@db.example:5432/app"
    )
    get_settings.cache_clear()


def test_database_url_defaults_to_docker_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "127.0.0.1:5432" in settings.database_url
    assert settings.database_url.endswith("/estudiomusica")
