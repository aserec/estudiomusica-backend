"""Operaciones de Alembic reutilizables por las revisiones."""

from alembic import op

from app.models.base import updated_at_trigger_name, updated_at_trigger_sql


def op_create_updated_at_trigger(table_name: str) -> None:
    """Monta ``BEFORE UPDATE ... set_updated_at()`` en ``table_name``.

    El evento ``after_create`` de ``PersistentModel`` solo corre con
    ``create_all`` (tests). En producción las tablas salen de las
    migraciones: autogenerate añade esta operación, o se llama a mano.
    """

    op.execute(updated_at_trigger_sql(table_name))


def op_drop_updated_at_trigger(table_name: str) -> None:
    op.execute(
        f"DROP TRIGGER IF EXISTS {updated_at_trigger_name(table_name)} ON {table_name}"
    )
