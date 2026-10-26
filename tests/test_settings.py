import logging

import pytest
from pydantic import ValidationError

from app.config import Settings, allow_test_database, get_settings

_DEFAULT_DEV_DSN = (
    "postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica"
)
_SECRET = "settings-secret-must-not-appear"


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
    assert settings.resolved_database_url() == (
        "postgresql+asyncpg://user:secret@db.example:5432/app"
    )
    get_settings.cache_clear()


def test_database_url_defaults_to_docker_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    dsn = settings.resolved_database_url()
    assert dsn.startswith("postgresql+asyncpg://")
    assert "127.0.0.1:5432" in dsn
    assert dsn.endswith("/estudiomusica")


def test_default_database_credentials_rejected_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _DEFAULT_DEV_DSN)
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="contraseña"):
        Settings()
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="contraseña"):
        Settings()
    get_settings.cache_clear()


def test_default_database_credentials_allowed_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _DEFAULT_DEV_DSN)
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    settings = Settings()
    assert settings.resolved_database_url().endswith("/estudiomusica")
    get_settings.cache_clear()


def test_production_rejects_default_password_on_other_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://estudiomusica:estudiomusica@db.example:5432/estudiomusica_prod",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="contraseña"):
        Settings()
    get_settings.cache_clear()


def test_production_rejects_default_user_with_other_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://estudiomusica:otra-clave@db.example:5432/app",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="usuario") as caught:
        Settings()
    assert "otra-clave" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_default_password_with_other_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:estudiomusica@db.example:5432/app",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="contraseña"):
        Settings()
    get_settings.cache_clear()


def test_production_rejects_test_database_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@127.0.0.1:5432/estudiomusica_test",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match=r"\*_test"):
        Settings()
    get_settings.cache_clear()


def test_allow_test_database_cannot_be_enabled_from_env_or_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@127.0.0.1:5432/estudiomusica_test",
    )
    monkeypatch.setenv("ALLOW_TEST_DATABASE", "true")
    monkeypatch.setenv("PYTEST_ALLOW_TEST_DATABASE", "true")
    monkeypatch.setenv("ALLOW_TEST_DATABASE_IN_PRODUCTION", "1")
    get_settings.cache_clear()
    assert allow_test_database() is False
    with pytest.raises(ValidationError, match=r"\*_test"):
        Settings()
    with pytest.raises(ValidationError, match=r"\*_test"):
        Settings(allow_test_database=True, _env_file=None)
    get_settings.cache_clear()


def test_allow_test_database_opt_in_is_only_via_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
    settings = Settings(
        app_env="production",
        database_url=(
            "postgresql+asyncpg://app:s3cret@127.0.0.1:5432/estudiomusica_test"
        ),
        auth_secret="production-auth-secret-at-least-32ch",
        _env_file=None,
    )
    assert settings.resolved_database_url().endswith("/estudiomusica_test")
    assert settings.auth_cookie_secure() is True


def test_allow_test_database_still_rejects_default_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
    with pytest.raises(ValidationError, match="usuario") as caught:
        Settings(
            app_env="production",
            database_url=(
                "postgresql+asyncpg://estudiomusica:otra-clave"
                "@db.example:5432/estudiomusica_test"
            ),
            auth_secret="production-auth-secret-at-least-32ch",
            _env_file=None,
        )
    assert "otra-clave" not in str(caught.value)


def test_production_accepts_non_default_database_url() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app",
        _env_file=None,
    )
    assert settings.resolved_database_url().endswith("/app")


def test_invalid_database_url_does_not_leak_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://app:{_SECRET}@127.0.0.1:not-a-port/app",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert _SECRET not in str(caught.value)
    assert _SECRET not in repr(caught.value)
    get_settings.cache_clear()


def test_production_rejects_query_password_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?password=estudiomusica",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="contraseña") as caught:
        Settings()
    assert "s3cret" not in str(caught.value)
    assert "estudiomusica" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_query_user_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?user=estudiomusica",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="usuario") as caught:
        Settings()
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_query_database_test_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?database=x_test",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match=r"\*_test") as caught:
        Settings()
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_query_dbname_test_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?dbname=x_test",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match=r"\*_test"):
        Settings()
    get_settings.cache_clear()


def test_short_dsn_password_is_hidden_in_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://a:{_SECRET}@h/d_test",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as caught:
        Settings()
    text = str(caught.value)
    assert _SECRET not in text
    assert "input_value" not in text
    get_settings.cache_clear()


_DEFAULT_DEV_AUTH_SECRET = "dev-insecure-auth-secret-change-me"


def test_default_auth_secret_rejected_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT_DEV_AUTH_SECRET)
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="AUTH_SECRET") as caught:
        Settings()
    assert _DEFAULT_DEV_AUTH_SECRET not in str(caught.value)
    assert _DEFAULT_DEV_AUTH_SECRET not in repr(caught.value)
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="AUTH_SECRET") as caught:
        Settings()
    assert _DEFAULT_DEV_AUTH_SECRET not in str(caught.value)
    get_settings.cache_clear()


def test_short_auth_secret_rejected_outside_development() -> None:
    leaked = "too-short-to-be-safe"
    with pytest.raises(ValidationError, match="32") as caught:
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app",
            auth_secret=leaked,
            _env_file=None,
        )
    assert leaked not in str(caught.value)
    assert leaked not in repr(caught.value)


def test_default_auth_secret_allowed_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT_DEV_AUTH_SECRET)
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    settings = Settings()
    assert settings.resolved_auth_secret() == _DEFAULT_DEV_AUTH_SECRET
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


def test_auth_secret_is_hidden_in_settings_repr() -> None:
    leaked = "super-secret-value-that-must-not-leak-32"
    settings = Settings(auth_secret=leaked, _env_file=None)
    assert leaked not in repr(settings)
    assert leaked not in str(settings)


def test_invalid_auth_secret_is_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT_DEV_AUTH_SECRET)
    get_settings.cache_clear()
    with caplog.at_level(logging.DEBUG), pytest.raises(ValidationError):
        Settings()
    assert _DEFAULT_DEV_AUTH_SECRET not in caplog.text
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
