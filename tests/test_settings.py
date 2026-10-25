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


def test_resolve_test_database_url_rejects_non_test_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import resolve_test_database_url

    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://estudiomusica:secret@127.0.0.1:5432/estudiomusica",
    )
    with pytest.raises(pytest.UsageError, match="_test"):
        resolve_test_database_url()


def test_database_url_defaults_to_docker_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "127.0.0.1:5432" in settings.database_url
    assert settings.database_url.endswith("/estudiomusica")


_DEFAULT_DEV_DSN = (
    "postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica"
)


def test_default_database_credentials_rejected_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _DEFAULT_DEV_DSN)
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="credenciales"):
        Settings()
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="credenciales"):
        Settings()
    get_settings.cache_clear()


def test_default_database_credentials_allowed_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _DEFAULT_DEV_DSN)
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    settings = Settings()
    assert settings.database_url.endswith("/estudiomusica")
    get_settings.cache_clear()


def test_production_accepts_non_default_database_url() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app",
        _env_file=None,
    )
    assert settings.database_url.endswith("/app")


def test_production_accepts_default_credentials_on_test_database() -> None:
    settings = Settings(
        app_env="production",
        database_url=(
            "postgresql+asyncpg://estudiomusica:estudiomusica"
            "@127.0.0.1:5432/estudiomusica_test"
        ),
        _env_file=None,
    )
    assert settings.database_url.endswith("/estudiomusica_test")


_DEFAULT_DEV_AUTH_SECRET = "dev-insecure-auth-secret-change-me"


def test_default_auth_secret_rejected_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT_DEV_AUTH_SECRET)
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="AUTH_SECRET"):
        Settings()
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="AUTH_SECRET"):
        Settings()
    get_settings.cache_clear()


def test_short_auth_secret_rejected_outside_development() -> None:
    with pytest.raises(ValidationError, match="32"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app",
            auth_secret="too-short-to-be-safe",
            _env_file=None,
        )


def test_default_auth_secret_allowed_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT_DEV_AUTH_SECRET)
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    settings = Settings()
    assert settings.auth_secret == _DEFAULT_DEV_AUTH_SECRET
    assert settings.auth_cookie_secure() is False
    get_settings.cache_clear()


def test_production_auth_cookie_is_secure() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app",
        auth_secret="production-auth-secret-at-least-32ch",
        _env_file=None,
    )
    assert settings.auth_cookie_secure() is True


def test_assistant_media_flag_defaults_false() -> None:
    settings = Settings(_env_file=None)
    assert settings.assistant_media_and_sm3_visible is False


def test_assistant_media_flag_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_MEDIA_AND_SM3_VISIBLE", "true")
    get_settings.cache_clear()
    settings = Settings()
    assert settings.assistant_media_and_sm3_visible is True
    get_settings.cache_clear()


def test_bootstrap_admin_email_set_normalizes() -> None:
    settings = Settings(
        bootstrap_admin_emails=" Ada@Example.com , , other@example.com ",
        _env_file=None,
    )
    assert settings.bootstrap_admin_email_set() == {
        "ada@example.com",
        "other@example.com",
    }


def test_expose_errors_in_production_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.config import get_settings
    from app.main import create_app, get_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EXPOSE_ERRORS", "true")
    monkeypatch.setenv("ENABLE_EXAMPLE_ROUTES", "false")
    get_settings.cache_clear()
    get_app.cache_clear()
    try:
        with caplog.at_level("WARNING", logger="app.main"):
            create_app()
        assert "EXPOSE_ERRORS" in caplog.text
        assert "production" in caplog.text
    finally:
        get_settings.cache_clear()
        get_app.cache_clear()
