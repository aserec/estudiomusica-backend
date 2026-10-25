"""Motor async de SQLAlchemy 2 y fábrica de sesiones."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Devuelve el motor async, creándolo la primera vez que se pide."""

    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            connect_args={"timeout": 2},
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
    """Olvida el motor cacheado sin disponer conexiones (antes de un loop nuevo)."""

    global _engine, _session_factory
    _engine = None
    _session_factory = None


async def dispose_engine() -> None:
    """Cierra el pool y olvida el motor. Se llama al apagar la app."""

    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
