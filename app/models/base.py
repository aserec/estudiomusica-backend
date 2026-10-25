"""Base declarativa y mixin de convenciones de persistencia."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """DeclarativeBase de SQLAlchemy 2. Sin tablas de negocio en este ticket."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class PersistentMixin:
    """Convención de columnas para entidades persistidas.

    - ``id``: UUID (texto UUID en JSON; tipo UUID en Postgres).
    - ``created_at`` / ``updated_at``: ``clock_timestamp()``, no ``now()``.
      ``now()`` es la hora de inicio de transacción y el SQL crudo no
      actualiza ``updated_at``. Las tablas de negocio deben montar
      ``BEFORE UPDATE ... EXECUTE FUNCTION set_updated_at()``.
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
