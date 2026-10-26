from functools import lru_cache
from pathlib import Path
from typing import Literal

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
