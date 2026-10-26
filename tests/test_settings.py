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


def test_allow_test_database_covers_uppercase_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://app:s3cret@127.0.0.1:5432/x_TEST",
        auth_secret="production-auth-secret-at-least-32ch",
        _env_file=None,
    )
    assert settings.resolved_database_url().endswith("/x_TEST")


def test_allow_test_database_still_requires_explicit_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
    with pytest.raises(ValidationError, match="explícitamente"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://app:s3cret@127.0.0.1:5432",
            auth_secret="production-auth-secret-at-least-32ch",
            _env_file=None,
        )


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


def test_production_rejects_uppercase_query_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q5: la clave de query no distingue mayúsculas (``Password=``)."""

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?Password=estudiomusica",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="contraseña"):
        Settings()
    get_settings.cache_clear()


def test_resolved_url_translates_sslmode_to_ssl() -> None:
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
    from sqlalchemy.engine.url import make_url

    settings = Settings(
        database_url=(
            "postgresql+asyncpg://app:s3cret@127.0.0.1:5432/app?sslmode=require"
        )
    )
    parsed = make_url(settings.resolved_database_url())
    query = {str(key).lower(): value for key, value in parsed.query.items()}
    assert query.get("ssl") == "require"
    assert "sslmode" not in query
    dialect = PGDialect_asyncpg()  # type: ignore[no-untyped-call]
    _args, opts = dialect.create_connect_args(parsed)  # type: ignore[no-untyped-call]
    assert opts.get("ssl") == "require"
    assert "sslmode" not in opts


def test_resolved_url_translates_uppercase_sslmode() -> None:
    """Q5b: ``SSLMODE=`` también se quita (hace falta ``.lower()``)."""

    from sqlalchemy.engine.url import make_url

    settings = Settings(
        database_url=(
            "postgresql+asyncpg://app:s3cret@127.0.0.1:5432/app?SSLMODE=require"
        )
    )
    parsed = make_url(settings.resolved_database_url())
    query = {str(key).lower(): value for key, value in parsed.query.items()}
    assert query.get("ssl") == "require"
    assert "sslmode" not in query
    assert all(str(key).lower() != "sslmode" for key in parsed.query)


def test_db_health_cache_ttl_default_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SF15/SF16: defecto 1 s; negativo y >60 se rechazan."""

    monkeypatch.delenv("DB_HEALTH_CACHE_TTL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.db_health_cache_ttl == 1.0
    with pytest.raises(ValidationError):
        Settings(db_health_cache_ttl=-0.1, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(db_health_cache_ttl=60.1, _env_file=None)
    assert Settings(db_health_cache_ttl=0, _env_file=None).db_health_cache_ttl == 0.0
    assert Settings(db_health_cache_ttl=60, _env_file=None).db_health_cache_ttl == 60.0


def test_production_rejects_client_cert_and_passfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?sslcert=/tmp/c.pem",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="certificado") as cert:
        Settings()
    assert "peer" not in str(cert.value).lower()
    assert "s3cret" not in str(cert.value)
    get_settings.cache_clear()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?passfile=/tmp/.pgpass",
    )
    with pytest.raises(ValidationError, match="passfile|pgpass") as passfile:
        Settings()
    assert "peer" not in str(passfile.value).lower()
    get_settings.cache_clear()


def test_production_rejects_pgpassfile_and_client_cert_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app",
    )
    monkeypatch.setenv("PGPASSFILE", "/tmp/.pgpass")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="PGPASSFILE") as caught:
        Settings()
    assert "peer" not in str(caught.value).lower()
    get_settings.cache_clear()
    monkeypatch.delenv("PGPASSFILE")
    monkeypatch.setenv("PGSSLCERT", "/tmp/c.pem")
    with pytest.raises(ValidationError, match="PGSSLCERT"):
        Settings()
    get_settings.cache_clear()
    monkeypatch.delenv("PGSSLCERT")
    monkeypatch.setenv("PGSSLKEY", "/tmp/k.pem")
    with pytest.raises(ValidationError, match="PGSSLKEY"):
        Settings()
    get_settings.cache_clear()
    monkeypatch.delenv("PGSSLKEY")
    monkeypatch.setenv("PGSSLROOTCERT", "/tmp/ca.pem")
    settings = Settings()
    assert settings.app_env == "production"
    get_settings.cache_clear()


def test_production_rejects_sslpassword_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SR3: ``?sslpassword=`` se rechaza fuera de development."""

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?sslpassword=client-secret",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="sslpassword|certificado") as caught:
        Settings()
    assert "client-secret" not in str(caught.value)
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_allows_sslrootcert_and_pgsslrootcert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?sslrootcert=/tmp/ca.pem",
    )
    get_settings.cache_clear()
    settings = Settings()
    assert "sslrootcert" in settings.resolved_database_url()
    get_settings.cache_clear()


def test_development_allows_pgpassfile_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("PGPASSFILE", "/tmp/.pgpass")
    monkeypatch.setenv("DATABASE_URL", _DEFAULT_DEV_DSN)
    get_settings.cache_clear()
    settings = Settings()
    assert settings.app_env == "development"
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


def test_production_rejects_duplicate_database_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app"
        "?database=ok&database=x_test",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="database/dbname") as caught:
        Settings()
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_duplicate_dbname_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/app?dbname=ok&dbname=x_test",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="database/dbname"):
        Settings()
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


def test_production_rejects_uppercase_default_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://EstudioMusica:otra-clave@db.example:5432/app",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="usuario") as caught:
        Settings()
    assert "otra-clave" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_uppercase_test_database_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/x_TEST",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match=r"\*_test") as caught:
        Settings()
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_dsn_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PGDATABASE", "x_test")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="explícitamente") as caught:
        Settings()
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_dsn_without_database_with_test_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://x_test:s3cret@db.example:5432",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="explícitamente") as caught:
        Settings()
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_dsn_without_password_despite_pgpassword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4: en producción no se hereda PGPASSWORD."""

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PGPASSWORD", "from-env-secret")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app@db.example:5432/app",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="contraseña") as caught:
        Settings()
    assert "from-env-secret" not in str(caught.value)
    get_settings.cache_clear()


def test_production_rejects_dsn_without_user_despite_pguser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6: en producción no se hereda PGUSER."""

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PGUSER", "from-env-user")
    monkeypatch.setenv("PGPASSWORD", "from-env-secret")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://:s3cret@db.example:5432/app",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="usuario") as caught:
        Settings()
    assert "from-env-secret" not in str(caught.value)
    assert "s3cret" not in str(caught.value)
    get_settings.cache_clear()


def test_development_allows_dsn_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("PGUSER", "estudiomusica")
    monkeypatch.setenv("PGPASSWORD", "estudiomusica")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://127.0.0.1:5432/estudiomusica",
    )
    get_settings.cache_clear()
    settings = Settings()
    assert "127.0.0.1:5432/estudiomusica" in settings.resolved_database_url()
    get_settings.cache_clear()


def _assert_secret_redacted(exc: ValidationError) -> None:
    dumped = exc.json()
    assert _SECRET not in dumped
    assert _SECRET not in str(exc)
    assert _SECRET not in repr(exc)
    for error in exc.errors():
        assert _SECRET not in str(error.get("input"))
        assert _SECRET not in repr(error.get("input"))
        assert _SECRET not in repr(error.get("ctx"))


def test_validation_error_redacts_query_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://app@db.example:5432/app_test?password={_SECRET}",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as caught:
        Settings()
    _assert_secret_redacted(caught.value)
    get_settings.cache_clear()


def test_validation_error_redacts_query_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:ok@db.example:5432/app_test"
        f"?dsn=postgresql://u:{_SECRET}@h/x",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as caught:
        Settings()
    _assert_secret_redacted(caught.value)
    get_settings.cache_clear()


def test_validation_error_redacts_dsn_without_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"u:{_SECRET}@h/app")
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as caught:
        Settings()
    _assert_secret_redacted(caught.value)
    get_settings.cache_clear()


def test_validation_error_fallback_does_not_blame_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as settings_mod

    real = ValidationError.from_exception_data
    state = {"n": 0}

    def flaky(
        title: str,
        line_errors: list[object],
        hide_input: bool = False,
    ) -> ValidationError:
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("cannot rebuild")
        return real(title, line_errors, hide_input=hide_input)

    monkeypatch.setattr(settings_mod.ValidationError, "from_exception_data", flaky)
    with pytest.raises(ValidationError) as caught:
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app_test",
            _env_file=None,
        )
    dumped = caught.value.json()
    assert "settings" in dumped
    assert '"database_url"' not in dumped
    assert "sin secretos" in str(caught.value)


def test_validation_error_does_not_keep_secret_in_exception_context() -> None:
    leaked = "context-leak-secret-too-short"
    with pytest.raises(ValidationError) as caught:
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app",
            auth_secret=leaked,
            _env_file=None,
        )
    exc = caught.value
    assert exc.__context__ is None
    assert exc.__cause__ is None
    assert leaked not in str(exc)
    assert leaked not in repr(exc)


def test_validation_error_errors_and_json_redact_auth_secret_type_error() -> None:
    """K2: un error de tipo no debe filtrar auth_secret en json()/errors()."""

    leaked = 123
    with pytest.raises(ValidationError) as caught:
        Settings(
            app_env="development",
            database_url=_DEFAULT_DEV_DSN,
            auth_secret=leaked,  # type: ignore[arg-type]
            _env_file=None,
        )
    dumped = caught.value.json()
    assert "123" not in dumped
    assert "123" not in str(caught.value)
    assert "123" not in repr(caught.value)
    for error in caught.value.errors():
        assert error.get("input") != leaked
        assert "123" not in str(error.get("input"))
        assert "123" not in repr(error.get("input"))
        assert "123" not in repr(error.get("ctx"))


def test_validation_error_errors_and_json_redact_auth_secret_on_model_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaked = "auth-secret-must-never-appear-in-errors-32"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db.example:5432/x_test",
    )
    monkeypatch.setenv("AUTH_SECRET", leaked)
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as caught:
        Settings()
    dumped = caught.value.json()
    assert leaked not in dumped
    assert leaked not in str(caught.value)
    assert leaked not in repr(caught.value)
    for error in caught.value.errors():
        assert leaked not in str(error.get("input"))
        assert leaked not in repr(error.get("input"))
        assert leaked not in repr(error.get("ctx"))
    get_settings.cache_clear()


def test_validation_error_errors_and_json_redact_dsn_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://app:{_SECRET}@db.example:5432/app_test",
    )
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as caught:
        Settings()
    dumped = caught.value.json()
    assert _SECRET not in dumped
    for error in caught.value.errors():
        assert _SECRET not in str(error.get("input"))
        assert _SECRET not in repr(error.get("input"))
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
