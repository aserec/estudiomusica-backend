import asyncio

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import alembic_config, resolve_test_database_url


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
) -> None:
    url = resolve_test_database_url()
    monkeypatch.setenv("DATABASE_URL", url)
    config = alembic_config(url)

    command.upgrade(config, "head")
    extension = _scalar(
        url, "SELECT extname FROM pg_extension WHERE extname = 'pgcrypto'"
    )
    assert extension == "pgcrypto"
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "0001_initial"

    command.downgrade(config, "base")
    assert (
        _scalar(url, "SELECT extname FROM pg_extension WHERE extname = 'pgcrypto'")
        is None
    )
    assert _scalar(url, "SELECT version_num FROM alembic_version") is None

    command.upgrade(config, "head")
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "0001_initial"


def test_alembic_has_a_single_initial_revision() -> None:
    config = alembic_config(resolve_test_database_url())
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0001_initial"]
    assert script.get_revision("0001_initial").down_revision is None
