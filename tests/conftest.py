from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.testclient import TestClient

from app.exceptions import Validation

_SETTINGS_ENV = {
    "APP_NAME": "EstudioMusica API",
    "APP_ENV": "development",
    "EXPOSE_ERRORS": "false",
    "CORS_ORIGINS": "http://localhost:3000,http://127.0.0.1:3000",
    "CORS_ALLOW_CREDENTIALS": "true",
    "CORS_ALLOW_METHODS": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    "CORS_ALLOW_HEADERS": "Authorization,Content-Type,Accept",
    "PAGINATION_DEFAULT_PAGE": "1",
    "PAGINATION_DEFAULT_PAGE_SIZE": "20",
    "PAGINATION_MAX_PAGE": "10000",
    "PAGINATION_MAX_PAGE_SIZE": "100",
    "MAX_BODY_BYTES": "1048576",
}

PROBE_FILE_BYTES = b"file-response-body\n" * 8
_PROBE_FILE = Path("/tmp/estudiomusica-probe.bin")


@pytest.fixture(scope="session", autouse=True)
def isolate_settings_env() -> Iterator[None]:
    """Fija todas las variables de Settings durante la sesión de tests."""

    with pytest.MonkeyPatch.context() as mp:
        for key, value in _SETTINGS_ENV.items():
            mp.setenv(key, value)
        mp.delenv("ENABLE_EXAMPLE_ROUTES", raising=False)
        yield
        from app.config import get_settings
        from app.main import get_app

        get_settings.cache_clear()
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

    @application.get("/api/v1/_test/http-418")
    def raise_http_418() -> None:
        raise HTTPException(status_code=418)

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

    @application.post("/api/v1/_test/echo-size")
    async def echo_size(request: Request) -> dict[str, int]:
        data = await request.body()
        return {"size": len(data)}

    @application.post("/api/v1/_test/disconnect")
    async def check_disconnect(request: Request) -> dict[str, bool]:
        await request.body()
        return {"disconnected": await request.is_disconnected()}

    @application.get("/api/v1/_test/stream")
    def stream_chunks() -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            for index in range(5):
                yield f"chunk{index}"

        return StreamingResponse(generate(), media_type="text/plain")

    @application.get("/api/v1/_test/stream-boom")
    def stream_boom() -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            yield "chunk0"
            raise RuntimeError("mid-stream")

        return StreamingResponse(generate(), media_type="text/plain")

    @application.get("/api/v1/_test/file")
    def file_probe() -> FileResponse:
        _PROBE_FILE.write_bytes(PROBE_FILE_BYTES)
        return FileResponse(_PROBE_FILE, media_type="application/octet-stream")


def _build_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[TestClient]:
    from app.config import get_settings
    from app.main import create_app, get_app

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_app.cache_clear()
    try:
        application = create_app()
        _mount_probes(application)
        with TestClient(application) as test_client:
            yield test_client
    finally:
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
def wildcard_cors_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_client(
        monkeypatch,
        APP_ENV="development",
        EXPOSE_ERRORS="false",
        ENABLE_EXAMPLE_ROUTES="true",
        CORS_ORIGINS="*",
        CORS_ALLOW_CREDENTIALS="false",
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
