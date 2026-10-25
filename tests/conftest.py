import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.exceptions import Validation

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica_test"
)
DOWN_DATABASE_URL = (
    "postgresql+asyncpg://leaky_user:supersecret-password-do-not-leak"
    "@127.0.0.1:1/no_such_db"
)


def resolve_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session", autouse=True)
def isolate_app_env() -> Iterator[None]:
    """Fija APP_ENV=development durante toda la sesión de tests.

    Se ignora el `.env` (y un APP_ENV exportado en la shell).
    MonkeyPatch.context() restaura el entorno al terminar.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("APP_ENV", "development")
        yield
        from app.main import get_app

        get_app.cache_clear()


def _mount_probes(application: FastAPI) -> None:
    @application.get("/api/v1/_test/errors/{code}")
    def raise_coded_error(code: str, status_code: int = 403) -> None:
        raise HTTPException(status_code=status_code, detail=code)

    @application.get("/api/v1/_test/boom")
    def raise_boom() -> None:
        raise RuntimeError("secret-leak")

    @application.get("/api/v1/_test/validation")
    def raise_validation() -> None:
        raise Validation(fields=["email"])

    @application.get("/api/v1/_test/http-400-text")
    def raise_http_400_text() -> None:
        raise HTTPException(status_code=400, detail="campo obligatorio")

    @application.get("/api/v1/_test/http-422")
    def raise_http_422() -> None:
        raise HTTPException(status_code=422, detail="unprocessable")

    @application.get("/api/v1/_test/http-400-dict")
    def raise_http_400_dict() -> None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_evaluation",
                "message": "secret-leak",
                "code": "no_solution",
            },
        )

    @application.post("/api/v1/_test/loose")
    def loose_body(body: dict[str, int]) -> dict[str, int]:
        return body


def _build_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[TestClient]:
    from app.config import get_settings
    from app.db import clear_engine
    from app.main import create_app, get_app

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_app.cache_clear()
    clear_engine()
    try:
        application = create_app()
        _mount_probes(application)
        with TestClient(application) as test_client:
            yield test_client
    finally:
        get_settings.cache_clear()
        get_app.cache_clear()
        clear_engine()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
    )


@pytest.fixture
def probe_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Cliente de tests con rutas de sonda para códigos de error."""

    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
    )


@pytest.fixture
def production_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="production",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="false",
    )


@pytest.fixture
def small_body_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        MAX_BODY_BYTES="64",
    )


@pytest.fixture
def expose_errors_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="true",
        ENABLE_EXAMPLE_ROUTES="true",
    )


@pytest.fixture
def tight_pagination_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        PAGINATION_MAX_PAGE_SIZE="5",
        PAGINATION_MAX_PAGE="50",
    )


@pytest.fixture
def db_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Cliente HTTP apuntando a la BD de test (docker-compose)."""

    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DATABASE_URL=resolve_test_database_url(),
    )


@pytest.fixture
def down_db_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DATABASE_URL=DOWN_DATABASE_URL,
    )


@pytest.fixture
def down_db_production_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="production",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="false",
        DATABASE_URL=DOWN_DATABASE_URL,
    )


@pytest.fixture
def down_db_expose_errors_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="true",
        ENABLE_EXAMPLE_ROUTES="true",
        DATABASE_URL=DOWN_DATABASE_URL,
    )


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
    from app.main import get_app

    url = resolve_test_database_url()
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    get_app.cache_clear()
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
    get_app.cache_clear()
