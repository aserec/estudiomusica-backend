"""Motor async de SQLAlchemy 2 y fábrica de sesiones."""

from collections.abc import AsyncIterator

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def redact_database_url(url: str) -> str:
    """DSN sin contraseña, para logs y mensajes de error."""

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<dsn-redactado>"


def get_engine() -> AsyncEngine:
    """Devuelve el motor async, creándolo la primera vez que se pide."""

    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.resolved_database_url(),
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            connect_args={
                "timeout": settings.db_connect_timeout,
                "command_timeout": settings.db_command_timeout,
            },
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia FastAPI: una sesión por petición, con commit o rollback."""

    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def clear_engine() -> None:
    """Cierra el pool síncrono y olvida el motor cacheado."""

    global _engine, _session_factory
    engine = _engine
    _engine = None
    _session_factory = None
    if engine is not None:
        engine.sync_engine.dispose()


async def dispose_engine() -> None:
    """Cierra el pool y olvida el motor. Se llama al apagar la app."""

    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
