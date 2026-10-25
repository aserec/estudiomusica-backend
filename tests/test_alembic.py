import asyncio

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import RedactedDsn
from tests.conftest import (
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
