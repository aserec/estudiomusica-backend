import asyncio
import errno
import hashlib
import logging
import os
import re
import secrets
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.db import (
    RedactedDsn,
    SessionDep,
    SessionStreamingDep,
    _get_session,
    asyncpg_url_and_connect_args,
    redact_database_url,
)
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
PRODUCTION_AUTH_SECRET = "production-test-auth-secret-at-least-32ch"
# Nombre único por ejecución: pid + host + boot_id + aleatorio. Dos
# contenedores con el mismo pid no comparten rol. El advisory lock es la
# única prueba de vida; el pid queda como pista.
_PRODUCTION_TEST_ROLE_PREFIX = "em_prod_tests_"
_PRODUCTION_TEST_ROLE_RE = re.compile(
    r"em_prod_tests_[1-9][0-9]{0,9}(?:_[A-Za-z0-9_]+)?"
)


def _sanitize_role_token(value: str, limit: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value)
    return (cleaned or "x")[:limit]


def _production_test_role_name() -> str:
    host = _sanitize_role_token(socket.gethostname(), 12)
    boot = "b"
    with suppress(OSError):
        boot = _sanitize_role_token(
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii"),
            8,
        )
    return (
        f"{_PRODUCTION_TEST_ROLE_PREFIX}{os.getpid()}_"
        f"{host}_{boot}_{secrets.token_hex(4)}"
    )


PRODUCTION_TEST_USER = _production_test_role_name()
_PRODUCTION_TEST_EMAIL_PREFIX = "prod-"
# Serializa el barrido de huérfanos entre pytest concurrentes.
_SWEEP_ADVISORY_LOCK_KEY1 = 0x454D50  # EMP
_SWEEP_ADVISORY_LOCK_KEY2 = 0x535750  # SWP
_SWEEP_COMMAND_TIMEOUT = 60.0
_SAFE_PG_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_log = logging.getLogger(__name__)
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
    "AUTH_SECRET": "test-auth-secret-not-the-dev-default",
    "AUTH_COOKIE_NAME": "session",
    "AUTH_SESSION_TTL_SECONDS": "604800",
    "BOOTSTRAP_ADMIN_EMAILS": "",
    "AUTH_ARGON2_TIME_COST": "1",
    "AUTH_ARGON2_MEMORY_COST": "8192",
    "AUTH_ARGON2_PARALLELISM": "1",
}

PROBE_FILE_BYTES = b"file-response-body\n" * 8
_PROBE_FILE: Path | None = None


def _is_test_database_name(name: str) -> bool:
    """Igual que settings: no distinguir mayúsculas en el sufijo ``_test``."""

    return name.casefold().endswith("_test")


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
    if not _is_test_database_name(name):
        raise pytest.UsageError(
            f"{env_name} debe apuntar a una base cuyo nombre termina "
            "en '_test' para no tocar desarrollo. Recibido: "
            f"{redact_database_url(url)}"
        )
    return url


def derive_alembic_database_url(test_url: str) -> str:
    parsed = make_url(test_url)
    name = parsed.database or ""
    stem = name[: -len("_test")] if _is_test_database_name(name) else name
    return parsed.set(database=f"{stem}_alembic_test").render_as_string(
        hide_password=False
    )


def resolve_test_database_url() -> str:
    from app.config.settings import _normalize_sslmode

    return _normalize_sslmode(
        require_test_database_url(
            os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
        )
    )


def resolve_alembic_database_url() -> str:
    from app.config.settings import _normalize_sslmode

    raw = os.environ.get("ALEMBIC_TEST_DATABASE_URL")
    if raw is None:
        raw = derive_alembic_database_url(resolve_test_database_url())
    return _normalize_sslmode(
        require_test_database_url(raw, env_name="ALEMBIC_TEST_DATABASE_URL")
    )


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
def validate_require_postgres() -> None:
    """Un REQUIRE_POSTGRES desconocido aborta siempre, también con PG arriba."""

    try:
        _postgres_required()
    except pytest.UsageError as exc:
        pytest.exit(str(exc), returncode=2)


@pytest.fixture(scope="session", autouse=True)
def validate_require_tls_tests() -> None:
    """Un REQUIRE_TLS_TESTS desconocido aborta siempre, también con Docker."""

    try:
        _tls_required()
    except pytest.UsageError as exc:
        pytest.exit(str(exc), returncode=2)


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
    async def session_probe(session: SessionDep) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        return {"session": "ok"}

    @application.post("/api/v1/_test/session/{label}", status_code=201)
    async def session_create(label: str, session: SessionDep) -> dict[str, str]:
        await session.execute(
            text(
                "INSERT INTO session_probes "
                "(id, label, created_at, updated_at) "
                "VALUES ("
                "gen_random_uuid(), :label, "
                "clock_timestamp(), clock_timestamp()"
                ")"
            ),
            {"label": label},
        )
        return {"label": label}

    @application.get("/api/v1/_test/session/{label}")
    async def session_get(label: str, session: SessionDep) -> dict[str, str | None]:
        found = await session.scalar(
            text("SELECT label FROM session_probes WHERE label = :label"),
            {"label": label},
        )
        return {"label": found}

    @application.get("/api/v1/_test/session-stream")
    async def session_stream(session: SessionDep) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            found = await session.scalar(text("SELECT 1"))
            yield f"row={found}"

        return StreamingResponse(generate(), media_type="text/plain")

    @application.get("/api/v1/_test/session-stream-ok")
    async def session_stream_ok(session: SessionStreamingDep) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            found = await session.scalar(text("SELECT 1"))
            yield f"row={found}"

        return StreamingResponse(generate(), media_type="text/plain")

    @application.get("/api/v1/_test/session-stream-write/{label}")
    async def session_stream_write(
        label: str, session: SessionStreamingDep
    ) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            await session.execute(
                text(
                    "INSERT INTO session_probes "
                    "(id, label, created_at, updated_at) "
                    "VALUES ("
                    "gen_random_uuid(), :label, "
                    "clock_timestamp(), clock_timestamp()"
                    ")"
                ),
                {"label": label},
            )
            yield f"row={label}"

        return StreamingResponse(generate(), media_type="text/plain")


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "http://testserver",
    **env: str,
) -> Iterator[TestClient]:
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
        with TestClient(application, base_url=base_url) as test_client:
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


def production_settings_env(
    database_url: str | RedactedDsn, **overrides: str
) -> dict[str, str]:
    """Variables de production reutilizables (auth, roles, health)."""

    raw = (
        database_url.secret() if isinstance(database_url, RedactedDsn) else database_url
    )
    env = {
        "APP_ENV": "production",
        "EXPOSE_ERRORS": "false",
        "ENABLE_EXAMPLE_ROUTES": "false",
        "AUTH_SECRET": PRODUCTION_AUTH_SECRET,
        "DATABASE_URL": raw,
    }
    env.update(overrides)
    return env


def _pg_ident(name: str) -> str:
    if not _SAFE_PG_IDENT.match(name):
        raise ValueError("identificador Postgres inválido")
    return f'"{name}"'


def _pg_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def production_database_url(test_url: str | RedactedDsn, password: str) -> RedactedDsn:
    """DSN de la base ``*_test`` con rol de production, sin password por defecto."""

    raw = test_url.secret() if isinstance(test_url, RedactedDsn) else test_url
    parsed = make_url(require_test_database_url(raw))
    return RedactedDsn(
        parsed.set(
            username=PRODUCTION_TEST_USER,
            password=password,
        ).render_as_string(hide_password=False)
    )


def _admin_engine(admin: RedactedDsn) -> AsyncEngine:
    return _create_async_engine(
        admin.secret(),
        isolation_level="AUTOCOMMIT",
        connect_args={
            "timeout": _PING_CONNECT_TIMEOUT,
            "command_timeout": _PING_CONNECT_TIMEOUT,
        },
    )


def _sweep_admin_engine(admin: RedactedDsn) -> AsyncEngine:
    """Motor transaccional: ``pg_advisory_xact_lock`` vive hasta el COMMIT."""

    return _create_async_engine(
        admin.secret(),
        connect_args={
            "timeout": _PING_CONNECT_TIMEOUT,
            "command_timeout": _SWEEP_COMMAND_TIMEOUT,
        },
    )


def _role_advisory_lock_keys(name: str) -> tuple[int, int]:
    """Dos int4 estables por rol; el mismo espacio que el lock de sesión."""

    digest = hashlib.sha256(f"em-prod-role:{name}".encode()).digest()
    key1 = int.from_bytes(digest[0:4], "big") & 0x7FFFFFFF
    key2 = int.from_bytes(digest[4:8], "big") & 0x7FFFFFFF
    return key1, key2


class _SessionAdvisoryLock:
    """Mantiene ``pg_try_advisory_lock`` en un hilo durante toda la sesión."""

    def __init__(self, admin: RedactedDsn, name: str) -> None:
        self._admin = admin
        self._name = name
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._acquired = False
        self._lost = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"em-role-lock-{name}", daemon=True
        )

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def lost(self) -> bool:
        return self._lost

    def start(self) -> bool:
        self._thread.start()
        if not self._ready.wait(timeout=15):
            self.stop()
            raise RuntimeError(f"timeout al marcar la vida del rol {self._name}")
        if self._error is not None:
            raise RuntimeError(
                f"no se pudo marcar la vida del rol {self._name} "
                f"({type(self._error).__name__})"
            ) from None
        return self._acquired

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        try:
            asyncio.run(self._hold())
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    async def _hold(self) -> None:
        engine = _create_async_engine(
            self._admin.secret(),
            isolation_level="AUTOCOMMIT",
            connect_args={"timeout": 5, "command_timeout": 5},
        )
        connection = None
        try:
            connection = await engine.connect()
            key1, key2 = _role_advisory_lock_keys(self._name)
            got = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:k1, :k2)"),
                {"k1": key1, "k2": key2},
            )
            self._acquired = bool(got)
            self._ready.set()
            if not self._acquired:
                return
            while not self._stop.is_set():
                try:
                    await connection.scalar(text("SELECT 1"))
                except Exception:
                    self._lost = True
                    return
                await asyncio.sleep(0.5)
            unlocked: object = False
            try:
                unlocked = await asyncio.wait_for(
                    connection.scalar(
                        text("SELECT pg_advisory_unlock(:k1, :k2)"),
                        {"k1": key1, "k2": key2},
                    ),
                    timeout=2,
                )
            except Exception:
                self._lost = True
            else:
                if not unlocked:
                    self._lost = True
        finally:
            if connection is not None:
                with suppress(Exception):
                    await asyncio.wait_for(connection.close(), timeout=2)
            with suppress(Exception):
                await asyncio.wait_for(engine.dispose(), timeout=2)


_ROLE_LIFE_LOCK: _SessionAdvisoryLock | None = None


def _hold_production_role_life_lock(admin: RedactedDsn) -> None:
    global _ROLE_LIFE_LOCK
    if _ROLE_LIFE_LOCK is not None:
        return
    lock = _SessionAdvisoryLock(admin, PRODUCTION_TEST_USER)
    if not lock.start():
        lock.stop()
        raise RuntimeError(
            "no se pudo marcar la vida del rol de production-tests "
            f"({PRODUCTION_TEST_USER})"
        )
    _ROLE_LIFE_LOCK = lock


def _release_production_role_life_lock() -> None:
    lock = _ROLE_LIFE_LOCK
    if lock is not None:
        lock.stop()


async def _role_life_lock_is_free(connection: Any, name: str) -> bool:
    key1, key2 = _role_advisory_lock_keys(name)
    got = await connection.scalar(
        text("SELECT pg_try_advisory_xact_lock(:k1, :k2)"),
        {"k1": key1, "k2": key2},
    )
    return bool(got)


def _holds_own_life_lock() -> bool:
    lock = _ROLE_LIFE_LOCK
    return (
        lock is not None and bool(lock.acquired) and lock._name == PRODUCTION_TEST_USER
    )


async def _should_drop_orphan(connection: Any, name: str) -> bool:
    if not _orphan_production_test_role(name):
        return False
    return await _role_life_lock_is_free(connection, name)


def _orphan_production_test_role(name: str) -> bool:
    """Candidato a huérfano por nombre. La vida la decide el advisory lock."""

    if name == PRODUCTION_TEST_USER:
        return False
    return _PRODUCTION_TEST_ROLE_RE.fullmatch(name) is not None


async def _other_owned_databases(connection: Any, name: str) -> list[str]:
    """Bases distintas a la actual donde el rol tiene dependencias."""

    rows = (
        await connection.execute(
            text(
                "SELECT d.datname "
                "FROM pg_catalog.pg_shdepend s "
                "JOIN pg_catalog.pg_database d ON d.oid = s.dbid "
                "JOIN pg_catalog.pg_roles r ON r.oid = s.refobjid "
                "WHERE r.rolname = :name "
                "AND s.dbid <> 0 "
                "AND d.datname <> current_database()"
            ),
            {"name": name},
        )
    ).all()
    seen: list[str] = []
    for (datname,) in rows:
        if datname not in seen:
            seen.append(datname)
    return seen


async def _databases_owned_by_role(connection: Any, name: str) -> list[str]:
    """Bases cuyo dueño es el rol (pg_shdepend.dbid = 0; DROP OWNED no las toca)."""

    rows = (
        await connection.execute(
            text(
                "SELECT d.datname "
                "FROM pg_catalog.pg_database d "
                "JOIN pg_catalog.pg_roles r ON r.oid = d.datdba "
                "WHERE r.rolname = :name "
                "ORDER BY 1"
            ),
            {"name": name},
        )
    ).all()
    return [datname for (datname,) in rows]


async def _drop_owned_in_database(admin: RedactedDsn, database: str, name: str) -> None:
    parsed = make_url(admin.secret())
    other = RedactedDsn(
        parsed.set(database=database).render_as_string(hide_password=False)
    )
    engine = _admin_engine(other)
    try:
        async with engine.connect() as connection:
            role = _pg_ident(name)
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
                {"name": name},
            )
            if exists is None:
                return
            await connection.execute(text(f"DROP OWNED BY {role}"))
    finally:
        await engine.dispose()


async def _drop_named_production_test_role(
    connection: Any, name: str, *, admin: RedactedDsn | None = None
) -> None:
    role = _pg_ident(name)
    exists = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": name},
    )
    if exists is None:
        return
    for database in await _databases_owned_by_role(connection, name):
        _log.warning(
            "el rol huérfano %s es dueño de la base %s "
            "(dbid=0; DROP OWNED no borra bases; "
            "no se tocan las que no acaban en _test)",
            name,
            database,
        )
    if admin is not None:
        for database in await _other_owned_databases(connection, name):
            if _is_test_database_name(database):
                await _drop_owned_in_database(admin, database, name)
                continue
            raise RuntimeError(
                f"el rol {name} tiene objetos en {database}, fuera de una base *_test"
            )
    await connection.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = :name AND pid <> pg_backend_pid()"
        ),
        {"name": name},
    )
    await connection.execute(text(f"DROP OWNED BY {role}"))
    await connection.execute(text(f"DROP ROLE IF EXISTS {role}"))


async def _drop_production_test_role(admin: RedactedDsn) -> None:
    engine = _sweep_admin_engine(admin)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
                {
                    "k1": _SWEEP_ADVISORY_LOCK_KEY1,
                    "k2": _SWEEP_ADVISORY_LOCK_KEY2,
                },
            )
            rows = (
                await connection.execute(
                    text(
                        "SELECT rolname FROM pg_roles "
                        "WHERE rolname LIKE 'em_prod_tests\\_%' ESCAPE '\\' "
                        "OR rolname = :own"
                    ),
                    {"own": PRODUCTION_TEST_USER},
                )
            ).all()
            names = [name for (name,) in rows]
            if PRODUCTION_TEST_USER not in names:
                names.append(PRODUCTION_TEST_USER)
            for name in names:
                is_own = name == PRODUCTION_TEST_USER
                if is_own:
                    if not _holds_own_life_lock():
                        continue
                elif not await _should_drop_orphan(connection, name):
                    continue
                try:
                    async with connection.begin_nested():
                        await _drop_named_production_test_role(
                            connection, name, admin=admin
                        )
                except Exception as exc:
                    if name == PRODUCTION_TEST_USER:
                        raise
                    _log.warning(
                        "No se pudo borrar el rol huérfano %s (%s)",
                        name,
                        type(exc).__name__,
                    )
    finally:
        await engine.dispose()


async def _cleanup_production_test_rows(admin: RedactedDsn) -> None:
    engine = _admin_engine(admin)
    try:
        async with engine.connect() as connection:
            users = await connection.scalar(text("SELECT to_regclass('public.users')"))
            if users is None:
                return
            await connection.execute(
                text(
                    "DELETE FROM auth_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email LIKE :prefix)"
                ),
                {"prefix": f"{_PRODUCTION_TEST_EMAIL_PREFIX}%"},
            )
            await connection.execute(
                text("DELETE FROM users WHERE email LIKE :prefix"),
                {"prefix": f"{_PRODUCTION_TEST_EMAIL_PREFIX}%"},
            )
    finally:
        await engine.dispose()


def drop_production_test_role(admin_url: str | RedactedDsn) -> None:
    admin = _as_redacted_dsn(admin_url)
    drop_error: BaseException | None = None
    try:
        asyncio.run(_drop_production_test_role(admin))
        asyncio.run(_cleanup_production_test_rows(admin))
    except Exception as exc:
        drop_error = exc
    finally:
        _release_production_role_life_lock()
    lock = _ROLE_LIFE_LOCK
    lost = lock is not None and lock.lost
    if lost:
        raise RuntimeError(
            "se perdió el lock de vida del rol de production-tests"
        ) from None
    if drop_error is None:
        return
    if _is_connection_error(drop_error):
        return
    raise RuntimeError(
        "No se pudo borrar el rol de production-tests "
        f"({type(drop_error).__name__}). {admin}"
    ) from None


def ensure_production_test_role(admin_url: str | RedactedDsn) -> RedactedDsn:
    """Crea el rol de tests de production (sin SUPERUSER) y devuelve el DSN."""

    admin = _as_redacted_dsn(admin_url)
    password = secrets.token_urlsafe(32)
    prod_dsn = production_database_url(admin, password)
    parsed = make_url(admin.secret())
    database = parsed.database or ""
    role = _pg_ident(PRODUCTION_TEST_USER)
    dbname = _pg_ident(database)

    async def create_role() -> None:
        await _drop_production_test_role(admin)
        await _cleanup_production_test_rows(admin)
        engine = _admin_engine(admin)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB "
                        f"NOCREATEROLE INHERIT PASSWORD {_pg_literal(password)}"
                    )
                )
                await connection.execute(
                    text(f"GRANT CONNECT ON DATABASE {dbname} TO {role}")
                )
                await connection.execute(
                    text(f"GRANT USAGE ON SCHEMA public TO {role}")
                )
                await connection.execute(
                    text(
                        "GRANT SELECT, INSERT, UPDATE, DELETE "
                        f"ON ALL TABLES IN SCHEMA public TO {role}"
                    )
                )
                await connection.execute(
                    text(
                        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                        f"TO {role}"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES "
                        f"TO {role}"
                    )
                )
                citext = await connection.scalar(
                    text("SELECT 1 FROM pg_type WHERE typname = 'citext'")
                )
                if citext is not None:
                    await connection.execute(
                        text(f"GRANT USAGE ON TYPE citext TO {role}")
                    )
        finally:
            await engine.dispose()

    try:
        _hold_production_role_life_lock(admin)
    except Exception:
        raise
    try:
        asyncio.run(create_role())
        _ping_postgres(prod_dsn)
    except ConnectionError:
        _release_production_role_life_lock()
        raise
    except Exception as exc:
        with suppress(Exception):
            asyncio.run(_drop_production_test_role(admin))
        _release_production_role_life_lock()
        if _is_connection_error(exc):
            raise ConnectionError(
                "No se pudo crear el rol de production-tests "
                f"({type(exc).__name__}). {admin}"
            ) from None
        raise RuntimeError(
            "No se pudo crear el rol de production-tests "
            f"({type(exc).__name__}). {admin}"
        ) from None
    return prod_dsn


@pytest.fixture
def offline_production_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Production sin Postgres: DSN dummy para errores y /health/db 503."""

    yield from _build_client(
        monkeypatch,
        **production_settings_env(UNUSED_DATABASE_URL),
    )


@pytest.fixture
def production_client(
    monkeypatch: pytest.MonkeyPatch,
    production_test_database_url: RedactedDsn,
    migrated_test_database: None,
    require_postgres: RedactedDsn,
) -> Iterator[TestClient]:
    """Cliente production contra la base ``*_test``, sin credenciales por defecto.

    ``APP_ENV=production``, ``AUTH_SECRET`` fuerte y DSN de ``estudiomusica_test``
    (o ``TEST_DATABASE_URL``) con el rol ``em_prod_tests_<pid>_<host>_…``. El DSN viaja
    como ``RedactedDsn`` para no filtrar la contraseña con ``--showlocals``.
    """

    monkeypatch.setattr("app.config.settings.allow_test_database", lambda: True)
    try:
        yield from _build_client(
            monkeypatch,
            base_url="https://testserver",
            **production_settings_env(production_test_database_url),
        )
    finally:
        with suppress(Exception):
            asyncio.run(_cleanup_production_test_rows(require_postgres))


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


def _env_flag(name: str) -> bool:
    """Los mismos booleanos que Pydantic; un valor desconocido aborta."""

    raw = os.environ.get(name, "").strip().lower()
    if raw in _PYDANTIC_TRUE:
        return True
    if raw in _PYDANTIC_FALSE:
        return False
    raise pytest.UsageError(
        f"{name} no es un booleano válido "
        f"(true/false, 1/0, yes/no, on/off, y/n, t/f). Recibido: {raw!r}"
    )


def _postgres_required() -> bool:
    """Los mismos booleanos que Pydantic; un valor desconocido aborta."""

    return _env_flag("REQUIRE_POSTGRES")


def _tls_required() -> bool:
    """Los mismos booleanos que Pydantic; un valor desconocido aborta."""

    return _env_flag("REQUIRE_TLS_TESTS")


class _MissingDatabase(RuntimeError):
    """La instancia responde pero la base de test no existe."""


def _skip_or_fail_postgres(message: str) -> None:
    if _postgres_required() or _tls_required():
        raise pytest.fail.Exception(message) from None
    raise pytest.skip.Exception(message) from None


def _skip_or_fail_tls(message: str) -> None:
    if _tls_required():
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


def _dsn_secret_values(dsn: RedactedDsn) -> tuple[str, ...]:
    """Contraseñas del netloc y de ``password`` / ``sslpassword`` en la query."""

    from urllib.parse import parse_qs, urlparse

    values: list[str] = []
    try:
        parsed = make_url(dsn.secret())
    except Exception:
        parsed = None
    if parsed is not None:
        if parsed.password:
            values.append(parsed.password)
        for key, raw in (parsed.query or {}).items():
            if str(key).lower() not in {"password", "sslpassword"}:
                continue
            if isinstance(raw, (list, tuple)):
                values.extend(str(item) for item in raw if item)
                continue
            if raw:
                values.append(str(raw))
    try:
        raw_query = urlparse(dsn.secret()).query
    except Exception:
        raw_query = ""
    if raw_query:
        for key, items in parse_qs(raw_query, keep_blank_values=True).items():
            if key.lower() not in {"password", "sslpassword"}:
                continue
            values.extend(item for item in items if item)
    unique: list[str] = []
    seen: set[str] = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return tuple(unique)


def _redacted_exception_detail(exc: BaseException, dsn: RedactedDsn) -> str:
    """Texto de la causa sin la contraseña del DSN."""

    from urllib.parse import quote

    text = str(exc)
    secret = dsn.secret()
    if secret:
        text = text.replace(secret, str(dsn))
    for password in _dsn_secret_values(dsn):
        text = text.replace(password, "***")
        text = text.replace(quote(password, safe=""), "***")
    text = " ".join(text.split())
    if len(text) > 240:
        text = text[:240] + "…"
    return text


def _create_async_engine(database_url: str, **kwargs: Any) -> AsyncEngine:
    """``create_async_engine`` con ``sslrootcert`` → ``SSLContext``."""

    url, ssl_args = asyncpg_url_and_connect_args(database_url)
    connect_args = dict(kwargs.pop("connect_args", {}) or {})
    connect_args.update(ssl_args)
    return create_async_engine(url, connect_args=connect_args, **kwargs)


def ensure_database_exists(database_url: str | RedactedDsn) -> None:
    """Crea la base conectando a la BD de mantenimiento ``postgres``."""

    dsn = _as_redacted_dsn(database_url)
    parsed = make_url(dsn.secret())
    db_name = parsed.database
    if db_name is None:
        raise RuntimeError(f"DSN sin nombre de base. {dsn}") from None

    async def create_if_missing() -> None:
        engine = _create_async_engine(
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


def drop_database_if_exists(database_url: str | RedactedDsn) -> None:
    """Borra una base de test. Conecta a la BD de mantenimiento ``postgres``."""

    dsn = _as_redacted_dsn(database_url)
    db_name = make_url(dsn.secret()).database
    if db_name is None:
        raise RuntimeError(f"DSN sin nombre de base. {dsn}") from None
    if not _is_test_database_name(db_name):
        raise RuntimeError(f"Solo se pueden borrar bases *_test. {dsn}") from None

    async def drop() -> None:
        engine = _create_async_engine(
            make_url(dsn.secret())
            .set(database="postgres")
            .render_as_string(hide_password=False),
            isolation_level="AUTOCOMMIT",
            connect_args={
                "timeout": _PING_CONNECT_TIMEOUT,
                "command_timeout": _PING_CONNECT_TIMEOUT,
            },
        )
        try:
            async with asyncio.timeout(_PING_TOTAL_TIMEOUT):
                async with engine.connect() as connection:
                    await connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :name AND pid <> pg_backend_pid()"
                        ),
                        {"name": db_name},
                    )
                    await connection.execute(
                        text(f'DROP DATABASE IF EXISTS "{db_name}"')
                    )
        finally:
            await engine.dispose()

    try:
        asyncio.run(drop())
    except Exception as exc:
        detail = _redacted_exception_detail(exc, dsn)
        if _is_connection_error(exc):
            raise ConnectionError(
                f"No se pudo borrar la base de test "
                f"({type(exc).__name__}: {detail}). {dsn}"
            ) from None
        raise RuntimeError(
            f"No se pudo borrar la base de test ({type(exc).__name__}: {detail}). {dsn}"
        ) from None


def _ping_postgres(database_url: str | RedactedDsn) -> None:
    from app.config.settings import _normalize_sslmode

    dsn = _as_redacted_dsn(database_url)

    async def ping() -> None:
        engine = _create_async_engine(
            _normalize_sslmode(dsn.secret()),
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


_LEFTOVER_TEST_TABLES = (
    "session_probes",
    "session_probe_leak",
    "session_probe_expire",
    "mixin_probes",
    "migrated_trigger_probes",
    "ListaCanciones",
    "order",
    "schema_trigger_probes",
)
_LEFTOVER_TEST_DATABASES = (
    "autogen_sa_probe_test",
    "autogen_hook_probe_test",
    "schema_autogen_probe_test",
    "autogen_empty_probe_test",
    "orphan_crossdb_probe_test",
)


def _drop_leftover_test_tables(dsn: RedactedDsn) -> None:
    async def drop() -> None:
        engine = _create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                for name in _LEFTOVER_TEST_TABLES:
                    await connection.execute(
                        text(f'DROP TABLE IF EXISTS "{name}" CASCADE')
                    )
                await connection.execute(text("DROP SCHEMA IF EXISTS catalogo CASCADE"))
                await connection.execute(text("DROP SCHEMA IF EXISTS ajeno CASCADE"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(drop())
    except Exception as exc:
        if _is_connection_error(exc):
            raise ConnectionError(
                f"No se pudieron borrar tablas de test ({type(exc).__name__}). {dsn}"
            ) from None
        raise RuntimeError(
            f"No se pudieron borrar tablas de test ({type(exc).__name__}). {dsn}"
        ) from None


def _drop_leftover_test_databases(dsn: RedactedDsn) -> None:
    parsed = make_url(dsn.secret())
    for name in _LEFTOVER_TEST_DATABASES:
        drop_database_if_exists(
            RedactedDsn(parsed.set(database=name).render_as_string(hide_password=False))
        )


@pytest.fixture(scope="session")
def production_test_database_url(
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> Iterator[RedactedDsn]:
    """DSN de la base ``*_test`` con rol de production (sin password por defecto).

    El rol se borra al terminar la sesión, también si la suite falla.
    """

    try:
        dsn = ensure_production_test_role(require_postgres)
    except ConnectionError as exc:
        _skip_or_fail_postgres(
            "No se pudo preparar el rol de production-tests. "
            f"En local: docker compose up -d. {exc}"
        )
        raise AssertionError("unreachable") from exc
    try:
        yield dsn
    finally:
        drop_production_test_role(require_postgres)


@pytest.fixture(scope="session")
def migrated_test_database(require_postgres: RedactedDsn) -> Iterator[None]:
    """Aplica las migraciones en la BD de test. Un fallo de migración es error."""

    _drop_leftover_test_tables(require_postgres)
    _drop_leftover_test_databases(require_postgres)
    drop_production_test_role(require_postgres)
    apply_test_migrations(require_postgres)
    try:
        yield
    finally:
        from app.config import get_settings

        drop_production_test_role(require_postgres)
        _drop_leftover_test_tables(require_postgres)
        _drop_leftover_test_databases(require_postgres)
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
    engine = _create_async_engine(dsn.secret(), pool_pre_ping=True)
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
            application.dependency_overrides[_get_session] = _override_session
            try:
                yield session
            finally:
                application.dependency_overrides.pop(_get_session, None)
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
    """Cliente HTTP en el mismo loop, con _get_session ligado a db_session."""

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

    application.dependency_overrides[_get_session] = _override_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.pop(_get_session, None)
    get_settings.cache_clear()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    lock = _ROLE_LIFE_LOCK
    if lock is None or not lock.lost:
        return
    if exitstatus == 0:
        session.exitstatus = 1
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line(
            "se perdió el lock de vida del rol de production-tests",
            red=True,
        )
