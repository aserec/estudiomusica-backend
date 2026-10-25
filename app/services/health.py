import asyncio
import logging

from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.schemas.health import DatabaseHealthResponse, HealthResponse

logger = logging.getLogger(__name__)


def check_health() -> HealthResponse:
    """Comprueba que el proceso responde. Sin dependencias externas."""

    return HealthResponse(status="ok")


async def check_database() -> DatabaseHealthResponse:
    """Comprueba Postgres con ``SELECT 1``.

    Cualquier fallo (incluida la expiración de ``db_health_timeout``) se
    traduce en ``down``. No se reenvía el mensaje de la excepción.
    """

    settings = get_settings()
    try:
        async with asyncio.timeout(settings.db_health_timeout):
            engine = get_engine()
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        return DatabaseHealthResponse(db="down")
    return DatabaseHealthResponse(db="ok")
