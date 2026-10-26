"""Cursos y FK de course_memberships.

Revision ID: 0005_courses
Revises: 0004_course_categories
Create Date: 2026-10-25

Crea ``courses`` (ficha, ``category_id`` FK a ``categories``, enums
por check, ``short_name`` único entre vigentes) y monta
``set_updated_at()`` con ``op_create_updated_at_trigger``. Añade la FK
``course_memberships.course_id`` → ``courses.id`` que #94 dejó pendiente.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

from app.migrations.ops import op_create_updated_at_trigger, op_drop_updated_at_trigger

revision: str = "0005_courses"
down_revision: str | None = "0004_course_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("short_name", CITEXT(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=2000), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column(
            "visible",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "level",
            sa.String(length=32),
            server_default=sa.text("'beginner'"),
            nullable=False,
        ),
        sa.Column(
            "default_group_mode",
            sa.String(length=32),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "level IN ('beginner', 'intermediate', 'advanced')",
            name="level",
        ),
        sa.CheckConstraint(
            "default_group_mode IN ('none', 'separate', 'visible')",
            name="default_group_mode",
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="end_not_before_start",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_courses_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_courses"),
    )
    op.create_index(
        "ix_courses_category_id",
        "courses",
        ["category_id"],
        unique=False,
    )
    op.create_index(
        "ix_courses_active_name",
        "courses",
        ["name", "id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_courses_short_name_active",
        "courses",
        ["short_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_foreign_key(
        "fk_course_memberships_course_id_courses",
        "course_memberships",
        "courses",
        ["course_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op_create_updated_at_trigger("courses")


def downgrade() -> None:
    op_drop_updated_at_trigger("courses")
    op.drop_constraint(
        "fk_course_memberships_course_id_courses",
        "course_memberships",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_courses_short_name_active",
        table_name="courses",
        if_exists=True,
    )
    op.drop_index(
        "ix_courses_active_name",
        table_name="courses",
        if_exists=True,
    )
    op.drop_index(
        "ix_courses_category_id",
        table_name="courses",
        if_exists=True,
    )
    op.drop_table("courses")
