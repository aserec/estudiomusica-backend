from importlib.metadata import version

from fastapi import APIRouter, FastAPI

from app.config import get_settings
from app.routers import health_router


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=version("estudiomusica-backend"),
        description="Backend FastAPI de EstudioMusica.",
    )

    api_router = APIRouter(prefix="/api/v1")
    api_router.include_router(health_router)
    application.include_router(api_router)

    # Liveness en la raíz para sondas. El contrato de la API es GET /api/v1/health.
    application.include_router(health_router)
    return application


app = create_app()
