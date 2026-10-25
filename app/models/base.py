"""Base declarativa y mixin de convenciones de persistencia."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Table, Uuid, event, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

UPDATED_AT_FUNCTION = "set_updated_at"


def updated_at_trigger_name(table_name: str) -> str:
    return f"{table_name}_set_updated_at"


def updated_at_trigger_sql(table_name: str) -> str:
    """SQL para montar el trigger BEFORE UPDATE en una tabla."""

    return (
        f"CREATE TRIGGER {updated_at_trigger_name(table_name)} "
        f"BEFORE UPDATE ON {table_name} "
        f"FOR EACH ROW EXECUTE FUNCTION {UPDATED_AT_FUNCTION}()"
    )


def attach_updated_at_trigger(connection: Connection, table_name: str) -> None:
    """Ejecuta el CREATE TRIGGER (conexión síncrona: Alembic / after_create)."""

    connection.execute(text(updated_at_trigger_sql(table_name)))


def _after_create_updated_at_trigger(
    target: Table, connection: Connection, **_kwargs: object
) -> None:
    attach_updated_at_trigger(connection, target.name)


class Base(DeclarativeBase):
    """DeclarativeBase de SQLAlchemy 2. Sin tablas de negocio en este ticket."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class PersistentMixin:
    """Convención de columnas para entidades persistidas.

    - ``id``: UUID (texto UUID en JSON; tipo UUID en Postgres).
    - ``created_at`` / ``updated_at``: ``clock_timestamp()``, no ``now()``.
      ``now()`` es la hora de inicio de transacción y el SQL crudo no
      actualiza ``updated_at``. En producción el trigger lo montan las
      migraciones (``op_create_updated_at_trigger`` / hook de
      autogenerate). El evento ``after_create`` solo cubre ``create_all``
      en tests; no corre con ``alembic upgrade``.
    - ``deleted_at``: borrado lógico (NULL = vigente). Sin índice genérico:
      un B-tree sobre toda la columna no ayuda a listar vigentes. Quien
      filtre ``deleted_at IS NULL`` debe añadir un índice parcial.

    Las issues de negocio heredan ``PersistentModel``. No filtra solas las
    filas borradas: eso es responsabilidad de cada repositorio.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class PersistentModel(PersistentMixin, Base):
    """Clase abstracta lista para que los modelos de negocio la hereden."""

    __abstract__ = True

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("__abstract__", False):
            return
        table = getattr(cls, "__table__", None)
        if table is None:
            return
        # Solo ``metadata.create_all`` (tests). Alembic no dispara after_create:
        # el hook de autogenerate / ``op_create_updated_at_trigger`` monta el
        # mismo trigger en las revisiones.
        event.listen(table, "after_create", _after_create_updated_at_trigger)
