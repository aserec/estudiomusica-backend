"""Operaciones de Alembic reutilizables por las revisiones."""

from alembic import op

from app.models.base import updated_at_drop_trigger_sql, updated_at_trigger_sql


def op_create_updated_at_trigger(table_name: str, schema: str | None = None) -> None:
    """Monta ``BEFORE UPDATE ... set_updated_at()`` en ``table_name``.

    El evento ``after_create`` de ``PersistentModel`` solo corre con
    ``create_all`` (tests). En producción las tablas salen de las
    migraciones: autogenerate añade esta operación, o se llama a mano.
    0001 solo crea la función, no el trigger.
    """

    op.execute(updated_at_trigger_sql(table_name, schema))


def op_drop_updated_at_trigger(table_name: str, schema: str | None = None) -> None:
    op.execute(updated_at_drop_trigger_sql(table_name, schema))
