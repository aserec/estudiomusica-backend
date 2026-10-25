import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import String, Table, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.db import RedactedDsn
from app.models import PersistentModel, updated_at_trigger_name


class MixinProbe(PersistentModel):
    """Tabla solo de test. No es un modelo de negocio y no va en Alembic."""

    __tablename__ = "mixin_probes"

    label: Mapped[str] = mapped_column(String(64), nullable=False)


def _column_names(sync_conn: Connection) -> list[str]:
    return [col["name"] for col in inspect(sync_conn).get_columns("mixin_probes")]


def _create_probe_table(sync_conn: Connection) -> None:
    probe_table = MixinProbe.__table__
    assert isinstance(probe_table, Table)
    probe_table.create(sync_conn, checkfirst=True)


@pytest.mark.db
async def test_persistent_mixin_columns_and_defaults(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    probe_table = MixinProbe.__table__
    assert isinstance(probe_table, Table)
    await connection.run_sync(probe_table.create)

    probe = MixinProbe(label="alpha")
    db_session.add(probe)
    await db_session.flush()
    await db_session.refresh(probe)

    assert isinstance(probe.id, uuid.UUID)
    assert probe.label == "alpha"
    assert probe.deleted_at is None
    assert probe.created_at.tzinfo is not None
    assert probe.updated_at.tzinfo is not None

    inspector_columns = await connection.run_sync(_column_names)
    assert set(inspector_columns) == {
        "id",
        "created_at",
        "updated_at",
        "deleted_at",
        "label",
    }

    found = await db_session.scalar(select(MixinProbe).where(MixinProbe.id == probe.id))
    assert found is not None
    assert found.label == "alpha"


@pytest.mark.db
async def test_persistent_mixin_soft_delete(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    probe_table = MixinProbe.__table__
    assert isinstance(probe_table, Table)
    await connection.run_sync(probe_table.create)

    probe = MixinProbe(label="beta")
    db_session.add(probe)
    await db_session.flush()

    probe.deleted_at = datetime.now(UTC)
    await db_session.flush()
    await db_session.refresh(probe)

    assert probe.deleted_at is not None


@pytest.mark.db
async def test_persistent_model_table_has_updated_at_trigger(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    probe_table = MixinProbe.__table__
    assert isinstance(probe_table, Table)
    await connection.run_sync(probe_table.create)

    trigger = await db_session.scalar(
        text(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'mixin_probes'::regclass "
            "AND tgname = :name"
        ),
        {"name": updated_at_trigger_name("mixin_probes")},
    )
    assert trigger == updated_at_trigger_name("mixin_probes")


@pytest.mark.db
async def test_updated_at_advances_twice_in_the_same_transaction(
    db_session: AsyncSession,
) -> None:
    """now() no avanzaría; clock_timestamp() sí."""

    connection = await db_session.connection()
    probe_table = MixinProbe.__table__
    assert isinstance(probe_table, Table)
    await connection.run_sync(probe_table.create)

    probe = MixinProbe(label="a")
    db_session.add(probe)
    await db_session.flush()
    await db_session.refresh(probe)
    first = probe.updated_at

    await db_session.execute(
        text("UPDATE mixin_probes SET label = :label WHERE id = :id"),
        {"label": "b", "id": probe.id},
    )
    await db_session.refresh(probe)
    second = probe.updated_at

    await db_session.execute(
        text("UPDATE mixin_probes SET label = :label WHERE id = :id"),
        {"label": "c", "id": probe.id},
    )
    await db_session.refresh(probe)
    third = probe.updated_at

    assert second > first
    assert third > second
    assert probe.label == "c"


@pytest.mark.db
async def test_updated_at_advances_across_separate_commits(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
    migrated_test_database: None,
) -> None:
    """El trigger usa clock_timestamp(); now() no avanzaría en la misma transacción."""

    from app.config import get_settings
    from app.db import dispose_engine

    monkeypatch.setenv("DATABASE_URL", require_postgres.secret())
    get_settings.cache_clear()
    engine = create_async_engine(require_postgres.secret(), pool_pre_ping=True)
    probe_table = MixinProbe.__table__
    assert isinstance(probe_table, Table)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_create_probe_table)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            probe = MixinProbe(label="first")
            session.add(probe)
            await session.commit()
            await session.refresh(probe)
            first_updated = probe.updated_at
            probe_id = probe.id

        await asyncio.sleep(0.02)

        async with factory() as session:
            await session.execute(
                text("UPDATE mixin_probes SET label = :label WHERE id = :id"),
                {"label": "second", "id": probe_id},
            )
            await session.commit()

        async with factory() as session:
            found = await session.get(MixinProbe, probe_id)
            assert found is not None
            assert found.label == "second"
            assert found.updated_at > first_updated
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: probe_table.drop(sync_conn, checkfirst=True)
            )
        await engine.dispose()
        await dispose_engine()
        get_settings.cache_clear()


def test_suite_does_not_create_dot_data_store() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / ".data").exists()
