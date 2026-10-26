from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import String, Table, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db import (
    RedactedDsn,
    SessionDep,
    _get_session,
    clear_engine,
    get_session_factory,
)
from app.models import PersistentModel
from tests.conftest import _create_async_engine


async def _finish(agen: AsyncIterator[object]) -> None:
    """Reanuda el generador después del yield para que haga commit."""

    try:
        await anext(agen)
    except StopAsyncIteration:
        return
    raise AssertionError("_get_session produjo una segunda sesión")


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
    agen = _get_session()
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
    agen = cast(AsyncGenerator[AsyncMock], _get_session())
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
    engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
            )

        agen = _get_session()
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
    engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.create(sync_conn, checkfirst=True)
            )

        agen = cast(AsyncGenerator[AsyncSession], _get_session())
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

    engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
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

    engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
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

    import app.db as db_module

    args = get_args(SessionDep)
    assert args[0] is AsyncSession
    dep = args[1]
    assert isinstance(dep, DependsParam)
    assert dep.dependency is _get_session
    assert dep.scope == "function"
    assert not hasattr(db_module, "get_session")


def _iter_dependants(dependant: Any) -> Any:
    yield dependant
    for child in getattr(dependant, "dependencies", None) or ():
        yield from _iter_dependants(child)


def _route_returns_streaming(route: Any) -> bool:
    from typing import get_args, get_origin, get_type_hints

    from fastapi.responses import StreamingResponse
    from starlette.responses import StreamingResponse as StarletteStreaming

    response_class = getattr(route, "response_class", None)
    if isinstance(response_class, type) and issubclass(
        response_class, StarletteStreaming
    ):
        return True
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return False
    returned = get_type_hints(endpoint).get("return")
    if returned is StreamingResponse or returned is StarletteStreaming:
        return True
    origin = get_origin(returned)
    if origin is None:
        return False
    return any(
        arg is StreamingResponse or arg is StarletteStreaming
        for arg in get_args(returned)
    )


def test_session_route_deps_use_function_scope() -> None:
    """Ninguna ruta puede inyectar la sesión con el scope por defecto.

    ``SessionStreamingDep`` solo en rutas que devuelven ``StreamingResponse``.
    """

    from typing import get_args

    from fastapi.params import Depends as DependsParam

    from app.db import SessionStreamingDep, _get_streaming_session
    from app.main import create_app
    from tests.conftest import _mount_probes

    application = create_app()
    _mount_probes(application)
    seen_session = False
    seen_streaming = False
    for route in application.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for item in _iter_dependants(dependant):
            call = getattr(item, "call", None)
            if call is _get_session:
                seen_session = True
                assert item.scope == "function", route
            elif call is _get_streaming_session:
                seen_streaming = True
                assert item.scope == "request", route
                assert _route_returns_streaming(route), route.path
    assert seen_session
    assert seen_streaming
    streaming_args = get_args(SessionStreamingDep)
    streaming_dep = streaming_args[1]
    assert isinstance(streaming_dep, DependsParam)
    assert streaming_dep.dependency is _get_streaming_session
    assert streaming_dep.scope == "request"


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
@pytest.mark.asyncio
async def test_http_posts_are_visible_from_another_session_before_response(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """400 POST 201: otra sesión (GET) ve la fila en cuanto responde el POST."""

    from httpx2 import ASGITransport, AsyncClient

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app
    from tests.conftest import _mount_probes

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    setup_engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    await _create_probe_table(setup_engine, probe_table)
    application = create_app()
    _mount_probes(application)
    try:
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for index in range(400):
                label = f"vis-{index:03d}"
                created = await client.post(f"/api/v1/_test/session/{label}")
                assert created.status_code == 201, created.text
                assert created.json() == {"label": label}
                read = await client.get(f"/api/v1/_test/session/{label}")
                assert read.status_code == 200, read.text
                assert read.json() == {"label": label}
    finally:
        await _drop_probe_table(setup_engine, probe_table)
        await setup_engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()
        clear_engine()


@pytest.mark.db
@pytest.mark.asyncio
async def test_http_session_commit_error_is_not_2xx(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """Un fallo de commit no puede devolver 2xx (scope=function)."""

    from httpx2 import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app
    from tests.conftest import _mount_probes

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    setup_engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    await _create_probe_table(setup_engine, probe_table)
    application = create_app()
    _mount_probes(application)

    async def _boom(self: SAAsyncSession) -> None:
        raise RuntimeError("commit-failed")

    monkeypatch.setattr(SAAsyncSession, "commit", _boom)
    try:
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/_test/session/commit-boom")
            assert not (200 <= response.status_code < 300)
            assert response.status_code >= 400
    finally:
        await _drop_probe_table(setup_engine, probe_table)
        await setup_engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()
        clear_engine()


async def _count_idle_in_transaction(dsn: RedactedDsn) -> int:
    engine = _create_async_engine(dsn.secret())
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'idle in transaction' "
                    "AND pid <> pg_backend_pid()"
                )
            )
            return int(value or 0)
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_streaming_session_dep_leaves_no_idle_in_transaction(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """M-1: StreamingResponse + SessionDep no deja idle in transaction."""

    from httpx2 import ASGITransport, AsyncClient

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app
    from tests.conftest import _mount_probes

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    application = create_app()
    _mount_probes(application)
    baseline = await _count_idle_in_transaction(require_postgres)
    try:
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            try:
                leaked = await client.get("/api/v1/_test/session-stream")
            except RuntimeError as exc:
                assert "SessionStreamingDep" in str(exc)
            else:
                assert leaked.status_code >= 400 or "row=" not in leaked.text
            after_leak = await _count_idle_in_transaction(require_postgres)
            assert after_leak == baseline
            ok = await client.get("/api/v1/_test/session-stream-ok")
            assert ok.status_code == 200
            assert ok.text == "row=1"
            after_ok = await _count_idle_in_transaction(require_postgres)
            assert after_ok == baseline
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        clear_engine()


@pytest.mark.db
@pytest.mark.asyncio
async def test_streaming_session_commits_written_row(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """ST2: tras el stream la fila insertada es visible."""

    from httpx2 import ASGITransport, AsyncClient

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app
    from tests.conftest import _mount_probes

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    setup_engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    await _create_probe_table(setup_engine, probe_table)
    application = create_app()
    _mount_probes(application)
    try:
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            written = await client.get("/api/v1/_test/session-stream-write/st2-row")
            assert written.status_code == 200
            assert written.text == "row=st2-row"
            visible = await client.get("/api/v1/_test/session/st2-row")
            assert visible.status_code == 200
            assert visible.json() == {"label": "st2-row"}
    finally:
        await _drop_probe_table(setup_engine, probe_table)
        await setup_engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()
        clear_engine()


@pytest.mark.db
@pytest.mark.asyncio
async def test_streaming_session_commit_error_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ST3: un commit fallido no se traga; la fila no queda."""

    from httpx2 import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

    from app.config import get_settings
    from app.db import dispose_engine
    from app.main import create_app
    from tests.conftest import _mount_probes

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    clear_engine()
    setup_engine = _create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = SessionProbe.__table__
    assert isinstance(probe_table, Table)
    await _create_probe_table(setup_engine, probe_table)
    application = create_app()
    _mount_probes(application)

    async def _boom(self: SAAsyncSession) -> None:
        raise RuntimeError("stream-commit-failed")

    monkeypatch.setattr(SAAsyncSession, "commit", _boom)
    try:
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            noticed = False
            try:
                response = await client.get(
                    "/api/v1/_test/session-stream-write/st3-row"
                )
            except Exception as exc:
                noticed = "stream-commit-failed" in str(exc)
            else:
                noticed = (
                    response.status_code >= 400
                    or "stream-commit-failed" in caplog.text
                    or "stream-commit-failed" in response.text
                )
            assert noticed
            leftover = await setup_engine.connect()
            try:
                found = await leftover.scalar(
                    text("SELECT label FROM session_probes WHERE label = :label"),
                    {"label": "st3-row"},
                )
                assert found is None
            finally:
                await leftover.close()
    finally:
        await _drop_probe_table(setup_engine, probe_table)
        await setup_engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()
        clear_engine()


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
