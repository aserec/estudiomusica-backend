import asyncio
import contextlib
import logging
from collections.abc import Iterator
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine.url import make_url

from app.config import Settings, get_settings
from app.db import HEALTH_APPLICATION_NAME
from app.schemas.health import DatabaseHealthResponse, HealthResponse

logger = logging.getLogger(__name__)

# Margen extra sobre ``db_health_timeout`` para abandonar un close congelado.
# La petición no espera más que timeout + este margen.
HEALTH_TIMEOUT_MARGIN_SECONDS = 0.4

_cleanup_tasks: set[asyncio.Task[Any]] = set()
_probe_inflight = False
_health_url_override: str | None = None
_held_raw: asyncpg.Connection | None = None


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
        record.stack_info = None
        return True


@contextlib.contextmanager
def _probe_log_context() -> Iterator[None]:
    """Filtra el ERROR del pool solo durante el abandono de la sonda.

    No se cuelga de los handlers de root: solo de los loggers de
    ``sqlalchemy.pool`` (por si algún close residual pasa por SA).
    """

    filt = _ExpectedTerminateFilter()
    loggers = (
        logging.getLogger("sqlalchemy.pool"),
        logging.getLogger("sqlalchemy.pool.impl.AsyncAdaptedQueuePool"),
        logging.getLogger("sqlalchemy.pool.impl.NullPool"),
    )
    attached: list[logging.Logger] = []
    for pool_logger in loggers:
        if not any(
            isinstance(item, _ExpectedTerminateFilter) for item in pool_logger.filters
        ):
            pool_logger.addFilter(filt)
            attached.append(pool_logger)
    try:
        yield
    finally:
        for pool_logger in attached:
            pool_logger.removeFilter(filt)


def check_health() -> HealthResponse:
    """Comprueba que el proceso responde. Sin dependencias externas."""

    return HealthResponse(status="ok")


def set_health_database_url(url: str | None) -> None:
    """DSN solo para la sonda. ``None`` vuelve a ``Settings``."""

    global _health_url_override
    _health_url_override = url


def _health_dsn() -> str:
    return _health_url_override or get_settings().resolved_database_url()


def _terminate_driver_object(driver: object) -> None:
    terminate = getattr(driver, "terminate", None)
    if callable(terminate):
        terminate()


def _raw_is_open(connection: asyncpg.Connection | None) -> bool:
    if connection is None:
        return False
    is_closed = getattr(connection, "is_closed", None)
    if callable(is_closed):
        try:
            return not bool(is_closed())
        except Exception:
            return True
    return True


def _abandon_health_connection(connection: object) -> None:
    """Termina el driver asyncpg sin ``close()`` ordenado."""

    with contextlib.suppress(Exception, asyncio.CancelledError):
        _terminate_driver_object(connection)


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


def _abandon_probe(
    connection: object | None,
    task: asyncio.Task[Any],
) -> None:
    global _held_raw
    if connection is not None:
        _abandon_health_connection(connection)
    if _raw_is_open(_held_raw) and _held_raw is not connection:
        _abandon_health_connection(_held_raw)
    _held_raw = None
    if task.done():
        with contextlib.suppress(Exception, asyncio.CancelledError):
            task.exception()
        return
    task.cancel()
    _track_cleanup(task)


def _probe_busy() -> bool:
    if _probe_inflight:
        return True
    return _raw_is_open(_held_raw)


def _connect_kwargs() -> dict[str, Any]:
    settings = get_settings()
    parsed = make_url(_health_dsn())
    kwargs: dict[str, Any] = {
        "host": parsed.host,
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password,
        "database": parsed.database,
        "timeout": settings.db_connect_timeout,
        "command_timeout": settings.db_command_timeout,
        "server_settings": {"application_name": HEALTH_APPLICATION_NAME},
    }
    # asyncpg en TCP usa ssl=prefer si no se pasa; hay que honrar el DSN
    # (p. ej. ssl=disable del proxy de freeze, que lee el protocolo en claro).
    sslmode = Settings._query_value(parsed, "sslmode", "ssl")
    if sslmode is not None:
        kwargs["ssl"] = sslmode
    return kwargs


def clear_health_probe() -> None:
    """Cierra la sonda residual (síncrono). Lo llama ``clear_engine``."""

    global _held_raw, _health_url_override, _probe_inflight
    if _raw_is_open(_held_raw):
        with contextlib.suppress(Exception, asyncio.CancelledError):
            _terminate_driver_object(_held_raw)
    _held_raw = None
    _health_url_override = None
    _probe_inflight = False


async def dispose_health_probe() -> None:
    """Cierra la sonda residual. Lo llama ``dispose_engine``."""

    clear_health_probe()


async def check_database() -> DatabaseHealthResponse:
    """Comprueba Postgres con ``SELECT 1`` por asyncpg, sin el pool de la app.

    Se abre una conexión propia (sin ``pre_ping``; el ``SELECT 1`` es el
    ping) y se guarda la referencia **antes** de la query. Como mucho hay
    una sonda en vuelo: si queda una abandonada pendiente se responde
    ``down`` sin abrir otra.

    El tiempo total queda acotado a ``db_health_timeout`` más
    ``HEALTH_TIMEOUT_MARGIN_SECONDS``. Lo que no termine se abandona con
    ``Connection.terminate`` (síncrono) sin bloquear la respuesta.
    """

    global _probe_inflight, _held_raw
    if _probe_busy():
        logger.warning("database health check failed: %s", "busy")
        return DatabaseHealthResponse(db="down")
    _probe_inflight = True
    settings = get_settings()
    raw: asyncpg.Connection | None = None
    budget = settings.db_health_timeout + HEALTH_TIMEOUT_MARGIN_SECONDS
    started = asyncio.get_running_loop().time()

    async def probe() -> None:
        nonlocal raw
        raw = await asyncpg.connect(**_connect_kwargs())
        _held_raw = raw
        await raw.execute("SELECT 1")

    task = asyncio.create_task(probe())
    try:
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=settings.db_health_timeout
            )
        except TimeoutError:
            logger.warning("database health check failed: %s", "TimeoutError")
            with _probe_log_context():
                _abandon_probe(raw, task)
            return DatabaseHealthResponse(db="down")
        except Exception as exc:
            logger.warning("database health check failed: %s", type(exc).__name__)
            with _probe_log_context():
                _abandon_probe(raw, task)
            return DatabaseHealthResponse(db="down")

        remaining = budget - (asyncio.get_running_loop().time() - started)
        if raw is None:
            return DatabaseHealthResponse(db="ok")
        close_task = asyncio.create_task(raw.close())
        try:
            await asyncio.wait_for(
                asyncio.shield(close_task), timeout=max(0.05, remaining)
            )
        except TimeoutError:
            logger.warning("database health check failed: %s", "TimeoutError")
            with _probe_log_context():
                _abandon_health_connection(raw)
                if not close_task.done():
                    close_task.cancel()
                _track_cleanup(close_task)
            _held_raw = None
            return DatabaseHealthResponse(db="down")
        except Exception as exc:
            logger.warning("database health check failed: %s", type(exc).__name__)
            with _probe_log_context():
                _abandon_health_connection(raw)
                if not close_task.done():
                    close_task.cancel()
                _track_cleanup(close_task)
            _held_raw = None
            return DatabaseHealthResponse(db="down")
        _held_raw = None
        return DatabaseHealthResponse(db="ok")
    finally:
        _probe_inflight = False
