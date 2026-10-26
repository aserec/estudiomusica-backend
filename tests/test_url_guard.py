import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import (
    ROOT,
    alembic_config,
    derive_alembic_database_url,
    require_test_database_url,
    resolve_alembic_database_url,
    resolve_test_database_url,
)

_SECRET = "guard-secret-must-not-appear"


def _dsn(path: str, query: str = "") -> str:
    suffix = f"?{query}" if query else ""
    return f"postgresql+asyncpg://estudiomusica:{_SECRET}@127.0.0.1:5432/{path}{suffix}"


def test_require_test_database_url_accepts_test_name() -> None:
    url = _dsn("estudiomusica_test")
    assert require_test_database_url(url) == url


def test_require_test_database_url_rejects_dev_name() -> None:
    with pytest.raises(pytest.UsageError, match="_test") as caught:
        require_test_database_url(_dsn("estudiomusica"))
    assert _SECRET not in str(caught.value)
    assert "***" in str(caught.value)


@pytest.mark.parametrize(
    "query",
    [
        "database=estudiomusica",
        "dbname=estudiomusica",
        "Database=estudiomusica",
        "service=dev",
        "options=-c%20search_path=public",
        "ssl=require&database=estudiomusica",
    ],
)
def test_require_test_database_url_rejects_override_query(query: str) -> None:
    with pytest.raises(pytest.UsageError, match="parámetros") as caught:
        require_test_database_url(_dsn("estudiomusica_test", query))
    assert _SECRET not in str(caught.value)


def test_require_test_database_url_allows_harmless_query() -> None:
    url = _dsn("estudiomusica_test", "ssl=require&application_name=pytest")
    assert require_test_database_url(url) == url


def test_resolve_test_database_url_rejects_query_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        _dsn("estudiomusica_test", "database=estudiomusica"),
    )
    with pytest.raises(pytest.UsageError, match="parámetros"):
        resolve_test_database_url()


def test_derive_alembic_database_url_from_test_name() -> None:
    from sqlalchemy.engine.url import make_url

    derived = derive_alembic_database_url(_dsn("estudiomusica_test"))
    parsed = make_url(derived)
    assert parsed.database == "estudiomusica_alembic_test"
    assert parsed.password == _SECRET
    require_test_database_url(derived, env_name="ALEMBIC_TEST_DATABASE_URL")


def test_resolve_alembic_database_url_derives_from_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", _dsn("app_test"))
    monkeypatch.delenv("ALEMBIC_TEST_DATABASE_URL", raising=False)
    url = resolve_alembic_database_url()
    assert url.endswith("/app_alembic_test")


def _packaged_heads() -> list[str]:
    from alembic.script import ScriptDirectory

    from app.migrations.config import make_alembic_config

    return list(ScriptDirectory.from_config(make_alembic_config()).get_heads())


def test_make_alembic_config_works_without_repo_ini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.script import ScriptDirectory

    from app.migrations.config import make_alembic_config

    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "alembic.ini").exists()
    url = (
        "postgresql+asyncpg://estudiomusica:p%40ss%25w0rd"
        "@127.0.0.1:5432/estudiomusica_test"
    )
    config = make_alembic_config(url)
    assert config.get_main_option("script_location") == "app:migrations"
    assert config.get_main_option("sqlalchemy.url") == url
    assert ScriptDirectory.from_config(config).get_heads() == _packaged_heads()


def test_packaged_alembic_ini_is_importable() -> None:
    from importlib.resources import files

    assert files("app").joinpath("alembic.ini").is_file()


def test_root_alembic_ini_is_not_a_symlink() -> None:
    path = ROOT / "alembic.ini"
    assert path.is_file()
    assert not path.is_symlink()


def test_make_alembic_config_loads_packaged_ini() -> None:
    from alembic.script import ScriptDirectory

    from app.migrations.config import make_alembic_config, packaged_alembic_ini

    config = make_alembic_config()
    assert config.config_file_name is not None
    loaded = Path(config.config_file_name).resolve()
    assert loaded == packaged_alembic_ini().resolve()
    assert loaded.parent.name == "app"
    assert "prepend_sys_path = ." not in loaded.read_text()
    assert ScriptDirectory.from_config(config).get_heads() == _packaged_heads()


def test_alembic_config_from_clean_subprocess(tmp_path: Path) -> None:
    """make_alembic_config en un proceso limpio, CWD sin ``app/`` local."""

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from alembic.script import ScriptDirectory\n"
            "from app.migrations.config import make_alembic_config\n"
            "cfg = make_alembic_config()\n"
            "print(repr(cfg.get_main_option('prepend_sys_path') or ''))\n"
            "print(','.join(ScriptDirectory.from_config(cfg).get_heads()))\n",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines[0] == "''"
    assert lines[1].split(",") == _packaged_heads()


def test_alembic_cli_packaged_ini_ignores_cwd_app(tmp_path: Path) -> None:
    """El ejecutable ``alembic -c <packaged>`` no importa un ``app/`` del CWD."""

    from app.migrations.config import packaged_alembic_ini

    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "__init__.py").write_text(
        "raise RuntimeError('cwd app shadowed the installed package')\n"
    )
    alembic = Path(sys.executable).resolve().parent / "alembic"
    if not alembic.is_file():
        alembic = ROOT / ".venv" / "bin" / "alembic"
    assert alembic.is_file()
    result = subprocess.run(
        [str(alembic), "-c", str(packaged_alembic_ini()), "heads"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "cwd app shadowed" not in output
    for head in _packaged_heads():
        assert head in output


def test_python_c_from_cwd_with_app_imports_local_package(tmp_path: Path) -> None:
    """``python -c`` / ``python -m alembic`` importan un ``app/`` del CWD."""

    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "__init__.py").write_text(
        "raise RuntimeError('cwd app shadowed the installed package')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.migrations.config import make_alembic_config\n"
            "make_alembic_config()\n",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "cwd app shadowed" in output


def test_alembic_config_escapes_percent_in_password() -> None:
    """set_main_option interpola '%'; hay que pasar '%%' para no romper el DSN."""

    url = (
        "postgresql+asyncpg://estudiomusica:p%40ss%25w0rd"
        "@127.0.0.1:5432/estudiomusica_test"
    )
    config = alembic_config(url)
    assert config.get_main_option("sqlalchemy.url") == url


def test_resolve_alembic_database_url_rejects_explicit_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALEMBIC_TEST_DATABASE_URL", _dsn("estudiomusica"))
    with pytest.raises(pytest.UsageError, match="ALEMBIC_TEST_DATABASE_URL") as caught:
        resolve_alembic_database_url()
    assert _SECRET not in str(caught.value)


def test_autouse_guard_aborts_session_on_dev_database() -> None:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["TEST_DATABASE_URL"] = (
        f"postgresql+asyncpg://estudiomusica:{_SECRET}@127.0.0.1:5432/estudiomusica"
    )
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
    assert _SECRET not in output
    assert "_test" in output
