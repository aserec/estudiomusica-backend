import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

os.environ.setdefault("APP_ENV", "development")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica_test"
)


def resolve_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app.config import get_settings
    from app.db import clear_engine
    from app.main import create_app

    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    clear_engine()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()
    clear_engine()


@pytest.fixture
def probe_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """App de producción más rutas de sonda para códigos de error de negocio."""

    from app.config import get_settings
    from app.db import clear_engine
    from app.main import create_app

    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    application = create_app()

    @application.get("/api/v1/_test/errors/{code}")
    def raise_coded_error(code: str, status_code: int = 403) -> None:
        raise HTTPException(status_code=status_code, detail=code)

    with TestClient(application) as test_client:
        yield test_client
    get_settings.cache_clear()
    clear_engine()


@pytest.fixture
def db_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Cliente HTTP apuntando a la BD de test (docker-compose)."""

    from app.config import get_settings
    from app.db import clear_engine
    from app.main import create_app

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", resolve_test_database_url())
    get_settings.cache_clear()
    clear_engine()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()
    clear_engine()


@pytest.fixture(scope="session")
def migrated_test_database() -> Iterator[None]:
    """Aplica las migraciones en la BD de test. Requiere docker compose up -d."""

    from app.config import get_settings

    url = resolve_test_database_url()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        command.upgrade(alembic_config(url), "head")
    except Exception as exc:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        raise RuntimeError(
            "Postgres de test no está disponible. "
            "En local: docker compose up -d && uv run alembic upgrade head. "
            f"TEST_DATABASE_URL={url}"
        ) from exc
    yield
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
    get_settings.cache_clear()


@pytest.fixture
async def db_session(
    monkeypatch: pytest.MonkeyPatch,
    migrated_test_database: None,
) -> AsyncIterator[AsyncSession]:
    """Sesión envuelta en una transacción que se revierte al terminar el test."""

    from app.config import get_settings
    from app.db import dispose_engine

    url = resolve_test_database_url()
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()
    await dispose_engine()
    get_settings.cache_clear()
