import asyncio
import errno
import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import RedactedDsn, get_session, redact_database_url
from app.exceptions import Validation

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica_test"
)
DOWN_DATABASE_URL = (
    "postgresql+asyncpg://leaky_user:supersecret-password-do-not-leak"
    "@127.0.0.1:1/no_such_db"
)
UNUSED_DATABASE_URL = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
_DATABASE_OVERRIDE_QUERY_KEYS = frozenset({"database", "dbname", "service", "options"})
_PING_CONNECT_TIMEOUT = 2.0
_PING_TOTAL_TIMEOUT = 3.0
_CONNECTION_ERRNOS = {
    errno.ECONNREFUSED,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
    errno.ECONNRESET,
}

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
_PROBE_FILE: Path | None = None


def require_test_database_url(url: str, *, env_name: str = "TEST_DATABASE_URL") -> str:
    """Exige una base ``*_test`` y rechaza query que pueda cambiar el destino."""

    try:
        parsed = make_url(url)
    except Exception as exc:
        raise pytest.UsageError(
            f"{env_name} no es un DSN válido. Recibido: {redact_database_url(url)}"
        ) from exc
    query_keys = {key.lower() for key in parsed.query}
    banned = query_keys & _DATABASE_OVERRIDE_QUERY_KEYS
    if banned:
        raise pytest.UsageError(
            f"{env_name} no puede incluir parámetros que cambien la base "
            f"({', '.join(sorted(banned))}). Recibido: {redact_database_url(url)}"
        )
    name = parsed.database or ""
    if not name.endswith("_test"):
        raise pytest.UsageError(
            f"{env_name} debe apuntar a una base cuyo nombre termina "
            "en '_test' para no tocar desarrollo. Recibido: "
            f"{redact_database_url(url)}"
        )
    return url


def derive_alembic_database_url(test_url: str) -> str:
    parsed = make_url(test_url)
    name = parsed.database or ""
    stem = name[: -len("_test")] if name.endswith("_test") else name
    return parsed.set(database=f"{stem}_alembic_test").render_as_string(
        hide_password=False
    )


def resolve_test_database_url() -> str:
    return require_test_database_url(
        os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    )


def resolve_alembic_database_url() -> str:
    raw = os.environ.get("ALEMBIC_TEST_DATABASE_URL")
    if raw is None:
        raw = derive_alembic_database_url(resolve_test_database_url())
    return require_test_database_url(raw, env_name="ALEMBIC_TEST_DATABASE_URL")


def alembic_config(database_url: str | RedactedDsn) -> Config:
    from app.migrations.config import make_alembic_config

    raw = (
        database_url.secret() if isinstance(database_url, RedactedDsn) else database_url
    )
    return make_alembic_config(raw)


@pytest.fixture(scope="session", autouse=True)
def reject_non_test_database() -> None:
    """Aborta la suite si una URL de test apunta a una base de desarrollo."""

    for env_name in ("TEST_DATABASE_URL", "ALEMBIC_TEST_DATABASE_URL"):
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            require_test_database_url(raw, env_name=env_name)
        except pytest.UsageError as exc:
            pytest.exit(str(exc), returncode=2)


@pytest.fixture(scope="session", autouse=True)
def probe_file_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    global _PROBE_FILE
    path = tmp_path_factory.mktemp("probes") / "estudiomusica-probe.bin"
    path.write_bytes(PROBE_FILE_BYTES)
    _PROBE_FILE = path
    yield path
    _PROBE_FILE = None


@pytest.fixture(scope="session", autouse=True)
def isolate_settings_env(reject_non_test_database: None) -> Iterator[None]:
    """Fija todas las variables de Settings durante la sesión de tests.

    ``DATABASE_URL`` queda en un DSN dummy para que production no herede
    las credenciales del compose. ``TEST_DATABASE_URL`` no es un setting:
    si el invocador no la exportó, se fija a ``estudiomusica_test``; si
    sí (y pasó ``reject_non_test_database``), se respeta.
    """

    with pytest.MonkeyPatch.context() as mp:
        for key, value in _SETTINGS_ENV.items():
            mp.setenv(key, value)
        mp.delenv("ENABLE_EXAMPLE_ROUTES", raising=False)
        if os.environ.get("TEST_DATABASE_URL") is None:
            mp.setenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
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
        if _PROBE_FILE is None:
            raise RuntimeError("probe_file_path no inicializó el fichero de sonda")
        return FileResponse(_PROBE_FILE, media_type="application/octet-stream")

    @application.get("/api/v1/_test/session")
    async def session_probe(
        session: Annotated[AsyncSession, Depends(get_session)],
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
    monkeypatch: pytest.MonkeyPatch, require_postgres: RedactedDsn
) -> Iterator[TestClient]:
    """Cliente HTTP apuntando a la BD de test (docker-compose)."""

    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DATABASE_URL=require_postgres.secret(),
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


_PYDANTIC_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_PYDANTIC_FALSE = frozenset({"0", "false", "f", "no", "n", "off", ""})


def _postgres_required() -> bool:
    """Los mismos booleanos que Pydantic; un valor desconocido aborta."""

    raw = os.environ.get("REQUIRE_POSTGRES", "").strip().lower()
    if raw in _PYDANTIC_TRUE:
        return True
    if raw in _PYDANTIC_FALSE:
        return False
    raise pytest.UsageError(
        "REQUIRE_POSTGRES no es un booleano válido "
        f"(true/false, 1/0, yes/no, on/off, y/n, t/f). Recibido: {raw!r}"
    )


class _MissingDatabase(RuntimeError):
    """La instancia responde pero la base de test no existe."""


def _skip_or_fail_postgres(message: str) -> None:
    if _postgres_required():
        raise pytest.fail.Exception(message) from None
    raise pytest.skip.Exception(message) from None


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionRefusedError | TimeoutError | asyncio.TimeoutError):
        return True
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, OSError) and exc.errno in _CONNECTION_ERRNOS:
        return True
    message = str(exc).lower()
    needles = (
        "connection refused",
        "connect call failed",
        "name or service not known",
        "nodename nor servname",
        "temporary failure in name resolution",
        "could not translate host",
        "network is unreachable",
        "no route to host",
        "timed out",
        "timeout expired",
        "error connecting to localhost",
    )
    if any(needle in message for needle in needles) and "password" not in message:
        return True
    for inner in (exc.__cause__, exc.__context__):
        if inner is not None and inner is not exc and _is_connection_error(inner):
            return True
    return False


def _as_redacted_dsn(database_url: str | RedactedDsn) -> RedactedDsn:
    if isinstance(database_url, RedactedDsn):
        return database_url
    return RedactedDsn(database_url)


def ensure_database_exists(database_url: str | RedactedDsn) -> None:
    """Crea la base conectando a la BD de mantenimiento ``postgres``."""

    dsn = _as_redacted_dsn(database_url)
    parsed = make_url(dsn.secret())
    db_name = parsed.database
    if db_name is None:
        raise RuntimeError(f"DSN sin nombre de base. {dsn}") from None

    async def create_if_missing() -> None:
        engine = create_async_engine(
            parsed.set(database="postgres").render_as_string(hide_password=False),
            isolation_level="AUTOCOMMIT",
            connect_args={
                "timeout": _PING_CONNECT_TIMEOUT,
                "command_timeout": _PING_CONNECT_TIMEOUT,
            },
        )
        try:
            async with asyncio.timeout(_PING_TOTAL_TIMEOUT):
                async with engine.connect() as connection:
                    exists = await connection.scalar(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": db_name},
                    )
                    if exists is None:
                        await connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        finally:
            await engine.dispose()

    try:
        asyncio.run(create_if_missing())
    except Exception as exc:
        if _is_connection_error(exc):
            raise ConnectionError(
                f"No se pudo crear la base de test ({type(exc).__name__}). {dsn}"
            ) from None
        raise RuntimeError(
            f"No se pudo crear la base de test ({type(exc).__name__}). {dsn}"
        ) from None


def _ping_postgres(database_url: str | RedactedDsn) -> None:
    dsn = _as_redacted_dsn(database_url)

    async def ping() -> None:
        engine = create_async_engine(
            dsn.secret(),
            pool_pre_ping=False,
            connect_args={
                "timeout": _PING_CONNECT_TIMEOUT,
                "command_timeout": _PING_CONNECT_TIMEOUT,
            },
        )
        try:
            async with asyncio.timeout(_PING_TOTAL_TIMEOUT):
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(ping())
    except Exception as exc:
        detail = str(exc).lower()
        if _is_connection_error(exc):
            raise ConnectionError(
                f"Postgres de test no está disponible ({type(exc).__name__}). {dsn}"
            ) from None
        if "does not exist" in detail and "database" in detail:
            raise _MissingDatabase(
                f"La base de test no existe ({type(exc).__name__}). {dsn}"
            ) from None
        raise RuntimeError(
            f"Postgres de test rechazó la conexión ({type(exc).__name__}). {dsn}"
        ) from None


def apply_test_migrations(database_url: str | RedactedDsn) -> None:
    """Aplica ``alembic upgrade head``. Un fallo de migración es error, no skip."""

    from app.config import get_settings

    dsn = _as_redacted_dsn(database_url)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", dsn.secret())
        get_settings.cache_clear()
        try:
            command.upgrade(alembic_config(dsn), "head")
        except Exception as exc:
            get_settings.cache_clear()
            raise RuntimeError(
                "No se pudieron aplicar las migraciones de test "
                f"({type(exc).__name__}). {dsn}"
            ) from None
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def require_postgres() -> RedactedDsn:
    """Salta o falla si Postgres no responde. Nunca imprime la contraseña."""

    dsn = RedactedDsn(resolve_test_database_url())
    try:
        _ping_postgres(dsn)
    except ConnectionError, _MissingDatabase:
        try:
            ensure_database_exists(dsn)
            _ping_postgres(dsn)
        except ConnectionError as exc:
            _skip_or_fail_postgres(
                "Postgres de test no está disponible. "
                f"En local: docker compose up -d. {exc}"
            )
    return dsn


@pytest.fixture(scope="session")
def migrated_test_database(require_postgres: RedactedDsn) -> Iterator[None]:
    """Aplica las migraciones en la BD de test. Un fallo de migración es error."""

    apply_test_migrations(require_postgres)
    yield
    from app.config import get_settings

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

    dsn = RedactedDsn(resolve_test_database_url())
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    get_settings.cache_clear()
    get_app.cache_clear()
    engine = create_async_engine(dsn.secret(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )

            async def _override_session() -> AsyncIterator[AsyncSession]:
                yield session

            application = get_app()
            application.dependency_overrides[get_session] = _override_session
            try:
                yield session
            finally:
                application.dependency_overrides.pop(get_session, None)
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()
        get_app.cache_clear()


@pytest.fixture
async def session_client(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    require_postgres: RedactedDsn,
) -> AsyncIterator[Any]:
    """Cliente HTTP en el mismo loop, con get_session ligado a db_session."""

    from httpx2 import ASGITransport, AsyncClient

    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EXPOSE_ERRORS", "false")
    monkeypatch.setenv("ENABLE_EXAMPLE_ROUTES", "true")
    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    application = create_app()
    _mount_probes(application)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.pop(get_session, None)
    get_settings.cache_clear()
