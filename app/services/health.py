from sqlalchemy import text

from app.db import get_engine
from app.schemas.health import DatabaseHealthResponse, HealthResponse


def check_health() -> HealthResponse:
    """Comprueba que el proceso responde. Sin dependencias externas."""

    return HealthResponse(status="ok")


async def check_database() -> DatabaseHealthResponse:
    """Comprueba Postgres con ``SELECT 1``.

    Cualquier fallo se traduce en ``down``. No se reenvía el mensaje de la
    excepción (asyncpg a veces incluye usuario, host o el DSN).
    """

    try:
        engine = get_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return DatabaseHealthResponse(db="down")
    return DatabaseHealthResponse(db="ok")
