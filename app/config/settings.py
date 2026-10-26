import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url

_DEFAULT_DEV_PASSWORD = "estudiomusica"
_DEFAULT_DEV_USER = "estudiomusica"
_DATABASE_NAME_QUERY_KEYS = ("database", "dbname")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Ajustes de runtime.

    Se leen de variables de entorno y del `.env` de la raíz del repo.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "EstudioMusica API"
    app_env: Literal["development", "staging", "production"] = "development"
    expose_errors: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    cors_allow_credentials: bool = True
    cors_allow_methods: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    cors_allow_headers: str = "Authorization,Content-Type,Accept"
    pagination_default_page: int = 1
    pagination_default_page_size: int = 20
    pagination_max_page: int = Field(default=10_000, ge=1)
    pagination_max_page_size: int = Field(default=100, ge=1)
    max_body_bytes: int = Field(default=1_048_576, ge=1)
    enable_example_routes: bool | None = None
    database_url: SecretStr = Field(
        default=SecretStr(
            "postgresql+asyncpg://estudiomusica:estudiomusica"
            "@127.0.0.1:5432/estudiomusica"
        ),
        description=(
            "DSN async de Postgres (SQLAlchemy). El valor por defecto coincide "
            "con docker-compose.yml."
        ),
        repr=False,
    )
    db_health_timeout: float = Field(default=2.0, gt=0)
    db_health_cache_ttl: float = Field(default=1.0, ge=0, le=60)
    db_connect_timeout: float = Field(default=2.0, gt=0)
    db_command_timeout: float = Field(default=5.0, gt=0)
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout: float = Field(default=30.0, gt=0)
    db_pool_recycle: int = Field(default=1800, ge=-1)

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _validation_error_without_secrets(exc) from None

    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]

    def cors_method_list(self) -> list[str]:
        return [
            method.strip()
            for method in self.cors_allow_methods.split(",")
            if method.strip()
        ]

    def cors_header_list(self) -> list[str]:
        return [
            header.strip()
            for header in self.cors_allow_headers.split(",")
            if header.strip()
        ]

    def example_routes_enabled(self) -> bool:
        if self.enable_example_routes is not None:
            return self.enable_example_routes
        return self.app_env != "production"

    def resolved_database_url(self) -> str:
        from app.db import normalize_asyncpg_ssl_url

        return normalize_asyncpg_ssl_url(self.database_url.get_secret_value())

    @model_validator(mode="after")
    def reject_unsafe_cors(self) -> Self:
        if not self.cors_allow_credentials:
            return self
        for origin in self.cors_origin_list():
            if origin == "*":
                raise ValueError(
                    "CORS_ORIGINS no puede ser '*' cuando CORS_ALLOW_CREDENTIALS=true"
                )
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    "Origen CORS inválido con credenciales "
                    f"(hace falta http(s)://): {origin}"
                )
        return self

    @staticmethod
    def _query_value(parsed: object, *keys: str) -> str | None:
        query = getattr(parsed, "query", {}) or {}
        lowered = {str(key).lower(): value for key, value in query.items()}
        for key in keys:
            value = lowered.get(key)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                return str(value[0]) if value else None
            return str(value)
        return None

    @model_validator(mode="after")
    def reject_default_database_credentials(self) -> Self:
        """Las credenciales y bases de test no valen fuera de development.

        Fuera de development el DSN debe traer usuario, contraseña y base
        explícitos: no se heredan de ``PGUSER`` / ``PGPASSWORD`` /
        ``PGDATABASE`` (asyncpg usaría el usuario como nombre de base).
        La comparación de usuario y del sufijo ``_test`` no distingue
        mayúsculas. asyncpg acepta ``password``, ``user``, ``database`` y
        ``dbname`` en la query; se aplican como override antes de validar.
        También se rechazan ``passfile`` / ``.pgpass`` y certificado de
        cliente (``sslcert`` / ``sslkey`` / ``sslpassword``, o
        ``PGPASSFILE`` / ``PGSSLCERT`` / ``PGSSLKEY``).
        ``sslrootcert`` en la query se traduce a un ``SSLContext`` con
        ``cafile`` (asyncpg no acepta ese kwarg). ``PGSSLROOTCERT`` es
        la alternativa por entorno. Ambos se permiten (``verify-full``).
        El cliente no puede rechazar autenticación peer: eso lo decide
        ``pg_hba.conf``.
        """

        if self.app_env == "development":
            return self
        try:
            parsed = make_url(self.resolved_database_url())
        except Exception:
            raise ValueError("DATABASE_URL no es un DSN válido") from None
        query_password = self._query_value(parsed, "password")
        query_user = self._query_value(parsed, "user")
        query_database = self._query_value(parsed, *_DATABASE_NAME_QUERY_KEYS)
        password = query_password if query_password is not None else parsed.password
        username = query_user if query_user is not None else parsed.username
        database = query_database if query_database is not None else parsed.database
        if not username:
            raise ValueError(
                "DATABASE_URL debe incluir el usuario explícitamente "
                "fuera de APP_ENV=development (no se hereda de PGUSER)"
            )
        if password is None or password == "":
            raise ValueError(
                "DATABASE_URL debe incluir la contraseña explícitamente "
                "fuera de APP_ENV=development (no se hereda de PGPASSWORD)"
            )
        if not database:
            raise ValueError(
                "DATABASE_URL debe incluir el nombre de base explícitamente "
                "fuera de APP_ENV=development (no se hereda de PGDATABASE "
                "ni del usuario)"
            )
        if password == _DEFAULT_DEV_PASSWORD or username.lower() == _DEFAULT_DEV_USER:
            raise ValueError(
                "DATABASE_URL no puede usar la contraseña o el usuario "
                "por defecto de desarrollo fuera de APP_ENV=development"
            )
        if database.lower().endswith("_test"):
            raise ValueError(
                "DATABASE_URL no puede apuntar a una base *_test "
                "fuera de APP_ENV=development"
            )
        if self._query_value(parsed, "passfile") is not None:
            raise ValueError(
                "DATABASE_URL no puede usar passfile/.pgpass "
                "fuera de APP_ENV=development"
            )
        for key in ("sslcert", "sslkey", "sslpassword"):
            if self._query_value(parsed, key) is not None:
                raise ValueError(
                    f"DATABASE_URL no puede usar certificado de cliente "
                    f"({key}) fuera de APP_ENV=development"
                )
        if os.environ.get("PGPASSFILE"):
            raise ValueError(
                "PGPASSFILE no puede estar definido fuera de APP_ENV=development"
            )
        for env_name in ("PGSSLCERT", "PGSSLKEY"):
            if os.environ.get(env_name):
                raise ValueError(
                    f"{env_name} no puede estar definido fuera de APP_ENV=development"
                )
        return self


def _normalize_sslmode(url: str) -> str:
    """Delegado al normalizador único de ``app.db`` (Settings, engine, Alembic)."""

    from app.db import normalize_asyncpg_ssl_url

    return normalize_asyncpg_ssl_url(url)


_DSN_FIELD_NAMES = frozenset({"database_url", "DATABASE_URL"})


def _loc_is_dsn(loc: object) -> bool:
    if isinstance(loc, (tuple, list)):
        return any(part in _DSN_FIELD_NAMES for part in loc)
    return loc in _DSN_FIELD_NAMES


def _redact_error_input(value: object, *, dsn_field: bool = False) -> object:
    """Si el campo es el DSN, se sustituye entero (sin parsear)."""

    if dsn_field:
        if isinstance(value, dict):
            return {
                key: _redact_error_input(item, dsn_field=key in _DSN_FIELD_NAMES)
                for key, item in value.items()
            }
        return "<dsn-redactado>"
    if isinstance(value, SecretStr):
        return "**********"
    if isinstance(value, dict):
        return {
            key: _redact_error_input(item, dsn_field=key in _DSN_FIELD_NAMES)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_error_input(item) for item in value]
    return value


def _validation_error_without_secrets(exc: ValidationError) -> ValidationError:
    """Copia el ValidationError sin el DSN original en ``input`` / ``json()``."""

    line_errors: list[Any] = []
    for error in exc.errors(include_url=False):
        dsn_field = _loc_is_dsn(error.get("loc"))
        item: dict[str, Any] = {
            "type": error.get("type", "value_error"),
            "loc": error.get("loc", ()),
            "input": _redact_error_input(error.get("input"), dsn_field=dsn_field),
        }
        ctx = error.get("ctx")
        if ctx is not None:
            item["ctx"] = _redact_error_input(ctx, dsn_field=dsn_field)
        line_errors.append(item)
    try:
        return ValidationError.from_exception_data(
            exc.title, line_errors, hide_input=True
        )
    except Exception:
        return ValidationError.from_exception_data(
            exc.title,
            [{"type": "value_error", "loc": ("database_url",), "input": None}],
            hide_input=True,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
