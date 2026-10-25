from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
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
    db_connect_timeout: float = Field(default=2.0, gt=0)
    db_command_timeout: float = Field(default=5.0, gt=0)
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout: float = Field(default=30.0, gt=0)
    db_pool_recycle: int = Field(default=1800, ge=-1)

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
        return self.database_url.get_secret_value()

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

        asyncpg acepta ``password``, ``user``, ``database`` y ``dbname``
        en la query; se aplican como override del DSN antes de validar.
        """

        if self.app_env == "development":
            return self
        try:
            parsed = make_url(self.resolved_database_url())
        except Exception:
            raise ValueError("DATABASE_URL no es un DSN válido") from None
        query_password = self._query_value(parsed, "password")
        query_user = self._query_value(parsed, "user")
        password = query_password or parsed.password
        database = (
            self._query_value(parsed, *_DATABASE_NAME_QUERY_KEYS)
            or parsed.database
            or ""
        )
        if password == _DEFAULT_DEV_PASSWORD or query_user == _DEFAULT_DEV_USER:
            raise ValueError(
                "DATABASE_URL no puede usar la contraseña o el usuario "
                "por defecto de desarrollo fuera de APP_ENV=development"
            )
        if database.endswith("_test"):
            raise ValueError(
                "DATABASE_URL no puede apuntar a una base *_test "
                "fuera de APP_ENV=development"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
