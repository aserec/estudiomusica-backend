import asyncio

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import alembic_config, resolve_alembic_database_url

pytestmark = pytest.mark.db


def _scalar(url: str, sql: str) -> object:
    async def run() -> object:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text(sql))
                return result.scalar()
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    db_name = parsed.database
    if db_name is None:
        raise RuntimeError("DSN de Alembic sin nombre de base")
    admin = parsed.set(database="estudiomusica")

    async def run() -> None:
        engine = create_async_engine(
            admin.render_as_string(hide_password=False),
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as connection:
                exists = await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": db_name},
                )
                if exists is None:
                    await connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_alembic_upgrade_head_and_downgrade_base(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: str,
) -> None:
    url = resolve_alembic_database_url()
    _ensure_database(url)
    monkeypatch.setenv("DATABASE_URL", url)
    config = alembic_config(url)

    command.upgrade(config, "head")
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "0001_initial"
    assert (
        _scalar(url, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
        == "set_updated_at"
    )
    assert _scalar(url, "SELECT gen_random_uuid() IS NOT NULL") is True

    command.downgrade(config, "base")
    assert _scalar(url, "SELECT version_num FROM alembic_version") is None
    assert (
        _scalar(url, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
        is None
    )

    command.upgrade(config, "head")
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "0001_initial"


def test_alembic_has_a_single_initial_revision() -> None:
    config = alembic_config(resolve_alembic_database_url())
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0001_initial"]
    assert script.get_revision("0001_initial").down_revision is None
