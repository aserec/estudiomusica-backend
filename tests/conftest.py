from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session", autouse=True)
def isolate_app_env() -> Iterator[None]:
    """Fija APP_ENV=development durante toda la sesión de tests.

    Se ignora el `.env` (y un APP_ENV exportado en la shell).
    MonkeyPatch.context() restaura el entorno al terminar.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("APP_ENV", "development")
        yield
        from app.main import get_app

        get_app.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.config import get_settings
    from app.main import create_app, get_app

    get_settings.cache_clear()
    get_app.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()
    get_app.cache_clear()
