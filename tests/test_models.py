import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import String, Table, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models import PersistentModel


class MixinProbe(PersistentModel):
    """Tabla solo de test. No es un modelo de negocio y no va en Alembic."""

    __tablename__ = "mixin_probes"

    label: Mapped[str] = mapped_column(String(64), nullable=False)


def _column_names(sync_conn: Connection) -> list[str]:
    return [col["name"] for col in inspect(sync_conn).get_columns("mixin_probes")]


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


def test_suite_does_not_create_dot_data_store() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / ".data").exists()
