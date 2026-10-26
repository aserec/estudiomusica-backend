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
    _SessionAdvisoryLock,
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
    return _unused_pids(1)[0]


def _unused_pids(count: int) -> list[int]:
    found: list[int] = []
    for candidate in range(2_000_000, 2_100_000):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            found.append(candidate)
            if len(found) >= count:
                return found
        except OSError:
            continue
    raise RuntimeError("no se encontraron pids libres para roles huérfanos")


def _plant_orphan_roles(admin: RedactedDsn, names: list[str]) -> None:
    async def plant() -> None:
        engine = _admin_engine(admin)
        try:
            async with engine.connect() as connection:
                for name in names:
                    role = _pg_ident(name)
                    await connection.execute(
                        text(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                            f"PASSWORD {_pg_literal('orphan-race-secret')}"
                        )
                    )
                    await connection.execute(
                        text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
                    )
        finally:
            await engine.dispose()

    asyncio.run(plant())


def _production_role_names(admin: RedactedDsn) -> list[str]:
    async def listed() -> list[str]:
        engine = _admin_engine(admin)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT rolname FROM pg_roles "
                            "WHERE rolname LIKE 'em_prod_tests_%' "
                            "ORDER BY 1"
                        )
                    )
                ).all()
        finally:
            await engine.dispose()
        return [name for (name,) in rows]

    return asyncio.run(listed())


def _cleanup_named_roles(admin: RedactedDsn, names: list[str]) -> None:
    async def cleanup() -> None:
        engine = _admin_engine(admin)
        try:
            async with engine.connect() as connection:
                for name in names:
                    await _drop_named_production_test_role(connection, name)
        finally:
            await engine.dispose()

    asyncio.run(cleanup())


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


@pytest.mark.db
@pytest.mark.timeout(90)
def test_concurrent_orphan_drops_do_not_fail(require_postgres: RedactedDsn) -> None:
    """Dos procesos pueden borrar huérfanos a la vez sin tumbar production."""

    planted = [f"em_prod_tests_{pid}" for pid in _unused_pids(8)]
    _plant_orphan_roles(require_postgres, planted)
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["TEST_DATABASE_URL"] = require_postgres.secret()
    try:
        workers = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "race_drop.py"),
                    "--drop-only",
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for _ in range(2)
        ]
        outputs: list[str] = []
        codes: list[int] = []
        for worker in workers:
            out, _ = worker.communicate(timeout=60)
            outputs.append(out)
            codes.append(int(worker.returncode or 0))
        assert codes == [0, 0], "".join(outputs)
        leftovers = set(_production_role_names(require_postgres)) & set(planted)
        assert leftovers == set()
    finally:
        _cleanup_named_roles(require_postgres, planted)


@pytest.mark.db
def test_orphan_objects_in_other_test_db_do_not_fail_production(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """Un huérfano con objetos en otra *_test no tumba el barrido."""

    orphan = f"em_prod_tests_{_unused_pid()}"
    other_name = "orphan_crossdb_probe_test"
    parsed = make_url(require_postgres.secret())
    other = RedactedDsn(
        parsed.set(database=other_name).render_as_string(hide_password=False)
    )
    role = _pg_ident(orphan)
    ensure_database_exists(other)

    async def plant() -> None:
        engine = _admin_engine(require_postgres)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                        f"PASSWORD {_pg_literal('orphan-crossdb-secret')}"
                    )
                )
                await connection.execute(
                    text(f"GRANT CONNECT ON DATABASE {_pg_ident(other_name)} TO {role}")
                )
        finally:
            await engine.dispose()
        other_engine = _admin_engine(other)
        try:
            async with other_engine.connect() as connection:
                await connection.execute(
                    text("CREATE TABLE IF NOT EXISTS orphan_crossdb_owned (id int)")
                )
                await connection.execute(
                    text(f"ALTER TABLE orphan_crossdb_owned OWNER TO {role}")
                )
        finally:
            await other_engine.dispose()

    try:
        asyncio.run(plant())
        assert asyncio.run(_role_exists(require_postgres, orphan)) == 1
        result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_ORPHAN_SETUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert asyncio.run(_role_exists(require_postgres, orphan)) is None
    finally:

        async def cleanup() -> None:
            other_engine = _admin_engine(other)
            try:
                async with other_engine.connect() as connection:
                    await connection.execute(
                        text("DROP TABLE IF EXISTS orphan_crossdb_owned")
                    )
            finally:
                await other_engine.dispose()
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await _drop_named_production_test_role(connection, orphan)
            finally:
                await engine.dispose()

        asyncio.run(cleanup())
        drop_database_if_exists(other)


@pytest.mark.db
def test_orphan_with_held_life_lock_is_not_dropped(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """os.kill no ve otros hosts: solo se borra un rol cuyo lock está libre."""

    orphan = f"em_prod_tests_{_unused_pid()}"
    assert _orphan_production_test_role(orphan) is True
    lock = _SessionAdvisoryLock(require_postgres, orphan)
    assert lock.start() is True
    role = _pg_ident(orphan)
    try:

        async def plant() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await connection.execute(
                        text(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                            f"PASSWORD {_pg_literal('orphan-lock-secret')}"
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(plant())
        result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_ORPHAN_SETUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert asyncio.run(_role_exists(require_postgres, orphan)) == 1
    finally:
        lock.stop()
        _cleanup_named_roles(require_postgres, [orphan])


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
