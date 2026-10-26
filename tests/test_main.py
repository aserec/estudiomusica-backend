import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import get_settings


def test_importing_main_does_not_read_settings_when_app_env_is_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import get_app

    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    get_app.cache_clear()
    try:
        import app.main as main

        assert "app" not in vars(main)
        with pytest.raises(ValidationError):
            main.create_app()
    finally:
        get_settings.cache_clear()
        get_app.cache_clear()


def test_create_app_imports_as_entrypoint() -> None:
    """``fastapi run`` carga ``app.main`` primero; no debe haber import circular."""

    result = subprocess.run(
        [sys.executable, "-c", "from app.main import create_app; create_app()"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_package_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    def raise_not_found(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(main, "version", raise_not_found)
    assert main._package_version() == "0.0.0"


def test_app_attribute_is_cached() -> None:
    import app.main as main

    main.get_app.cache_clear()
    try:
        assert main.app is main.app
    finally:
        main.get_app.cache_clear()


def test_fastapi_cli_discovers_app() -> None:
    import app.main as main

    assert "app" in dir(main)

    try:
        from fastapi_cli.discover import get_import_data
    except ImportError:
        return

    main.get_app.cache_clear()
    try:
        data = get_import_data(path=Path("app/main.py"))
        assert data.app_name == "app"
        assert data.import_string == "app.main:app"
    finally:
        main.get_app.cache_clear()
