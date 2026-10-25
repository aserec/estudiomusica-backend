"""Hook de autogenerate: trigger ``updated_at`` en tablas PersistentModel."""

from alembic.operations import ops

from app.models import Base, PersistentModel
from app.models.base import updated_at_trigger_sql


def persistent_model_table_names() -> set[str]:
    """Tablas concretas que heredan ``PersistentModel`` (no abstractas)."""

    names: set[str] = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if not issubclass(cls, PersistentModel):
            continue
        if cls.__dict__.get("__abstract__", False):
            continue
        table = getattr(cls, "__table__", None)
        if table is not None:
            names.add(table.name)
    return names


def process_revision_directives(
    context: object, revision: object, directives: list[object]
) -> None:
    """Añade el trigger ``updated_at`` a cada ``CreateTable`` PersistentModel."""

    if not directives:
        return
    script = directives[0]
    upgrade_ops = getattr(script, "upgrade_ops", None)
    if upgrade_ops is None:
        return
    persistent = persistent_model_table_names()
    extra: list[ops.ExecuteSQLOp] = []
    for operation in upgrade_ops.ops:
        if (
            isinstance(operation, ops.CreateTableOp)
            and operation.table_name in persistent
        ):
            extra.append(ops.ExecuteSQLOp(updated_at_trigger_sql(operation.table_name)))
    upgrade_ops.ops.extend(extra)
