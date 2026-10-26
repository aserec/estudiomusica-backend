"""L1/L2: las limpiezas de leftovers de conftest son comportamentales."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

from app.db import RedactedDsn
from tests.conftest import (
    PRODUCTION_TEST_USER,
    ROOT,
    _admin_engine,
    _create_async_engine,
    _drop_leftover_test_databases,
    _drop_leftover_test_tables,
    _drop_named_production_test_role,
    _orphan_production_test_role,
    _pg_ident,
    _pg_literal,
    drop_database_if_exists,
    ensure_database_exists,
)


def _leftover_db(require_postgres: RedactedDsn) -> RedactedDsn:
    parsed = make_url(require_postgres.secret())
    return RedactedDsn(
        parsed.set(database="autogen_sa_probe_test").render_as_string(
            hide_password=False
        )
    )


async def _table_exists(dsn: RedactedDsn) -> object:
    engine = _create_async_engine(dsn.secret())
    try:
        async with engine.connect() as connection:
            return await connection.scalar(
                text("SELECT to_regclass('public.session_probe_leak')")
            )
    finally:
        await engine.dispose()


async def _database_exists(require_postgres: RedactedDsn, name: str) -> object:
    parsed = make_url(require_postgres.secret())
    engine = _create_async_engine(
        parsed.set(database="postgres").render_as_string(hide_password=False)
    )
    try:
        async with engine.connect() as connection:
            return await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            )
    finally:
        await engine.dispose()


@pytest.mark.db
def test_drop_leftover_test_tables_removes_known_names(
    require_postgres: RedactedDsn,
) -> None:
    async def create() -> None:
        engine = _create_async_engine(require_postgres.secret())
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
        engine = _create_async_engine(require_postgres.secret())
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
    leftover = _leftover_db(require_postgres)
    ensure_database_exists(leftover)
    _drop_leftover_test_databases(require_postgres)
    assert (
        asyncio.run(_database_exists(require_postgres, "autogen_sa_probe_test")) is None
    )
    drop_database_if_exists(leftover)


def _plant_session_leftovers(require_postgres: RedactedDsn) -> None:
    leftover = _leftover_db(require_postgres)
    ensure_database_exists(leftover)

    async def plant_table() -> None:
        engine = _create_async_engine(require_postgres.secret())
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE IF NOT EXISTS session_probe_leak (id integer)")
                )
        finally:
            await engine.dispose()

    asyncio.run(plant_table())


def _run_leftover_probe(
    tmp_path: Path, require_postgres: RedactedDsn, source: str
) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "test_plant_session_leftover.py"
    probe.write_text(source)
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["TEST_DATABASE_URL"] = require_postgres.secret()
    env["REQUIRE_POSTGRES"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-q",
            str(probe),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


_CHILD_SETUP_AND_PLANT = """
import asyncio

from sqlalchemy import text
from sqlalchemy.engine.url import make_url

from app.db import RedactedDsn
from tests.conftest import _create_async_engine, resolve_test_database_url


def test_setup_cleared_then_plant(migrated_test_database, require_postgres):
    async def leftover():
        engine = _create_async_engine(require_postgres.secret())
        try:
            async with engine.connect() as connection:
                table = await connection.scalar(
                    text("SELECT to_regclass('public.session_probe_leak')")
                )
                db = await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": "autogen_sa_probe_test"},
                )
        finally:
            await engine.dispose()
        return table, db

    table, db = asyncio.run(leftover())
    assert table is None
    assert db is None

    async def plant_table():
        engine = _create_async_engine(resolve_test_database_url())
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE IF NOT EXISTS session_probe_leak (id integer)")
                )
        finally:
            await engine.dispose()

    asyncio.run(plant_table())
    parsed = make_url(require_postgres.secret())
    leftover = RedactedDsn(
        parsed.set(database="autogen_sa_probe_test").render_as_string(
            hide_password=False
        )
    )
    from tests.conftest import ensure_database_exists

    ensure_database_exists(leftover)
"""


@pytest.mark.db
def test_migrated_fixture_setup_and_teardown_drop_leftovers(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """L1d/L2/L1b: setup y teardown de sesión borran tablas y bases sobrantes."""

    _plant_session_leftovers(require_postgres)
    result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_SETUP_AND_PLANT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert asyncio.run(_table_exists(require_postgres)) is None
    assert (
        asyncio.run(_database_exists(require_postgres, "autogen_sa_probe_test")) is None
    )
    drop_database_if_exists(_leftover_db(require_postgres))


@pytest.mark.db
def test_leftover_child_then_production_role(
    require_postgres: RedactedDsn,
    tmp_path: Path,
    production_test_database_url: RedactedDsn,
) -> None:
    """El hijo de leftovers no tumba el rol de production del padre."""

    _plant_session_leftovers(require_postgres)
    result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_SETUP_AND_PLANT)
    assert result.returncode == 0, result.stdout + result.stderr

    async def prod_alive() -> object:
        engine = _create_async_engine(production_test_database_url.secret())
        try:
            async with engine.connect() as connection:
                return await connection.scalar(text("SELECT current_user"))
        finally:
            await engine.dispose()

    assert asyncio.run(prod_alive()) == PRODUCTION_TEST_USER
    assert asyncio.run(_table_exists(require_postgres)) is None
    assert (
        asyncio.run(_database_exists(require_postgres, "autogen_sa_probe_test")) is None
    )
    drop_database_if_exists(_leftover_db(require_postgres))


def _unused_pid() -> int:
    for candidate in range(2_000_000, 2_000_400):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    raise RuntimeError("no se encontró un pid libre para el rol huérfano")


async def _role_exists(dsn: RedactedDsn, name: str) -> object:
    engine = _create_async_engine(dsn.secret())
    try:
        async with engine.connect() as connection:
            return await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
                {"name": name},
            )
    finally:
        await engine.dispose()


_CHILD_ORPHAN_SETUP = """
def test_setup_clears_orphans(migrated_test_database):
    assert True
"""


@pytest.mark.db
def test_setup_drops_orphan_production_test_role(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """Tras kill -9 queda em_prod_tests_<pid> muerto; el setup lo borra."""

    dead_pid = _unused_pid()
    orphan = f"em_prod_tests_{dead_pid}"
    assert _orphan_production_test_role(orphan) is True
    assert _orphan_production_test_role(PRODUCTION_TEST_USER) is False
    role = _pg_ident(orphan)

    async def plant() -> None:
        engine = _admin_engine(require_postgres)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                        f"PASSWORD {_pg_literal('orphan-role-secret')}"
                    )
                )
                await connection.execute(
                    text("CREATE TABLE IF NOT EXISTS orphan_owned_by_dead_pid (id int)")
                )
                await connection.execute(
                    text(f"ALTER TABLE orphan_owned_by_dead_pid OWNER TO {role}")
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(plant())
        assert asyncio.run(_role_exists(require_postgres, orphan)) == 1
        result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_ORPHAN_SETUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert asyncio.run(_role_exists(require_postgres, orphan)) is None
        assert (
            asyncio.run(
                _table_exists_named(require_postgres, "orphan_owned_by_dead_pid")
            )
            is None
        )
    finally:

        async def cleanup_orphan() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await _drop_named_production_test_role(connection, orphan)
                    await connection.execute(
                        text("DROP TABLE IF EXISTS orphan_owned_by_dead_pid")
                    )
            finally:
                await engine.dispose()

        asyncio.run(cleanup_orphan())


async def _table_exists_named(dsn: RedactedDsn, name: str) -> object:
    engine = _create_async_engine(dsn.secret())
    try:
        async with engine.connect() as connection:
            return await connection.scalar(
                text("SELECT to_regclass(:reg)"),
                {"reg": f"public.{name}"},
            )
    finally:
        await engine.dispose()


def test_orphan_production_test_role_ignores_live_and_foreign_names() -> None:
    assert _orphan_production_test_role(PRODUCTION_TEST_USER) is False
    assert _orphan_production_test_role("em_prod_tests") is False
    assert _orphan_production_test_role("em_prod_tests_abc") is False
    assert _orphan_production_test_role("other_1") is False
    assert _orphan_production_test_role(f"em_prod_tests_{_unused_pid()}") is True


def test_own_schema_names_includes_public() -> None:
    """AG6: ``public`` está siempre, aunque no haya tablas con schema."""

    from app.migrations.autogen import own_schema_names

    assert "public" in own_schema_names()
