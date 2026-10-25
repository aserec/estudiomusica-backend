import asyncio
import contextlib
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
    """Descarta la sonda sin un ``close()`` ordenado.

    No toca ``Connection.connection``: ese property revalida y lanza
    ``PendingRollbackError`` que se escapa de ``suppress``. Se usa
    ``_dbapi_connection.driver_connection.terminate()`` y después
    ``invalidate()``. Solo en timeout o error: el caso sano devuelve
    la conexión al pool.
    """

    try:
        sync_connection = connection.sync_connection
        if sync_connection is None:
            return
        proxied = getattr(sync_connection, "_dbapi_connection", None)
        driver = (
            getattr(proxied, "driver_connection", None) if proxied is not None else None
        )
        terminate = getattr(driver, "terminate", None)
        if callable(terminate):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                terminate()
        # invalidate() hace checkin y deja checkedout=0. close() no: el
        # property ``connection`` revalida y lanza PendingRollbackError.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            sync_connection.invalidate()
    except Exception, asyncio.CancelledError:
        return


async def check_database() -> DatabaseHealthResponse:
    """Comprueba Postgres con ``SELECT 1``.

    Cualquier fallo (incluida la expiración de ``db_health_timeout``) se
    traduce en ``down``. No se reenvía el mensaje de la excepción.
    En éxito la conexión vuelve al pool; solo se descarta si falla.
    """

    settings = get_settings()
    engine = get_engine()
    connection: AsyncConnection | None = None

    async def probe() -> None:
        nonlocal connection
        connection = await engine.connect()
        await connection.execute(text("SELECT 1"))

    task = asyncio.create_task(probe())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=settings.db_health_timeout)
    except TimeoutError:
        logger.warning("database health check failed: %s", "TimeoutError")
        if connection is not None:
            _abandon_health_connection(connection)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=0.3)
        return DatabaseHealthResponse(db="down")
    except Exception as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        if connection is not None:
            _abandon_health_connection(connection)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=0.3)
        return DatabaseHealthResponse(db="down")

    try:
        if connection is not None:
            await connection.close()
    except Exception as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        if connection is not None:
            _abandon_health_connection(connection)
        return DatabaseHealthResponse(db="down")
    return DatabaseHealthResponse(db="ok")
