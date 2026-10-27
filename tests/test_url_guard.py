import ast
import configparser
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


def _ast_down_revision(value: ast.AST | None) -> str | tuple[str, ...] | None:
    """``down_revision``: ``None``, un id, o una tupla de merge."""

    if value is None:
        return None
    if isinstance(value, ast.Constant):
        return value.value if isinstance(value.value, str) else None
    if isinstance(value, ast.Tuple | ast.List):
        parents = tuple(
            elt.value
            for elt in value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
        return parents or None
    return None


def _flatten_down_revisions(down: str | tuple[str, ...] | None) -> tuple[str, ...]:
    if down is None:
        return ()
    if isinstance(down, tuple):
        return down
    return (down,)


def _revision_graph(
    versions: Path | None = None,
) -> dict[str, str | tuple[str, ...] | None]:
    """revision → down_revision parseado de los ficheros (no de Alembic)."""

    directory = versions or (ROOT / "app" / "migrations" / "versions")
    graph: dict[str, str | tuple[str, ...] | None] = {}
    for path in directory.glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text())
        rev: str | None = None
        down: str | tuple[str, ...] | None = None
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign) or not isinstance(
                node.target, ast.Name
            ):
                continue
            if (
                node.target.id == "revision"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                rev = node.value.value
            if node.target.id == "down_revision":
                down = _ast_down_revision(node.value)
        if rev is not None:
            graph[rev] = down
    return graph


def _expected_heads(versions: Path | None = None) -> list[str]:
    graph = _revision_graph(versions)
    downs: set[str] = set()
    for down in graph.values():
        downs.update(_flatten_down_revisions(down))
    return sorted(revision for revision in graph if revision not in downs)


def _packaged_heads() -> list[str]:
    from alembic.script import ScriptDirectory

    from app.migrations.config import make_alembic_config

    return sorted(ScriptDirectory.from_config(make_alembic_config()).get_heads())


def alembic_cli() -> Path:
    """Ejecutable Alembic del intérprete actual (``sys.prefix``, no resolve)."""

    name = "alembic.exe" if os.name == "nt" else "alembic"
    bindir = "Scripts" if os.name == "nt" else "bin"
    return Path(sys.prefix) / bindir / name


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
    assert sorted(ScriptDirectory.from_config(config).get_heads()) == _expected_heads()


def test_packaged_alembic_ini_is_importable() -> None:
    from importlib.resources import files

    assert files("app").joinpath("alembic.ini").is_file()


def test_root_alembic_ini_is_not_a_symlink() -> None:
    path = ROOT / "alembic.ini"
    assert path.is_file()
    assert not path.is_symlink()


def test_expected_heads_does_not_treat_merge_as_root(tmp_path: Path) -> None:
    """Una merge ``down_revision = ('a', 'b')`` no es cabeza; sus padres sí."""

    (tmp_path / "0001_a.py").write_text(
        "revision: str = 'a'\ndown_revision: str | None = None\n"
    )
    (tmp_path / "0002_b.py").write_text(
        "revision: str = 'b'\ndown_revision: str | None = None\n"
    )
    (tmp_path / "0003_merge.py").write_text(
        "revision: str = 'merge'\ndown_revision: tuple[str, ...] = ('a', 'b')\n"
    )
    assert _expected_heads(tmp_path) == ["merge"]
    graph = _revision_graph(tmp_path)
    assert graph["merge"] == ("a", "b")


def test_make_alembic_config_loads_packaged_ini() -> None:
    from alembic.script import ScriptDirectory

    from app.migrations.config import make_alembic_config, packaged_alembic_ini

    config = make_alembic_config()
    assert config.config_file_name is not None
    loaded = Path(config.config_file_name).resolve()
    assert loaded == packaged_alembic_ini().resolve()
    assert loaded.parent.name == "app"
    assert "prepend_sys_path = ." not in loaded.read_text()
    assert sorted(ScriptDirectory.from_config(config).get_heads()) == _expected_heads()


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
    assert sorted(lines[1].split(",")) == _expected_heads()


def test_alembic_cli_packaged_ini_ignores_cwd_app(tmp_path: Path) -> None:
    """El ejecutable ``alembic -c <packaged>`` no importa un ``app/`` del CWD."""

    from app.migrations.config import packaged_alembic_ini

    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "__init__.py").write_text(
        "raise RuntimeError('cwd app shadowed the installed package')\n"
    )
    alembic = alembic_cli()
    assert alembic.is_file()
    assert alembic.parent == Path(sys.prefix) / (
        "Scripts" if os.name == "nt" else "bin"
    )
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
    for head in _expected_heads():
        assert head in output


def test_root_alembic_ini_matches_packaged_keys() -> None:
    """INI1-3: la raíz no puede divergir de script_location / prepend / logger."""

    from app.migrations.config import packaged_alembic_ini

    root = configparser.ConfigParser()
    root.read(ROOT / "alembic.ini")
    packaged = configparser.ConfigParser()
    packaged.read(packaged_alembic_ini())
    assert root.get("alembic", "script_location") == packaged.get(
        "alembic", "script_location"
    )
    assert root.get("alembic", "script_location") == "app:migrations"
    assert (root.get("alembic", "prepend_sys_path", fallback="") or "") == ""
    assert (packaged.get("alembic", "prepend_sys_path", fallback="") or "") == ""
    assert root.get("logger_alembic", "level") == packaged.get(
        "logger_alembic", "level"
    )


def test_alembic_cli_with_uv_project_environment(tmp_path: Path) -> None:
    """``sys.prefix`` resuelve alembic dentro de UV_PROJECT_ENVIRONMENT."""

    from app.migrations.config import packaged_alembic_ini

    alt = tmp_path / "uv-env"
    created = subprocess.run(
        ["uv", "venv", str(alt)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    py = alt / "bin" / "python"
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(py), "-e", str(ROOT)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    probe = subprocess.run(
        [
            str(py),
            "-c",
            "import sys\n"
            "from pathlib import Path\n"
            "print(sys.prefix)\n"
            "print(Path(sys.prefix) / 'bin' / 'alembic')\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    prefix, alembic_path = [
        line.strip() for line in probe.stdout.splitlines() if line.strip()
    ]
    assert Path(prefix) == alt.resolve() or Path(prefix) == alt
    assert Path(alembic_path).is_file()
    fake = tmp_path / "cwd"
    fake.mkdir()
    (fake / "app").mkdir()
    (fake / "app" / "__init__.py").write_text(
        "raise RuntimeError('cwd app shadowed the installed package')\n"
    )
    result = subprocess.run(
        [alembic_path, "-c", str(packaged_alembic_ini()), "heads"],
        cwd=fake,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "cwd app shadowed" not in output
    for head in _expected_heads():
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
