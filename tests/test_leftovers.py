"""L1/L2: las limpiezas de leftovers de conftest son comportamentales."""

import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import RedactedDsn
from tests.conftest import (
    ROOT,
    _drop_leftover_test_databases,
    _drop_leftover_test_tables,
    drop_database_if_exists,
    ensure_database_exists,
)


@pytest.mark.db
def test_drop_leftover_test_tables_removes_known_names(
    require_postgres: RedactedDsn,
) -> None:
    async def create() -> None:
        engine = create_async_engine(require_postgres.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE IF NOT EXISTS session_probe_leak (id integer)")
                )
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS ajeno"))
        finally:
            await engine.dispose()

    asyncio.run(create())
    _drop_leftover_test_tables(require_postgres)

    async def leftover() -> tuple[object, object]:
        engine = create_async_engine(require_postgres.secret())
        try:
            async with engine.connect() as connection:
                table = await connection.scalar(
                    text("SELECT to_regclass('public.session_probe_leak')")
                )
                schema = await connection.scalar(
                    text("SELECT 1 FROM pg_namespace WHERE nspname = 'ajeno'")
                )
        finally:
            await engine.dispose()
        return table, schema

    table, schema = asyncio.run(leftover())
    assert table is None
    assert schema is None


@pytest.mark.db
def test_drop_leftover_test_databases_removes_known_names(
    require_postgres: RedactedDsn,
) -> None:
    parsed = make_url(require_postgres.secret())
    leftover = RedactedDsn(
        parsed.set(database="autogen_sa_probe_test").render_as_string(
            hide_password=False
        )
    )
    ensure_database_exists(leftover)
    _drop_leftover_test_databases(require_postgres)

    async def exists() -> object:
        engine = create_async_engine(
            parsed.set(database="postgres").render_as_string(hide_password=False)
        )
        try:
            async with engine.connect() as connection:
                return await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": "autogen_sa_probe_test"},
                )
        finally:
            await engine.dispose()

    assert asyncio.run(exists()) is None
    drop_database_if_exists(leftover)


@pytest.mark.db
def test_migrated_fixture_teardown_drops_planted_leftover(
    require_postgres: RedactedDsn,
) -> None:
    """L1b: el teardown de sesión borra leftovers plantados durante el test."""

    probe = ROOT / "tests" / "_probe_plant_session_leftover.py"
    probe.write_text(
        "import asyncio\n"
        "import pytest\n"
        "from sqlalchemy import text\n"
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "\n"
        "from tests.conftest import resolve_test_database_url\n"
        "\n"
        "@pytest.mark.db\n"
        "def test_plant_leftover(migrated_test_database):\n"
        "    async def plant() -> None:\n"
        "        engine = create_async_engine(resolve_test_database_url())\n"
        "        try:\n"
        "            async with engine.begin() as connection:\n"
        "                await connection.execute(\n"
        "                    text(\n"
        "                        'CREATE TABLE IF NOT EXISTS "
        "session_probe_leak (id integer)'\n"
        "                    )\n"
        "                )\n"
        "        finally:\n"
        "            await engine.dispose()\n"
        "    asyncio.run(plant())\n"
    )
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["TEST_DATABASE_URL"] = require_postgres.secret()
    env["REQUIRE_POSTGRES"] = "1"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                str(probe.relative_to(ROOT)),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
    finally:
        probe.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout + result.stderr

    async def leftover() -> object:
        engine = create_async_engine(require_postgres.secret())
        try:
            async with engine.connect() as connection:
                return await connection.scalar(
                    text("SELECT to_regclass('public.session_probe_leak')")
                )
        finally:
            await engine.dispose()

    assert asyncio.run(leftover()) is None


def test_own_schema_names_includes_public() -> None:
    """AG6: ``public`` está siempre, aunque no haya tablas con schema."""

    from app.migrations.autogen import own_schema_names

    assert "public" in own_schema_names()
