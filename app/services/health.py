import asyncio
import contextlib
import logging
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine.url import make_url

from app.config import get_settings
from app.db import HEALTH_APPLICATION_NAME
from app.schemas.health import DatabaseHealthResponse, HealthResponse

logger = logging.getLogger(__name__)

# Tope para el close ordenado. Un close congelado se termina.
HEALTH_TIMEOUT_MARGIN_SECONDS = 0.4
_DIALECT_ONLY_CONNECT_KEYS = frozenset(
    {
        "prepared_statement_cache_size",
        "prepared_statement_name_func",
        "async_creator_fn",
    }
)

_cleanup_tasks: set[asyncio.Task[Any]] = set()
_health_url_override: str | None = None
_held_raw: asyncpg.Connection | None = None
_probe_task: asyncio.Task[DatabaseHealthResponse] | None = None
_waiters = 0
_cache: tuple[float, DatabaseHealthResponse] | None = None


def check_health() -> HealthResponse:
    """Comprueba que el proceso responde. Sin dependencias externas."""

    return HealthResponse(status="ok")


def set_health_database_url(url: str | None) -> None:
    """DSN solo para la sonda. ``None`` vuelve a ``Settings``."""

    global _health_url_override, _cache
    _health_url_override = url
    _cache = None


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
    global _held_raw, _probe_task
    if connection is not None:
        _abandon_health_connection(connection)
    if _raw_is_open(_held_raw) and _held_raw is not connection:
        _abandon_health_connection(_held_raw)
    _held_raw = None
    if _probe_task is task:
        _probe_task = None
    if task.done():
        with contextlib.suppress(Exception, asyncio.CancelledError):
            task.exception()
        return
    task.cancel()
    _track_cleanup(task)


def _connect_kwargs() -> dict[str, Any]:
    """Parámetros del dialecto menos las claves que solo consume SQLAlchemy."""

    from app.db import get_engine

    settings = get_settings()
    url = make_url(_health_dsn())
    _args, opts = get_engine().sync_engine.dialect.create_connect_args(url)
    kwargs = {
        key: value
        for key, value in dict(opts).items()
        if key not in _DIALECT_ONLY_CONNECT_KEYS
    }
    sslmode = None
    for key in list(kwargs):
        if str(key).lower() == "sslmode":
            sslmode = kwargs.pop(key)
    if sslmode is not None and "ssl" not in kwargs:
        kwargs["ssl"] = sslmode
    kwargs["timeout"] = settings.db_connect_timeout
    kwargs["command_timeout"] = settings.db_command_timeout
    server_settings = dict(kwargs.get("server_settings") or {})
    server_settings["application_name"] = HEALTH_APPLICATION_NAME
    kwargs["server_settings"] = server_settings
    return kwargs


def clear_health_probe() -> None:
    """Cierra la sonda residual (síncrono). Lo llama ``clear_engine``."""

    global _held_raw, _health_url_override, _probe_task, _waiters, _cache
    if _raw_is_open(_held_raw):
        with contextlib.suppress(Exception, asyncio.CancelledError):
            _terminate_driver_object(_held_raw)
    if _probe_task is not None and not _probe_task.done():
        _probe_task.cancel()
        _track_cleanup(_probe_task)
    _held_raw = None
    _health_url_override = None
    _probe_task = None
    _waiters = 0
    _cache = None


async def dispose_health_probe() -> None:
    """Cierra la sonda residual. Lo llama ``dispose_engine``."""

    clear_health_probe()


def _cached(now: float, ttl: float) -> DatabaseHealthResponse | None:
    if ttl <= 0 or _cache is None:
        return None
    stamped, result = _cache
    if now - stamped >= ttl:
        return None
    return result


async def _run_probe() -> DatabaseHealthResponse:
    global _held_raw, _cache
    raw: asyncpg.Connection | None = None
    settings = get_settings()
    try:
        async with asyncio.timeout(settings.db_health_timeout):
            raw = await asyncpg.connect(**_connect_kwargs())
            _held_raw = raw
            await raw.execute("SELECT 1")
            try:
                await asyncio.wait_for(
                    raw.close(), timeout=HEALTH_TIMEOUT_MARGIN_SECONDS
                )
            except Exception:
                _abandon_health_connection(raw)
                raise
            result = DatabaseHealthResponse(db="ok")
            _cache = (asyncio.get_running_loop().time(), result)
            return result
    except Exception as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        result = DatabaseHealthResponse(db="down")
        _cache = (asyncio.get_running_loop().time(), result)
        return result
    except BaseException:
        _abandon_health_connection(raw)
        raise
    finally:
        if raw is not None and _raw_is_open(raw):
            _abandon_health_connection(raw)
        if _held_raw is raw:
            _held_raw = None


async def check_database() -> DatabaseHealthResponse:
    """Una sonda asyncpg a la vez; las peticiones concurrentes esperan.

    El DSN se traduce con el dialecto de la app (sin claves propias
    del adaptador). El resultado se cachea
    ``db_health_cache_ttl`` segundos. Cada waiter tiene su propio
    ``db_health_timeout`` sobre ``asyncio.shield`` de la tarea compartida.
    La sonda en sí también vive como máximo ``db_health_timeout``.
    """

    global _probe_task, _waiters
    settings = get_settings()
    loop = asyncio.get_running_loop()
    cached = _cached(loop.time(), settings.db_health_cache_ttl)
    if cached is not None:
        return cached
    if _probe_task is None or _probe_task.done():
        _probe_task = asyncio.create_task(_run_probe())
    task = _probe_task
    _waiters += 1
    try:
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=settings.db_health_timeout
            )
        except TimeoutError:
            logger.warning("database health check failed: %s", "TimeoutError")
            return DatabaseHealthResponse(db="down")
    finally:
        _waiters -= 1
        if _waiters == 0 and not task.done():
            _abandon_probe(_held_raw, task)
