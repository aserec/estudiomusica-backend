"""Categoría jerárquica de cursos. ``courses.category_id`` apunta aquí."""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import PersistentModel


class Category(PersistentModel):
    """Nodo del árbol de categorías (un solo nivel: padre = raíz).

    ``courses.category_id`` apunta a ``categories.id``. Borrar o mover
    (cambiar ``parent_id``) responde ``409 category_not_empty`` si hay
    cursos vigentes.
    """

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="parent_not_self",
        ),
        Index(
            "ix_categories_active_sort",
            "sort_order",
            "name",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_categories_root_name_active",
            "name_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND parent_id IS NULL"),
        ),
        Index(
            "uq_categories_parent_id_name_key",
            "parent_id",
            "name_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND parent_id IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name_key: Mapped[str] = mapped_column(
        CITEXT(),
        Computed("category_name_key(name)", persisted=True),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
