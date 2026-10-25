"""Esquema inicial: función set_updated_at() con clock_timestamp().

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-25

No se instala pgcrypto: en Postgres >= 13 ``gen_random_uuid()`` es nativa.
``PersistentModel`` monta ``BEFORE UPDATE ... set_updated_at()`` al crear
cada tabla. Las migraciones de negocio pueden usar
``updated_at_trigger_sql`` / ``attach_updated_at_trigger``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION set_updated_at()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          NEW.updated_at = clock_timestamp();
          RETURN NEW;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
