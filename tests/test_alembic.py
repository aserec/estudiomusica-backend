import asyncio

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import (
    alembic_config,
    ensure_database_exists,
    resolve_alembic_database_url,
)

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


def test_alembic_upgrade_head_and_downgrade_base(
    monkeypatch: pytest.MonkeyPatch,
    require_postgres: str,
) -> None:
    url = resolve_alembic_database_url()
    ensure_database_exists(url)
    monkeypatch.setenv("DATABASE_URL", url)
    config = alembic_config(url)

    command.upgrade(config, "head")
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "0002_auth_users"
    assert (
        _scalar(url, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
        == "set_updated_at"
    )
    assert _scalar(url, "SELECT gen_random_uuid() IS NOT NULL") is True
    assert _scalar(url, "SELECT to_regclass('public.users')") == "users"
    assert _scalar(url, "SELECT to_regclass('public.auth_sessions')") == "auth_sessions"
    assert (
        _scalar(url, "SELECT extname FROM pg_extension WHERE extname = 'citext'")
        == "citext"
    )

    command.downgrade(config, "0001_initial")
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "0001_initial"
    assert _scalar(url, "SELECT to_regclass('public.users')") is None
    assert _scalar(url, "SELECT to_regclass('public.auth_sessions')") is None
    assert (
        _scalar(url, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
        == "set_updated_at"
    )

    command.downgrade(config, "base")
    assert _scalar(url, "SELECT version_num FROM alembic_version") is None
    assert (
        _scalar(url, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
        is None
    )

    command.upgrade(config, "head")
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "0002_auth_users"
    assert _scalar(url, "SELECT to_regclass('public.users')") == "users"


def test_alembic_revision_chain_is_linear() -> None:
    config = alembic_config(resolve_alembic_database_url())
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0002_auth_users"]
    assert script.get_revision("0002_auth_users").down_revision == "0001_initial"
    assert script.get_revision("0001_initial").down_revision is None
