from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, FastAPI

from app.config import get_settings
from app.routers import health_router


def _package_version() -> str:
    try:
        return version("estudiomusica-backend")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=_package_version(),
        description="Backend FastAPI de EstudioMusica.",
    )

    api_router = APIRouter(prefix="/api/v1")
    api_router.include_router(health_router)
    application.include_router(api_router)

    # Liveness en la raíz para sondas. El contrato de la API es GET /api/v1/health.
    application.include_router(health_router, include_in_schema=False)
    return application


def __getattr__(name: str) -> FastAPI:
    """Expone `app` de forma perezosa para Uvicorn/FastAPI CLI.

    Importar este módulo no instancia Settings, así un APP_ENV inválido
    en la shell no rompe la recogida de tests.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
