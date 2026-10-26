import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import RedactedDsn
from app.schemas.health import DatabaseHealthResponse
from app.services import health as health_service

SECRET_PASSWORD = "supersecret-password-do-not-leak"


def test_health_db_ok_when_postgres_is_up(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/api/v1/health/db")

    assert response.status_code == 200
    assert response.json() == {"db": "ok"}
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-api-version"] == "v1"


def test_root_health_db_alias(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"db": "ok"}


def test_health_db_ok_in_production_without_examples(
    production_client: TestClient,
) -> None:
    """health/db se monta siempre; /examples no.

    En production Settings rechaza bases ``*_test`` y la contraseña del
    compose, así que el cliente usa un DSN dummy: 503 es la sonda, no un
    sobre de error.
    """

    response = production_client.get("/api/v1/health/db")

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert production_client.get("/api/v1/examples").status_code == 404


def test_health_db_down_without_credentials(
    down_db_client: TestClient,
) -> None:
    response = down_db_client.get("/api/v1/health/db")

    assert response.status_code == 503
    body = response.json()
    assert body == {"db": "down"}
    assert "error" not in body
    text = response.text
    assert SECRET_PASSWORD not in text
    assert "leaky_user" not in text
    assert "DATABASE_URL" not in text
    assert "postgresql+asyncpg" not in text


def test_health_db_down_is_not_business_error_envelope(
    down_db_production_client: TestClient,
) -> None:
    response = down_db_production_client.get("/api/v1/health/db")

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert list(response.json().keys()) == ["db"]


def test_health_db_down_does_not_leak_with_expose_errors(
    down_db_expose_errors_client: TestClient,
) -> None:
    response = down_db_expose_errors_client.get("/api/v1/health/db")

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert SECRET_PASSWORD not in response.text
    assert "message" not in response.json()


@pytest.fixture
def hang_timeout_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import asyncpg  # type: ignore[import-untyped]

    from tests.conftest import _build_client

    async def _hang(*_args: object, **_kwargs: object) -> object:
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")

    monkeypatch.setattr(asyncpg, "connect", _hang)
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_TIMEOUT="0.3",
    )


def test_health_db_returns_503_if_probe_hangs(
    hang_timeout_client: TestClient,
) -> None:
    started = time.monotonic()
    response = hang_timeout_client.get("/api/v1/health/db")
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert elapsed < 2.0


@contextmanager
def _silent_tcp_server() -> Iterator[int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = int(server.getsockname()[1])
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.2)
        held: list[socket.socket] = []
        try:
            while not stop.is_set():
                try:
                    conn, _addr = server.accept()
                except TimeoutError:
                    continue
                held.append(conn)
        finally:
            for conn in held:
                conn.close()
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=2)


@pytest.fixture
def silent_tcp_health_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from tests.conftest import _build_client

    with _silent_tcp_server() as port:
        yield from _build_client(
            monkeypatch,
            APP_ENV="development",
            EXPOSE_ERRORS="false",
            ENABLE_EXAMPLE_ROUTES="true",
            DB_HEALTH_TIMEOUT="0.4",
            DB_CONNECT_TIMEOUT="2",
            DATABASE_URL=(
                f"postgresql+asyncpg://leaky_user:{SECRET_PASSWORD}"
                f"@127.0.0.1:{port}/silent_test"
            ),
        )


def test_health_db_silent_tcp_server_stays_within_timeout(
    silent_tcp_health_client: TestClient,
) -> None:
    started = time.monotonic()
    response = silent_tcp_health_client.get("/api/v1/health/db")
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert SECRET_PASSWORD not in response.text
    assert elapsed < 1.5


def test_health_db_ok_body_has_only_db_key(
    db_client: TestClient, migrated_test_database: None
) -> None:
    response = db_client.get("/api/v1/health/db")
    assert response.status_code == 200
    assert list(response.json().keys()) == ["db"]
    assert response.json()["db"] == "ok"


def _frozen_url(require_postgres: RedactedDsn, port: int) -> str:
    from sqlalchemy.engine.url import make_url

    parsed = make_url(require_postgres.secret())
    return parsed.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)


def _dsn_with_ssl_query(url: str, **ssl_query: str | None) -> str:
    """Copia el DSN y sustituye ``ssl`` / ``sslmode``. ``None`` los quita."""

    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    query = {
        str(key): value
        for key, value in (parsed.query or {}).items()
        if str(key).lower() not in {"ssl", "sslmode"}
    }
    for key, value in ssl_query.items():
        if value is not None:
            query[key] = value
    return parsed.set(query=query).render_as_string(hide_password=False)


async def _count_app_backends(
    dsn: RedactedDsn,
    *,
    connect: Any | None = None,
) -> tuple[int, int]:
    """Backends de la app: sonda (application_name) y usuario, sin el inspector.

    Devuelve ``(health_backends, user_backends)``. El inspector se excluye
    con ``pid <> pg_backend_pid()`` para no inflar el recuento.
    """

    import asyncpg
    from sqlalchemy.engine.url import make_url

    from app.db import HEALTH_APPLICATION_NAME

    opener = connect if callable(connect) else asyncpg.connect
    parsed = make_url(dsn.secret())
    connection = await opener(
        host=parsed.host,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database=parsed.database,
    )
    try:
        health = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = $1 AND pid <> pg_backend_pid()",
            HEALTH_APPLICATION_NAME,
        )
        user = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE usename = $1 AND pid <> pg_backend_pid()",
            parsed.username,
        )
    finally:
        await connection.close()
    return int(health or 0), int(user or 0)


async def _run_frozen_health(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    *,
    freeze_on: str,
    health_timeout: float = 0.6,
    command_timeout: float = 8.0,
) -> tuple[float, DatabaseHealthResponse, int, int]:
    """Devuelve (elapsed, result, terminate_calls, main_checkedout)."""

    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import clear_engine, dispose_engine, get_engine
    from app.services.health import HEALTH_TIMEOUT_MARGIN_SECONDS
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    parsed = make_url(require_postgres.secret())
    proxy = FreezeAfterLoginProxy(
        parsed.host or "127.0.0.1",
        parsed.port or 5432,
        freeze_on=freeze_on,  # type: ignore[arg-type]
    )
    await proxy.start()
    frozen_url = _frozen_url(require_postgres, proxy.port)
    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", str(health_timeout))
    monkeypatch.setenv("DB_COMMAND_TIMEOUT", str(command_timeout))
    get_settings.cache_clear()
    clear_engine()
    main = get_engine()
    health_service.set_health_database_url(frozen_url)
    terminates = {"n": 0}
    original = health_service._terminate_driver_object

    def _spy(driver: object) -> None:
        terminates["n"] += 1
        original(driver)

    monkeypatch.setattr(health_service, "_terminate_driver_object", _spy)
    budget = health_timeout + HEALTH_TIMEOUT_MARGIN_SECONDS + 0.35
    try:
        started = time.monotonic()
        result = await health_service.check_database()
        elapsed = time.monotonic() - started
        checkedout = main.sync_engine.pool.checkedout()  # type: ignore[attr-defined]
        print(
            f"health freeze[{freeze_on}] elapsed={elapsed:.3f}s "
            f"timeout={health_timeout} command={command_timeout} "
            f"terminates={terminates['n']} main_checkedout={checkedout}",
            flush=True,
        )
        assert elapsed < budget, (
            f"elapsed={elapsed:.3f}s budget={budget:.3f}s freeze_on={freeze_on}"
        )
        return elapsed, result, terminates["n"], int(checkedout)
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        with suppress(Exception):
            await asyncio.wait_for(proxy.aclose(), timeout=2)


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_db_frozen_select_1_returns_503_quickly(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Congela SELECT 1 tras inicializar el dialecto."""

    from tests.conftest import _build_client

    elapsed, result, terminates, checkedout = await _run_frozen_health(
        monkeypatch, require_postgres, freeze_on="select_1"
    )
    assert result.db == "down"
    assert terminates >= 1
    assert checkedout == 0

    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import clear_engine
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    url = make_url(require_postgres.secret())
    proxy = FreezeAfterLoginProxy(url.host or "127.0.0.1", url.port or 5432)
    await proxy.start()
    frozen_url = _frozen_url(require_postgres, proxy.port)
    get_settings.cache_clear()
    clear_engine()
    client_iter = _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_TIMEOUT="0.6",
        DB_COMMAND_TIMEOUT="8",
        DATABASE_URL=frozen_url,
    )
    client = next(client_iter)
    try:
        started = time.monotonic()
        response = client.get("/api/v1/health/db")
        http_elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert response.json() == {"db": "down"}
        assert http_elapsed < 1.4
    finally:
        close = getattr(client_iter, "close", None)
        if callable(close):
            close()
        with suppress(Exception):
            await asyncio.wait_for(proxy.aclose(), timeout=2)
    assert elapsed < 1.4


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_db_frozen_connect_returns_503_quickly(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    elapsed, result, _terminates, checkedout = await _run_frozen_health(
        monkeypatch, require_postgres, freeze_on="connect"
    )
    assert result.db == "down"
    assert checkedout == 0
    assert elapsed < 1.4


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_db_frozen_rollback_returns_503_quickly(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    elapsed, result, terminates, checkedout = await _run_frozen_health(
        monkeypatch, require_postgres, freeze_on="rollback"
    )
    assert result.db == "down"
    assert terminates >= 1
    assert checkedout == 0
    assert elapsed < 1.4


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_db_does_not_touch_main_pool(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    from sqlalchemy import event, text

    from app.config import get_settings
    from app.db import clear_engine, dispose_engine, get_engine

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    engine = get_engine()
    connects = {"n": 0}

    def _on_connect(*_args: object, **_kwargs: object) -> None:
        connects["n"] += 1

    event.listen(engine.sync_engine, "connect", _on_connect)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        baseline = connects["n"]
        first = await health_service.check_database()
        second = await health_service.check_database()
        assert first.db == "ok"
        assert second.db == "ok"
        assert connects["n"] == baseline
    finally:
        event.remove(engine.sync_engine, "connect", _on_connect)
        await dispose_engine()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_db_alternating_freeze_does_not_accumulate_backends(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Sondas sanas y congeladas no dejan backends residuales.

    El recuento *después* de cada ciclo (antes: «backends máximos 0»)
    mide leftovers: la sonda ya cerró. Una sonda sana abre ≥1 backend
    de la app; ese pico se mide con la conexión aún abierta.
    """

    import warnings

    import asyncpg
    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import (
        clear_engine,
        dispose_engine,
        get_engine,
    )
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    parsed = make_url(require_postgres.secret())
    proxy = FreezeAfterLoginProxy(
        parsed.host or "127.0.0.1", parsed.port or 5432, freeze_on="none"
    )
    await proxy.start()
    frozen_url = _frozen_url(require_postgres, proxy.port)
    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "0.5")
    monkeypatch.setenv("DB_COMMAND_TIMEOUT", "30")
    get_settings.cache_clear()
    clear_engine()
    main = get_engine()
    health_service.set_health_database_url(frozen_url)
    original_connect = asyncpg.connect
    baseline_health, baseline_user = await _count_app_backends(
        require_postgres, connect=original_connect
    )
    peak = {"health": baseline_health, "user": baseline_user}
    leftover_after_cycle = {"health": 0, "user": 0}

    async def _connect_and_count(*args: object, **kwargs: object) -> object:
        connection = await original_connect(*args, **kwargs)
        health, user = await _count_app_backends(
            require_postgres, connect=original_connect
        )
        peak["health"] = max(peak["health"], health)
        peak["user"] = max(peak["user"], user)
        return connection

    monkeypatch.setattr(asyncpg, "connect", _connect_and_count)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for index in range(8):
                proxy.freeze_on = "none"
                healthy = await health_service.check_database()
                assert healthy.db == "ok"
                proxy.freeze_on = "select_1" if index % 2 == 0 else "connect"
                frozen = await health_service.check_database()
                assert frozen.db == "down"
                leftover_h, leftover_u = await _count_app_backends(
                    require_postgres, connect=original_connect
                )
                leftover_after_cycle["health"] = max(
                    leftover_after_cycle["health"], leftover_h
                )
                leftover_after_cycle["user"] = max(
                    leftover_after_cycle["user"], leftover_u
                )
            # leftover_after_cycle es residual *después* de cerrar
            # (el «backends máximos 0» anterior). El pico real se mide
            # con la conexión de la sonda aún abierta.
            print(
                f"alternating baseline health={baseline_health} "
                f"app_user={baseline_user} peak_during health={peak['health']} "
                f"app_user={peak['user']} leftover_after_cycle "
                f"health={leftover_after_cycle['health']} "
                f"app_user={leftover_after_cycle['user']}",
                flush=True,
            )
            assert peak["health"] >= 1
            assert leftover_after_cycle["health"] == 0
            assert leftover_after_cycle["user"] <= baseline_user
            assert main.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            await dispose_engine()
            end_health, end_user = await _count_app_backends(
                require_postgres, connect=original_connect
            )
            print(
                f"alternating leftover_after_dispose health={end_health} "
                f"app_user={end_user}",
                flush=True,
            )
            assert end_health == 0
            assert end_user <= baseline_user
            unclosed = [
                item
                for item in caught
                if issubclass(item.category, ResourceWarning)
                and "unclosed" in str(item.message).lower()
            ]
            assert unclosed == []
    finally:
        get_settings.cache_clear()
        with suppress(Exception):
            await asyncio.wait_for(proxy.aclose(), timeout=2)


@pytest.mark.asyncio
async def test_abandon_probe_cancels_pending_task() -> None:
    """T7: sin ``task.cancel()`` la sonda pendiente sigue viva."""

    async def hang() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(hang())
    try:
        health_service._abandon_probe(None, task)
        await asyncio.sleep(0)
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def test_expected_terminate_filter_downgrades_pool_error() -> None:
    """T12: el filtro baja ERROR+traza a WARNING sin traceback."""

    import logging

    filt = health_service._ExpectedTerminateFilter()
    record = logging.LogRecord(
        name="sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Exception terminating connection %s",
        args=("conn",),
        exc_info=(
            asyncio.CancelledError,
            asyncio.CancelledError(),
            None,
        ),
    )
    assert filt.filter(record) is True
    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"
    assert record.exc_info is None


def test_health_log_filter_does_not_attach_to_root_handlers() -> None:
    import logging

    root = logging.getLogger()
    before = [id(item) for handler in root.handlers for item in handler.filters]
    with health_service._probe_log_context():
        during = [id(item) for handler in root.handlers for item in handler.filters]
        pool = logging.getLogger("sqlalchemy.pool")
        assert any(
            isinstance(item, health_service._ExpectedTerminateFilter)
            for item in pool.filters
        )
    assert during == before
    after = [id(item) for handler in root.handlers for item in handler.filters]
    assert after == before


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"sslmode": "require"}, "require"),
        ({"ssl": "disable"}, "disable"),
        ({"ssl": "disable", "sslmode": "verify-full"}, "verify-full"),
        ({}, None),
    ],
)
def test_health_connect_kwargs_forwards_dsn_ssl(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, str],
    expected: str | None,
) -> None:
    """``ssl`` / ``sslmode`` del DSN llegan a asyncpg; sin ellos no se pasa."""

    from app.config import get_settings

    base = "postgresql+asyncpg://probe:secret@127.0.0.1:5432/probe_db"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", base)
    get_settings.cache_clear()
    health_service.set_health_database_url(_dsn_with_ssl_query(base, **query))
    try:
        kwargs = health_service._connect_kwargs()
        if expected is None:
            assert "ssl" not in kwargs
        else:
            assert kwargs["ssl"] == expected
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_probe_passes_sslmode_to_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La sonda reenvía sslmode a ``asyncpg.connect`` (no el prefer implícito)."""

    import asyncpg

    from app.config import get_settings

    captured: dict[str, object] = {}

    async def _fake_connect(**kwargs: object) -> object:
        captured.update(kwargs)
        raise OSError("stop-after-capture")

    base = "postgresql+asyncpg://probe:secret@127.0.0.1:5432/probe_db"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", base)
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "1")
    monkeypatch.setattr(asyncpg, "connect", _fake_connect)
    get_settings.cache_clear()
    health_service.set_health_database_url(_dsn_with_ssl_query(base, sslmode="require"))
    try:
        result = await health_service.check_database()
        assert result.db == "down"
        assert captured.get("ssl") == "require"
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_plain_proxy_works_without_sslmode(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Sin ssl/sslmode el proxy en claro sigue congelando SELECT 1."""

    from sqlalchemy.engine.url import make_url

    stripped = _dsn_with_ssl_query(require_postgres.secret())
    parsed = make_url(stripped)
    query_keys = {str(key).lower() for key in (parsed.query or {})}
    assert "ssl" not in query_keys
    assert "sslmode" not in query_keys

    elapsed, result, terminates, checkedout = await _run_frozen_health(
        monkeypatch, RedactedDsn(stripped), freeze_on="select_1"
    )
    assert result.db == "down"
    assert terminates >= 1
    assert checkedout == 0
    assert elapsed < 1.4
