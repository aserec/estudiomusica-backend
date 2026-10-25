from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas.pagination import (
    DEFAULT_PAGE,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
)

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

AppEnv = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    """Ajustes de runtime. Se leen de variables de entorno y de `.env`."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "EstudioMusica API"
    app_env: AppEnv = "development"
    api_v1_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    cors_allow_credentials: bool = True
    cors_allow_methods: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    cors_allow_headers: str = "Authorization,Content-Type,Accept"
    pagination_default_page: int = Field(default=DEFAULT_PAGE)
    pagination_default_page_size: int = Field(default=DEFAULT_PAGE_SIZE)
    pagination_max_page_size: int = Field(default=MAX_PAGE_SIZE)

    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def cors_method_list(self) -> list[str]:
        return [method.strip() for method in self.cors_allow_methods.split(",") if method.strip()]

    def cors_header_list(self) -> list[str]:
        return [header.strip() for header in self.cors_allow_headers.split(",") if header.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
