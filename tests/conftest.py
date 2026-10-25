from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session", autouse=True)
def isolate_app_env() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("APP_ENV", "development")
        yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()
