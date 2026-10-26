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


def _frozen_url(require_postgres: RedactedDsn, port: int) -> str:
    """DSN hacia el proxy en claro.

    El proxy lee el protocolo Postgres sin TLS. Forzamos ``ssl=disable``
    aunque ``TEST_DATABASE_URL`` lleve ``ssl`` / ``sslmode``.
    """

    from sqlalchemy.engine.url import make_url

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
    frozen_url = _frozen_url(require_postgres, proxy.port)
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
        assert password_kwargs["ssl"] == "disable"
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
        skip = {"timeout", "command_timeout", "server_settings", "sslmode"}
        for key, value in app_opts.items():
            if key in skip:
                continue
            assert probe_opts[key] == value
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
        assert captured.get("ssl") == "disable"
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
    """P12: dentro del TTL no se abre otra conexión."""

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
    finally:
        health_service.clear_health_probe()
        get_settings.cache_clear()
