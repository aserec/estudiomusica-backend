import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app, create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def probe_client() -> TestClient:
    """App de producción más rutas de sonda para códigos de error de negocio."""

    application = create_app()

    @application.get("/api/v1/_test/errors/{code}")
    def raise_coded_error(code: str, status_code: int = 403) -> None:
        raise HTTPException(status_code=status_code, detail=code)

    return TestClient(application)
