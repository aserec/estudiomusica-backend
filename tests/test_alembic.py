import asyncio
import importlib
import importlib.util
import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import ops
from alembic.script import ScriptDirectory
from sqlalchemy import String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.db import RedactedDsn
from app.migrations.autogen import process_revision_directives
from app.models import PersistentModel
from app.models.base import updated_at_trigger_name
from tests.conftest import (
    ROOT,
    alembic_config,
    ensure_database_exists,
    resolve_alembic_database_url,
)

pytestmark = pytest.mark.db


def _scalar(dsn: RedactedDsn, sql: str) -> object:
    async def run() -> object:
        engine = create_async_engine(dsn.secret())
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
    process_revision_directives(None, None, [script])
    sql_ops = [
        operation
        for operation in script.upgrade_ops.ops
        if isinstance(operation, ops.ExecuteSQLOp)
    ]
    assert len(sql_ops) == 1
    sql = str(sql_ops[0].sqltext)
    assert updated_at_trigger_name("migrated_trigger_probes") in sql
    assert "unrelated_table" not in sql


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
            engine = create_async_engine(dsn.secret())
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
from sqlalchemy.ext.asyncio import async_engine_from_config

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
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
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
            engine = create_async_engine(dsn.secret())
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
        engine = create_async_engine(dsn.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE IF NOT EXISTS dep_trig (updated_at timestamptz)")
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
            engine = create_async_engine(dsn.secret())
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("DROP TABLE IF EXISTS dep_trig"))
                    await connection.execute(
                        text("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")
                    )
            finally:
                await engine.dispose()

        asyncio.run(cleanup())
        command.stamp(config, "base")
