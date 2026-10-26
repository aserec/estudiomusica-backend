"""Hook de autogenerate: trigger ``updated_at`` en tablas PersistentModel."""

from collections.abc import Iterable, MutableMapping
from typing import Literal

from alembic.operations import ops
from alembic.operations.ops import MigrationScript
from alembic.runtime.migration import MigrationContext

from app.models import Base, PersistentModel
from app.models.base import updated_at_trigger_sql


def persistent_model_tables() -> set[tuple[str | None, str]]:
    """``(schema, table)`` de las tablas concretas PersistentModel."""

    names: set[tuple[str | None, str]] = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if not issubclass(cls, PersistentModel):
            continue
        if cls.__dict__.get("__abstract__", False):
            continue
        table = getattr(cls, "__table__", None)
        if table is not None:
            names.add((table.schema, table.name))
    return names


def own_schema_names() -> set[str]:
    """Schemas de las tablas del metadata (más ``public``)."""

    names = {"public"}
    for table in Base.metadata.tables.values():
        if table.schema:
            names.add(table.schema)
    return names


_IncludeNameType = Literal[
    "schema",
    "table",
    "column",
    "index",
    "unique_constraint",
    "foreign_key_constraint",
    "check_constraint",
]
_IncludeParent = MutableMapping[
    Literal["schema_name", "table_name", "schema_qualified_table_name"],
    str | None,
]


def include_name(
    name: str | None,
    type_: _IncludeNameType,
    _parent_names: _IncludeParent,
) -> bool:
    """Limita la reflexión a los schemas propios cuando ``include_schemas``."""

    if type_ == "schema":
        # Alembic pasa ``None`` para el schema por defecto.
        return name is None or name in own_schema_names()
    return True


def process_revision_directives(
    context: MigrationContext,
    revision: str | Iterable[str | None] | Iterable[str],
    directives: list[MigrationScript],
) -> None:
    """Añade el trigger ``updated_at`` a cada ``CreateTable`` PersistentModel.

    Las revisiones vacías solo se descartan con ``--autogenerate``
    (``cmd_opts.autogenerate``). ``alembic check`` necesita el script.
    """

    if not directives:
        return
    script = directives[0]
    upgrade_ops = script.upgrade_ops
    if upgrade_ops is None:
        return
    persistent = persistent_model_tables()
    extra: list[ops.ExecuteSQLOp] = []
    for operation in upgrade_ops.ops:
        if not isinstance(operation, ops.CreateTableOp):
            continue
        key = (operation.schema, operation.table_name)
        if key in persistent or (None, operation.table_name) in persistent:
            extra.append(
                ops.ExecuteSQLOp(
                    updated_at_trigger_sql(
                        operation.table_name, schema=operation.schema
                    )
                )
            )
    upgrade_ops.ops.extend(extra)
    config = getattr(context, "config", None) if context is not None else None
    cmd_opts = getattr(config, "cmd_opts", None)
    if not getattr(cmd_opts, "autogenerate", False):
        return
    downgrade_ops = script.downgrade_ops
    upgrade_empty = upgrade_ops.is_empty()
    downgrade_empty = downgrade_ops is None or downgrade_ops.is_empty()
    if upgrade_empty and downgrade_empty:
        directives[:] = []
