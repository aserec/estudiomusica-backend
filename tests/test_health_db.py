import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress

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
    offline_production_client: TestClient,
) -> None:
    """health/db se monta siempre; /examples no.

    Este cliente usa un DSN dummy (sin Postgres): 503 es la sonda, no un
    sobre de error. El fixture ``production_client`` cubre production con BD.
    """

    response = offline_production_client.get("/api/v1/health/db")

    assert response.status_code == 503
    assert response.json() == {"db": "down"}
    assert offline_production_client.get("/api/v1/examples").status_code == 404


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


class _HangingConnect:
    async def __aenter__(self) -> _HangingConnect:
        await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _HangingEngine:
    def connect(self) -> _HangingConnect:
        return _HangingConnect()


@pytest.fixture
def hang_timeout_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from tests.conftest import _build_client

    monkeypatch.setattr(health_service, "get_engine", lambda: _HangingEngine())
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
    # El proxy lee el protocolo en claro; asyncpg negocia TLS por defecto.
    return (
        parsed.set(host="127.0.0.1", port=port)
        .update_query_dict({"ssl": "disable"})
        .render_as_string(hide_password=False)
    )


async def _run_frozen_health(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    *,
    freeze_on: str,
    health_timeout: float = 0.6,
    command_timeout: float = 8.0,
    warmup: bool = False,
) -> tuple[float, DatabaseHealthResponse, int, int]:
    """Devuelve (elapsed, result, abandon_calls, checkedout)."""

    from app.config import get_settings
    from app.db import clear_engine, dispose_engine, get_engine
    from app.services.health import HEALTH_TIMEOUT_MARGIN_SECONDS
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    parsed_host = require_postgres.secret()
    from sqlalchemy.engine.url import make_url

    parsed = make_url(parsed_host)
    proxy = FreezeAfterLoginProxy(
        parsed.host or "127.0.0.1",
        parsed.port or 5432,
        freeze_on=freeze_on,  # type: ignore[arg-type]
    )
    await proxy.start()
    frozen_url = _frozen_url(require_postgres, proxy.port)
    monkeypatch.setenv("DATABASE_URL", frozen_url)
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", str(health_timeout))
    monkeypatch.setenv("DB_COMMAND_TIMEOUT", str(command_timeout))
    get_settings.cache_clear()
    clear_engine()
    abandons = {"n": 0}
    original = health_service._abandon_health_connection

    def _spy(connection: object) -> None:
        abandons["n"] += 1
        original(connection)  # type: ignore[arg-type]

    monkeypatch.setattr(health_service, "_abandon_health_connection", _spy)
    budget = health_timeout + HEALTH_TIMEOUT_MARGIN_SECONDS + 0.35
    try:
        if warmup:
            warm = await health_service.check_database()
            assert warm.db == "ok"
        started = time.monotonic()
        result = await health_service.check_database()
        elapsed = time.monotonic() - started
        engine = get_engine()
        checkedout = engine.sync_engine.pool.checkedout()  # type: ignore[attr-defined]
        print(
            f"health freeze[{freeze_on}] elapsed={elapsed:.3f}s "
            f"timeout={health_timeout} command={command_timeout} "
            f"abandons={abandons['n']} checkedout={checkedout}",
            flush=True,
        )
        assert elapsed < budget, (
            f"elapsed={elapsed:.3f}s budget={budget:.3f}s freeze_on={freeze_on}"
        )
        return elapsed, result, abandons["n"], int(checkedout)
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
    """Congela SELECT 1 tras inicializar el dialecto. Mata T1/T2/T4/T5."""

    from tests.conftest import _build_client

    elapsed, result, abandons, checkedout = await _run_frozen_health(
        monkeypatch, require_postgres, freeze_on="select_1"
    )
    assert result.db == "down"
    assert abandons >= 1
    assert checkedout == 0

    parsed = require_postgres.secret()
    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import clear_engine
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    url = make_url(parsed)
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
async def test_health_db_frozen_pre_ping_returns_503_quickly(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    elapsed, result, _abandons, checkedout = await _run_frozen_health(
        monkeypatch, require_postgres, freeze_on="pre_ping", warmup=True
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
    elapsed, result, abandons, checkedout = await _run_frozen_health(
        monkeypatch, require_postgres, freeze_on="rollback"
    )
    assert result.db == "down"
    assert abandons >= 1
    assert checkedout == 0
    assert elapsed < 1.4


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_db_reuses_pool_connection_when_postgres_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    from sqlalchemy import event

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
        first = await health_service.check_database()
        second = await health_service.check_database()
        assert first.db == "ok"
        assert second.db == "ok"
        assert connects["n"] == 1
    finally:
        event.remove(engine.sync_engine, "connect", _on_connect)
        await dispose_engine()
        get_settings.cache_clear()
