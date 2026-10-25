import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import get_session, redact_database_url
from app.exceptions import Validation

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica_test"
)
DEFAULT_ALEMBIC_DATABASE_URL = (
    "postgresql+asyncpg://estudiomusica:estudiomusica"
    "@127.0.0.1:5432/estudiomusica_alembic_test"
)
DOWN_DATABASE_URL = (
    "postgresql+asyncpg://leaky_user:supersecret-password-do-not-leak"
    "@127.0.0.1:1/no_such_db"
)
UNUSED_DATABASE_URL = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"

_SETTINGS_ENV = {
    "APP_NAME": "EstudioMusica API",
    "APP_ENV": "development",
    "EXPOSE_ERRORS": "false",
    "CORS_ORIGINS": "http://localhost:3000,http://127.0.0.1:3000",
    "CORS_ALLOW_CREDENTIALS": "true",
    "CORS_ALLOW_METHODS": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    "CORS_ALLOW_HEADERS": "Authorization,Content-Type,Accept",
    "PAGINATION_DEFAULT_PAGE": "1",
    "PAGINATION_DEFAULT_PAGE_SIZE": "20",
    "PAGINATION_MAX_PAGE": "10000",
    "PAGINATION_MAX_PAGE_SIZE": "100",
    "MAX_BODY_BYTES": "1048576",
    "DATABASE_URL": UNUSED_DATABASE_URL,
    "DB_HEALTH_TIMEOUT": "2",
    "DB_CONNECT_TIMEOUT": "2",
    "DB_COMMAND_TIMEOUT": "5",
    "DB_POOL_SIZE": "5",
    "DB_MAX_OVERFLOW": "10",
    "DB_POOL_TIMEOUT": "30",
    "DB_POOL_RECYCLE": "1800",
}

PROBE_FILE_BYTES = b"file-response-body\n" * 8
_PROBE_FILE = Path("/tmp/estudiomusica-probe.bin")


def _require_test_database_name(url: str) -> str:
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise pytest.UsageError(
            "TEST_DATABASE_URL debe apuntar a una base cuyo nombre termina "
            "en '_test' para no tocar desarrollo. Recibido: "
            f"{redact_database_url(url)}"
        )
    return url


def resolve_test_database_url() -> str:
    return _require_test_database_name(
        os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    )


def resolve_alembic_database_url() -> str:
    return _require_test_database_name(
        os.environ.get("ALEMBIC_TEST_DATABASE_URL", DEFAULT_ALEMBIC_DATABASE_URL)
    )


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session", autouse=True)
def reject_non_test_database() -> None:
    """Aborta la suite si TEST_DATABASE_URL apunta a una base de desarrollo."""

    raw = os.environ.get("TEST_DATABASE_URL")
    if raw is None:
        return
    try:
        _require_test_database_name(raw)
    except pytest.UsageError as exc:
        pytest.exit(str(exc), returncode=2)


@pytest.fixture(scope="session", autouse=True)
def isolate_settings_env() -> Iterator[None]:
    """Fija todas las variables de Settings durante la sesión de tests."""

    with pytest.MonkeyPatch.context() as mp:
        for key, value in _SETTINGS_ENV.items():
            mp.setenv(key, value)
        mp.delenv("ENABLE_EXAMPLE_ROUTES", raising=False)
        yield
        from app.config import get_settings
        from app.main import get_app

        get_settings.cache_clear()
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

    @application.get("/api/v1/_test/http-418")
    def raise_http_418() -> None:
        raise HTTPException(status_code=418)

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

    @application.post("/api/v1/_test/echo-size")
    async def echo_size(request: Request) -> dict[str, int]:
        data = await request.body()
        return {"size": len(data)}

    @application.post("/api/v1/_test/disconnect")
    async def check_disconnect(request: Request) -> dict[str, bool]:
        await request.body()
        return {"disconnected": await request.is_disconnected()}

    @application.get("/api/v1/_test/stream")
    def stream_chunks() -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            for index in range(5):
                yield f"chunk{index}"

        return StreamingResponse(generate(), media_type="text/plain")

    @application.get("/api/v1/_test/stream-boom")
    def stream_boom() -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            yield "chunk0"
            raise RuntimeError("mid-stream")

        return StreamingResponse(generate(), media_type="text/plain")

    @application.get("/api/v1/_test/file")
    def file_probe() -> FileResponse:
        _PROBE_FILE.write_bytes(PROBE_FILE_BYTES)
        return FileResponse(_PROBE_FILE, media_type="application/octet-stream")

    @application.get("/api/v1/_test/session")
    async def session_probe(
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        return {"session": "ok"}


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
def wildcard_cors_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        CORS_ORIGINS="*",
        CORS_ALLOW_CREDENTIALS="false",
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
def db_client(
    monkeypatch: pytest.MonkeyPatch, require_postgres: str
) -> Iterator[TestClient]:
    """Cliente HTTP apuntando a la BD de test (docker-compose)."""

    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DATABASE_URL=require_postgres,
    )


@pytest.fixture
def production_db_client(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: str,
    migrated_test_database: None,
) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="production",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="false",
        DATABASE_URL=require_postgres,
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


def _postgres_is_available(url: str) -> bool:
    async def ping() -> None:
        engine = create_async_engine(url, pool_pre_ping=False)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(ping())
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def require_postgres() -> str:
    """Salta los tests de BD si Postgres no responde. Nunca imprime la contraseña."""

    url = resolve_test_database_url()
    if not _postgres_is_available(url):
        pytest.skip(
            "Postgres de test no está disponible. "
            "En local: docker compose up -d. "
            f"{redact_database_url(url)}"
        )
    return url


@pytest.fixture(scope="session")
def migrated_test_database(require_postgres: str) -> Iterator[None]:
    """Aplica las migraciones en la BD de test. Requiere docker compose up -d."""

    from app.config import get_settings

    url = require_postgres
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        try:
            command.upgrade(alembic_config(url), "head")
        except Exception:
            get_settings.cache_clear()
            pytest.skip(
                "No se pudieron aplicar las migraciones de test. "
                "En local: docker compose up -d && uv run alembic upgrade head. "
                f"{redact_database_url(url)}"
            )
        yield
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

        async def _override_session() -> AsyncIterator[AsyncSession]:
            yield session

        get_app().dependency_overrides[get_session] = _override_session
        try:
            yield session
        finally:
            get_app().dependency_overrides.pop(get_session, None)
            await session.close()
            await transaction.rollback()
    await engine.dispose()
    await dispose_engine()
    get_settings.cache_clear()
    get_app.cache_clear()


@pytest.fixture
def session_client(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    require_postgres: str,
) -> Iterator[TestClient]:
    """Cliente HTTP con get_session ligado a la transacción de db_session."""

    from app.config import get_settings
    from app.db import clear_engine
    from app.main import create_app, get_app

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EXPOSE_ERRORS", "false")
    monkeypatch.setenv("ENABLE_EXAMPLE_ROUTES", "true")
    monkeypatch.setenv("DATABASE_URL", require_postgres)
    get_settings.cache_clear()
    get_app.cache_clear()
    clear_engine()
    application = create_app()
    _mount_probes(application)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()
        get_app.cache_clear()
        clear_engine()
