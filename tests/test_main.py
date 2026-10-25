from importlib.metadata import PackageNotFoundError

import pytest
from pydantic import ValidationError

from app.config import get_settings


def test_importing_main_does_not_read_settings_when_app_env_is_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    import app.main as main

    assert "app" not in vars(main)
    with pytest.raises(ValidationError):
        main.create_app()
    get_settings.cache_clear()


def test_package_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    def raise_not_found(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(main, "version", raise_not_found)
    assert main._package_version() == "0.0.0"
