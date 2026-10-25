import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import get_settings
from app.db import get_engine
from app.schemas.health import DatabaseHealthResponse, HealthResponse

logger = logging.getLogger(__name__)


def check_health() -> HealthResponse:
    """Comprueba que el proceso responde. Sin dependencias externas."""

    return HealthResponse(status="ok")


def _abandon_health_connection(connection: AsyncConnection) -> None:
    """Libera la sonda sin un ``close()`` ordenado.

    Con Postgres congelado ``close()`` puede bloquear ~4 s y el
    ``asyncio.timeout`` no lo cancela. ``terminate`` / ``invalidate``
    cortan el socket en el hilo actual.
    """

    sync_connection = connection.sync_connection
    if sync_connection is None:
        return
    raw = getattr(sync_connection, "connection", None)
    dbapi = getattr(raw, "dbapi_connection", None)
    if dbapi is None:
        dbapi = raw
    terminate = getattr(dbapi, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except Exception:
            pass
    try:
        sync_connection.invalidate()
    except Exception:
        pass


async def check_database() -> DatabaseHealthResponse:
    """Comprueba Postgres con ``SELECT 1``.

    Cualquier fallo (incluida la expiración de ``db_health_timeout``) se
    traduce en ``down``. No se reenvía el mensaje de la excepción.
    """

    settings = get_settings()
    connection: AsyncConnection | None = None
    try:
        async with asyncio.timeout(settings.db_health_timeout):
            engine = get_engine()
            connection = await engine.connect()
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        return DatabaseHealthResponse(db="down")
    else:
        return DatabaseHealthResponse(db="ok")
    finally:
        if connection is not None:
            _abandon_health_connection(connection)
