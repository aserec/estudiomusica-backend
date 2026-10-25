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
    - ``created_at`` / ``updated_at``: timestamps con zona horaria.
    - ``deleted_at``: borrado lógico (NULL = vigente).

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
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )


class PersistentModel(PersistentMixin, Base):
    """Clase abstracta lista para que los modelos de negocio la hereden."""

    __abstract__ = True
