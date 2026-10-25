import asyncio
import errno
import socket
import time

import pytest

from app.db import RedactedDsn
from tests.conftest import (
    _PING_TOTAL_TIMEOUT,
    _is_connection_error,
    _ping_postgres,
    _postgres_required,
    _skip_or_fail_postgres,
    apply_test_migrations,
)


def test_connection_refused_is_connection_error() -> None:
    assert _is_connection_error(ConnectionRefusedError("Connection refused"))


def test_name_resolution_is_connection_error() -> None:
    assert _is_connection_error(socket.gaierror(-2, "Name or service not known"))


def test_timeout_is_connection_error() -> None:
    assert _is_connection_error(TimeoutError())
    assert _is_connection_error(TimeoutError())


def test_oserror_refused_is_connection_error() -> None:
    assert _is_connection_error(OSError(errno.ECONNREFUSED, "Connection refused"))


def test_password_error_is_not_connection_error() -> None:
    exc = RuntimeError('password authentication failed for user "estudiomusica"')
    assert _is_connection_error(exc) is False


def test_invalid_catalog_is_not_connection_error() -> None:
    exc = RuntimeError('database "estudiomusica_test" does not exist')
    assert _is_connection_error(exc) is False


def test_wrapped_refused_is_connection_error() -> None:
    inner = ConnectionRefusedError("Connection refused")
    outer = RuntimeError("connect failed")
    outer.__cause__ = inner
    assert _is_connection_error(outer)


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "yes", "YES", "on", "ON", "y", "t"]
)
def test_postgres_required_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_POSTGRES", value)
    assert _postgres_required() is True


@pytest.mark.parametrize(
    "value", ["0", "false", "FALSE", "no", "off", "OFF", "n", "f", ""]
)
def test_postgres_required_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_POSTGRES", value)
    assert _postgres_required() is False


def test_postgres_required_unknown_value_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_POSTGRES", "maybe")
    with pytest.raises(pytest.UsageError, match="REQUIRE_POSTGRES"):
        _postgres_required()


def test_skip_or_fail_fails_when_required_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_POSTGRES", "on")
    with pytest.raises(pytest.fail.Exception, match="no está disponible"):
        _skip_or_fail_postgres("Postgres de test no está disponible")


def test_skip_or_fail_fails_when_required_TRUE(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_POSTGRES", "TRUE")
    with pytest.raises(pytest.fail.Exception, match="no está disponible"):
        _skip_or_fail_postgres("Postgres de test no está disponible")


def test_postgres_required_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    assert _postgres_required() is False


def test_skip_or_fail_fails_when_postgres_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_POSTGRES", "1")
    with pytest.raises(pytest.fail.Exception, match="no está disponible"):
        _skip_or_fail_postgres("Postgres de test no está disponible")


def test_skip_or_fail_skips_when_postgres_is_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no está disponible"):
        _skip_or_fail_postgres("Postgres de test no está disponible")


def test_apply_test_migrations_raises_not_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broken migration")

    monkeypatch.setattr("tests.conftest.command.upgrade", boom)
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    with pytest.raises(RuntimeError, match="migraciones de test"):
        apply_test_migrations(
            RedactedDsn(
                "postgresql+asyncpg://u:p@127.0.0.1:5432/estudiomusica_test"
            )
        )


def test_ping_postgres_enforces_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangAfterConnect:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        async def __aenter__(self) -> _HangAfterConnect:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _HangEngine:
        def connect(self) -> _HangAfterConnect:
            return _HangAfterConnect()

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(
        "tests.conftest.create_async_engine", lambda *_a, **_k: _HangEngine()
    )
    started = time.monotonic()
    with pytest.raises((ConnectionError, RuntimeError)):
        _ping_postgres(
            RedactedDsn("postgresql+asyncpg://u:p@127.0.0.1:5432/estudiomusica_test")
        )
    elapsed = time.monotonic() - started
    assert elapsed < _PING_TOTAL_TIMEOUT + 1.5
