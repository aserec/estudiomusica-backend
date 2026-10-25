"""Curso. El contenido (secciones/actividades) llega en #402 y siguientes."""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import PersistentModel


class CourseLevel(StrEnum):
    """Nivel del curso. Enums en snake_case (convención #91)."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class DefaultGroupMode(StrEnum):
    """Modo de grupo por defecto. Enums en snake_case (convención #91)."""

    NONE = "none"
    SEPARATE = "separate"
    VISIBLE = "visible"


class Course(PersistentModel):
    """Ficha de un curso. Sin secciones ni actividades en este ticket.

    ``short_name`` es único entre vigentes (índice parcial; el borrado
    lógico libera el nombre). ``category_id`` apunta a ``categories``.
    """

    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint(
            "level IN ('beginner', 'intermediate', 'advanced')",
            name="level",
        ),
        CheckConstraint(
            "default_group_mode IN ('none', 'separate', 'visible')",
            name="default_group_mode",
        ),
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="end_not_before_start",
        ),
        Index(
            "ix_courses_active_name",
            "name",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_courses_short_name_active",
            "short_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    short_name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    visible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    level: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'beginner'"),
    )
    default_group_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'none'"),
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
