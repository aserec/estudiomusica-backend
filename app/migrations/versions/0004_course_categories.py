"""Categorías jerárquicas de curso.

Revision ID: 0004_course_categories
Revises: 0003_system_roles
Create Date: 2026-10-25

Crea ``categories`` (árbol con ``parent_id``) y ``name_key`` como
columna CITEXT normal (la clave se calcula en Python: Cf de relleno
fuera, ZWJ/ZWNJ solo en contexto). Monta ``set_updated_at()`` con
``op_create_updated_at_trigger`` (0001 solo crea la función; el SQL
del trigger va quoted por el preparer). #97 añadirá
``courses.category_id`` FK a esta tabla.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

from app.migrations.ops import op_create_updated_at_trigger, op_drop_updated_at_trigger

revision: str = "0004_course_categories"
down_revision: str | None = "0003_system_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
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
        sa.Column("name", CITEXT(), nullable=False),
        sa.Column("name_key", CITEXT(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="parent_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["categories.id"],
            name="fk_categories_parent_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
    )
    op.create_index(
        "ix_categories_parent_id",
        "categories",
        ["parent_id"],
        unique=False,
    )
    op.create_index(
        "ix_categories_active_sort",
        "categories",
        ["sort_order", "name"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_categories_root_name_active",
        "categories",
        ["name_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND parent_id IS NULL"),
    )
    op.create_index(
        "uq_categories_parent_id_name_key",
        "categories",
        ["parent_id", "name_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND parent_id IS NOT NULL"),
    )
    op_create_updated_at_trigger("categories")


def downgrade() -> None:
    op_drop_updated_at_trigger("categories")
    op.drop_index(
        "uq_categories_parent_id_name_key",
        table_name="categories",
        if_exists=True,
    )
    op.drop_index(
        "uq_categories_root_name_active",
        table_name="categories",
        if_exists=True,
    )
    op.drop_index(
        "ix_categories_active_sort",
        table_name="categories",
        if_exists=True,
    )
    op.drop_index(
        "ix_categories_parent_id",
        table_name="categories",
        if_exists=True,
    )
    op.drop_table("categories")
    # Las 0004 de revisión 8924e0a0 y 510acf77 creaban
    # public.category_name_key. 78b08eaf no: no tenía función ni name_key.
    # El downgrade de HEAD la quita para que
    # `downgrade 0003 && upgrade head` deje la BD limpia.
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS public.category_name_key(public.citext)")
    )
