"""Motor async de SQLAlchemy 2 y fábrica de sesiones."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any, get_origin, get_type_hints
from urllib.parse import parse_qsl, urlparse

from fastapi import Depends, Request
from sqlalchemy import event
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session as SyncSession

from app.config import get_settings

HEALTH_APPLICATION_NAME = "estudiomusica-health"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

_ADAPTER_INT_KEYS = frozenset({"prepared_statement_cache_size"})
_ADAPTER_CALLABLE_KEYS = frozenset(
    {
        "prepared_statement_name_func",
        "async_creator_fn",
    }
)
_SSLROOTCERT_KEYS = frozenset({"sslrootcert"})
_SSLMODE_KEYS = frozenset({"sslmode"})
_SSL_QUERY_KEYS = frozenset({"ssl", "sslmode"})
_SSL_CONFLICT_MSG = "DATABASE_URL no puede mezclar ni repetir ssl y sslmode"


_QUERY_SECRET_KEYS = frozenset({"password", "sslpassword"})


def redact_database_url(url: str) -> str:
    """DSN sin contraseña, para logs y mensajes de error.

    Oculta la del netloc y las de ``password`` / ``sslpassword`` en la
    query, sin distinguir mayúsculas.
    """

    try:
        parsed = make_url(url)
    except Exception:
        return "<dsn-redactado>"
    query = dict(parsed.query or {})
    for key in list(query):
        if str(key).lower() in _QUERY_SECRET_KEYS:
            query[key] = "***"
    return parsed.set(query=query).render_as_string(hide_password=True)


class RedactedDsn:
    """DSN cuyo ``str``/``repr`` nunca incluye la contraseña.

    pytest imprime argumentos y locales de fixtures (``-l`` / ``--showlocals``).
    Guardar el DSN crudo en una variable o en la firma del fixture filtra
    la contraseña. Este objeto solo la entrega con ``secret()``.
    """

    __slots__ = ("_secret",)

    def __init__(self, url: str) -> None:
        object.__setattr__(self, "_secret", url)

    def secret(self) -> str:
        value: str = object.__getattribute__(self, "_secret")
        return value

    def __str__(self) -> str:
        return redact_database_url(self.secret())

    def __repr__(self) -> str:
        return f"RedactedDsn({str(self)!r})"


def _query_scalar(query: Mapping[Any, Any], *keys: str) -> str | None:
    lowered = {str(key).lower(): value for key, value in query.items()}
    for key in keys:
        value = lowered.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else None
        return str(value)
    return None


def normalize_asyncpg_ssl_url(url: str) -> str:
    """Una sola regla para ``ssl`` / ``sslmode`` (Settings, engine, Alembic).

    Rechaza ``ssl`` y ``sslmode`` juntos y las claves repetidas.
    ``SSL`` / ``Ssl`` pasan a ``ssl``. ``sslmode`` pasa a ``ssl``.
    """

    try:
        parsed = make_url(url)
    except Exception:
        return url
    pairs = parse_qsl(urlparse(url).query, keep_blank_values=True)
    counts: dict[str, int] = {}
    for key, _value in pairs:
        lowered = key.lower()
        if lowered in _SSL_QUERY_KEYS:
            counts[lowered] = counts.get(lowered, 0) + 1
    if counts.get("ssl", 0) > 1 or counts.get("sslmode", 0) > 1:
        raise ValueError(_SSL_CONFLICT_MSG)
    if "ssl" in counts and "sslmode" in counts:
        raise ValueError(_SSL_CONFLICT_MSG)
    query = dict(parsed.query or {})
    ssl_value: object | None = None
    new_query: dict[Any, Any] = {}
    for key, value in query.items():
        lowered = str(key).lower()
        if lowered in _SSL_QUERY_KEYS:
            if isinstance(value, (list, tuple)):
                if len(value) > 1:
                    raise ValueError(_SSL_CONFLICT_MSG)
                ssl_value = value[0] if value else None
            else:
                ssl_value = value
            continue
        new_query[key] = value
    if ssl_value is not None:
        new_query["ssl"] = str(ssl_value)
    return parsed.set(query=new_query).render_as_string(hide_password=False)


def _reject_conflicting_ssl_opts(opts: Mapping[str, Any]) -> None:
    lowered = [str(key).lower() for key in opts]
    if lowered.count("ssl") > 1 or lowered.count("sslmode") > 1:
        raise ValueError(_SSL_CONFLICT_MSG)
    if "ssl" in lowered and "sslmode" in lowered:
        raise ValueError(_SSL_CONFLICT_MSG)


def ssl_context_from_rootcert(cafile: str, ssl_value: object = None) -> ssl.SSLContext:
    """``sslrootcert`` → ``SSLContext`` con ``cafile`` (asyncpg no tiene ese kwarg).

    Misma construcción que asyncpg con ``PGSSLROOTCERT``: no usa
    ``create_default_context`` (Python 3.13+ activa ``VERIFY_X509_STRICT``
    y rechaza CAs sin ``keyUsage``; libpq las acepta). Sigue verificando
    hostname y cadena. Tampoco activa ``VERIFY_X509_PARTIAL_CHAIN``: un
    ``sslrootcert`` que solo tenga una intermedia no basta (igual que
    libpq/asyncpg).
    """

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=cafile)
    mode = str(ssl_value).lower() if isinstance(ssl_value, str) else ""
    if mode == "verify-ca":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    # ssl=require + sslrootcert también verifica el hostname; más estricto
    # que libpq (require no comprueba CA ni hostname).
    ctx.check_hostname = mode not in {"disable", "allow"}
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _ssl_is_disabled(ssl_value: object) -> bool:
    if ssl_value is False:
        return True
    return isinstance(ssl_value, str) and ssl_value.lower() in {"disable", "false"}


def asyncpg_ssl_connect_arg(ssl_value: object, cafile: str | None) -> object:
    """SSL del engine y de la sonda. Sin ``SSLContext`` si ssl es disable/false."""

    if _ssl_is_disabled(ssl_value):
        return False
    if cafile:
        return ssl_context_from_rootcert(cafile, ssl_value)
    return ssl_value


def prepare_asyncpg_connect_kwargs(opts: Mapping[str, Any]) -> dict[str, Any]:
    """Kwargs de ``create_connect_args`` listos para ``asyncpg.connect``.

    Quita ``prepared_statement_cache_size`` (solo el adaptador). Quita
    ``async_creator_fn`` / ``prepared_statement_name_func`` solo si son
    callable; un str de la query se deja para que tumbe igual que la app.
    ``sslmode`` pasa a ``ssl``. ``sslrootcert`` pasa a un ``SSLContext``
    salvo si ``ssl`` es ``disable`` / ``false`` (misma regla que el engine).
    """

    _reject_conflicting_ssl_opts(opts)
    kwargs: dict[str, Any] = {}
    sslmode = None
    sslrootcert = None
    for key, value in dict(opts).items():
        lowered = str(key).lower()
        if key in _ADAPTER_INT_KEYS or lowered in _ADAPTER_INT_KEYS:
            continue
        if (
            key in _ADAPTER_CALLABLE_KEYS or lowered in _ADAPTER_CALLABLE_KEYS
        ) and callable(value):
            continue
        if lowered == "sslmode":
            sslmode = value
            continue
        if lowered == "ssl":
            kwargs["ssl"] = value
            continue
        if lowered in _SSLROOTCERT_KEYS:
            if isinstance(value, (list, tuple)):
                sslrootcert = str(value[0]) if value else None
            else:
                sslrootcert = str(value) if value else None
            continue
        kwargs[key] = value
    if sslmode is not None and "ssl" not in kwargs:
        kwargs["ssl"] = sslmode
    if sslrootcert or "ssl" in kwargs:
        kwargs["ssl"] = asyncpg_ssl_connect_arg(kwargs.get("ssl"), sslrootcert)
    return kwargs


def asyncpg_url_and_connect_args(raw: str) -> tuple[str, dict[str, Any]]:
    """Quita ``sslrootcert`` / ``sslmode`` de la URL (asyncpg no los acepta).

    El SSL lo decide ``asyncpg_ssl_connect_arg``, la misma función que la sonda.
    """

    raw = normalize_asyncpg_ssl_url(raw)
    parsed = make_url(raw)
    query = dict(parsed.query or {})
    prepared = prepare_asyncpg_connect_kwargs(query)
    connect_args: dict[str, Any] = {}
    drop = {
        key
        for key in query
        if str(key).lower() in _SSLROOTCERT_KEYS or str(key).lower() in _SSLMODE_KEYS
    }
    if drop:
        query = {key: value for key, value in query.items() if key not in drop}
        raw = parsed.set(query=query).render_as_string(hide_password=False)
    ssl_value = prepared.get("ssl")
    if ssl_value is not None:
        connect_args["ssl"] = ssl_value
    return raw, connect_args


def _engine_url_and_connect_args() -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    raw, connect_args = asyncpg_url_and_connect_args(settings.resolved_database_url())
    connect_args["timeout"] = settings.db_connect_timeout
    connect_args["command_timeout"] = settings.db_command_timeout
    return raw, connect_args


def get_engine() -> AsyncEngine:
    """Devuelve el motor async, creándolo la primera vez que se pide."""

    global _engine
    if _engine is None:
        settings = get_settings()
        url, connect_args = _engine_url_and_connect_args()
        _engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            connect_args=connect_args,
        )
    return _engine


async def _dispose_health_probe() -> None:
    from app.services.health import dispose_health_probe

    await dispose_health_probe()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


async def _get_session() -> AsyncIterator[AsyncSession]:
    """Sesión por petición: commit al salir del yield, rollback si falla.

    FastAPI 0.162 ejecuta el código posterior al ``yield`` *después* de
    enviar la respuesta si el ``Depends`` usa el scope por defecto
    (``request``). ``SessionDep`` fija ``scope="function"`` para que el
    commit ocurra antes de responder: un POST no puede devolver 2xx
    si el commit falla o si la fila aún no es visible.

    Un ``StreamingResponse`` que consulte la sesión en el generador
    arrancaría otra transacción *después* de ese commit (idle in
    transaction → 500 de QueuePool). El listener ``after_begin`` lo
    prohíbe. Para streaming usa ``SessionStreamingDep``. Con
    ``SessionDep`` el cliente ve 200 truncado (``RemoteProtocolError``).
    """

    factory = get_session_factory()
    async with factory() as session:
        released = False

        def _reject_begin_after_yield(
            _orm_session: object,
            _transaction: object,
            connection: object,
        ) -> None:
            if not released:
                return
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                rollback()
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            raise RuntimeError(
                "la sesión no se puede usar después del yield; "
                "un StreamingResponse o BackgroundTasks necesita "
                "SessionStreamingDep"
            )

        sync = getattr(session, "sync_session", None)
        if isinstance(sync, SyncSession):
            event.listen(sync, "after_begin", _reject_begin_after_yield)
        try:
            yield session
            await session.commit()
            released = True
        except Exception:
            await session.rollback()
            raise


def route_allows_streaming_session(route: object) -> bool:
    """``SessionStreamingDep`` solo si el retorno es ``StreamingResponse``.

    La anotación tiene que ser exactamente ``StreamingResponse`` o una
    subclase. Un ``Union`` / ``X | Y`` (p. ej. ``StreamingResponse | dict``)
    no vale: ese POST JSON devolvería 201 con el commit aún pendiente.
    """

    from starlette.responses import StreamingResponse as StarletteStreaming

    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        try:
            returned = get_type_hints(endpoint).get("return")
        except (NameError, TypeError):
            returned = None
        if returned is not None:
            if get_origin(returned) is not None:
                return False
            return isinstance(returned, type) and issubclass(
                returned, StarletteStreaming
            )
    response_class = getattr(route, "response_class", None)
    return isinstance(response_class, type) and issubclass(
        response_class, StarletteStreaming
    )


def _reject_non_streaming_route(request: Request) -> None:
    route = request.scope.get("route")
    if route_allows_streaming_session(route):
        return
    raise RuntimeError(
        "SessionStreamingDep solo en rutas que devuelven StreamingResponse"
    )


async def _get_streaming_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Sesión para ``StreamingResponse``: el commit espera al generador.

    ``SessionDep`` (scope=function) cierra la transacción antes de
    enviar el cuerpo. Esta dependencia usa scope=request para que el
    generador pueda consultar y el commit/rollback corran al terminar
    el stream, sin dejar idle in transaction.

    Un fallo de commit no cambia el status: el cuerpo ya salió
    (200 truncado). No uses ``SessionStreamingDep`` en un POST JSON:
    la dependencia lo rechaza si la anotación no es exactamente
    ``StreamingResponse`` (nada de ``Union``).
    """

    _reject_non_streaming_route(request)
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Las ramas #93–#97 deben importar SessionDep, no Depends(get_session):
# get_session ya no es pública (el scope por defecto reproducía 201
# con la fila aún no visible).
SessionDep = Annotated[AsyncSession, Depends(_get_session, scope="function")]
SessionStreamingDep = Annotated[
    AsyncSession, Depends(_get_streaming_session, scope="request")
]


def clear_engine() -> None:
    """Cierra el pool síncrono y olvida el motor cacheado."""

    global _engine, _session_factory
    engine = _engine
    _engine = None
    _session_factory = None
    if engine is not None:
        engine.sync_engine.dispose()
    from app.services.health import clear_health_probe

    clear_health_probe()


async def dispose_engine() -> None:
    """Cierra el pool y olvida el motor. Se llama al apagar la app."""

    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    await _dispose_health_probe()
