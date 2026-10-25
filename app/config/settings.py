from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    database_url: str = Field(
        default=(
            "postgresql+asyncpg://estudiomusica:estudiomusica"
            "@127.0.0.1:5432/estudiomusica"
        ),
        description=(
            "DSN async de Postgres (SQLAlchemy). El valor por defecto coincide "
            "con docker-compose.yml."
        ),
    )

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
