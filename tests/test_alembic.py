import ast
import asyncio
import importlib
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import ops
from alembic.script import ScriptDirectory
from sqlalchemy import String, Table, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import Mapped, mapped_column

from app.db import RedactedDsn
from app.migrations.autogen import process_revision_directives
from app.models import PersistentModel
from app.models.base import updated_at_trigger_name
from tests.conftest import (
    ROOT,
    _create_async_engine,
    alembic_config,
    drop_database_if_exists,
    ensure_database_exists,
    resolve_alembic_database_url,
)

pytestmark = pytest.mark.db

_ALEMBIC_TEST_APP_NAME = f"estudiomusica-alembic-{os.getpid()}"
_logger = logging.getLogger(__name__)


def _own_alembic_engine(url: str) -> AsyncEngine:
    return _create_async_engine(
        url,
        connect_args={"server_settings": {"application_name": _ALEMBIC_TEST_APP_NAME}},
    )


async def _log_and_terminate_own_backends(connection: AsyncConnection) -> None:
    result = await connection.execute(
        text(
            "SELECT pid, usename, application_name, state, "
            "wait_event_type, wait_event, left(query, 120) AS query "
            "FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "ORDER BY pid"
        )
    )
    for row in result.mappings():
        _logger.info(
            "alembic test backend before terminate: pid=%s usename=%s "
            "application_name=%s state=%s wait_event=%s/%s query=%s",
            row["pid"],
            row["usename"],
            row["application_name"],
            row["state"],
            row["wait_event_type"],
            row["wait_event"],
            row["query"],
        )
    await connection.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() "
            "AND usename = current_user "
            "AND application_name = :app_name "
            "AND pid <> pg_backend_pid()"
        ),
        {"app_name": _ALEMBIC_TEST_APP_NAME},
    )


def _scalar(dsn: RedactedDsn, sql: str) -> object:
    async def run() -> object:
        engine = _create_async_engine(dsn.secret())
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text(sql))
                return result.scalar()
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_alembic_upgrade_head_and_downgrade_base(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
) -> None:
    dsn = RedactedDsn(resolve_alembic_database_url())
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    config = alembic_config(dsn)

    command.upgrade(config, "head")
    assert _scalar(dsn, "SELECT version_num FROM alembic_version") == "0001_initial"
    assert (
        _scalar(dsn, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
        == "set_updated_at"
    )
    assert _scalar(dsn, "SELECT gen_random_uuid() IS NOT NULL") is True

    command.downgrade(config, "base")
    assert _scalar(dsn, "SELECT version_num FROM alembic_version") is None
    assert (
        _scalar(dsn, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
        is None
    )

    command.upgrade(config, "head")
    assert _scalar(dsn, "SELECT version_num FROM alembic_version") == "0001_initial"


def test_alembic_current_accepts_sslmode(
    require_postgres: RedactedDsn,
) -> None:
    """B-N3: ``sqlalchemy.url`` con ``?sslmode=`` no lanza TypeError."""

    from sqlalchemy.engine.url import make_url

    parsed = make_url(require_postgres.secret())
    query = dict(parsed.query or {})
    for key in list(query):
        if str(key).lower() in {"ssl", "sslmode"}:
            del query[key]
    query["sslmode"] = "prefer"
    with_mode = RedactedDsn(
        parsed.set(query=query).render_as_string(hide_password=False)
    )
    command.current(alembic_config(with_mode))


def test_alembic_has_a_single_initial_revision() -> None:
    config = alembic_config(resolve_alembic_database_url())
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0001_initial"]
    assert script.get_revision("0001_initial").down_revision is None


class MigratedTriggerProbe(PersistentModel):
    """Tabla solo de test: el hook de autogenerate debe verla."""

    __tablename__ = "migrated_trigger_probes"

    label: Mapped[str] = mapped_column(String(32), nullable=False)


def test_autogenerate_hook_adds_updated_at_trigger() -> None:
    class _Script:
        def __init__(self) -> None:
            self.upgrade_ops = ops.UpgradeOps(
                [
                    ops.CreateTableOp("migrated_trigger_probes", []),
                    ops.CreateTableOp("unrelated_table", []),
                ]
            )

    script = _Script()
    process_revision_directives(None, None, [script])  # type: ignore[arg-type, list-item]
    sql_ops = [
        operation
        for operation in script.upgrade_ops.ops
        if isinstance(operation, ops.ExecuteSQLOp)
    ]
    assert len(sql_ops) == 1
    sql = str(sql_ops[0].sqltext)
    assert updated_at_trigger_name("migrated_trigger_probes") in sql
    assert '"migrated_trigger_probes"' in sql
    assert "unrelated_table" not in sql


def _configure_keywords(fn_name: str) -> set[str]:
    tree = ast.parse((ROOT / "app" / "migrations" / "env.py").read_text())
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != fn_name:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr == "configure":
                return {kw.arg for kw in child.keywords if kw.arg}
    raise AssertionError(f"no se encontró context.configure en {fn_name}")


def test_env_wires_autogen_hook_offline_and_online() -> None:
    """K2 (sin enganche) y K3 (solo offline) deben morir."""

    offline = _configure_keywords("run_migrations_offline")
    online = _configure_keywords("do_run_migrations")
    assert "process_revision_directives" in offline
    assert "process_revision_directives" in online
    assert "include_schemas" in offline
    assert "include_schemas" in online


_PROBE_REVISION = '''"""Probe PersistentModel created via Alembic, not create_all.

Revision ID: 0002_migrated_trigger
Revises: 0001_initial
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations.ops import op_create_updated_at_trigger, op_drop_updated_at_trigger

revision: str = "0002_migrated_trigger"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "migrated_trigger_probes",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
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
        sa.Column("label", sa.String(32), nullable=False),
    )
    op_create_updated_at_trigger("migrated_trigger_probes")


def downgrade() -> None:
    op_drop_updated_at_trigger("migrated_trigger_probes")
    op.drop_table("migrated_trigger_probes")
'''


def _alembic_config_for(script_location: Path, dsn: RedactedDsn) -> Config:
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", dsn.secret().replace("%", "%%"))
    return config


def test_migrated_database_has_updated_at_trigger_without_create_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    require_postgres: RedactedDsn,
) -> None:
    dest = tmp_path / "migrations"
    shutil.copytree(ROOT / "app" / "migrations", dest)
    (dest / "versions" / "0002_migrated_trigger_probe.py").write_text(_PROBE_REVISION)

    dsn = RedactedDsn(resolve_alembic_database_url())
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    config = _alembic_config_for(dest, dsn)
    command.upgrade(config, "head")
    try:
        trigger = _scalar(
            dsn,
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'migrated_trigger_probes'::regclass "
            f"AND tgname = '{updated_at_trigger_name('migrated_trigger_probes')}'",
        )
        assert trigger == updated_at_trigger_name("migrated_trigger_probes")

        tables_without_trigger = _scalar(
            dsn,
            """
            SELECT COUNT(*) FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_name = 'updated_at'
              AND NOT EXISTS (
                SELECT 1 FROM pg_trigger t
                JOIN pg_class rel ON rel.oid = t.tgrelid
                WHERE rel.relname = c.table_name
                  AND t.tgname = c.table_name || '_set_updated_at'
                  AND NOT t.tgisinternal
              )
            """,
        )
        assert tables_without_trigger == 0

        async def advance() -> None:
            engine = _create_async_engine(dsn.secret())
            try:
                async with engine.begin() as connection:
                    inserted = await connection.execute(
                        text(
                            "INSERT INTO migrated_trigger_probes (label) "
                            "VALUES ('first') RETURNING id, updated_at"
                        )
                    )
                    row = inserted.one()
                    first = row.updated_at
                    await connection.execute(
                        text(
                            "UPDATE migrated_trigger_probes SET label = 'second' "
                            "WHERE id = :id"
                        ),
                        {"id": row.id},
                    )
                    second = await connection.scalar(
                        text(
                            "SELECT updated_at FROM migrated_trigger_probes "
                            "WHERE id = :id"
                        ),
                        {"id": row.id},
                    )
                    assert second is not None
                    assert second > first
            finally:
                await engine.dispose()

        asyncio.run(advance())
    finally:
        command.downgrade(config, "base")


_AUTOGEN_ENV = """\
import asyncio

from alembic import context
from sqlalchemy import Column, Integer, MetaData, String, Table, pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import asyncpg_url_and_connect_args

config = context.config
target_metadata = MetaData()
Table(
    "autogen_sa_probe",
    target_metadata,
    Column("id", Integer, primary_key=True),
    Column("label", String(32), nullable=False),
)


def get_database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or ""


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url, connect_args = asyncpg_url_and_connect_args(get_database_url())
    connectable = create_async_engine(
        url,
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
"""


def test_autogenerate_revision_imports_sqlalchemy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    require_postgres: RedactedDsn,
) -> None:
    from sqlalchemy.engine.url import make_url

    dest = tmp_path / "migrations"
    dest.mkdir()
    (dest / "versions").mkdir()
    shutil.copy(ROOT / "app" / "migrations" / "script.py.mako", dest / "script.py.mako")
    (dest / "env.py").write_text(_AUTOGEN_ENV)

    parsed = make_url(resolve_alembic_database_url())
    dsn = RedactedDsn(
        parsed.set(database="autogen_sa_probe_test").render_as_string(
            hide_password=False
        )
    )
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    config = _alembic_config_for(dest, dsn)
    try:
        command.revision(config, message="sa_probe", autogenerate=True)

        generated = list((dest / "versions").glob("*.py"))
        assert generated
        source = generated[0].read_text()
        assert "import sqlalchemy as sa" in source
        assert "sa." in source

        spec = importlib.util.spec_from_file_location("sa_probe_rev", generated[0])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module.upgrade()
        except NameError:
            raise AssertionError(
                "la revisión autogenerada no importa sqlalchemy as sa"
            ) from None
        except Exception:
            pass
        assert callable(module.upgrade)
        assert callable(module.downgrade)
    finally:
        drop_database_if_exists(dsn)


def test_0001_upgrade_duplicate_function_has_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = importlib.import_module("app.migrations.versions.0001_initial")

    def boom(_sql: object) -> None:
        raise RuntimeError("function set_updated_at() already exists")

    monkeypatch.setattr(mod.op, "execute", boom)
    with pytest.raises(RuntimeError, match="medio migrar") as caught:
        mod.upgrade()
    assert "set_updated_at" in str(caught.value)


def test_0001_downgrade_dependent_trigger_has_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = importlib.import_module("app.migrations.versions.0001_initial")

    def boom(_sql: object) -> None:
        raise RuntimeError(
            "cannot drop function set_updated_at because other objects depend on it"
        )

    monkeypatch.setattr(mod.op, "execute", boom)
    with pytest.raises(RuntimeError, match="triggers de negocio") as caught:
        mod.downgrade()
    assert "set_updated_at" in str(caught.value)


def test_0001_upgrade_after_stamp_base_is_clear(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
) -> None:
    dsn = RedactedDsn(resolve_alembic_database_url())
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    config = alembic_config(dsn)
    command.upgrade(config, "head")
    command.stamp(config, "base")
    try:
        with pytest.raises(Exception, match="medio migrar"):
            command.upgrade(config, "head")
    finally:

        async def cleanup() -> None:
            engine = _create_async_engine(dsn.secret())
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")
                    )
            finally:
                await engine.dispose()

        asyncio.run(cleanup())
        command.stamp(config, "base")


def test_0001_downgrade_with_business_trigger_is_clear(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
) -> None:
    from app.models.base import updated_at_trigger_sql

    dsn = RedactedDsn(resolve_alembic_database_url())
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    config = alembic_config(dsn)
    command.upgrade(config, "head")

    async def add_dependent_trigger() -> None:
        engine = _own_alembic_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout = '8s'"))
                await _log_and_terminate_own_backends(connection)
                await connection.execute(text("DROP TABLE IF EXISTS dep_trig"))
                await connection.execute(
                    text("CREATE TABLE dep_trig (updated_at timestamptz)")
                )
                await connection.execute(text(updated_at_trigger_sql("dep_trig")))
        finally:
            await engine.dispose()

    asyncio.run(add_dependent_trigger())
    try:
        with pytest.raises(Exception, match="triggers de negocio"):
            command.downgrade(config, "base")
    finally:

        async def cleanup() -> None:
            engine = _own_alembic_engine(dsn.secret())
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("SET LOCAL lock_timeout = '8s'"))
                    await _log_and_terminate_own_backends(connection)
                    await connection.execute(text("DROP TABLE IF EXISTS dep_trig"))
                    await connection.execute(
                        text("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")
                    )
            finally:
                await engine.dispose()

        asyncio.run(cleanup())
        command.stamp(config, "base")


_TABLE_DDL = """
CREATE TABLE {qualified} (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deleted_at timestamptz,
    label varchar(32) NOT NULL
)
"""


def test_updated_at_trigger_quotes_uppercase_reserved_and_schema(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
) -> None:
    from app.models.base import (
        attach_updated_at_trigger,
        qualify_table,
        quote_ident,
        updated_at_drop_trigger_sql,
        updated_at_trigger_name,
        updated_at_trigger_sql,
    )

    dsn = RedactedDsn(resolve_alembic_database_url())
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    command.upgrade(alembic_config(dsn), "head")

    cases = (
        ("ListaCanciones", None),
        ("order", None),
        ("piezas", "catalogo"),
    )

    async def run() -> None:
        engine = _create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS catalogo"))
                for table_name, schema in cases:
                    qualified = qualify_table(table_name, schema)
                    await connection.execute(text(f"DROP TABLE IF EXISTS {qualified}"))
                    await connection.execute(
                        text(_TABLE_DDL.format(qualified=qualified))
                    )

                    def _attach(
                        sync_conn: object,
                        name: str = table_name,
                        sch: str | None = schema,
                    ) -> None:
                        attach_updated_at_trigger(sync_conn, name, schema=sch)  # type: ignore[arg-type]

                    await connection.run_sync(_attach)
                    sql = updated_at_trigger_sql(table_name, schema)
                    assert quote_ident(table_name) in sql
                    if schema:
                        assert quote_ident(schema) in sql
                    trigger = await connection.scalar(
                        text(
                            "SELECT tgname FROM pg_trigger t "
                            "JOIN pg_class c ON c.oid = t.tgrelid "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE c.relname = :table AND n.nspname = :schema "
                            "AND t.tgname = :tg"
                        ),
                        {
                            "table": table_name,
                            "schema": schema or "public",
                            "tg": updated_at_trigger_name(table_name),
                        },
                    )
                    assert trigger == updated_at_trigger_name(table_name)

                    inserted = await connection.execute(
                        text(
                            f"INSERT INTO {qualified} (label) "
                            "VALUES ('first') RETURNING id, updated_at"
                        )
                    )
                    row = inserted.one()
                    await connection.execute(
                        text(f"UPDATE {qualified} SET label = 'second' WHERE id = :id"),
                        {"id": row.id},
                    )
                    second = await connection.scalar(
                        text(f"SELECT updated_at FROM {qualified} WHERE id = :id"),
                        {"id": row.id},
                    )
                    assert second is not None
                    assert second > row.updated_at

                    await connection.execute(
                        text(updated_at_drop_trigger_sql(table_name, schema))
                    )
                    leftover = await connection.scalar(
                        text(
                            "SELECT tgname FROM pg_trigger t "
                            "JOIN pg_class c ON c.oid = t.tgrelid "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE c.relname = :table AND n.nspname = :schema "
                            "AND t.tgname = :tg AND NOT t.tgisinternal"
                        ),
                        {
                            "table": table_name,
                            "schema": schema or "public",
                            "tg": updated_at_trigger_name(table_name),
                        },
                    )
                    assert leftover is None
                    await connection.execute(text(f"DROP TABLE {qualified}"))
                await connection.execute(text("DROP SCHEMA IF EXISTS catalogo CASCADE"))
        finally:
            await engine.dispose()

    asyncio.run(run())


class SchemaTriggerProbe(PersistentModel):
    """Tabla con schema para create_all / ops / autogen."""

    __tablename__ = "schema_trigger_probes"
    __table_args__ = {"schema": "catalogo"}

    label: Mapped[str] = mapped_column(String(32), nullable=False)


def test_autogenerate_hook_quotes_schema_on_create_table() -> None:
    class _Script:
        def __init__(self) -> None:
            self.upgrade_ops = ops.UpgradeOps(
                [ops.CreateTableOp("schema_trigger_probes", [], schema="catalogo")]
            )

    script = _Script()
    process_revision_directives(None, None, [script])  # type: ignore[arg-type, list-item]
    sql_ops = [
        operation
        for operation in script.upgrade_ops.ops
        if isinstance(operation, ops.ExecuteSQLOp)
    ]
    assert len(sql_ops) == 1
    sql = str(sql_ops[0].sqltext)
    assert '"catalogo"' in sql
    assert '"schema_trigger_probes"' in sql


def _schema_trigger_lookup_sql(*, leftover: bool = False) -> str:
    from app.models.base import updated_at_trigger_name

    name = updated_at_trigger_name("schema_trigger_probes")
    extra = " AND NOT t.tgisinternal" if leftover else ""
    return (
        "SELECT tgname FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = 'schema_trigger_probes' "
        f"AND n.nspname = 'catalogo' AND t.tgname = '{name}'{extra}"
    )


def test_create_all_and_ops_mount_schema_trigger(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: RedactedDsn,
) -> None:
    """K4/K6/K7: create_all y ops montan y borran el trigger con schema."""

    from app.migrations.ops import (
        op_create_updated_at_trigger,
        op_drop_updated_at_trigger,
    )
    from app.models.base import updated_at_trigger_name

    dsn = RedactedDsn(resolve_alembic_database_url())
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    command.upgrade(alembic_config(dsn), "head")

    async def run() -> None:
        engine = _create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS catalogo"))
                await connection.execute(
                    text('DROP TABLE IF EXISTS "catalogo"."schema_trigger_probes"')
                )
                probe_table = SchemaTriggerProbe.__table__
                assert isinstance(probe_table, Table)
                await connection.run_sync(
                    lambda sync_conn: probe_table.create(sync_conn)
                )
                trigger = await connection.scalar(text(_schema_trigger_lookup_sql()))
                assert trigger == updated_at_trigger_name("schema_trigger_probes")
                inserted = await connection.execute(
                    text(
                        'INSERT INTO "catalogo"."schema_trigger_probes" (label) '
                        "VALUES ('first') RETURNING id, updated_at"
                    )
                )
                row = inserted.one()
                await connection.execute(
                    text(
                        'UPDATE "catalogo"."schema_trigger_probes" '
                        "SET label = 'second' WHERE id = :id"
                    ),
                    {"id": row.id},
                )
                second = await connection.scalar(
                    text(
                        'SELECT updated_at FROM "catalogo"."schema_trigger_probes" '
                        "WHERE id = :id"
                    ),
                    {"id": row.id},
                )
                assert second is not None
                assert second > row.updated_at
                await connection.execute(
                    text('DROP TABLE "catalogo"."schema_trigger_probes"')
                )
                qualified = '"catalogo"."schema_trigger_probes"'
                await connection.execute(text(_TABLE_DDL.format(qualified=qualified)))

                def _ops(sync_conn: object) -> None:
                    from alembic.migration import MigrationContext
                    from alembic.operations import Operations

                    ctx = MigrationContext.configure(sync_conn)  # type: ignore[arg-type]
                    with Operations.context(ctx):
                        op_create_updated_at_trigger(
                            "schema_trigger_probes", schema="catalogo"
                        )

                await connection.run_sync(_ops)
                trigger = await connection.scalar(text(_schema_trigger_lookup_sql()))
                assert trigger == updated_at_trigger_name("schema_trigger_probes")

                def _drop(sync_conn: object) -> None:
                    from alembic.migration import MigrationContext
                    from alembic.operations import Operations

                    ctx = MigrationContext.configure(sync_conn)  # type: ignore[arg-type]
                    with Operations.context(ctx):
                        op_drop_updated_at_trigger(
                            "schema_trigger_probes", schema="catalogo"
                        )

                await connection.run_sync(_drop)
                leftover = await connection.scalar(
                    text(_schema_trigger_lookup_sql(leftover=True))
                )
                assert leftover is None
                await connection.execute(
                    text('DROP TABLE "catalogo"."schema_trigger_probes"')
                )
                await connection.execute(text("DROP SCHEMA IF EXISTS catalogo CASCADE"))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_autogenerate_from_env_adds_trigger_not_lambda(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    require_postgres: RedactedDsn,
) -> None:
    """K2b: el hook real añade el trigger; un lambda *a: None no lo haría."""

    from sqlalchemy.engine.url import make_url

    dest = tmp_path / "migrations"
    shutil.copytree(ROOT / "app" / "migrations", dest)
    parsed = make_url(resolve_alembic_database_url())
    dsn = RedactedDsn(
        parsed.set(database="autogen_hook_probe_test").render_as_string(
            hide_password=False
        )
    )
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    config = _alembic_config_for(dest, dsn)
    try:
        command.upgrade(config, "head")
        command.revision(config, message="hook_probe", autogenerate=True)
        generated = [
            path
            for path in (dest / "versions").glob("*.py")
            if path.name != "0001_initial.py" and path.name != "__init__.py"
        ]
        assert generated
        source = generated[0].read_text()
        assert "CREATE TRIGGER" in source
        assert "schema_trigger_probes" in source
        assert '"catalogo"' in source or "catalogo" in source
        assert "set_updated_at" in source
    finally:
        drop_database_if_exists(dsn)


def test_alembic_check_is_clean_after_schema_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    require_postgres: RedactedDsn,
) -> None:
    """K12: include_schemas hace que check no vuelva a ver catalogo.piezas."""

    from sqlalchemy.engine.url import make_url

    dest = tmp_path / "migrations"
    shutil.copytree(ROOT / "app" / "migrations", dest)
    (dest / "versions" / "0002_schema_probe.py").write_text(
        '''"""schema probe

Revision ID: 0002_schema_probe
Revises: 0001_initial
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations.ops import op_create_updated_at_trigger, op_drop_updated_at_trigger

revision: str = "0002_schema_probe"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS catalogo")
    op.create_table(
        "schema_trigger_probes",
        sa.Column(
            "id", sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label", sa.String(32), nullable=False),
        schema="catalogo",
    )
    op_create_updated_at_trigger("schema_trigger_probes", schema="catalogo")


def downgrade() -> None:
    op_drop_updated_at_trigger("schema_trigger_probes", schema="catalogo")
    op.drop_table("schema_trigger_probes", schema="catalogo")
'''
    )
    parsed = make_url(resolve_alembic_database_url())
    dsn = RedactedDsn(
        parsed.set(database="schema_autogen_probe_test").render_as_string(
            hide_password=False
        )
    )
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    config = _alembic_config_for(dest, dsn)
    try:
        command.upgrade(config, "head")
        command.revision(config, message="after_schema", autogenerate=True)
        generated = [
            path
            for path in (dest / "versions").glob("*.py")
            if path.name
            not in {"0001_initial.py", "0002_schema_probe.py", "__init__.py"}
        ]
        assert generated
        source = generated[0].read_text()
        assert "schema_trigger_probes" not in source
    finally:
        drop_database_if_exists(dsn)


def test_alembic_check_ignores_foreign_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    require_postgres: RedactedDsn,
) -> None:
    """AG1: include_name=True propondría borrar ``ajeno.cosas``."""

    dsn = RedactedDsn(resolve_alembic_database_url())
    ensure_database_exists(dsn)
    monkeypatch.setenv("DATABASE_URL", dsn.secret())
    command.upgrade(alembic_config(dsn), "head")

    async def add_foreign() -> None:
        engine = _create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS ajeno"))
                await connection.execute(
                    text("CREATE TABLE IF NOT EXISTS ajeno.cosas (id integer)")
                )
        finally:
            await engine.dispose()

    asyncio.run(add_foreign())
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn.secret()
    env["PYTHONPATH"] = str(ROOT)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from alembic import command\n"
                "from app.migrations.config import make_alembic_config\n"
                f"command.check(make_alembic_config({dsn.secret()!r}))\n",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, output
        assert "ajeno" not in output.lower() or "No new upgrade" in output
    finally:

        async def drop_foreign() -> None:
            engine = _create_async_engine(dsn.secret())
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("DROP SCHEMA IF EXISTS ajeno CASCADE")
                    )
            finally:
                await engine.dispose()

        asyncio.run(drop_foreign())


def test_autogenerate_discards_empty_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    require_postgres: RedactedDsn,
) -> None:
    """AG5: con ``--autogenerate`` no se escribe una revisión vacía."""

    dest = tmp_path / "migrations"
    shutil.copytree(ROOT / "app" / "migrations", dest)
    from sqlalchemy.engine.url import make_url

    parsed = make_url(resolve_alembic_database_url())
    dsn = RedactedDsn(
        parsed.set(database="autogen_empty_probe_test").render_as_string(
            hide_password=False
        )
    )
    ensure_database_exists(dsn)
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn.secret()
    env["PYTHONPATH"] = str(ROOT)
    script = (
        "from argparse import Namespace\n"
        "from pathlib import Path\n"
        "from alembic import command\n"
        "from alembic.config import Config\n"
        f"dest = Path({str(dest)!r})\n"
        "config = Config()\n"
        "config.cmd_opts = Namespace(autogenerate=True)\n"
        "config.set_main_option('script_location', str(dest))\n"
        "config.set_main_option(\n"
        f"    'sqlalchemy.url', {dsn.secret()!r}.replace('%', '%%')\n"
        ")\n"
        "command.upgrade(config, 'head')\n"
        "before = {p.name for p in (dest / 'versions').glob('*.py')}\n"
        "command.revision(config, message='empty_probe', autogenerate=True)\n"
        "after = {p.name for p in (dest / 'versions').glob('*.py')}\n"
        "new = after - before\n"
        "assert not new, new\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    finally:
        drop_database_if_exists(dsn)
