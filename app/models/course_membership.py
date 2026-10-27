"""Membresía de un usuario en un curso (rol de curso). Sin CRUD de matrícula."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import PersistentModel


class CourseMembership(PersistentModel):
    """Inscripción mínima para resolver permisos de curso.

    No hay FK a ``courses``: esa tabla llega en el CRUD de cursos. El ticket
    de matrículas (#403) será quien asigne el rol; este modelo solo lo lee.
    """

    __tablename__ = "course_memberships"
    __table_args__ = (
        CheckConstraint(
            "course_role IN ('student', 'assistant', 'teacher')",
            name="course_role",
        ),
        Index(
            "uq_course_memberships_user_course_active",
            "user_id",
            "course_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True,
    )
    course_role: Mapped[str] = mapped_column(String(32), nullable=False)
