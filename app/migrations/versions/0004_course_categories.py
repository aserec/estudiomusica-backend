"""Categorías jerárquicas de curso.

Revision ID: 0004_course_categories
Revises: 0003_system_roles
Create Date: 2026-10-25

Crea ``categories`` (árbol con ``parent_id``), la función
``public.category_name_key`` (Cf congelado, ``search_path`` fijo) y monta
``set_updated_at()`` con ``op_create_updated_at_trigger`` (0001 solo crea
la función; el SQL del trigger va quoted por el preparer). #97 añadirá
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

# Unicode 16/17 Cf salvo ZWJ, ZWNJ y tags de bandera (E0020-E007F).
# Literal congelado: una revisión nueva si cambia el conjunto.
_CF_STRIP_CHARS = (
    "\u00ad\u0600\u0601\u0602\u0603\u0604\u0605\u061c\u06dd\u070f"
    "\u0890\u0891\u08e2\u180e\u200b\u200e\u200f\u202a\u202b\u202c"
    "\u202d\u202e\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068"
    "\u2069\u206a\u206b\u206c\u206d\u206e\u206f\ufeff\ufff9\ufffa"
    "\ufffb\U000110bd\U000110cd\U00013430\U00013431\U00013432"
    "\U00013433\U00013434\U00013435\U00013436\U00013437\U00013438"
    "\U00013439\U0001343a\U0001343b\U0001343c\U0001343d\U0001343e"
    "\U0001343f\U0001bca0\U0001bca1\U0001bca2\U0001bca3\U0001d173"
    "\U0001d174\U0001d175\U0001d176\U0001d177\U0001d178\U0001d179"
    "\U0001d17a\U000e0001"
)


def _category_name_key_function_sql() -> str:
    return (
        "CREATE FUNCTION public.category_name_key(value public.citext) "
        "RETURNS public.citext "
        "LANGUAGE sql "
        "IMMUTABLE "
        "STRICT "
        "PARALLEL SAFE "
        "SET search_path TO pg_catalog, public "
        "AS $fn$ "
        "SELECT btrim(regexp_replace("
        f"translate(value::pg_catalog.text, $cf${_CF_STRIP_CHARS}$cf$, ''), "
        r"'\s+', ' ', 'g'"
        "))::public.citext "
        "$fn$"
    )


def upgrade() -> None:
    op.execute(sa.text(_category_name_key_function_sql()))
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
        sa.Column(
            "name_key",
            CITEXT(),
            sa.Computed("public.category_name_key(name)", persisted=True),
            nullable=False,
        ),
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
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS public.category_name_key(public.citext)")
    )
