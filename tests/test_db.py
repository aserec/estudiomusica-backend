from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import String, Table, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.db import (
    RedactedDsn,
    SessionDep,
    clear_engine,
    get_session,
    get_session_factory,
)
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


class LeakProbe(PersistentModel):
    """Tabla persistida fuera de db_session; cada test la borra."""

    __tablename__ = "session_probe_leak"

    label: Mapped[str] = mapped_column(String(64), nullable=False)


class ExpireProbe(PersistentModel):
    """Tabla para probar expire_on_commit de app.db, no del fixture."""

    __tablename__ = "session_probe_expire"

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
async def test_get_session_factory_does_not_expire_on_commit(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """expire_on_commit=False vive en app.db, no en el fixture db_session."""

    from sqlalchemy import inspect as sa_inspect

    from app.config import get_settings
    from app.db import dispose_engine, get_engine, get_session_factory

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    factory = get_session_factory()
    assert factory.kw.get("expire_on_commit") is False

    engine = get_engine()
    probe_table = ExpireProbe.__table__
    assert isinstance(probe_table, Table)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
            )
        async with factory() as session:
            probe = ExpireProbe(label="still-loaded")
            session.add(probe)
            await session.commit()
            assert sa_inspect(probe).expired is False
            assert probe.label == "still-loaded"
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
            )
        await dispose_engine()
        get_settings.cache_clear()


_DB_SESSION_LEAK_LABEL = "db-session-must-not-leak"


@pytest.fixture
async def leak_probe_table(require_postgres: RedactedDsn) -> AsyncIterator[Table]:
    """Crea la tabla fuera de db_session y la borra después del rollback."""

    engine = create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = LeakProbe.__table__
    assert isinstance(probe_table, Table)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
        )
        await connection.execute(text("DELETE FROM session_probe_leak"))
    try:
        yield probe_table
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
            )
        await engine.dispose()


@pytest.mark.db
async def test_db_session_commit_is_not_visible_outside_the_fixture(
    leak_probe_table: Table,
    db_session: AsyncSession,
    require_postgres: RedactedDsn,
) -> None:
    """El fixture real envuelve el commit en una transacción que se revierte."""

    db_session.add(LeakProbe(label=_DB_SESSION_LEAK_LABEL))
    await db_session.commit()
    found = await db_session.scalar(
        text("SELECT label FROM session_probe_leak WHERE label = :label"),
        {"label": _DB_SESSION_LEAK_LABEL},
    )
    assert found == _DB_SESSION_LEAK_LABEL

    engine = create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            leftover = await connection.scalar(
                text("SELECT label FROM session_probe_leak WHERE label = :label"),
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
    await connection.run_sync(
        lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
    )
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
    await connection.run_sync(
        lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
    )
    leftover = await db_session.scalar(
        text("SELECT label FROM session_probes WHERE label = :label"),
        {"label": _DB_SESSION_LEAK_LABEL},
    )
    assert leftover is None


def test_session_dep_commits_before_response() -> None:
    """FastAPI 0.162: scope=function sale del yield antes de enviar."""

    from typing import get_args

    from fastapi.params import Depends as DependsParam

    args = get_args(SessionDep)
    assert args[0] is AsyncSession
    dep = args[1]
    assert isinstance(dep, DependsParam)
    assert dep.dependency is get_session
    assert dep.scope == "function"


async def _create_probe_table(engine: Any, probe_table: Table) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
        )
        await connection.execute(text("DELETE FROM session_probes"))


async def _drop_probe_table(engine: Any, probe_table: Table) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
        )


@pytest.mark.db
def test_http_posts_are_visible_from_another_session_before_response(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """400 POST 201: al enviar la respuesta la fila ya es visible."""

    import asyncio

    import asyncpg  # type: ignore[import-untyped]
    from fastapi.testclient import TestClient
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import dispose_engine, get_engine
    from app.main import create_app
    from tests.conftest import _mount_probes

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    engine = get_engine()
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    dialect = PGDialect_asyncpg()  # type: ignore[no-untyped-call]
    _args, opts = dialect.create_connect_args(  # type: ignore[no-untyped-call]
        make_url(require_postgres.secret())
    )
    login = {
        key: opts[key]
        for key in ("host", "port", "user", "password", "database", "ssl")
        if key in opts and opts[key] is not None
    }
    if "port" not in login:
        login["port"] = 5432
    seen: list[str | None] = []
    reader: dict[str, Any] = {}

    async def _read_label(label: str) -> str | None:
        connection = reader.get("c")
        if connection is None or connection.is_closed():
            connection = await asyncpg.connect(**login, timeout=2)
            reader["c"] = connection
        value: str | None = await connection.fetchval(
            "SELECT label FROM session_probes WHERE label = $1", label
        )
        return value

    asyncio.run(_create_probe_table(engine, probe_table))
    application = create_app()
    _mount_probes(application)

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await application(scope, receive, send)
            held = reader.get("c")
            if held is not None and not held.is_closed():
                await held.close()
            return

        async def send_wrapper(message: Any) -> None:
            if (
                scope.get("method") == "POST"
                and message.get("type") == "http.response.start"
            ):
                path = str(scope.get("path") or "")
                prefix = "/api/v1/_test/session/"
                if path.startswith(prefix):
                    label = path[len(prefix) :]
                    seen.append(await _read_label(label))
            await send(message)

        await application(scope, receive, send_wrapper)

    try:
        with TestClient(wrapped) as client:
            for index in range(400):
                label = f"vis-{index:03d}"
                response = client.post(f"/api/v1/_test/session/{label}")
                assert response.status_code == 201, response.text
                assert response.json() == {"label": label}
                assert seen[-1] == label
        assert len(seen) == 400
        assert all(item is not None for item in seen)
    finally:
        asyncio.run(_drop_probe_table(engine, probe_table))
        asyncio.run(dispose_engine())
        get_settings.cache_clear()


@pytest.mark.db
def test_http_session_commit_error_is_not_2xx(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Un fallo de commit no puede devolver 2xx (scope=function)."""

    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

    from app.config import get_settings
    from app.db import dispose_engine, get_engine
    from tests.conftest import _build_client

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    engine = get_engine()
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    asyncio.run(_create_probe_table(engine, probe_table))

    async def _boom(self: SAAsyncSession) -> None:
        raise RuntimeError("commit-failed")

    monkeypatch.setattr(SAAsyncSession, "commit", _boom)
    try:
        client_iter = _build_client(
            monkeypatch,
            APP_ENV="development",
            EXPOSE_ERRORS="false",
            ENABLE_EXAMPLE_ROUTES="true",
            DATABASE_URL=require_postgres.secret(),
        )
        client = next(client_iter)
        try:
            response = client.post("/api/v1/_test/session/commit-boom")
            assert not (200 <= response.status_code < 300)
            assert response.status_code >= 400
        finally:
            close = getattr(client_iter, "close", None)
            if callable(close):
                close()
    finally:
        asyncio.run(_drop_probe_table(engine, probe_table))
        asyncio.run(dispose_engine())
        get_settings.cache_clear()


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
