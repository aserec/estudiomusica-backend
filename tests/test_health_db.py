import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress

import pytest
from fastapi.testclient import TestClient

from app.db import RedactedDsn
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


@pytest.mark.db
@pytest.mark.asyncio
async def test_health_db_frozen_after_login_returns_503_quickly(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Proxy que autentica y congela: 503, no 500, dentro del timeout.

    Sin terminate el execute cancelado espera command_timeout (~4 s).
    Sin invalidate la conexión queda checked-out. Abandon vacío cuelga.
    """

    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import clear_engine, dispose_engine, get_engine
    from tests.conftest import _build_client
    from tests.pg_freeze_proxy import FreezeAfterLoginProxy

    parsed = make_url(require_postgres.secret())
    proxy = FreezeAfterLoginProxy(parsed.host or "127.0.0.1", parsed.port or 5432)
    await proxy.start()
    frozen_url = parsed.set(host="127.0.0.1", port=proxy.port).render_as_string(
        hide_password=False
    )
    monkeypatch.setenv("DATABASE_URL", frozen_url)
    monkeypatch.setenv("DB_HEALTH_TIMEOUT", "0.6")
    monkeypatch.setenv("DB_COMMAND_TIMEOUT", "8")
    get_settings.cache_clear()
    clear_engine()
    try:
        started = time.monotonic()
        result = await health_service.check_database()
        elapsed = time.monotonic() - started
        assert result.db == "down"
        assert elapsed < 2.0

        engine = get_engine()
        assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]

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
            elapsed = time.monotonic() - started
            assert response.status_code == 503
            assert response.json() == {"db": "down"}
            assert elapsed < 2.0
        finally:
            close = getattr(client_iter, "close", None)
            if callable(close):
                close()
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        with suppress(Exception):
            await asyncio.wait_for(proxy.aclose(), timeout=2)


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
