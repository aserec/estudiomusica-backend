from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import String, Table, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.db import RedactedDsn, clear_engine, get_session, get_session_factory
from app.models import PersistentModel


async def _finish(agen: AsyncIterator[object]) -> None:
    """Reanuda el generador después del yield para que haga commit."""

    try:
        await anext(agen)
    except StopAsyncIteration:
        return
    raise AssertionError("get_session produjo una segunda sesión")


class SessionProbe(PersistentModel):
    """Tabla solo de test para commit/rollback reales."""

    __tablename__ = "session_probes"

    label: Mapped[str] = mapped_column(String(64), nullable=False)


@pytest.mark.asyncio
async def test_get_session_commits_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def _cm() -> AsyncIterator[AsyncMock]:
        yield session

    monkeypatch.setattr("app.db.get_session_factory", lambda: _cm)
    agen = get_session()
    yielded = await anext(agen)
    assert yielded is session
    await _finish(agen)
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_session_rollbacks_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def _cm() -> AsyncIterator[AsyncMock]:
        yield session

    monkeypatch.setattr("app.db.get_session_factory", lambda: _cm)
    agen = cast(AsyncGenerator[AsyncMock], get_session())
    await anext(agen)
    with pytest.raises(RuntimeError, match="boom"):
        await agen.athrow(RuntimeError("boom"))
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.db
async def test_get_session_override_runs_select(session_client: Any) -> None:
    response = await session_client.get("/api/v1/_test/session")

    assert response.status_code == 200
    assert response.json() == {"session": "ok"}


@pytest.mark.db
async def test_get_session_commits_persistently(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    from app.config import get_settings
    from app.db import dispose_engine

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    engine = create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
            )

        agen = get_session()
        session = await anext(agen)
        session.add(SessionProbe(label="committed"))
        await _finish(agen)

        factory = get_session_factory()
        async with factory() as other:
            found = await other.scalar(
                text("SELECT label FROM session_probes WHERE label = 'committed'")
            )
            assert found == "committed"
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
            )
        await engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()


@pytest.mark.db
async def test_get_session_rollbacks_persistently(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    from app.config import get_settings
    from app.db import dispose_engine

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    engine = create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
            )

        agen = cast(AsyncGenerator[AsyncSession], get_session())
        session = await anext(agen)
        session.add(SessionProbe(label="rolled-back"))
        await session.flush()
        with pytest.raises(RuntimeError, match="boom"):
            await agen.athrow(RuntimeError("boom"))

        factory = get_session_factory()
        async with factory() as other:
            found = await other.scalar(
                text("SELECT label FROM session_probes WHERE label = 'rolled-back'")
            )
            assert found is None
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
            )
        await engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()


@pytest.mark.db
async def test_db_session_does_not_expire_on_commit(
    db_session: AsyncSession,
) -> None:
    from sqlalchemy import inspect as sa_inspect

    connection = await db_session.connection()
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    await connection.run_sync(probe_table.create)

    probe = SessionProbe(label="still-loaded")
    db_session.add(probe)
    await db_session.commit()
    assert sa_inspect(probe).expired is False
    assert probe.label == "still-loaded"


_DB_SESSION_LEAK_LABEL = "db-session-must-not-leak"


@pytest.mark.db
async def test_db_session_commit_is_not_visible_outside_the_fixture(
    db_session: AsyncSession,
    require_postgres: RedactedDsn,
) -> None:
    """El fixture real envuelve el commit en una transacción que se revierte."""

    engine = create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
            )
            await connection.execute(
                text("DELETE FROM session_probes WHERE label = :label"),
                {"label": _DB_SESSION_LEAK_LABEL},
            )

        db_session.add(SessionProbe(label=_DB_SESSION_LEAK_LABEL))
        await db_session.commit()
        found = await db_session.scalar(
            text("SELECT label FROM session_probes WHERE label = :label"),
            {"label": _DB_SESSION_LEAK_LABEL},
        )
        assert found == _DB_SESSION_LEAK_LABEL

        async with engine.connect() as connection:
            leftover = await connection.scalar(
                text("SELECT label FROM session_probes WHERE label = :label"),
                {"label": _DB_SESSION_LEAK_LABEL},
            )
            assert leftover is None
    finally:
        await engine.dispose()


@pytest.mark.db
async def test_db_session_commit_does_not_leave_rows_for_the_next_test(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    await connection.run_sync(lambda sync_conn: probe_table.create(sync_conn, checkfirst=True))
    db_session.add(SessionProbe(label=_DB_SESSION_LEAK_LABEL))
    await db_session.commit()
    found = await db_session.scalar(
        text("SELECT label FROM session_probes WHERE label = :label"),
        {"label": _DB_SESSION_LEAK_LABEL},
    )
    assert found == _DB_SESSION_LEAK_LABEL


@pytest.mark.db
async def test_db_session_next_test_does_not_see_previous_commit(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    await connection.run_sync(lambda sync_conn: probe_table.create(sync_conn, checkfirst=True))
    leftover = await db_session.scalar(
        text("SELECT label FROM session_probes WHERE label = :label"),
        {"label": _DB_SESSION_LEAK_LABEL},
    )
    assert leftover is None


def test_clear_engine_disposes_sync_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import db as db_module
    from app.config import get_settings

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
    )
    get_settings.cache_clear()
    clear_engine()
    engine = db_module.get_engine()
    disposed = {"called": False}
    original = engine.sync_engine.dispose

    def _dispose() -> None:
        disposed["called"] = True
        original()

    monkeypatch.setattr(engine.sync_engine, "dispose", _dispose)
    clear_engine()
    assert disposed["called"] is True
    assert db_module._engine is None
    get_settings.cache_clear()
