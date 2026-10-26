import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import get_settings
from app.db import get_engine
from app.schemas.health import DatabaseHealthResponse, HealthResponse

logger = logging.getLogger(__name__)

# Margen extra sobre ``db_health_timeout`` para abandonar un close/reset
# congelado. La petición no espera más que timeout + este margen.
HEALTH_TIMEOUT_MARGIN_SECONDS = 0.4

_cleanup_tasks: set[asyncio.Task[Any]] = set()


class _ExpectedTerminateFilter(logging.Filter):
    """SQLAlchemy registra ERROR+traza al terminar una sonda congelada."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "Exception terminating connection" not in message:
            return True
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        record.exc_info = None
        record.exc_text = None
        return True


def _install_pool_log_filter() -> None:
    """Los filtros no se heredan: hay que colgarlos del logger que emite."""

    filt = _ExpectedTerminateFilter()
    names = {
        "sqlalchemy.pool",
        "sqlalchemy.pool.base",
        "sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
        "sqlalchemy.pool.impl.QueuePool",
    }
    manager = logging.root.manager
    for name, candidate in manager.loggerDict.items():
        if (
            isinstance(name, str)
            and name.startswith("sqlalchemy.pool")
            and isinstance(candidate, logging.Logger)
        ):
            names.add(name)
    for name in names:
        pool_logger = logging.getLogger(name)
        if not any(
            isinstance(item, _ExpectedTerminateFilter) for item in pool_logger.filters
        ):
            pool_logger.addFilter(filt)
        for handler in pool_logger.handlers:
            if not any(
                isinstance(item, _ExpectedTerminateFilter) for item in handler.filters
            ):
                handler.addFilter(filt)
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(
            isinstance(item, _ExpectedTerminateFilter) for item in handler.filters
        ):
            handler.addFilter(filt)


_install_pool_log_filter()


def check_health() -> HealthResponse:
    """Comprueba que el proceso responde. Sin dependencias externas."""

    return HealthResponse(status="ok")


def _driver_terminate(connection: AsyncConnection) -> None:
    sync_connection = connection.sync_connection
    if sync_connection is None:
        return
    proxied = getattr(sync_connection, "_dbapi_connection", None)
    driver = None
    if proxied is not None:
        driver = getattr(proxied, "driver_connection", None)
    terminate = getattr(driver, "terminate", None)
    if callable(terminate):
        terminate()


def _abandon_health_connection(connection: AsyncConnection) -> None:
    """Descarta la sonda sin un ``close()`` ordenado.

    No toca ``Connection.connection``: ese property revalida y lanza
    ``PendingRollbackError``. Se termina el driver (sin await) y se
    invalida. Solo en timeout o error: el caso sano devuelve al pool.
    """

    try:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            _driver_terminate(connection)
        sync_connection = connection.sync_connection
        if sync_connection is None:
            return
        with contextlib.suppress(Exception, asyncio.CancelledError):
            sync_connection.invalidate()
    except Exception, asyncio.CancelledError:
        return


def _track_cleanup(task: asyncio.Task[Any]) -> None:
    _cleanup_tasks.add(task)

    def _consume(done: asyncio.Task[Any]) -> None:
        _cleanup_tasks.discard(done)
        if done.cancelled():
            return
        with contextlib.suppress(Exception, asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                logger.warning("database health cleanup: %s", type(exc).__name__)

    task.add_done_callback(_consume)


def _release_stuck_checkouts(engine: AsyncEngine) -> None:
    """Suelta checkouts congelados sin esperar un close ordenado."""

    with contextlib.suppress(Exception, asyncio.CancelledError):
        engine.sync_engine.dispose(close=False)


def _abandon_probe(
    engine: AsyncEngine,
    connection: AsyncConnection | None,
    task: asyncio.Task[Any],
) -> None:
    if connection is not None:
        _abandon_health_connection(connection)
    else:
        _release_stuck_checkouts(engine)
    if task.done():
        with contextlib.suppress(Exception, asyncio.CancelledError):
            task.exception()
        return
    task.cancel()
    _track_cleanup(task)


async def check_database() -> DatabaseHealthResponse:
    """Comprueba Postgres con ``SELECT 1``.

    Cualquier fallo (incluida la expiración de ``db_health_timeout``) se
    traduce en ``down``. No se reenvía el mensaje de la excepción.
    En éxito la conexión vuelve al pool; solo se descarta si falla.

    El tiempo total de la petición queda acotado a ``db_health_timeout``
    más ``HEALTH_TIMEOUT_MARGIN_SECONDS``. Lo que no termine se abandona
    (terminate síncrono + invalidate, o dispose del pool si aún no hay
    ``AsyncConnection``) sin bloquear la respuesta. Las tareas de limpieza
    se guardan en ``_cleanup_tasks`` para no dejar huérfanas.
    """

    _install_pool_log_filter()
    settings = get_settings()
    engine = get_engine()
    connection: AsyncConnection | None = None
    budget = settings.db_health_timeout + HEALTH_TIMEOUT_MARGIN_SECONDS
    started = asyncio.get_running_loop().time()

    async def probe() -> None:
        nonlocal connection
        connection = await engine.connect()
        await connection.execute(text("SELECT 1"))

    task = asyncio.create_task(probe())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=settings.db_health_timeout)
    except TimeoutError:
        logger.warning("database health check failed: %s", "TimeoutError")
        _abandon_probe(engine, connection, task)
        return DatabaseHealthResponse(db="down")
    except Exception as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        _abandon_probe(engine, connection, task)
        return DatabaseHealthResponse(db="down")

    remaining = budget - (asyncio.get_running_loop().time() - started)
    if connection is None:
        return DatabaseHealthResponse(db="ok")
    close_task = asyncio.create_task(connection.close())
    try:
        await asyncio.wait_for(asyncio.shield(close_task), timeout=max(0.05, remaining))
    except TimeoutError:
        logger.warning("database health check failed: %s", "TimeoutError")
        _abandon_health_connection(connection)
        if not close_task.done():
            close_task.cancel()
        _track_cleanup(close_task)
        return DatabaseHealthResponse(db="down")
    except Exception as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        _abandon_health_connection(connection)
        if not close_task.done():
            close_task.cancel()
        _track_cleanup(close_task)
        return DatabaseHealthResponse(db="down")
    return DatabaseHealthResponse(db="ok")
