from fastapi import FastAPI

from app.config import get_settings
from app.routers import health


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Backend FastAPI de EstudioMusica.",
    )
    application.include_router(health.router)
    return application


app = create_app()
