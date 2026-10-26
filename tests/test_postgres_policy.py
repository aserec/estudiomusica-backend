import asyncio
import errno
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.db import RedactedDsn
from tests.conftest import (
    _PING_TOTAL_TIMEOUT,
    ROOT,
    _is_connection_error,
    _ping_postgres,
    _postgres_required,
    _skip_or_fail_postgres,
    _skip_or_fail_tls,
    _tls_required,
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


def test_unknown_require_postgres_aborts_even_when_postgres_is_up() -> None:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["REQUIRE_POSTGRES"] = "maybe"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "tests/test_health.py::test_api_health_returns_ok",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 2
    assert "REQUIRE_POSTGRES" in output
    assert "maybe" in output


def test_postgres_required_unknown_value_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_POSTGRES", "maybe")
    with pytest.raises(pytest.UsageError, match="REQUIRE_POSTGRES"):
        _postgres_required()


def test_skip_or_fail_suppresses_cause_from_require_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U3: ``from None`` en el skip/fail de require_postgres."""

    monkeypatch.setenv("REQUIRE_POSTGRES", "1")
    try:
        raise ConnectionError("refused")
    except ConnectionError:
        with pytest.raises(pytest.fail.Exception) as caught:
            _skip_or_fail_postgres("Postgres de test no está disponible")
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


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


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "yes", "YES", "on", "ON", "y", "t"]
)
def test_tls_required_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_TLS_TESTS", value)
    assert _tls_required() is True


@pytest.mark.parametrize(
    "value", ["0", "false", "FALSE", "no", "off", "OFF", "n", "f", ""]
)
def test_tls_required_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_TLS_TESTS", value)
    assert _tls_required() is False


def test_tls_required_unknown_value_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUIRE_TLS_TESTS", "maybe")
    with pytest.raises(pytest.UsageError, match="REQUIRE_TLS_TESTS"):
        _tls_required()


def test_unknown_require_tls_tests_aborts_even_when_suite_starts() -> None:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["REQUIRE_TLS_TESTS"] = "maybe"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "tests/test_health.py::test_api_health_returns_ok",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 2
    assert "REQUIRE_TLS_TESTS" in output
    assert "maybe" in output


def test_skip_or_fail_tls_fails_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_TLS_TESTS", "1")
    with pytest.raises(BaseException, match="no se pudo arrancar") as caught:
        _skip_or_fail_tls("no se pudo arrancar Postgres TLS")
    assert type(caught.value) is pytest.fail.Exception


def test_skip_or_fail_tls_skips_when_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no se pudo arrancar"):
        _skip_or_fail_tls("no se pudo arrancar Postgres TLS")


def test_tls_cafile_missing_fails_when_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_health_db import _reuse_tls_test_env

    monkeypatch.setenv("REQUIRE_TLS_TESTS", "1")
    monkeypatch.setenv("TLS_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@h/db_test")
    monkeypatch.setenv("TLS_TEST_CAFILE", str(tmp_path / "missing-ca.pem"))
    with pytest.raises(BaseException, match="TLS_TEST_CAFILE no existe") as caught:
        _reuse_tls_test_env()
    assert type(caught.value) is pytest.fail.Exception


def test_tls_cafile_missing_skips_when_optional(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_health_db import _reuse_tls_test_env

    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    monkeypatch.setenv("TLS_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@h/db_test")
    monkeypatch.setenv("TLS_TEST_CAFILE", str(tmp_path / "missing-ca.pem"))
    with pytest.raises(pytest.skip.Exception, match="TLS_TEST_CAFILE no existe"):
        _reuse_tls_test_env()


def test_tls_url_without_cafile_fails_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _reuse_tls_test_env

    monkeypatch.setenv("REQUIRE_TLS_TESTS", "1")
    monkeypatch.setenv("TLS_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@h/db_test")
    monkeypatch.delenv("TLS_TEST_CAFILE", raising=False)
    with pytest.raises(BaseException, match="sin TLS_TEST_CAFILE") as caught:
        _reuse_tls_test_env()
    assert type(caught.value) is pytest.fail.Exception


def test_tls_url_without_cafile_skips_when_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _reuse_tls_test_env

    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    monkeypatch.setenv("TLS_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@h/db_test")
    monkeypatch.delenv("TLS_TEST_CAFILE", raising=False)
    with pytest.raises(pytest.skip.Exception, match="sin TLS_TEST_CAFILE"):
        _reuse_tls_test_env()


def test_tls_cafile_reuses_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_health_db import _reuse_tls_test_env

    cafile = tmp_path / "ca.pem"
    cafile.write_text("dummy-ca\n", encoding="utf-8")
    url = "postgresql+asyncpg://u:p@h/db_test"
    monkeypatch.setenv("TLS_TEST_DATABASE_URL", url)
    monkeypatch.setenv("TLS_TEST_CAFILE", str(cafile))
    assert _reuse_tls_test_env() == (url, str(cafile))


def test_tls_docker_missing_fails_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _fail_or_skip_tls_docker_missing

    monkeypatch.setenv("REQUIRE_TLS_TESTS", "1")
    with pytest.raises(BaseException, match="docker no está en PATH") as caught:
        _fail_or_skip_tls_docker_missing()
    assert type(caught.value) is pytest.fail.Exception


def test_tls_docker_missing_skips_when_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _fail_or_skip_tls_docker_missing

    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="docker no está en PATH"):
        _fail_or_skip_tls_docker_missing()


def test_tls_docker_run_fails_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _fail_or_skip_tls_docker_run

    monkeypatch.setenv("REQUIRE_TLS_TESTS", "1")
    with pytest.raises(BaseException, match="no se pudo arrancar") as caught:
        _fail_or_skip_tls_docker_run("daemon not running")
    assert type(caught.value) is pytest.fail.Exception
    assert "daemon not running" in str(caught.value)


def test_tls_docker_run_skips_when_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _fail_or_skip_tls_docker_run

    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no se pudo arrancar"):
        _fail_or_skip_tls_docker_run("daemon not running")


def test_tls_docker_ping_fails_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _fail_or_skip_tls_docker_ping

    monkeypatch.setenv("REQUIRE_TLS_TESTS", "1")
    with pytest.raises(BaseException, match="no aceptó conexiones") as caught:
        _fail_or_skip_tls_docker_ping("timeout")
    assert type(caught.value) is pytest.fail.Exception
    assert "timeout" in str(caught.value)


def test_tls_docker_ping_skips_when_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_health_db import _fail_or_skip_tls_docker_ping

    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no aceptó conexiones"):
        _fail_or_skip_tls_docker_ping("timeout")


def test_tls_required_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    assert _tls_required() is False


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
    monkeypatch.delenv("REQUIRE_TLS_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no está disponible"):
        _skip_or_fail_postgres("Postgres de test no está disponible")


def test_skip_or_fail_postgres_fails_when_tls_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    monkeypatch.setenv("REQUIRE_TLS_TESTS", "1")
    with pytest.raises(BaseException, match="no está disponible") as caught:
        _skip_or_fail_postgres("Postgres de test no está disponible")
    assert type(caught.value) is pytest.fail.Exception


@pytest.mark.timeout(90)
def test_require_tls_without_postgres_fails_the_suite() -> None:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("REQUIRE_POSTGRES", None)
    env.pop("TLS_TEST_DATABASE_URL", None)
    env.pop("TLS_TEST_CAFILE", None)
    env["REQUIRE_TLS_TESTS"] = "1"
    env["TEST_DATABASE_URL"] = "postgresql+asyncpg://u:p@127.0.0.1:1/estudiomusica_test"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "tests/test_health_db.py::test_health_db_ok_when_postgres_is_up",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr


def test_apply_test_migrations_raises_not_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broken migration")

    monkeypatch.setattr("tests.conftest.command.upgrade", boom)
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    with pytest.raises(RuntimeError, match="migraciones de test"):
        apply_test_migrations(
            RedactedDsn("postgresql+asyncpg://u:p@127.0.0.1:5432/estudiomusica_test")
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
