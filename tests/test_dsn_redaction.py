"""La contraseña del DSN no sale en fixtures, tracebacks ni --showlocals."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.db import RedactedDsn
from tests.conftest import (
    ROOT,
    _ping_postgres,
    apply_test_migrations,
    ensure_database_exists,
)

SENTINEL = "sentinel-password-must-not-appear-9f3c"
_DOWN_DSN = f"postgresql+asyncpg://leaky_user:{SENTINEL}@127.0.0.1:1/no_such_db_test"
_BAD_AUTH_DSN = (
    f"postgresql+asyncpg://estudiomusica:{SENTINEL}@127.0.0.1:5432/estudiomusica_test"
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
        "--tb=short",
        "-p",
        "no:cacheprovider",
        _DB_PROBE,
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
