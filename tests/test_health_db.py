import asyncio
import os
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
        DB_HEALTH_CACHE_TTL="0",
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


def _inspector_connect_kwargs(dsn: RedactedDsn) -> dict[str, Any]:
    """Login de la sonda/inspector: ``create_connect_args`` (incluye ``?password=``)."""

    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
    from sqlalchemy.engine.url import make_url

    from app.db import prepare_asyncpg_connect_kwargs

    dialect = PGDialect_asyncpg()  # type: ignore[no-untyped-call]
    _args, opts = dialect.create_connect_args(  # type: ignore[no-untyped-call]
        make_url(dsn.secret())
    )
    prepared = prepare_asyncpg_connect_kwargs(opts)
    kwargs: dict[str, Any] = {
        key: prepared[key]
        for key in ("host", "port", "user", "password", "database", "ssl")
        if key in prepared and prepared[key] is not None
    }
    if "port" not in kwargs:
        kwargs["port"] = 5432
    return kwargs


def _is_hostssl_no_encryption(exc: BaseException) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    return sqlstate == "28000" and "no encryption" in str(exc).lower()


def _maybe_skip_hostssl(reason: str) -> None:
    if os.environ.get("REQUIRE_FREEZE_TESTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        raise AssertionError(f"{reason} (REQUIRE_FREEZE_TESTS)")
    pytest.skip(reason)


def _skip_if_hostssl_rejects_plain(
    require_postgres: RedactedDsn, *, freeze_on: str = "select_1"
) -> None:
    """El proxy habla en claro; un servidor ``hostssl`` rechaza ``ssl=disable``.

    Solo los modos que hacen login (no ``freeze_on="connect"``). Exige
    sqlstate 28000 y «no encryption». ``REQUIRE_FREEZE_TESTS=1`` convierte
    el skip en fallo.
    """

    if freeze_on == "connect":
        return

    import threading

    import asyncpg

    login = _inspector_connect_kwargs(require_postgres)

    async def probe() -> None:
        connection = await asyncpg.connect(
            host=login.get("host"),
            port=login.get("port") or 5432,
            user=login.get("user"),
            password=login.get("password"),
            database=login.get("database"),
            ssl=False,
            timeout=2,
        )
        await connection.close()

    error: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(probe())
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    if not error:
        return
    exc = error[0]
    if _is_hostssl_no_encryption(exc):
        _maybe_skip_hostssl(
            "el servidor rechaza ssl=disable (hostssl); "
            "el proxy de freeze habla en claro"
        )
    raise exc


def _frozen_url(
    require_postgres: RedactedDsn, port: int, *, freeze_on: str = "select_1"
) -> str:
    """DSN hacia el proxy en claro.

    El proxy lee el protocolo Postgres sin TLS. Forzamos ``ssl=disable``
    aunque ``TEST_DATABASE_URL`` lleve ``ssl`` / ``sslmode``.
    """

    from sqlalchemy.engine.url import make_url

    _skip_if_hostssl_rejects_plain(require_postgres, freeze_on=freeze_on)
    parsed = make_url(require_postgres.secret())
    query = {
        str(key): value
        for key, value in (parsed.query or {}).items()
        if str(key).lower() not in {"ssl", "sslmode"}
    }
    query["ssl"] = "disable"
    return parsed.set(host="127.0.0.1", port=port, query=query).render_as_string(
        hide_password=False
    )


def _dsn_with_query(url: str, **updates: str | None) -> str:
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    query = dict(parsed.query or {})
    for key, value in updates.items():
        drop = [item for item in query if str(item).lower() == key.lower()]
        for item in drop:
            del query[item]
        if value is not None:
            query[key] = value
    return parsed.set(query=query).render_as_string(hide_password=False)


async def _count_health_backends(
    dsn: RedactedDsn,
    *,
    connect: Any | None = None,
) -> int:
    """Backends de la sonda (``application_name``), sin el inspector."""

    import asyncpg

    from app.db import HEALTH_APPLICATION_NAME

    opener = connect if callable(connect) else asyncpg.connect
    login = _inspector_connect_kwargs(dsn)
    connection = await opener(
        host=login.get("host"),
        port=login.get("port") or 5432,
        user=login.get("user"),
        password=login.get("password"),
        database=login.get("database"),
        ssl=login.get("ssl"),
    )
    try:
        health = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = $1 AND pid <> pg_backend_pid()",
            HEALTH_APPLICATION_NAME,
        )
    finally:
        await connection.close()
    return int(health or 0)


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
    frozen_url = _frozen_url(require_postgres, proxy.port, freeze_on=freeze_on)
    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", str(health_timeout))
    monkeypatch.setenv("DB_COMMAND_TIMEOUT", str(command_timeout))
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
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
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
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
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    main = get_engine()
    health_service.set_health_database_url(frozen_url)
    original_connect = asyncpg.connect
    baseline = await _count_health_backends(require_postgres, connect=original_connect)
    peak = baseline
    leftover_after_cycle = baseline

    async def _connect_and_count(*args: object, **kwargs: object) -> object:
        nonlocal peak
        connection = await original_connect(*args, **kwargs)
        peak = max(
            peak,
            await _count_health_backends(require_postgres, connect=original_connect),
        )
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
                leftover_after_cycle = max(
                    leftover_after_cycle,
                    await _count_health_backends(
                        require_postgres, connect=original_connect
                    ),
                )
            print(
                f"alternating baseline={baseline} peak_during={peak} "
                f"leftover_after_cycle={leftover_after_cycle}",
                flush=True,
            )
            assert peak >= baseline + 1
            assert leftover_after_cycle == baseline
            assert main.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            await dispose_engine()
            end_health = await _count_health_backends(
                require_postgres, connect=original_connect
            )
            print(f"alternating leftover_after_dispose={end_health}", flush=True)
            assert end_health == baseline
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

    async def hang() -> DatabaseHealthResponse:
        await asyncio.Event().wait()
        return DatabaseHealthResponse(db="down")

    task = asyncio.create_task(hang())
    health_service._probe_task = task
    try:
        health_service._abandon_probe(None, task)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert health_service._probe_task is None
    finally:
        health_service._probe_task = None
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_request_after_timeout_does_not_join_cancelled_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tras un timeout, la siguiente petición no se une a la tarea cancelada."""

    import asyncpg

    async def _hang(*_args: object, **_kwargs: object) -> object:
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")

    _probe_settings(
        monkeypatch, "postgresql+asyncpg://probe:secret@127.0.0.1:1/probe_db"
    )
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "0.15")
    monkeypatch.setattr(asyncpg, "connect", _hang)
    from app.config import get_settings

    get_settings.cache_clear()
    health_service.clear_health_probe()
    try:
        first = await health_service.check_database()
        assert first.db == "down"
        second = await health_service.check_database()
        assert second.db == "down"
        assert health_service._probe_task is None or health_service._probe_task.done()
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_run_probe_dies_under_continuous_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La sonda no vive más que ``DB_HEALTH_TIMEOUT`` si hay waiters."""

    import asyncpg

    from app.config import get_settings

    async def _hang(*_args: object, **_kwargs: object) -> object:
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")

    finished: list[float] = []
    original = health_service._run_probe

    async def _tracked() -> DatabaseHealthResponse:
        started = time.monotonic()
        try:
            return await original()
        finally:
            finished.append(time.monotonic() - started)

    _probe_settings(
        monkeypatch, "postgresql+asyncpg://probe:secret@127.0.0.1:1/probe_db"
    )
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "0.2")
    monkeypatch.setenv("DB_COMMAND_TIMEOUT", "30")
    monkeypatch.setattr(asyncpg, "connect", _hang)
    monkeypatch.setattr(health_service, "_run_probe", _tracked)
    get_settings.cache_clear()
    health_service.clear_health_probe()
    started = time.monotonic()

    async def flood() -> None:
        while time.monotonic() - started < 0.7:
            await health_service.check_database()

    try:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(flood(), flood(), flood()), timeout=0.85
            )
        assert finished
        assert all(elapsed < 0.5 for elapsed in finished)
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


def _probe_settings(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    from app.config import get_settings
    from app.db import clear_engine

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    health_service.clear_health_probe()


def test_health_connect_kwargs_follow_app_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?password=``, socket ``?host=`` y ``ssl`` salen de create_connect_args."""

    from app.config import get_settings
    from app.db import get_engine

    base = "postgresql+asyncpg://probe@127.0.0.1:5432/probe_db"
    _probe_settings(monkeypatch, base)
    try:
        health_service.set_health_database_url(
            _dsn_with_query(base, password="from-query", ssl="disable")
        )
        password_kwargs = health_service._connect_kwargs()
        assert password_kwargs["password"] == "from-query"
        assert password_kwargs["ssl"] is False
        assert password_kwargs["command_timeout"] == get_settings().db_command_timeout

        socket_url = "postgresql+asyncpg://probe:secret@/probe_db?host=/tmp/pg.sock"
        health_service.set_health_database_url(socket_url)
        socket_kwargs = health_service._connect_kwargs()
        assert socket_kwargs["host"] == "/tmp/pg.sock"
        assert socket_kwargs.get("port") in {None, 5432}

        sslmode_url = _dsn_with_query(base, password="x", sslmode="require")
        health_service.set_health_database_url(sslmode_url)
        ssl_kwargs = health_service._connect_kwargs()
        assert ssl_kwargs.get("ssl") == "require"
        assert "sslmode" not in ssl_kwargs

        engine = get_engine()
        _args, app_opts = engine.sync_engine.dialect.create_connect_args(engine.url)
        health_service.set_health_database_url(None)
        probe_opts = health_service._connect_kwargs()
        skip = {
            "timeout",
            "command_timeout",
            "server_settings",
            "sslmode",
            "prepared_statement_cache_size",
        }
        for key, value in app_opts.items():
            if key in skip:
                continue
            assert probe_opts[key] == value

        cache_url = _dsn_with_query(
            base, password="x", prepared_statement_cache_size="0"
        )
        health_service.set_health_database_url(cache_url)
        cache_kwargs = health_service._connect_kwargs()
        assert "prepared_statement_cache_size" not in cache_kwargs
        assert "prepared_statement_name_func" not in cache_kwargs
        assert "async_creator_fn" not in cache_kwargs

        extra_url = _dsn_with_query(base, password="x", foo="bar")
        health_service.set_health_database_url(extra_url)
        extra_kwargs = health_service._connect_kwargs()
        assert extra_kwargs.get("foo") == "bar"

        creator_url = _dsn_with_query(
            base, password="x", async_creator_fn="not-callable"
        )
        health_service.set_health_database_url(creator_url)
        creator_kwargs = health_service._connect_kwargs()
        assert creator_kwargs.get("async_creator_fn") == "not-callable"

        name_fn_url = _dsn_with_query(
            base, password="x", prepared_statement_name_func="not-callable"
        )
        health_service.set_health_database_url(name_fn_url)
        name_fn_kwargs = health_service._connect_kwargs()
        assert name_fn_kwargs.get("prepared_statement_name_func") == "not-callable"
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_pgbouncer_cache_size_query_is_up(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """``?prepared_statement_cache_size=0`` no tumba la sonda (pgbouncer)."""

    from app.config import get_settings
    from app.db import clear_engine

    url = _dsn_with_query(require_postgres.secret(), prepared_statement_cache_size="0")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    health_service.clear_health_probe()
    health_service.set_health_database_url(url)
    try:
        result = await health_service.check_database()
        assert result.db == "ok"
        assert "prepared_statement_cache_size" not in health_service._connect_kwargs()
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_unknown_query_key_is_down(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """``?foo=bar`` tumba la sonda igual que la app (TypeError en connect)."""

    from app.config import get_settings
    from app.db import clear_engine

    url = _dsn_with_query(require_postgres.secret(), foo="bar")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    health_service.clear_health_probe()
    health_service.set_health_database_url(url)
    try:
        result = await health_service.check_database()
        assert result.db == "down"
        assert health_service._connect_kwargs().get("foo") == "bar"
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_probe_passes_ssl_to_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    from app.config import get_settings

    captured: dict[str, object] = {}

    async def _fake_connect(**kwargs: object) -> object:
        captured.update(kwargs)
        raise OSError("stop-after-capture")

    base = "postgresql+asyncpg://probe:secret@127.0.0.1:5432/probe_db"
    _probe_settings(monkeypatch, base)
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "1")
    monkeypatch.setattr(asyncpg, "connect", _fake_connect)
    get_settings.cache_clear()
    health_service.set_health_database_url(_dsn_with_query(base, ssl="disable"))
    try:
        result = await health_service.check_database()
        assert result.db == "down"
        assert captured.get("ssl") is False
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_plain_proxy_forces_ssl_disable(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """El proxy en claro siempre recibe ``ssl=disable`` (vale con PG+TLS)."""

    from sqlalchemy.engine.url import make_url

    tls_dsn = RedactedDsn(_dsn_with_query(require_postgres.secret(), ssl="require"))
    frozen = _frozen_url(tls_dsn, 59999)
    parsed = make_url(frozen)
    query = {str(key).lower(): value for key, value in (parsed.query or {}).items()}
    assert query.get("ssl") == "disable"
    assert "sslmode" not in query

    elapsed, result, terminates, checkedout = await _run_frozen_health(
        monkeypatch, tls_dsn, freeze_on="select_1"
    )
    assert result.db == "down"
    assert terminates >= 1
    assert checkedout == 0
    assert elapsed < 1.4


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_fifty_parallel_share_one_connection(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """P5: 50 peticiones sanas → 50 ok y una sola conexión de sonda."""

    import asyncpg

    from app.config import get_settings
    from app.db import clear_engine

    connects = {"n": 0}
    original = asyncpg.connect

    async def _count_connect(*args: object, **kwargs: object) -> object:
        connects["n"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "1")
    get_settings.cache_clear()
    clear_engine()
    monkeypatch.setattr(asyncpg, "connect", _count_connect)
    health_service.clear_health_probe()
    try:
        results = await asyncio.gather(
            *[health_service.check_database() for _ in range(50)]
        )
        assert {item.db for item in results} == {"ok"}
        assert connects["n"] == 1
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_legit_survives_attacker_loop(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Con atacantes en bucle la sonda legítima sigue en 200."""

    from app.config import get_settings
    from app.db import clear_engine
    from tests.conftest import _build_client

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "1")
    get_settings.cache_clear()
    clear_engine()
    client_iter = _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DATABASE_URL=require_postgres.secret(),
        DB_HEALTH_CACHE_TTL="1",
    )
    client = next(client_iter)
    try:

        async def attacker() -> None:
            for _ in range(20):
                await health_service.check_database()

        results = await asyncio.gather(
            health_service.check_database(),
            *[attacker() for _ in range(8)],
        )
        legit = results[0]
        assert isinstance(legit, DatabaseHealthResponse)
        assert legit.db == "ok"
        response = client.get("/api/v1/health/db")
        assert response.status_code == 200
        assert response.json() == {"db": "ok"}
    finally:
        close = getattr(client_iter, "close", None)
        if callable(close):
            close()
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_cancel_terminates_frozen_select(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """CancelledError limpia la conexión congelada (terminate + re-raise)."""

    import asyncpg
    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import clear_engine, dispose_engine
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    parsed = make_url(require_postgres.secret())
    proxy = FreezeAfterLoginProxy(
        parsed.host or "127.0.0.1", parsed.port or 5432, freeze_on="select_1"
    )
    await proxy.start()
    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "2")
    monkeypatch.setenv("DB_COMMAND_TIMEOUT", "30")
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    health_service.set_health_database_url(_frozen_url(require_postgres, proxy.port))
    original = asyncpg.connect
    baseline = await _count_health_backends(require_postgres, connect=original)
    task = asyncio.create_task(health_service.check_database())
    try:
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        leftover = await _count_health_backends(require_postgres, connect=original)
        assert leftover == baseline
    finally:
        health_service.clear_health_probe()
        await dispose_engine()
        get_settings.cache_clear()
        with suppress(Exception):
            await asyncio.wait_for(proxy.aclose(), timeout=2)


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_cancel_one_waiter_does_not_kill_shared_probe(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """P6: shield. Cancelar un waiter no tumba la sonda compartida."""

    import asyncpg

    from app.config import get_settings
    from app.db import clear_engine

    original = asyncpg.connect
    connects = {"n": 0}

    async def _slow_connect(*args: object, **kwargs: object) -> object:
        connects["n"] += 1
        connection = await original(*args, **kwargs)
        await asyncio.sleep(0.25)
        return connection

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "2")
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    monkeypatch.setattr(asyncpg, "connect", _slow_connect)
    health_service.clear_health_probe()
    first = asyncio.create_task(health_service.check_database())
    second = asyncio.create_task(health_service.check_database())
    try:
        for _ in range(50):
            if health_service._waiters >= 2:
                break
            await asyncio.sleep(0.01)
        assert health_service._waiters >= 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await second
        assert result.db == "ok"
        assert connects["n"] == 1
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_cache_ttl_reuses_result(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Dentro del TTL no se abre otra conexión."""

    import asyncpg

    from app.config import get_settings
    from app.db import clear_engine

    original = asyncpg.connect
    connects = {"n": 0}

    async def _count_connect(*args: object, **kwargs: object) -> object:
        connects["n"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "30")
    get_settings.cache_clear()
    clear_engine()
    monkeypatch.setattr(asyncpg, "connect", _count_connect)
    health_service.clear_health_probe()
    try:
        first = await health_service.check_database()
        second = await health_service.check_database()
        assert first.db == second.db == "ok"
        assert connects["n"] == 1
        cached = health_service._cached(
            asyncio.get_running_loop().time(), get_settings().db_health_cache_ttl
        )
        assert cached is not None
        assert cached.db == "ok"
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


def test_health_cache_ttl_helpers() -> None:
    """``_cached`` caduca en el TTL y con TTL 0 no reutiliza."""

    ok = DatabaseHealthResponse(db="ok")
    down = DatabaseHealthResponse(db="down")
    health_service._cache = (100.0, ok)
    try:
        assert health_service._cached(100.4, 1.0) is ok
        assert health_service._cached(101.0, 1.0) is None
        assert health_service._cached(101.5, 1.0) is None
        assert health_service._cached(100.4, 0.0) is None
        health_service._cache = (100.0, down)
        cached_down = health_service._cached(100.4, 1.0)
        assert cached_down is down
        assert cached_down.db == "down"
    finally:
        health_service._cache = None


@pytest.mark.asyncio
async def test_health_cache_expires_and_failure_stays_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SF2/SF3: tras el TTL se vuelve a abrir; un fallo no se cachea como ok."""

    import asyncpg

    from app.config import get_settings
    from app.db import clear_engine

    connects = {"n": 0}

    async def _fail(*_args: object, **_kwargs: object) -> object:
        connects["n"] += 1
        raise OSError("probe-down")

    _probe_settings(
        monkeypatch, "postgresql+asyncpg://probe:secret@127.0.0.1:1/probe_db"
    )
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "30")
    monkeypatch.setattr(asyncpg, "connect", _fail)
    get_settings.cache_clear()
    clear_engine()
    health_service.clear_health_probe()
    try:
        first = await health_service.check_database()
        second = await health_service.check_database()
        assert first.db == second.db == "down"
        assert connects["n"] == 1
        loop = asyncio.get_running_loop()
        stamped, cached = health_service._cache or (0.0, first)
        assert cached.db == "down"
        assert health_service._cached(stamped + 30.0, 30.0) is None
        assert health_service._cached(loop.time() + 31.0, 30.0) is None
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_cache_ttl_zero_does_not_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL 0 abre una conexión por petición."""

    import asyncpg

    from app.config import get_settings
    from app.db import clear_engine

    connects = {"n": 0}

    async def _fail(*_args: object, **_kwargs: object) -> object:
        connects["n"] += 1
        raise OSError("probe-down")

    _probe_settings(
        monkeypatch, "postgresql+asyncpg://probe:secret@127.0.0.1:1/probe_db"
    )
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    monkeypatch.setattr(asyncpg, "connect", _fail)
    get_settings.cache_clear()
    clear_engine()
    health_service.clear_health_probe()
    try:
        first = await health_service.check_database()
        second = await health_service.check_database()
        assert first.db == second.db == "down"
        assert connects["n"] == 2
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_set_health_database_url_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SF14: cambiar la URL de la sonda dentro del TTL abre otra conexión."""

    import asyncpg

    from app.config import get_settings
    from app.db import clear_engine

    connects = {"n": 0}

    async def _flip(*_args: object, **_kwargs: object) -> object:
        connects["n"] += 1
        if connects["n"] == 1:
            raise OSError("first-down")
        raise OSError("second-down")

    _probe_settings(
        monkeypatch, "postgresql+asyncpg://probe:secret@127.0.0.1:1/probe_db"
    )
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "30")
    monkeypatch.setattr(asyncpg, "connect", _flip)
    get_settings.cache_clear()
    clear_engine()
    health_service.clear_health_probe()
    health_service.set_health_database_url(
        "postgresql+asyncpg://probe:secret@127.0.0.1:1/first_db"
    )
    try:
        first = await health_service.check_database()
        assert first.db == "down"
        assert connects["n"] == 1
        health_service.set_health_database_url(
            "postgresql+asyncpg://probe:secret@127.0.0.1:1/second_db"
        )
        second = await health_service.check_database()
        assert second.db == "down"
        assert connects["n"] == 2
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
async def test_dispose_engine_clears_hung_probe(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """P12: ``dispose_engine`` abandona la sonda colgada y su backend."""

    import asyncpg
    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import clear_engine, dispose_engine
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    _skip_if_hostssl_rejects_plain(require_postgres, freeze_on="select_1")
    parsed = make_url(require_postgres.secret())
    proxy = FreezeAfterLoginProxy(
        parsed.host or "127.0.0.1", parsed.port or 5432, freeze_on="select_1"
    )
    await proxy.start()
    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "2")
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    health_service.set_health_database_url(
        _frozen_url(require_postgres, proxy.port, freeze_on="select_1")
    )
    original = asyncpg.connect
    baseline = await _count_health_backends(require_postgres, connect=original)
    task = asyncio.create_task(health_service.check_database())
    try:
        for _ in range(50):
            if health_service._held_raw is not None or (
                health_service._probe_task is not None
                and not health_service._probe_task.done()
            ):
                break
            await asyncio.sleep(0.02)
        assert health_service._probe_task is not None
        await dispose_engine()
        assert health_service._probe_task is None
        assert health_service._held_raw is None
        leftover = await _count_health_backends(require_postgres, connect=original)
        assert leftover == baseline
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        health_service.clear_health_probe()
        get_settings.cache_clear()
        with suppress(Exception):
            await asyncio.wait_for(proxy.aclose(), timeout=2)


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_string_adapter_keys_are_down(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """D3/D4: ``?async_creator_fn=`` / ``?prepared_statement_name_func=`` no dan ok."""

    from app.config import get_settings
    from app.db import clear_engine

    for key, value in (
        ("async_creator_fn", "not-callable"),
        ("prepared_statement_name_func", "not-callable"),
    ):
        url = _dsn_with_query(require_postgres.secret(), **{key: value})
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
        get_settings.cache_clear()
        clear_engine()
        health_service.clear_health_probe()
        health_service.set_health_database_url(url)
        try:
            result = await health_service.check_database()
            assert result.db == "down"
            assert health_service._connect_kwargs().get(key) == value
        finally:
            health_service.clear_health_probe()
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_clear_health_probe_maps_external_cancel_to_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-3: clear con waiter en vuelo no reinicia ``_waiters``; cancel → down."""

    import asyncpg

    from app.config import get_settings

    async def _hang(*_args: object, **_kwargs: object) -> object:
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")

    _probe_settings(
        monkeypatch, "postgresql+asyncpg://probe:secret@127.0.0.1:1/probe_db"
    )
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "2")
    monkeypatch.setattr(asyncpg, "connect", _hang)
    get_settings.cache_clear()
    health_service.clear_health_probe()
    waiter = asyncio.create_task(health_service.check_database())
    try:
        for _ in range(100):
            if health_service._waiters >= 1:
                break
            await asyncio.sleep(0.01)
        assert health_service._waiters >= 1
        health_service.clear_health_probe()
        result = await asyncio.wait_for(waiter, timeout=2)
        assert result.db == "down"
        assert health_service._waiters == 0
    finally:
        if not waiter.done():
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
        health_service.clear_health_probe()
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::ResourceWarning")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
async def test_frozen_close_returns_down_under_one_second(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """P11: ``close()`` congelado y timeout 2 baja en <1 s (margen 0,4 s)."""

    import asyncpg

    from app.config import get_settings
    from app.db import clear_engine

    original = asyncpg.connect
    held: dict[str, Any] = {}

    class _FrozenClose:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def execute(self, *args: object, **kwargs: object) -> object:
            return await self._inner.execute(*args, **kwargs)

        async def close(self, *args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

        def terminate(self) -> None:
            terminate = getattr(self._inner, "terminate", None)
            if callable(terminate):
                terminate()

        def is_closed(self) -> bool:
            is_closed = getattr(self._inner, "is_closed", None)
            if callable(is_closed):
                return bool(is_closed())
            return False

    async def _connect_and_freeze_close(*args: object, **kwargs: object) -> object:
        connection = await original(*args, **kwargs)
        frozen = _FrozenClose(connection)
        held["c"] = frozen
        return frozen

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "2")
    monkeypatch.setenv("DB_HEALTH_CACHE_TTL", "0")
    get_settings.cache_clear()
    clear_engine()
    monkeypatch.setattr(asyncpg, "connect", _connect_and_freeze_close)
    health_service.clear_health_probe()
    health_service.set_health_database_url(require_postgres.secret())
    try:
        started = time.monotonic()
        result = await health_service.check_database()
        elapsed = time.monotonic() - started
        assert result.db == "down"
        assert elapsed < 1.0
    finally:
        connection = held.get("c")
        if connection is not None:
            terminate = getattr(connection, "terminate", None)
            if callable(terminate):
                terminate()
        health_service.clear_health_probe()
        await asyncio.sleep(0)
        get_settings.cache_clear()


def test_frozen_connect_skip_helper_does_not_skip_connect_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-5: ``freeze_on=connect`` no salta aunque el servidor sea hostssl."""

    class _Hostssl(Exception):
        sqlstate = "28000"

        def __str__(self) -> str:
            return "no encryption"

    async def _boom(*_a: object, **_k: object) -> object:
        raise _Hostssl()

    import asyncpg

    monkeypatch.setattr(asyncpg, "connect", _boom)
    try:
        _skip_if_hostssl_rejects_plain(
            RedactedDsn("postgresql+asyncpg://u:p@127.0.0.1:1/db_test"),
            freeze_on="connect",
        )
    except pytest.skip.Exception as skipped:
        raise AssertionError("freeze_on=connect no debe saltar") from skipped


def test_require_freeze_tests_converts_skip_to_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K1: ``REQUIRE_FREEZE_TESTS=1`` convierte el skip de hostssl en fallo."""

    monkeypatch.setenv("REQUIRE_FREEZE_TESTS", "1")
    with pytest.raises(AssertionError, match="REQUIRE_FREEZE_TESTS"):
        _maybe_skip_hostssl("el servidor rechaza ssl=disable (hostssl)")


@pytest.mark.db
def test_hostssl_helper_does_not_skip_on_plain_host(
    require_postgres: RedactedDsn,
) -> None:
    """K1 en host: si ssl=False conecta, el helper no salta.

    En hostssl real (28000/no encryption) este caso se salta; matar K1
    ahí exige ``REQUIRE_FREEZE_TESTS=1``.
    """

    import threading

    import asyncpg

    login = _inspector_connect_kwargs(require_postgres)
    error: list[BaseException] = []

    async def probe() -> None:
        connection = await asyncpg.connect(
            host=login.get("host"),
            port=login.get("port") or 5432,
            user=login.get("user"),
            password=login.get("password"),
            database=login.get("database"),
            ssl=False,
            timeout=2,
        )
        await connection.close()

    def worker() -> None:
        try:
            asyncio.run(probe())
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    if error and _is_hostssl_no_encryption(error[0]):
        pytest.skip("hostssl real; K1 en hostssl exige REQUIRE_FREEZE_TESTS")
    if error:
        raise error[0]
    try:
        _skip_if_hostssl_rejects_plain(require_postgres)
    except pytest.skip.Exception as skipped:
        raise AssertionError("K1: el helper saltó en un host plano") from skipped


def test_hostssl_helper_reraises_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K2: un error que no es 28000/no encryption no se convierte en skip."""

    class _Other(Exception):
        sqlstate = "28P01"

        def __str__(self) -> str:
            return "password authentication failed"

    async def _boom(*_a: object, **_k: object) -> object:
        raise _Other()

    import asyncpg

    monkeypatch.setattr(asyncpg, "connect", _boom)
    try:
        _skip_if_hostssl_rejects_plain(
            RedactedDsn("postgresql+asyncpg://u:p@127.0.0.1:1/db_test")
        )
    except pytest.skip.Exception as skipped:
        raise AssertionError("K2: un error distinto no debe saltar") from skipped
    except _Other:
        return
    raise AssertionError("se esperaba el error de autenticación, no un skip")


@pytest.fixture(scope="session")
def tls_postgres(
    tmp_path_factory: pytest.TempPathFactory,
    require_postgres: RedactedDsn,
) -> Iterator[tuple[Any, Any, str]]:
    """Postgres TLS efímero para ``?sslrootcert=``. Se destruye al terminar.

    Si hay ``TLS_TEST_DATABASE_URL`` y ``TLS_TEST_CAFILE``, se reutilizan.
    Sin Docker y sin esas variables, se salta (salvo ``REQUIRE_TLS_TESTS=1``).
    """

    import shutil
    from pathlib import Path

    _ = require_postgres
    env_url = os.environ.get("TLS_TEST_DATABASE_URL")
    env_ca = os.environ.get("TLS_TEST_CAFILE")
    tmp = Path(str(tmp_path_factory.mktemp("tls-pg")))
    if env_url and env_ca and Path(env_ca).is_file():
        yield Path(env_ca), _write_unrelated_ca(tmp / "bad-ca.pem"), env_url
        return
    require = os.environ.get("REQUIRE_TLS_TESTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if shutil.which("docker") is None:
        if require:
            raise AssertionError("REQUIRE_TLS_TESTS=1 y docker no está en PATH")
        pytest.skip(
            "tests TLS: docker no está en PATH (o define TLS_TEST_DATABASE_URL "
            "y TLS_TEST_CAFILE)"
        )
    cafile, bad_ca, dsn, cleanup = _start_ephemeral_tls_postgres(tmp)
    try:
        yield cafile, bad_ca, dsn
    finally:
        cleanup()


@pytest.mark.db
@pytest.mark.timeout(60)
def test_health_sslrootcert_good_ca_up_bad_ca_down(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    tls_postgres: tuple[Any, Any, str],
) -> None:
    """B-2: ``?sslrootcert=`` con CA buena da 200 y con CA mala 503."""

    from sqlalchemy import Table, text

    from tests.conftest import _create_async_engine
    from tests.test_db import SessionProbe

    cafile, bad_ca, dsn = tls_postgres
    good = _dsn_with_query(dsn, ssl="verify-full", sslrootcert=str(cafile))
    bad = _dsn_with_query(dsn, ssl="verify-full", sslrootcert=str(bad_ca))
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)

    async def _session_probes(*, create: bool) -> None:
        engine = _create_async_engine(good, pool_pre_ping=True)
        try:
            async with engine.begin() as connection:
                if create:
                    await connection.execute(
                        text(
                            "CREATE OR REPLACE FUNCTION set_updated_at() "
                            "RETURNS trigger LANGUAGE plpgsql AS $$ "
                            "BEGIN NEW.updated_at = clock_timestamp(); "
                            "RETURN NEW; END; $$;"
                        )
                    )
                    await connection.run_sync(
                        lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
                    )
                    await connection.run_sync(
                        lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
                    )
                else:
                    await connection.run_sync(
                        lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
                    )
        finally:
            await engine.dispose()

    asyncio.run(_session_probes(create=True))
    try:
        _run_sslrootcert_clients(monkeypatch, good, bad)
    finally:
        asyncio.run(_session_probes(create=False))


def _run_sslrootcert_clients(
    monkeypatch: pytest.MonkeyPatch,
    good: str,
    bad: str,
) -> None:
    from alembic import command

    from app.db import clear_engine
    from tests.conftest import _build_client, alembic_config

    good_iter = _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_CACHE_TTL="0",
        DATABASE_URL=good,
    )
    good_client = next(good_iter)
    try:
        response = good_client.get("/api/v1/health/db")
        assert response.status_code == 200
        assert response.json() == {"db": "ok"}
        created = good_client.post("/api/v1/_test/session/sslroot")
        assert created.status_code == 201, created.text
        assert created.json() == {"label": "sslroot"}
        visible = good_client.get("/api/v1/_test/session/sslroot")
        assert visible.status_code == 200, visible.text
        assert visible.json() == {"label": "sslroot"}
    finally:
        close = getattr(good_iter, "close", None)
        if callable(close):
            close()
    command.current(alembic_config(RedactedDsn(good)))
    clear_engine()
    bad_iter = _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_CACHE_TTL="0",
        DATABASE_URL=bad,
    )
    bad_client = next(bad_iter)
    try:
        response = bad_client.get("/api/v1/health/db")
        assert response.status_code == 503
        assert response.json() == {"db": "down"}
    finally:
        close = getattr(bad_iter, "close", None)
        if callable(close):
            close()


@pytest.mark.db
@pytest.mark.timeout(60)
def test_health_verify_ca_rejects_unrelated_ca(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    tls_postgres: tuple[Any, Any, str],
) -> None:
    """X3b: ``ssl=verify-ca`` con CA ajena da 503 (no CERT_NONE)."""

    from tests.conftest import _build_client

    del require_postgres, migrated_test_database
    cafile, bad_ca, dsn = tls_postgres
    good = _dsn_with_query(dsn, ssl="verify-ca", sslrootcert=str(cafile))
    bad = _dsn_with_query(dsn, ssl="verify-ca", sslrootcert=str(bad_ca))
    good_iter = _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_CACHE_TTL="0",
        DATABASE_URL=good,
    )
    good_client = next(good_iter)
    try:
        response = good_client.get("/api/v1/health/db")
        assert response.status_code == 200
        assert response.json() == {"db": "ok"}
    finally:
        close = getattr(good_iter, "close", None)
        if callable(close):
            close()
    bad_iter = _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_CACHE_TTL="0",
        DATABASE_URL=bad,
    )
    bad_client = next(bad_iter)
    try:
        response = bad_client.get("/api/v1/health/db")
        assert response.status_code == 503
        assert response.json() == {"db": "down"}
    finally:
        close = getattr(bad_iter, "close", None)
        if callable(close):
            close()


def test_prepare_asyncpg_kwargs_translates_sslrootcert(
    tmp_path: Any,
) -> None:
    import ssl as ssl_mod
    from pathlib import Path

    from app.db import prepare_asyncpg_connect_kwargs

    cafile = _write_unrelated_ca(Path(str(tmp_path)) / "unit-ca.pem")
    kwargs = prepare_asyncpg_connect_kwargs(
        {"host": "127.0.0.1", "ssl": "verify-full", "sslrootcert": str(cafile)}
    )
    assert "sslrootcert" not in kwargs
    assert isinstance(kwargs["ssl"], ssl_mod.SSLContext)


def test_ssl_disable_with_sslrootcert_skips_context(tmp_path: Any) -> None:
    """T9: ``ssl=disable`` + ``sslrootcert`` no fuerza TLS (engine y sonda)."""

    import ssl as ssl_mod
    from pathlib import Path

    from app.db import (
        asyncpg_ssl_connect_arg,
        asyncpg_url_and_connect_args,
        prepare_asyncpg_connect_kwargs,
    )

    cafile = _write_unrelated_ca(Path(str(tmp_path)) / "unit-ca.pem")
    assert asyncpg_ssl_connect_arg("disable", str(cafile)) is False
    assert asyncpg_ssl_connect_arg("false", str(cafile)) is False
    assert asyncpg_ssl_connect_arg(False, str(cafile)) is False
    probe = prepare_asyncpg_connect_kwargs(
        {"host": "127.0.0.1", "ssl": "disable", "sslrootcert": str(cafile)}
    )
    _url, engine_args = asyncpg_url_and_connect_args(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test"
        f"?ssl=disable&sslrootcert={cafile}"
    )
    assert probe.get("ssl") is False
    assert engine_args.get("ssl") is False
    assert not isinstance(probe.get("ssl"), ssl_mod.SSLContext)
    assert not isinstance(engine_args.get("ssl"), ssl_mod.SSLContext)


def test_asyncpg_url_translates_sslmode(tmp_path: Any) -> None:
    """B-N3: ``?sslmode=`` no llega a asyncpg (TypeError)."""

    import ssl as ssl_mod
    from pathlib import Path

    from app.db import asyncpg_url_and_connect_args

    cafile = _write_unrelated_ca(Path(str(tmp_path)) / "sslmode-ca.pem")
    url, args = asyncpg_url_and_connect_args(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test"
        f"?sslmode=verify-full&sslrootcert={cafile}"
    )
    assert "sslmode=" not in url.lower()
    assert "sslrootcert=" not in url.lower()
    assert isinstance(args.get("ssl"), ssl_mod.SSLContext)
    plain, plain_args = asyncpg_url_and_connect_args(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test?sslmode=disable"
    )
    assert "sslmode=" not in plain.lower()
    assert plain_args.get("ssl") is False
    mode_only, mode_args = asyncpg_url_and_connect_args(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test?sslmode=verify-full"
    )
    assert "sslmode=" not in mode_only.lower()
    assert mode_args.get("ssl") == "verify-full"


_ALTA_MIX_QUERIES = (
    "ssl=disable&sslmode=verify-full",
    "ssl=require&sslmode=disable&sslrootcert=/tmp/r92-ca.pem",
    "sslmode=disable&sslmode=verify-full",
)


def _exercise_ssl_path(
    path: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> str:
    """App (Settings+engine), alembic-url y alembic-env. No importa env.py."""

    from alembic import command

    from app.config import get_settings
    from app.config.settings import Settings
    from app.db import asyncpg_url_and_connect_args
    from app.migrations.config import make_alembic_config

    if path == "app":
        settings = Settings(app_env="development", database_url=url)
        return asyncpg_url_and_connect_args(settings.resolved_database_url())[0]
    if path == "alembic-url":
        command.current(make_alembic_config(url))
        return url
    if path == "alembic-env":
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        try:
            command.current(make_alembic_config())
        finally:
            get_settings.cache_clear()
        return url
    raise AssertionError(f"camino SSL desconocido: {path}")


def _ssl_path_error(path: str, url: str, monkeypatch: pytest.MonkeyPatch) -> Exception:
    try:
        _exercise_ssl_path(path, url, monkeypatch)
    except Exception as exc:
        return exc
    raise AssertionError(f"{path} no falló: {url}")


@pytest.mark.parametrize("path", ["app", "alembic-url", "alembic-env"])
@pytest.mark.parametrize("query", _ALTA_MIX_QUERIES)
def test_conflicting_ssl_query_rejected_on_all_paths(
    path: str, query: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIA-1: ssl+sslmode o sslmode repetido no conectan en claro."""

    from app.db import _SSL_CONFLICT_MSG

    url = f"postgresql+asyncpg://u:p@127.0.0.1:5432/db_test?{query}"
    caught = _ssl_path_error(path, url, monkeypatch)
    messages = [str(caught)]
    current: BaseException | None = caught
    while current is not None:
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    assert any("mezclar ni repetir" in item for item in messages), messages
    assert any(_SSL_CONFLICT_MSG in item for item in messages), messages


@pytest.mark.parametrize("path", ["app", "alembic-url", "alembic-env"])
@pytest.mark.parametrize(
    ("query", "expected"),
    (("SSL=disable", "disable"), ("Ssl=verify-full", "verify-full")),
)
def test_uppercase_ssl_query_key_normalizes(
    path: str, query: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?SSL=`` / ``?Ssl=`` se normalizan a ``ssl=`` (no TypeError de asyncpg)."""

    from sqlalchemy.engine.url import make_url

    from app.config.settings import Settings
    from app.db import asyncpg_url_and_connect_args, normalize_asyncpg_ssl_url

    url = f"postgresql+asyncpg://u:p@127.0.0.1:1/db_test?{query}"
    settings = Settings(app_env="development", database_url=url)
    resolved = settings.resolved_database_url()
    parsed = make_url(resolved)
    query_map = {str(key): value for key, value in (parsed.query or {}).items()}
    assert "SSL" not in query_map
    assert "Ssl" not in query_map
    assert query_map.get("ssl") == expected
    assert make_url(normalize_asyncpg_ssl_url(url)).query.get("ssl") == expected
    if path == "app":
        engine_url, args = asyncpg_url_and_connect_args(resolved)
        assert "SSL=" not in engine_url
        assert "Ssl=" not in engine_url
        if expected == "disable":
            assert args.get("ssl") is False
        else:
            assert args.get("ssl") == expected
        return
    caught = _ssl_path_error(path, url, monkeypatch)
    text = f"{type(caught).__name__}: {caught}".lower()
    assert "ssl" not in type(caught).__name__.lower()
    assert "unexpected keyword" not in text
    assert "argument 'ssl'" not in text


@pytest.mark.db
@pytest.mark.timeout(60)
def test_sslmode_verify_full_without_rootcert_rejects_untrusted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    tls_postgres: tuple[Any, Any, str],
) -> None:
    """ALTA: ``?sslmode=verify-full`` sin CA no degrada a prefer (Alembic y engine)."""

    from pathlib import Path

    from alembic import command
    from sqlalchemy import text

    from app.db import asyncpg_url_and_connect_args
    from app.migrations.config import make_alembic_config
    from tests.conftest import _create_async_engine

    _cafile, _bad_ca, dsn = tls_postgres
    raw = _dsn_with_query(dsn, sslmode="verify-full", sslrootcert=None, ssl=None)
    assert "sslmode=verify-full" in raw.lower()
    assert "sslrootcert=" not in raw.lower()
    url, args = asyncpg_url_and_connect_args(raw)
    assert "sslmode=" not in url.lower()
    assert args.get("ssl") == "verify-full"

    home = Path(str(tmp_path)) / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    monkeypatch.delenv("PGSSLMODE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    with pytest.raises(Exception) as alembic_exc:
        command.current(make_alembic_config(raw))
    alembic_text = str(alembic_exc.value).lower()
    assert any(
        token in alembic_text
        for token in ("ssl", "certificate", "certificat", "verify")
    ), alembic_exc.value

    engine = _create_async_engine(raw, pool_pre_ping=True)

    async def _connect() -> None:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    with pytest.raises(Exception) as engine_exc:
        asyncio.run(_connect())
    engine_text = str(engine_exc.value).lower()
    assert any(
        token in engine_text for token in ("ssl", "certificate", "certificat", "verify")
    ), engine_exc.value


def test_prepare_translates_uppercase_sslmode_alone() -> None:
    """S4a: ``SSLMODE`` se traduce a ``ssl`` sin mezclar hostname ni CA."""

    from app.db import asyncpg_url_and_connect_args, prepare_asyncpg_connect_kwargs

    mode_only = prepare_asyncpg_connect_kwargs(
        {"host": "127.0.0.1", "SSLMODE": "require"}
    )
    assert "sslmode" not in {str(key).lower() for key in mode_only}
    assert "SSLMODE" not in mode_only
    assert mode_only.get("ssl") == "require"
    url, args = asyncpg_url_and_connect_args(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test?SSLMODE=require"
    )
    assert "sslmode=" not in url.lower()
    assert args.get("ssl") == "require"


def test_ssl_require_hostname_without_sslmode(tmp_path: Any) -> None:
    """S7: ``ssl=require`` + CA comprueba hostname; no usa SSLMODE."""

    import ssl as ssl_mod
    from pathlib import Path

    from app.db import ssl_context_from_rootcert

    cafile = str(_write_unrelated_ca(Path(str(tmp_path)) / "s7-ca.pem"))
    ctx = ssl_context_from_rootcert(cafile, "require")
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl_mod.CERT_REQUIRED
    mixed = ssl_context_from_rootcert(cafile, "Require")
    assert mixed.check_hostname is True


def test_sslrootcert_and_sslmode_are_case_insensitive(tmp_path: Any) -> None:
    """S2a/S4a: ``SSLROOTCERT`` / ``SSLMODE`` se traducen igual."""

    import ssl as ssl_mod
    from pathlib import Path

    from app.db import asyncpg_url_and_connect_args, prepare_asyncpg_connect_kwargs

    cafile = _write_unrelated_ca(Path(str(tmp_path)) / "case-ca.pem")
    kwargs = prepare_asyncpg_connect_kwargs(
        {"host": "127.0.0.1", "SSL": "verify-full", "SSLROOTCERT": str(cafile)}
    )
    assert "sslrootcert" not in {str(key).lower() for key in kwargs}
    assert isinstance(kwargs["ssl"], ssl_mod.SSLContext)
    mode_only = prepare_asyncpg_connect_kwargs(
        {"host": "127.0.0.1", "SSLMODE": "require"}
    )
    assert "sslmode" not in {str(key).lower() for key in mode_only}
    assert "SSLMODE" not in mode_only
    assert mode_only.get("ssl") == "require"
    url, args = asyncpg_url_and_connect_args(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test"
        f"?SSLMODE=verify-full&SSLROOTCERT={cafile}"
    )
    assert "sslmode=" not in url.lower()
    assert "sslrootcert=" not in url.lower()
    assert isinstance(args.get("ssl"), ssl_mod.SSLContext)
    disable_url, disable_args = asyncpg_url_and_connect_args(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test?SSLMODE=disable"
    )
    assert "sslmode=" not in disable_url.lower()
    assert disable_args.get("ssl") is False


def test_sslrootcert_and_pgsslrootcert_accept_ca_without_keyusage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """CA sin ``keyUsage``: ``?sslrootcert=`` y ``PGSSLROOTCERT`` (asyncpg real)."""

    import ssl as ssl_mod
    from pathlib import Path

    from app.db import prepare_asyncpg_connect_kwargs, ssl_context_from_rootcert

    ca_dir = Path(str(tmp_path)) / "no-ku"
    cafile, server_crt, server_key = _write_ca_without_keyusage_and_leaf(ca_dir)
    server_ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(server_crt), str(server_key))

    query_kwargs = prepare_asyncpg_connect_kwargs(
        {"ssl": "verify-full", "sslrootcert": str(cafile)}
    )
    query_ctx = query_kwargs["ssl"]
    assert isinstance(query_ctx, ssl_mod.SSLContext)
    helper_ctx = ssl_context_from_rootcert(str(cafile), "verify-full")
    strict = getattr(ssl_mod, "VERIFY_X509_STRICT", 0)
    partial = getattr(ssl_mod, "VERIFY_X509_PARTIAL_CHAIN", 0)
    for ctx in (query_ctx, helper_ctx):
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl_mod.CERT_REQUIRED
        if strict:
            assert ctx.verify_flags & strict == 0
        if partial:
            assert ctx.verify_flags & partial == 0
        _tls_handshake(ctx, server_ctx, "localhost")
    _asyncpg_connect_with_pgsslrootcert(monkeypatch, cafile, server_ctx)

    with pytest.raises(ssl_mod.SSLError):
        _tls_handshake(query_ctx, server_ctx, "wrong.example")
    bad_ca = _write_unrelated_ca(Path(str(tmp_path)) / "other-ca.pem")
    bad_ctx = ssl_context_from_rootcert(str(bad_ca), "verify-full")
    with pytest.raises(ssl_mod.SSLError):
        _tls_handshake(bad_ctx, server_ctx, "localhost")


def test_ssl_context_from_rootcert_trusts_only_given_ca(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """X4/X5: una CA (no las del sistema), TLS ≥1.2 y sin SECLEVEL=0."""

    import ssl as ssl_mod
    from pathlib import Path

    from app.db import ssl_context_from_rootcert

    ours = Path(str(tmp_path)) / "ours"
    other = Path(str(tmp_path)) / "other"
    cafile, server_crt, server_key = _write_ca_without_keyusage_and_leaf(ours)
    other_ca, other_crt, other_key = _write_ca_without_keyusage_and_leaf(other)
    monkeypatch.setenv("SSL_CERT_FILE", str(other_ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(other))
    ctx = ssl_context_from_rootcert(str(cafile), "verify-full")
    assert len(ctx.get_ca_certs()) == 1
    assert ctx.minimum_version >= ssl_mod.TLSVersion.TLSv1_2
    weak = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_CLIENT)
    weak.minimum_version = ssl_mod.TLSVersion.MINIMUM_SUPPORTED
    weak.set_ciphers("DEFAULT@SECLEVEL=0")
    assert ctx.minimum_version > weak.minimum_version
    ours_ciphers = {item.get("name") for item in ctx.get_ciphers()}
    weak_ciphers = {item.get("name") for item in weak.get_ciphers()}
    assert not ours_ciphers.issuperset(weak_ciphers)

    good_server = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    good_server.load_cert_chain(str(server_crt), str(server_key))
    _tls_handshake(ctx, good_server, "localhost")
    other_server = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    other_server.load_cert_chain(str(other_crt), str(other_key))
    with pytest.raises(ssl_mod.SSLError):
        _tls_handshake(ctx, other_server, "localhost")


def test_ssl_require_with_rootcert_checks_hostname(tmp_path: Any) -> None:
    """T1/T11: ``ssl=require`` + CA verifica hostname (más estricto que libpq)."""

    import ssl as ssl_mod
    from pathlib import Path

    from app.db import ssl_context_from_rootcert

    cafile = str(_write_unrelated_ca(Path(str(tmp_path)) / "require-ca.pem"))
    require = ssl_context_from_rootcert(cafile, "require")
    mixed = ssl_context_from_rootcert(cafile, "Require")
    full = ssl_context_from_rootcert(cafile, "verify-full")
    verify_ca = ssl_context_from_rootcert(cafile, "verify-ca")
    assert require.check_hostname is True
    assert mixed.check_hostname is True
    assert require.verify_mode == ssl_mod.CERT_REQUIRED
    assert full.check_hostname is True
    assert verify_ca.check_hostname is False


@pytest.mark.db
@pytest.mark.timeout(60)
def test_ssl_require_rootcert_rejects_hostname_mismatch(
    tls_postgres: tuple[Any, Any, str],
) -> None:
    """T1/T11: hostname distinto con ``ssl=require`` + CA no conecta."""

    import socket as sock
    import ssl as ssl_mod

    from sqlalchemy.engine.url import make_url

    from app.db import ssl_context_from_rootcert

    cafile, _bad_ca, dsn = tls_postgres
    parsed = make_url(dsn)
    host = str(parsed.host or "127.0.0.1")
    port = int(parsed.port or 5432)
    ctx = ssl_context_from_rootcert(str(cafile), "require")
    raw = sock.create_connection((host, port), timeout=3)
    try:
        with pytest.raises(ssl_mod.SSLError):
            ctx.wrap_socket(raw, server_hostname="wrong.example")
    finally:
        raw.close()
    matched = sock.create_connection((host, port), timeout=3)
    try:
        wrapped = ctx.wrap_socket(matched, server_hostname=host)
        wrapped.close()
    finally:
        matched.close()


@pytest.mark.db
def test_ssl_disable_sslrootcert_engine_matches_probe(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    tmp_path: Any,
) -> None:
    """T9: health y engine en claro si ``ssl=disable`` aunque haya CA."""

    from pathlib import Path

    from sqlalchemy import text

    from app.config import get_settings
    from app.db import clear_engine, dispose_engine, get_engine
    from tests.conftest import _build_client

    _skip_if_hostssl_rejects_plain(require_postgres)
    cafile = _write_unrelated_ca(Path(str(tmp_path)) / "t9-ca.pem")
    dsn = _dsn_with_query(
        require_postgres.secret(), ssl="disable", sslrootcert=str(cafile)
    )
    monkeypatch.setenv("DATABASE_URL", dsn)
    get_settings.cache_clear()
    clear_engine()
    client_iter = _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        DB_HEALTH_CACHE_TTL="0",
        DATABASE_URL=dsn,
    )
    client = next(client_iter)
    try:
        response = client.get("/api/v1/health/db")
        assert response.status_code == 200
        assert response.json() == {"db": "ok"}
    finally:
        close = getattr(client_iter, "close", None)
        if callable(close):
            close()

    async def _engine_select() -> None:
        engine = get_engine()
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT 1")) == 1
        finally:
            await dispose_engine()

    asyncio.run(_engine_select())
    clear_engine()
    get_settings.cache_clear()


def _write_unrelated_ca(path: Any) -> Any:
    import subprocess

    key = path.with_suffix(".key")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(path),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=bad-ca",
        ],
        check=True,
        capture_output=True,
    )
    return path


def _write_ca_without_keyusage_and_leaf(directory: Any) -> tuple[Any, Any, Any]:
    """CA sin ``keyUsage`` + hoja firmada (SAN localhost)."""

    import subprocess
    from pathlib import Path

    root = Path(str(directory))
    root.mkdir(parents=True, exist_ok=True)
    ca_cnf = root / "ca.cnf"
    ca_cnf.write_text(
        "[req]\n"
        "distinguished_name = dn\n"
        "x509_extensions = v3_ca\n"
        "prompt = no\n"
        "[dn]\n"
        "CN = no-ku-ca\n"
        "[v3_ca]\n"
        "basicConstraints = critical,CA:TRUE\n"
        "subjectKeyIdentifier = hash\n",
        encoding="utf-8",
    )
    ca_key = root / "ca.key"
    ca_pem = root / "ca.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_pem),
            "-days",
            "1",
            "-nodes",
            "-config",
            str(ca_cnf),
        ],
        check=True,
        capture_output=True,
    )
    text = subprocess.run(
        ["openssl", "x509", "-in", str(ca_pem), "-noout", "-text"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Key Usage" not in text
    assert "CA:TRUE" in text.replace(" ", "")

    leaf_cnf = root / "leaf.cnf"
    leaf_cnf.write_text(
        "[req]\n"
        "distinguished_name = dn\n"
        "req_extensions = v3_req\n"
        "prompt = no\n"
        "[dn]\n"
        "CN = localhost\n"
        "[v3_req]\n"
        "basicConstraints = CA:FALSE\n"
        "subjectAltName = DNS:localhost,IP:127.0.0.1\n",
        encoding="utf-8",
    )
    server_key = root / "server.key"
    server_csr = root / "server.csr"
    server_crt = root / "server.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(server_key),
            "-out",
            str(server_csr),
            "-config",
            str(leaf_cnf),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(server_csr),
            "-CA",
            str(ca_pem),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(server_crt),
            "-days",
            "1",
            "-extfile",
            str(leaf_cnf),
            "-extensions",
            "v3_req",
        ],
        check=True,
        capture_output=True,
    )
    return ca_pem, server_crt, server_key


def _asyncpg_connect_with_pgsslrootcert(
    monkeypatch: pytest.MonkeyPatch,
    cafile: Any,
    server_ctx: Any,
) -> None:
    """Handshake TLS con asyncpg + ``PGSSLROOTCERT`` (no se copia el SSLContext)."""

    import ssl as ssl_mod

    import asyncpg

    port_box: list[int] = []
    ready = threading.Event()

    def serve() -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port_box.append(int(listener.getsockname()[1]))
        listener.listen(1)
        ready.set()
        conn, _ = listener.accept()
        try:
            wrapped = server_ctx.wrap_socket(conn, server_side=True)
            wrapped.close()
        except OSError:
            pass
        finally:
            conn.close()
            listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(5)
    monkeypatch.setenv("PGSSLROOTCERT", str(cafile))
    monkeypatch.delenv("PGSSLCERT", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    async def _connect() -> None:
        try:
            await asyncpg.connect(
                host="127.0.0.1",
                port=port_box[0],
                user="u",
                password="p",
                database="db",
                ssl="verify-full",
                timeout=3,
            )
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}".lower()
            if isinstance(exc, ssl_mod.SSLError) or "certificate verify" in text:
                raise

    try:
        asyncio.run(_connect())
    finally:
        thread.join(timeout=5)


def _tls_handshake(client_ctx: Any, server_ctx: Any, hostname: str) -> None:
    errors: list[BaseException] = []
    left, right = socket.socketpair()

    def serve() -> None:
        try:
            wrapped = server_ctx.wrap_socket(right, server_side=True)
            wrapped.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            right.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        wrapped = client_ctx.wrap_socket(left, server_hostname=hostname)
        wrapped.close()
    finally:
        left.close()
        thread.join(timeout=5)
    if errors:
        raise errors[0]


def _start_ephemeral_tls_postgres(
    tmp_path: Any,
) -> tuple[Any, Any, str, Any]:
    import socket as sock
    import subprocess
    import time as time_mod
    from pathlib import Path

    certs = Path(str(tmp_path)) / "certs"
    certs.mkdir()
    server_crt = certs / "server.crt"
    server_key = certs / "server.key"
    bad_ca = Path(str(tmp_path)) / "bad-ca.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(server_key),
            "-out",
            str(server_crt),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    _write_unrelated_ca(bad_ca)
    os.chmod(server_crt, 0o644)
    os.chmod(server_key, 0o644)
    probe = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    name = f"em-tls-{os.getpid()}-{port}"

    def _cleanup() -> None:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_USER=tlsprobe",
            "-e",
            "POSTGRES_PASSWORD=tlsprobe-secret",
            "-e",
            "POSTGRES_DB=tlsprobe_test",
            "-p",
            f"127.0.0.1:{port}:5432",
            "-v",
            f"{certs}:/certs:ro",
            "postgres:17-alpine",
            "sh",
            "-c",
            "cp /certs/server.crt /tmp/server.crt "
            "&& cp /certs/server.key /tmp/server.key "
            "&& chown postgres /tmp/server.crt /tmp/server.key "
            "&& chmod 600 /tmp/server.key "
            "&& exec docker-entrypoint.sh postgres "
            "-c ssl=on "
            "-c ssl_cert_file=/tmp/server.crt "
            "-c ssl_key_file=/tmp/server.key",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(f"no se pudo arrancar Postgres TLS: {started.stderr}")
    dsn = (
        f"postgresql+asyncpg://tlsprobe:tlsprobe-secret@127.0.0.1:{port}/tlsprobe_test"
    )
    deadline = time_mod.monotonic() + 40
    last_error = "timeout"
    while time_mod.monotonic() < deadline:
        try:
            import ssl as ssl_mod

            import asyncpg

            async def _ping() -> None:
                ctx = ssl_mod.create_default_context(cafile=str(server_crt))
                connection = await asyncpg.connect(
                    host="127.0.0.1",
                    port=port,
                    user="tlsprobe",
                    password="tlsprobe-secret",
                    database="tlsprobe_test",
                    ssl=ctx,
                    timeout=2,
                )
                await connection.close()

            asyncio.run(_ping())
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time_mod.sleep(0.4)
    else:
        _cleanup()
        pytest.skip(f"Postgres TLS no aceptó conexiones: {last_error}")
    return server_crt, bad_ca, dsn, _cleanup
