"""Esquema inicial: función set_updated_at() con clock_timestamp().

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-25

No se instala pgcrypto: en Postgres >= 13 ``gen_random_uuid()`` es nativa.
Las tablas ``PersistentModel`` montan el trigger en las revisiones
(``op_create_updated_at_trigger`` / hook de autogenerate). ``after_create``
solo aplica a ``create_all`` en tests.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    try:
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
    except Exception as exc:
        raise RuntimeError(
            "No se pudo crear set_updated_at(). Si la función ya existe, "
            "0001 no se puede relanzar sobre una base a medio migrar: "
            "revisa alembic_version o elimina la función a mano."
        ) from exc


def downgrade() -> None:
    try:
        op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    except Exception as exc:
        raise RuntimeError(
            "No se pudo eliminar set_updated_at(). Hay triggers de negocio "
            "que dependen de ella: haz downgrade de esas revisiones "
            "antes de bajar a base."
        ) from exc
