from collections.abc import Iterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.exceptions import Validation


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


def _mount_probes(application: FastAPI) -> None:
    @application.get("/api/v1/_test/errors/{code}")
    def raise_coded_error(code: str, status_code: int = 403) -> None:
        raise HTTPException(status_code=status_code, detail=code)

    @application.get("/api/v1/_test/boom")
    def raise_boom() -> None:
        raise RuntimeError("secret-leak")

    @application.get("/api/v1/_test/validation")
    def raise_validation() -> None:
        raise Validation(fields=["email"])

    @application.get("/api/v1/_test/http-400-text")
    def raise_http_400_text() -> None:
        raise HTTPException(status_code=400, detail="campo obligatorio")

    @application.get("/api/v1/_test/http-422")
    def raise_http_422() -> None:
        raise HTTPException(status_code=422, detail="unprocessable")

    @application.get("/api/v1/_test/http-400-dict")
    def raise_http_400_dict() -> None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_evaluation",
                "message": "secret-leak",
                "code": "no_solution",
            },
        )

    @application.post("/api/v1/_test/loose")
    def loose_body(body: dict[str, int]) -> dict[str, int]:
        return body


def _build_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[TestClient]:
    from app.config import get_settings
    from app.main import create_app, get_app

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_app.cache_clear()
    application = create_app()
    _mount_probes(application)
    with TestClient(application) as test_client:
        yield test_client
    get_settings.cache_clear()
    get_app.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
    )


@pytest.fixture
def probe_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Cliente de tests con rutas de sonda para códigos de error."""

    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
    )


@pytest.fixture
def production_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="production",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="false",
    )


@pytest.fixture
def small_body_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        MAX_BODY_BYTES="64",
    )


@pytest.fixture
def expose_errors_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="true",
        ENABLE_EXAMPLE_ROUTES="true",
    )


@pytest.fixture
def tight_pagination_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        PAGINATION_MAX_PAGE_SIZE="5",
        PAGINATION_MAX_PAGE="50",
    )
