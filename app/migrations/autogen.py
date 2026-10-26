"""Hook de autogenerate: trigger ``updated_at`` en tablas PersistentModel."""

from alembic.operations import ops
from alembic.operations.ops import MigrationScript
from alembic.runtime.migration import MigrationContext
from alembic.script.revision import _GetRevArg

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
    context: MigrationContext,
    revision: _GetRevArg,
    directives: list[MigrationScript],
) -> None:
    """Añade el trigger ``updated_at`` a cada ``CreateTable`` PersistentModel."""

    if not directives:
        return
    script = directives[0]
    upgrade_ops = script.upgrade_ops
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
    config = getattr(context, "config", None)
    cmd_opts = getattr(config, "cmd_opts", None) if config is not None else None
    # Solo `alembic revision --autogenerate`: no escribir un fichero pass.
    # `alembic check` reutiliza este hook y necesita el directive vacío
    # (si se borra, IndexError / exit 1). Una revisión vacía sería lineal,
    # no un segundo head.
    if upgrade_ops.is_empty() and bool(getattr(cmd_opts, "autogenerate", False)):
        directives[:] = []
