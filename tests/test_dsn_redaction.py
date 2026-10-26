"""La contraseña del DSN no sale en fixtures, tracebacks ni --showlocals."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from app.db import RedactedDsn, redact_database_url
from tests.conftest import (
    ROOT,
    _as_redacted_dsn,
    _ping_postgres,
    apply_test_migrations,
    drop_database_if_exists,
    ensure_database_exists,
)

SENTINEL = "sent/inel%pass@word+must-not-appear-9f3c"
SENTINEL_QUOTED = quote(SENTINEL, safe="")
_DOWN_DSN = (
    f"postgresql+asyncpg://leaky_user:{SENTINEL_QUOTED}@127.0.0.1:1/no_such_db_test"
)
_DOWN_QUERY_DSN = (
    f"postgresql+asyncpg://leaky_user@127.0.0.1:1/no_such_db_test"
    f"?password={SENTINEL_QUOTED}"
)
_DOWN_QUERY_UPPER_DSN = (
    f"postgresql+asyncpg://leaky_user@127.0.0.1:1/no_such_db_test"
    f"?PASSWORD={SENTINEL_QUOTED}"
)
_DOWN_SSLPASS_DSN = (
    f"postgresql+asyncpg://leaky_user:ok@127.0.0.1:1/no_such_db_test"
    f"?sslpassword={SENTINEL_QUOTED}"
)
_DOWN_NETLOC_AND_QUERY_DSN = (
    f"postgresql+asyncpg://leaky_user:{SENTINEL_QUOTED}@127.0.0.1:1/no_such_db_test"
    f"?PASSWORD={SENTINEL_QUOTED}&sslpassword={SENTINEL_QUOTED}"
)
_BAD_AUTH_DSN = (
    f"postgresql+asyncpg://estudiomusica:{SENTINEL_QUOTED}"
    f"@127.0.0.1:5432/estudiomusica_test"
)
_DB_PROBE = "tests/test_health_db.py::test_health_db_ok_when_postgres_is_up"


def test_redacted_dsn_hides_password_in_str_and_repr() -> None:
    dsn = RedactedDsn(
        f"postgresql+asyncpg://leaky_user:{SENTINEL}@127.0.0.1:5432/app_test"
    )
    assert SENTINEL not in str(dsn)
    assert SENTINEL not in repr(dsn)
    assert "***" in str(dsn)
    assert dsn.secret().endswith("/app_test")


def test_redacted_dsn_hides_query_password_and_sslpassword() -> None:
    for raw in (_DOWN_QUERY_DSN, _DOWN_QUERY_UPPER_DSN, _DOWN_SSLPASS_DSN):
        dsn = RedactedDsn(raw)
        assert SENTINEL not in str(dsn)
        assert SENTINEL not in repr(dsn)
        rendered = str(dsn)
        assert "***" in rendered or "%2A%2A%2A" in rendered


def test_as_redacted_dsn_wraps_raw_string() -> None:
    dsn = _as_redacted_dsn(
        f"postgresql+asyncpg://leaky_user:{SENTINEL}@127.0.0.1:5432/app_test"
    )
    assert type(dsn) is RedactedDsn
    assert SENTINEL not in repr(dsn)
    assert SENTINEL not in str(dsn)


def test_ping_postgres_raw_str_does_not_leak_or_chain_cause() -> None:
    with pytest.raises(ConnectionError) as caught:
        _ping_postgres(_DOWN_DSN)
    assert SENTINEL not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_ensure_database_exists_raw_str_does_not_chain_cause() -> None:
    with pytest.raises(ConnectionError) as caught:
        ensure_database_exists(_DOWN_DSN)
    assert SENTINEL not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_apply_test_migrations_does_not_chain_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broken migration")

    monkeypatch.setattr("tests.conftest.command.upgrade", boom)
    with pytest.raises(RuntimeError, match="migraciones de test") as caught:
        apply_test_migrations(_DOWN_DSN)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert SENTINEL not in str(caught.value)


def test_ping_postgres_masks_password_in_connection_error() -> None:
    with pytest.raises(ConnectionError) as caught:
        _ping_postgres(RedactedDsn(_DOWN_DSN))
    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in repr(caught.value)


def test_ensure_database_exists_masks_password_in_connection_error() -> None:
    with pytest.raises(ConnectionError) as caught:
        ensure_database_exists(RedactedDsn(_DOWN_DSN))
    assert SENTINEL not in str(caught.value)


def test_apply_test_migrations_masks_password_when_upgrade_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broken migration")

    monkeypatch.setattr("tests.conftest.command.upgrade", boom)
    with pytest.raises(RuntimeError, match="migraciones de test") as caught:
        apply_test_migrations(RedactedDsn(_DOWN_DSN))
    assert SENTINEL not in str(caught.value)
    assert "broken migration" not in str(caught.value)


def _run_suite_with_showlocals(
    env_extra: dict[str, str],
    *,
    plugin: Path | None = None,
    tb: str = "short",
    target: str = _DB_PROBE,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.update(env_extra)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-l",
        "--showlocals",
        f"--tb={tb}",
        "-p",
        "no:cacheprovider",
        target,
    ]
    if plugin is not None:
        command[6:6] = ["-p", plugin.stem]
        env["PYTHONPATH"] = (
            f"{plugin.parent}{os.pathsep}{env.get('PYTHONPATH', '')}"
        ).rstrip(os.pathsep)
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        check=False,
    )


def _assert_failed_without_sentinel(result: subprocess.CompletedProcess[str]) -> str:
    output = f"{result.stdout}\n{result.stderr}"
    assert SENTINEL not in output
    assert result.returncode != 0
    skipped_only = result.returncode == 0 or (
        "skipped" in output.lower() and "failed" not in output.lower()
    )
    assert not skipped_only
    return output


def test_auth_failure_does_not_leak_password_with_showlocals() -> None:
    result = _run_suite_with_showlocals(
        {
            "TEST_DATABASE_URL": _BAD_AUTH_DSN,
            "REQUIRE_POSTGRES": "1",
        }
    )
    output = _assert_failed_without_sentinel(result)
    assert "rechazó" in output or "no está disponible" in output


def test_require_postgres_without_pg_does_not_leak_password_with_showlocals() -> None:
    result = _run_suite_with_showlocals(
        {
            "TEST_DATABASE_URL": _DOWN_DSN,
            "REQUIRE_POSTGRES": "1",
        }
    )
    output = _assert_failed_without_sentinel(result)
    assert "no está disponible" in output


def test_query_password_zero_hits_with_long_traceback_when_pg_down() -> None:
    result = _run_suite_with_showlocals(
        {
            "TEST_DATABASE_URL": _DOWN_QUERY_DSN,
            "REQUIRE_POSTGRES": "1",
        },
        tb="long",
    )
    output = _assert_failed_without_sentinel(result)
    assert output.count(SENTINEL) == 0
    assert "no está disponible" in output


def test_drop_query_password_zero_hits_with_long_traceback(tmp_path: Path) -> None:
    probe = tmp_path / "test_drop_query_password.py"
    probe.write_text(
        "import os\n"
        "from app.db import RedactedDsn\n"
        "from tests.conftest import drop_database_if_exists\n"
        "\n"
        "def test_drop_fails():\n"
        "    drop_database_if_exists(RedactedDsn(os.environ['LEAK_DSN']))\n"
    )
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["LEAK_DSN"] = _DOWN_QUERY_DSN
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-l",
            "--tb=long",
            "-p",
            "no:cacheprovider",
            str(probe),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert output.count(SENTINEL) == 0
    assert "No se pudo borrar" in output


@pytest.mark.parametrize(
    "raw_dsn",
    [
        _DOWN_DSN,
        _DOWN_QUERY_DSN,
        _DOWN_QUERY_UPPER_DSN,
        _DOWN_SSLPASS_DSN,
        _DOWN_NETLOC_AND_QUERY_DSN,
    ],
)
def test_drop_database_redacts_auth_failure_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    raw_dsn: str,
) -> None:
    """C1b/C2/C3 y S2–S6: neto, PASSWORD, sslpassword y DSN completo, con / % @ +."""

    from tests.conftest import drop_database_if_exists as drop

    def boom(_coro: object) -> None:
        close = getattr(_coro, "close", None)
        if callable(close):
            close()
        raise RuntimeError(
            'password authentication failed for user "leaky_user" '
            f"dsn={raw_dsn} secret={SENTINEL} quoted={SENTINEL_QUOTED}"
        )

    monkeypatch.setattr("tests.conftest.asyncio.run", boom)
    with pytest.raises(RuntimeError) as caught:
        drop(raw_dsn)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    message = str(caught.value)
    assert SENTINEL not in message
    assert SENTINEL_QUOTED not in message
    assert "No se pudo borrar" in message
    assert "password authentication failed" in message
    assert "***" in message


def test_redacted_exception_detail_replaces_full_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6: sin extraer contraseñas, el DSN completo se sustituye por el redactado."""

    from tests.conftest import _redacted_exception_detail

    monkeypatch.setattr("tests.conftest._dsn_secret_values", lambda _dsn: ())
    dsn = RedactedDsn(
        f"postgresql+asyncpg://leaky_user:{SENTINEL_QUOTED}@127.0.0.1:1/no_such_db_test"
    )
    message = _redacted_exception_detail(
        RuntimeError(f"boom {dsn.secret()} marker"),
        dsn,
    )
    assert SENTINEL not in message
    assert SENTINEL_QUOTED not in message
    assert dsn.secret() not in message
    assert str(dsn) in message
    assert "marker" in message


def test_redact_database_url_invalid_dsn() -> None:
    """R5: un DSN inválido no se parsea; no aparece el secreto."""

    assert redact_database_url("::::") == "<dsn-redactado>"
    leaked = redact_database_url(f"not-a-dsn {SENTINEL}")
    assert leaked == "<dsn-redactado>"
    assert SENTINEL not in leaked


def test_dsn_secret_values_keeps_all_repeated_passwords() -> None:
    from tests.conftest import _dsn_secret_values

    dsn = RedactedDsn(
        "postgresql+asyncpg://u:netloc-pw@127.0.0.1:1/db_test"
        "?password=first-pw&password=second-pw&PASSWORD=third-pw"
    )
    values = _dsn_secret_values(dsn)
    assert "netloc-pw" in values
    assert "first-pw" in values
    assert "second-pw" in values
    assert "third-pw" in values


def test_drop_database_from_none_includes_redacted_cause() -> None:
    with pytest.raises(ConnectionError) as caught:
        drop_database_if_exists(_DOWN_DSN)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in repr(caught.value)
    message = str(caught.value)
    assert "No se pudo borrar" in message
    assert "(" in message and ":" in message


def test_drop_database_does_not_leak_password_with_long_traceback(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "test_drop_database_fail.py"
    probe.write_text(
        "import os\n"
        "from app.db import RedactedDsn\n"
        "from tests.conftest import drop_database_if_exists\n"
        "\n"
        "def test_drop_fails():\n"
        "    drop_database_if_exists(RedactedDsn(os.environ['LEAK_DSN']))\n"
    )
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["LEAK_DSN"] = _DOWN_DSN
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-l",
            "--tb=long",
            "-p",
            "no:cacheprovider",
            str(probe),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert SENTINEL not in output
    assert "No se pudo borrar" in output


def test_ping_postgres_normalizes_sslmode(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.engine.url import make_url

    seen: dict[str, object] = {}

    def _capture(url: str, **_kwargs: object) -> object:
        seen["url"] = url
        raise ConnectionRefusedError("stop-after-capture")

    monkeypatch.setattr("tests.conftest.create_async_engine", _capture)
    with pytest.raises(ConnectionError):
        _ping_postgres(
            "postgresql+asyncpg://leaky_user:x@127.0.0.1:1/db_test?sslmode=require"
        )
    parsed = make_url(str(seen["url"]))
    query = {str(key).lower(): value for key, value in (parsed.query or {}).items()}
    assert query.get("ssl") == "require"
    assert "sslmode" not in query


def test_broken_migration_does_not_leak_password_with_showlocals(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "break_alembic_upgrade.py"
    plugin.write_text(
        "from alembic import command\n"
        "from tests import conftest as _conftest\n"
        "\n"
        "def pytest_configure(config):\n"
        "    def ok(_dsn=None, *_args, **_kwargs):\n"
        "        return None\n"
        "    def boom(*_args, **_kwargs):\n"
        "        raise RuntimeError('broken migration')\n"
        "    _conftest._ping_postgres = ok\n"
        "    _conftest.ensure_database_exists = ok\n"
        "    _conftest._drop_leftover_test_tables = ok\n"
        "    _conftest._drop_leftover_test_databases = ok\n"
        "    _conftest.drop_production_test_role = ok\n"
        "    command.upgrade = boom\n"
    )
    result = _run_suite_with_showlocals(
        {
            "TEST_DATABASE_URL": _BAD_AUTH_DSN,
            "REQUIRE_POSTGRES": "1",
        },
        plugin=plugin,
    )
    output = _assert_failed_without_sentinel(result)
    assert "migraciones de test" in output
