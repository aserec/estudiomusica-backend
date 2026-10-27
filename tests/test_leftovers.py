"""L1/L2: las limpiezas de leftovers de conftest son comportamentales."""

import asyncio
import logging
import os
import secrets
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import RedactedDsn
from tests.conftest import (
    _LEFTOVER_TEST_DATABASES,
    _LEFTOVER_TEST_TABLES,
    PRODUCTION_TEST_USER,
    ROOT,
    _admin_engine,
    _create_async_engine,
    _drop_leftover_test_databases,
    _drop_leftover_test_tables,
    _drop_named_production_test_role,
    _drop_production_test_role,
    _hold_production_role_life_lock,
    _holds_own_life_lock,
    _orphan_production_test_role,
    _pg_ident,
    _pg_literal,
    _role_advisory_lock_keys,
    _SWEEP_ADVISORY_LOCK_KEY1,
    _SWEEP_ADVISORY_LOCK_KEY2,
    _SessionAdvisoryLock,
    _skip_or_fail_unix_socket_lock,
    drop_database_if_exists,
    drop_production_test_role,
    ensure_database_exists,
    ensure_production_test_role,
    pytest_sessionfinish,
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


def _orphan_role_name(pid: int | None = None) -> str:
    suffix = pid if pid is not None else _unused_pid()
    return f"em_prod_tests_{suffix}_t_dead_{secrets.token_hex(2)}"


def _unique_lock_role(kind: str) -> str:
    return f"em_prod_tests_1_{kind}_{os.getpid()}_{secrets.token_hex(3)}"


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
    engine = _admin_engine(dsn)
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
    """Tras kill -9 queda em_prod_tests_<pid> muerto; DROP OWNED borra su objeto."""

    assert "orphan_drop_owned_probe" not in _LEFTOVER_TEST_TABLES
    dead_pid = _unused_pid()
    orphan = _orphan_role_name(dead_pid)
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
                    text("CREATE TABLE IF NOT EXISTS orphan_drop_owned_probe (id int)")
                )
                await connection.execute(
                    text(f"ALTER TABLE orphan_drop_owned_probe OWNER TO {role}")
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
                _table_exists_named(require_postgres, "orphan_drop_owned_probe")
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
                        text("DROP TABLE IF EXISTS orphan_drop_owned_probe")
                    )
            finally:
                await engine.dispose()

        asyncio.run(cleanup_orphan())


@pytest.mark.db
@pytest.mark.timeout(90)
def test_concurrent_orphan_drops_do_not_fail(require_postgres: RedactedDsn) -> None:
    """Dos procesos pueden borrar huérfanos a la vez sin tumbar production."""

    planted = [_orphan_role_name(pid) for pid in _unused_pids(8)]
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

    orphan = _orphan_role_name()
    other_name = f"orphan_crossdb_{os.getpid()}_{secrets.token_hex(3)}_test"
    assert other_name not in _LEFTOVER_TEST_DATABASES
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
        if asyncio.run(_database_exists(require_postgres, other_name)) is not None:

            async def cleanup_other() -> None:
                other_engine = _admin_engine(other)
                try:
                    async with other_engine.connect() as connection:
                        await connection.execute(
                            text("DROP TABLE IF EXISTS orphan_crossdb_owned")
                        )
                finally:
                    await other_engine.dispose()

            asyncio.run(cleanup_other())
        drop_database_if_exists(other)

        async def cleanup_role() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await _drop_named_production_test_role(connection, orphan)
            finally:
                await engine.dispose()

        asyncio.run(cleanup_role())


@pytest.mark.db
def test_orphan_objects_in_mixed_case_test_db_are_dropped(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """CF3: el DROP OWNED cruzado acepta un sufijo _TEST (casefold)."""

    orphan = _orphan_role_name()
    other_name = f"orphan_cf3_{os.getpid()}_{secrets.token_hex(3)}_TEST"
    assert other_name not in _LEFTOVER_TEST_DATABASES
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
                        f"PASSWORD {_pg_literal('orphan-cf3-secret')}"
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
                    text("CREATE TABLE IF NOT EXISTS orphan_cf3_owned (id int)")
                )
                await connection.execute(
                    text(f"ALTER TABLE orphan_cf3_owned OWNER TO {role}")
                )
        finally:
            await other_engine.dispose()

    try:
        asyncio.run(plant())
        result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_ORPHAN_SETUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert asyncio.run(_role_exists(require_postgres, orphan)) is None
    finally:
        if asyncio.run(_database_exists(require_postgres, other_name)) is not None:

            async def cleanup_other() -> None:
                other_engine = _admin_engine(other)
                try:
                    async with other_engine.connect() as connection:
                        await connection.execute(
                            text("DROP TABLE IF EXISTS orphan_cf3_owned")
                        )
                finally:
                    await other_engine.dispose()

            asyncio.run(cleanup_other())
        drop_database_if_exists(other)

        async def cleanup_role() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await _drop_named_production_test_role(connection, orphan)
            finally:
                await engine.dispose()

        asyncio.run(cleanup_role())


def _non_test_probe_database(kind: str) -> str:
    token = secrets.token_hex(2)
    return f"{kind}_{os.getpid()}_{token}_probe"


def _is_allowed_probe_database(name: str) -> bool:
    prefixes = ("orphan_owner_", "orphan_nontest_objects_")
    return name.startswith(prefixes) and name.endswith("_probe")


def _maintenance_engine(admin: RedactedDsn) -> AsyncEngine:
    parsed = make_url(admin.secret())
    return _create_async_engine(
        parsed.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )


def _create_probe_database(
    admin: RedactedDsn, name: str, *, owner: str | None = None
) -> None:
    if not _is_allowed_probe_database(name):
        raise RuntimeError(f"base sonda no permitida: {name}")

    async def create() -> None:
        engine = _maintenance_engine(admin)
        try:
            async with engine.connect() as connection:
                exists = await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": name},
                )
                if exists is not None:
                    return
                owner_sql = f" OWNER {_pg_ident(owner)}" if owner else ""
                await connection.execute(
                    text(f"CREATE DATABASE {_pg_ident(name)}{owner_sql}")
                )
        finally:
            await engine.dispose()

    asyncio.run(create())


def _drop_probe_database(admin: RedactedDsn, name: str) -> None:
    if not _is_allowed_probe_database(name):
        raise RuntimeError(f"base sonda no permitida: {name}")

    async def drop() -> None:
        engine = _maintenance_engine(admin)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": name},
                )
                await connection.execute(
                    text(f"DROP DATABASE IF EXISTS {_pg_ident(name)}")
                )
        finally:
            await engine.dispose()

    asyncio.run(drop())


@pytest.mark.db
def test_orphan_owning_database_does_not_abort_sweep(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """L8: un huérfano dueño de una base no aborta la transacción del barrido."""

    orphan = _orphan_role_name()
    dbname = _non_test_probe_database("orphan_owner")
    role = _pg_ident(orphan)
    try:

        async def plant_role() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await connection.execute(
                        text(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                            f"PASSWORD {_pg_literal('orphan-owner-secret')}"
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(plant_role())
        _create_probe_database(require_postgres, dbname, owner=orphan)
        result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_ORPHAN_SETUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert asyncio.run(_database_exists(require_postgres, dbname)) == 1
    finally:
        _drop_probe_database(require_postgres, dbname)
        _cleanup_named_roles(require_postgres, [orphan])


@pytest.mark.db
def test_failed_orphan_drop_keeps_sweep_transaction_healthy(
    require_postgres: RedactedDsn,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SP1/SP2: un DROP de huérfano fallido no aborta el resto del barrido."""

    blocker = _orphan_role_name()
    victim = _orphan_role_name()
    dbname = _non_test_probe_database("orphan_owner")
    role = _pg_ident(blocker)
    monkeypatch.setattr(
        "tests.conftest.PRODUCTION_TEST_USER", _unique_lock_role("sp_not_ours")
    )
    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", None)
    try:

        async def plant() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await connection.execute(
                        text(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                            f"PASSWORD {_pg_literal('orphan-sp-blocker')}"
                        )
                    )
                    victim_ident = _pg_ident(victim)
                    await connection.execute(
                        text(
                            f"CREATE ROLE {victim_ident} LOGIN NOSUPERUSER "
                            f"PASSWORD {_pg_literal('orphan-sp-victim')}"
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(plant())
        _create_probe_database(require_postgres, dbname, owner=blocker)
        caplog.set_level(logging.WARNING, logger="tests.conftest")
        asyncio.run(_drop_production_test_role(require_postgres))
        assert asyncio.run(_role_exists(require_postgres, blocker)) == 1
        assert asyncio.run(_role_exists(require_postgres, victim)) is None
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "dueño de la base" in message and dbname in message for message in messages
        )
    finally:
        _drop_probe_database(require_postgres, dbname)
        _cleanup_named_roles(require_postgres, [blocker, victim])


@pytest.mark.db
def test_orphan_objects_in_non_test_db_are_not_dropped(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """L11: DROP OWNED no corre en bases que no terminan en _test."""

    orphan = _orphan_role_name()
    dbname = _non_test_probe_database("orphan_nontest_objects")
    parsed = make_url(require_postgres.secret())
    other = RedactedDsn(
        parsed.set(database=dbname).render_as_string(hide_password=False)
    )
    role = _pg_ident(orphan)
    holder_engine: AsyncEngine | None = None
    holder_conn: Any = None
    try:

        async def plant_role() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await connection.execute(
                        text(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                            f"PASSWORD {_pg_literal('orphan-nontest-secret')}"
                        )
                    )
            finally:
                await engine.dispose()

        async def plant_table() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    await connection.execute(
                        text(f"GRANT CONNECT ON DATABASE {_pg_ident(dbname)} TO {role}")
                    )
            finally:
                await engine.dispose()
            other_engine = _admin_engine(other)
            try:
                async with other_engine.connect() as connection:
                    await connection.execute(
                        text("CREATE TABLE orphan_nontest_owned (id int)")
                    )
                    await connection.execute(
                        text(f"ALTER TABLE orphan_nontest_owned OWNER TO {role}")
                    )
            finally:
                await other_engine.dispose()

        async def acquire_sweep_lock() -> None:
            nonlocal holder_engine, holder_conn
            holder_engine = _admin_engine(require_postgres)
            holder_conn = await holder_engine.connect()
            await holder_conn.scalar(
                text("SELECT pg_advisory_lock(:k1, :k2)"),
                {
                    "k1": _SWEEP_ADVISORY_LOCK_KEY1,
                    "k2": _SWEEP_ADVISORY_LOCK_KEY2,
                },
            )

        async def release_sweep_lock() -> None:
            if holder_conn is not None:
                with suppress(Exception):
                    await holder_conn.scalar(
                        text("SELECT pg_advisory_unlock(:k1, :k2)"),
                        {
                            "k1": _SWEEP_ADVISORY_LOCK_KEY1,
                            "k2": _SWEEP_ADVISORY_LOCK_KEY2,
                        },
                    )
                with suppress(Exception):
                    await holder_conn.close()
            if holder_engine is not None:
                await holder_engine.dispose()

        asyncio.run(acquire_sweep_lock())
        try:
            asyncio.run(plant_role())
            _create_probe_database(require_postgres, dbname)
            asyncio.run(plant_table())
        finally:
            asyncio.run(release_sweep_lock())
        result = _run_leftover_probe(tmp_path, require_postgres, _CHILD_ORPHAN_SETUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert asyncio.run(_database_exists(require_postgres, dbname)) == 1

        async def table_exists() -> object:
            engine = _create_async_engine(other.secret())
            try:
                async with engine.connect() as connection:
                    return await connection.scalar(
                        text("SELECT to_regclass('public.orphan_nontest_owned')")
                    )
            finally:
                await engine.dispose()

        assert asyncio.run(table_exists()) is not None
    finally:
        if asyncio.run(_database_exists(require_postgres, dbname)) is not None:

            async def cleanup_table() -> None:
                engine = _admin_engine(other)
                try:
                    async with engine.connect() as connection:
                        await connection.execute(
                            text("DROP TABLE IF EXISTS orphan_nontest_owned")
                        )
                finally:
                    await engine.dispose()

            asyncio.run(cleanup_table())
        _drop_probe_database(require_postgres, dbname)
        _cleanup_named_roles(require_postgres, [orphan])


@pytest.mark.db
def test_orphan_with_held_life_lock_is_not_dropped(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """os.kill no ve otros hosts: solo se borra un rol cuyo lock está libre."""

    orphan = _orphan_role_name()
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


_CHILD_LOCK_NO_RELEASE = """
import os
import sys

from app.db import RedactedDsn
from tests.conftest import _SessionAdvisoryLock

lock = _SessionAdvisoryLock(
    RedactedDsn(os.environ["TEST_DATABASE_URL"]),
    os.environ["EM_SHARED_ROLE"],
)
ok = lock.start()
sys.exit(0 if ok else 2)
"""


@pytest.mark.db
@pytest.mark.timeout(15)
def test_lock_without_release_does_not_hang_interpreter(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """L6: sin stop()/release el intérprete no puede quedarse colgado al salir."""

    result = _run_child_script(
        tmp_path,
        require_postgres,
        _CHILD_LOCK_NO_RELEASE,
        _unique_lock_role("l6_norelease"),
    )
    assert result.returncode == 0, result.stdout + result.stderr


_CHILD_SAME_PID_DROP = """
import asyncio
import os
import sys

import tests.conftest as conftest
from app.db import RedactedDsn

conftest.PRODUCTION_TEST_USER = os.environ["EM_SHARED_ROLE"]
admin = RedactedDsn(os.environ["TEST_DATABASE_URL"])
asyncio.run(conftest._drop_production_test_role(admin))
sys.exit(0)
"""

_CHILD_SAME_PID_ENSURE = """
import os
import sys

import tests.conftest as conftest
from app.db import RedactedDsn

conftest.PRODUCTION_TEST_USER = os.environ["EM_SHARED_ROLE"]
admin = RedactedDsn(os.environ["TEST_DATABASE_URL"])
try:
    conftest.ensure_production_test_role(admin)
except RuntimeError as exc:
    if "no se pudo marcar la vida" in str(exc):
        sys.exit(0)
    raise
sys.exit(2)
"""


def _run_child_script(
    tmp_path: Path, require_postgres: RedactedDsn, source: str, shared: str
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "pidns_child.py"
    script.write_text(source)
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["TEST_DATABASE_URL"] = require_postgres.secret()
    env["EM_SHARED_ROLE"] = shared
    env["PYTHONPATH"] = (
        f"{ROOT}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(ROOT)
    )
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.mark.db
def test_same_pid_peer_role_is_not_dropped_without_life_lock(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """Dos contenedores con el mismo pid no deben borrar el rol vivo ajeno."""

    shared = _unique_lock_role("peer_locktest")
    lock = _SessionAdvisoryLock(require_postgres, shared)
    assert lock.start() is True
    role = _pg_ident(shared)
    try:

        async def plant() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    exists = await connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
                        {"name": shared},
                    )
                    if exists is None:
                        await connection.execute(
                            text(
                                f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                                f"PASSWORD {_pg_literal('pidns-peer-secret')}"
                            )
                        )
            finally:
                await engine.dispose()

        asyncio.run(plant())
        result = _run_child_script(
            tmp_path, require_postgres, _CHILD_SAME_PID_DROP, shared
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert asyncio.run(_role_exists(require_postgres, shared)) == 1
    finally:
        lock.stop()
        _cleanup_named_roles(require_postgres, [shared])


@pytest.mark.db
@pytest.mark.timeout(20)
def test_ensure_fails_without_sweeping_when_life_lock_is_busy(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """Si no se consigue el lock, ensure falla y no barre el rol del otro."""

    shared = _unique_lock_role("peer_ensbusy")
    lock = _SessionAdvisoryLock(require_postgres, shared)
    assert lock.start() is True
    role = _pg_ident(shared)
    try:

        async def plant() -> None:
            engine = _admin_engine(require_postgres)
            try:
                async with engine.connect() as connection:
                    exists = await connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
                        {"name": shared},
                    )
                    if exists is None:
                        await connection.execute(
                            text(
                                f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                                f"PASSWORD {_pg_literal('pidns-ensure-secret')}"
                            )
                        )
            finally:
                await engine.dispose()

        asyncio.run(plant())
        result = _run_child_script(
            tmp_path, require_postgres, _CHILD_SAME_PID_ENSURE, shared
        )
        assert result.returncode == 0, result.stdout + result.stderr
        lock.stop()
        assert asyncio.run(_role_exists(require_postgres, shared)) == 1
    finally:
        lock.stop()
        _cleanup_named_roles(require_postgres, [shared])


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
    assert _orphan_production_test_role("em_prod_tests_03300003") is False
    assert _orphan_production_test_role("em_prod_tests_\u0661") is False
    assert _orphan_production_test_role("em_prod_tests_99999999999999999999") is False
    assert _orphan_production_test_role(_orphan_role_name()) is True
    assert _orphan_production_test_role(f"em_prod_tests_{_unused_pid()}") is True


_CHILD_REAL_TEARDOWN = """
import os
from pathlib import Path

from tests.conftest import PRODUCTION_TEST_USER


def test_records_own_production_role(production_test_database_url):
    Path(os.environ["EM_ROLE_FILE"]).write_text(PRODUCTION_TEST_USER)
    assert production_test_database_url is not None
"""


@pytest.mark.db
@pytest.mark.timeout(120)
def test_production_role_teardown_drops_own_role(
    require_postgres: RedactedDsn,
    tmp_path: Path,
) -> None:
    """P1: el teardown real borra el rol propio, sin parchear el nombre."""

    role_file = tmp_path / "own-role.txt"
    probe = tmp_path / "test_real_teardown.py"
    probe.write_text(_CHILD_REAL_TEARDOWN)
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["TEST_DATABASE_URL"] = require_postgres.secret()
    env["REQUIRE_POSTGRES"] = "1"
    env["EM_ROLE_FILE"] = str(role_file)
    result = subprocess.run(
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
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    name = role_file.read_text().strip()
    assert name.startswith("em_prod_tests_")
    assert name != "em_prod_tests"
    assert asyncio.run(_role_exists(require_postgres, name)) is None


def test_is_test_database_name_ignores_case() -> None:
    from tests.conftest import _is_test_database_name

    assert _is_test_database_name("foo_test") is True
    assert _is_test_database_name("foo_TEST") is True
    assert _is_test_database_name("foo_Test") is True
    assert _is_test_database_name("foo_prod") is False


def test_drop_database_if_exists_accepts_mixed_case_test_suffix() -> None:
    """CF2: drop_database_if_exists no rechaza un sufijo _TEST."""

    dsn = RedactedDsn("postgresql+asyncpg://u:p@127.0.0.1:1/cf2_mixed_TEST")
    with pytest.raises(ConnectionError):
        drop_database_if_exists(dsn)


def test_production_test_role_name_is_unique() -> None:
    from tests.conftest import _production_test_role_name

    first = _production_test_role_name()
    second = _production_test_role_name()
    assert first != second
    assert first.startswith("em_prod_tests_")
    assert str(os.getpid()) in first.split("_")
    assert _orphan_production_test_role(first) is True


def test_orphan_role_eperm_is_not_orphan() -> None:
    """os.kill ya no decide la vida: un nombre sin pid no es candidato."""

    assert _orphan_production_test_role("em_prod_tests") is False
    assert _orphan_production_test_role("em_prod_tests_1") is True


def test_orphan_production_test_role_captures_overflow() -> None:
    assert _orphan_production_test_role("em_prod_tests_99999999999") is False


def test_holds_own_life_lock_is_false_when_missing_or_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_holds_own_life_lock no lee lock._name y no es True si lost."""

    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", None)
    assert _holds_own_life_lock() is False

    class _Held:
        acquired = True
        lost = False

    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", _Held())
    assert _holds_own_life_lock() is True

    class _Lost:
        acquired = True
        lost = True

    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", _Lost())
    assert _holds_own_life_lock() is False


@pytest.mark.db
def test_lock_start_is_false_when_already_held(
    require_postgres: RedactedDsn,
) -> None:
    """L5: acquired no puede ser True si pg_try_advisory_lock no lo dio."""

    name = _unique_lock_role("l5_busy")
    holder = _SessionAdvisoryLock(require_postgres, name)
    other = _SessionAdvisoryLock(require_postgres, name)
    assert holder.start() is True
    try:
        assert other.start() is False
        assert other.acquired is False
    finally:
        holder.stop()
        other.stop()


@pytest.mark.db
def test_hold_raises_when_life_lock_is_busy(
    require_postgres: RedactedDsn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L19: si start() es False, ensure/hold debe fallar."""

    name = _unique_lock_role("l19_busy")
    monkeypatch.setattr("tests.conftest.PRODUCTION_TEST_USER", name)
    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", None)
    holder = _SessionAdvisoryLock(require_postgres, name)
    assert holder.start() is True
    try:
        with pytest.raises(RuntimeError, match="no se pudo marcar la vida"):
            _hold_production_role_life_lock(require_postgres)
    finally:
        holder.stop()
        monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", None)


@pytest.mark.db
def test_ensure_takes_life_lock_before_creating_role(
    require_postgres: RedactedDsn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L20: ensure no crea el rol si no marca la vida."""

    called: list[str] = []

    def fake_hold(_admin: RedactedDsn) -> None:
        called.append("hold")
        raise RuntimeError("no se pudo marcar la vida del rol de production-tests")

    monkeypatch.setattr("tests.conftest._hold_production_role_life_lock", fake_hold)
    with pytest.raises(RuntimeError, match="no se pudo marcar la vida"):
        ensure_production_test_role(require_postgres)
    assert called == ["hold"]


@pytest.mark.db
def test_lock_stop_finishes_quickly_and_reaps_thread(
    require_postgres: RedactedDsn,
) -> None:
    """stop() no debe bloquearse ni dejar el hilo del lock vivo."""

    import time

    lock = _SessionAdvisoryLock(require_postgres, _unique_lock_role("stop_quick"))
    assert lock.start() is True
    started = time.monotonic()
    lock.stop()
    assert time.monotonic() - started < 3
    assert lock._thread.is_alive() is False


@pytest.mark.db
def test_lock_stop_joins_with_cleanup_timeout(
    require_postgres: RedactedDsn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMO2: stop() hace join con timeout, no un join() indefinido."""

    import threading

    seen: list[float | None] = []
    real_join = threading.Thread.join

    def spy_join(self: threading.Thread, timeout: float | None = None) -> None:
        seen.append(timeout)
        real_join(self, timeout=timeout)

    monkeypatch.setattr(threading.Thread, "join", spy_join)
    lock = _SessionAdvisoryLock(require_postgres, _unique_lock_role("tmo2"))
    assert lock.start() is True
    lock.stop()
    assert seen
    assert seen[0] is not None
    assert seen[0] >= 6


@pytest.mark.db
def test_killed_lock_connection_fails_teardown(
    require_postgres: RedactedDsn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si se mata la conexión del lock, el teardown debe fallar (rc≠0)."""

    import time

    name = _unique_lock_role("kill_lock")
    lock = _SessionAdvisoryLock(require_postgres, name)
    assert lock.start() is True
    monkeypatch.setattr("tests.conftest.PRODUCTION_TEST_USER", name)
    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", lock)
    key1, key2 = _role_advisory_lock_keys(name)

    async def terminate_holder() -> None:
        engine = _admin_engine(require_postgres)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT pid FROM pg_locks "
                            "WHERE locktype = 'advisory' "
                            "AND classid = :k1 AND objid = :k2 "
                            "AND pid <> pg_backend_pid()"
                        ),
                        {"k1": key1, "k2": key2},
                    )
                ).all()
                for (pid,) in rows:
                    await connection.execute(
                        text("SELECT pg_terminate_backend(:pid)"),
                        {"pid": pid},
                    )
        finally:
            await engine.dispose()

    try:
        asyncio.run(terminate_holder())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not lock.lost:
            time.sleep(0.1)
        assert lock.lost is True
        with pytest.raises(RuntimeError, match="se perdió el lock de vida"):
            drop_production_test_role(require_postgres)
    finally:
        lock.stop()


def test_unlock_returning_false_marks_lock_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOST8: unlock → False cuenta como pérdida del lock de vida."""

    class _FakeConnection:
        async def scalar(self, statement: object, _params: object = None) -> object:
            sql = str(statement)
            if "pg_try_advisory_lock" in sql:
                return True
            if "pg_advisory_unlock" in sql:
                return False
            return 1

        async def close(self) -> None:
            return None

    class _FakeEngine:
        async def connect(self) -> _FakeConnection:
            return _FakeConnection()

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(
        "tests.conftest._create_async_engine",
        lambda *_args, **_kwargs: _FakeEngine(),
    )
    dummy = RedactedDsn("postgresql+asyncpg://u:p@127.0.0.1:1/app_test")
    lock = _SessionAdvisoryLock(dummy, _unique_lock_role("lost8"))
    assert lock.start() is True
    lock.stop()
    assert lock.lost is True
    assert lock._thread.is_alive() is False


@pytest.mark.db
def test_drop_detects_lost_lock_immediately_after_kill(
    require_postgres: RedactedDsn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A: unlock fallido o false cuenta como pérdida, leída tras stop()/join."""

    name = _unique_lock_role("kill_lock_now")
    lock = _SessionAdvisoryLock(require_postgres, name)
    assert lock.start() is True
    monkeypatch.setattr("tests.conftest.PRODUCTION_TEST_USER", name)
    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", lock)
    key1, key2 = _role_advisory_lock_keys(name)

    async def terminate_holder() -> None:
        engine = _admin_engine(require_postgres)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT pid FROM pg_locks "
                            "WHERE locktype = 'advisory' "
                            "AND classid = :k1 AND objid = :k2 "
                            "AND pid <> pg_backend_pid()"
                        ),
                        {"k1": key1, "k2": key2},
                    )
                ).all()
                for (pid,) in rows:
                    await connection.execute(
                        text("SELECT pg_terminate_backend(:pid)"),
                        {"pid": pid},
                    )
        finally:
            await engine.dispose()

    try:
        asyncio.run(terminate_holder())
        with pytest.raises(RuntimeError, match="se perdió el lock de vida"):
            drop_production_test_role(require_postgres)
        assert lock.lost is True
    finally:
        lock.stop()


def test_drop_own_role_non_connection_error_is_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOST10: un fallo del DROP propio que no es de conexión no se traga."""

    async def boom(_admin: RedactedDsn) -> None:
        raise RuntimeError("permission denied to drop role")

    async def noop_cleanup(_admin: RedactedDsn) -> None:
        return None

    monkeypatch.setattr("tests.conftest._drop_production_test_role", boom)
    monkeypatch.setattr("tests.conftest._cleanup_production_test_rows", noop_cleanup)
    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", None)
    dummy = RedactedDsn("postgresql+asyncpg://u:p@127.0.0.1:1/app_test")
    with pytest.raises(RuntimeError, match="No se pudo borrar el rol"):
        drop_production_test_role(dummy)


def test_sessionfinish_fails_when_life_lock_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOST3: pytest_sessionfinish debe poner rc≠0 si el lock se perdió."""

    class _LostLock:
        lost = True

    class _Reporter:
        def write_line(self, _msg: str, **_kwargs: object) -> None:
            return None

    class _Plugins:
        def get_plugin(self, _name: str) -> _Reporter:
            return _Reporter()

    class _Config:
        pluginmanager = _Plugins()

    class _Session:
        exitstatus = 0
        config = _Config()

    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", _LostLock())
    session = _Session()
    pytest_sessionfinish(cast(pytest.Session, session), 0)
    assert session.exitstatus == 1


@pytest.mark.db
def test_drop_detects_lost_lock_during_sweep(
    require_postgres: RedactedDsn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B: el holder muere a mitad del barrido; lost se lee tras stop()."""

    name = _unique_lock_role("kill_lock_sweep")
    lock = _SessionAdvisoryLock(require_postgres, name)
    assert lock.start() is True
    monkeypatch.setattr("tests.conftest.PRODUCTION_TEST_USER", name)
    monkeypatch.setattr("tests.conftest._ROLE_LIFE_LOCK", lock)
    key1, key2 = _role_advisory_lock_keys(name)
    original = _drop_production_test_role

    async def terminate_holder() -> None:
        engine = _admin_engine(require_postgres)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT pid FROM pg_locks "
                            "WHERE locktype = 'advisory' "
                            "AND classid = :k1 AND objid = :k2 "
                            "AND pid <> pg_backend_pid()"
                        ),
                        {"k1": key1, "k2": key2},
                    )
                ).all()
                for (pid,) in rows:
                    await connection.execute(
                        text("SELECT pg_terminate_backend(:pid)"),
                        {"pid": pid},
                    )
        finally:
            await engine.dispose()

    async def slow_drop(admin: RedactedDsn) -> None:
        await terminate_holder()
        await asyncio.sleep(0.2)
        await original(admin)

    monkeypatch.setattr("tests.conftest._drop_production_test_role", slow_drop)
    try:
        with pytest.raises(RuntimeError, match="se perdió el lock de vida"):
            drop_production_test_role(require_postgres)
        assert lock.lost is True
    finally:
        lock.stop()


def _unix_socket_lock_dsn(admin: RedactedDsn, directory: str, port: str) -> str:
    """DSN ``?host=/socket&port=`` conservando user/password (authority o query)."""

    parsed = make_url(admin.secret())
    # Conservar user/password (authority o query). .set(query={}) los tira.
    return (
        parsed.set(host=None, port=None)
        .update_query_dict({"host": directory, "port": port})
        .render_as_string(hide_password=False)
    )


async def _postgres_system_identifier(dsn: RedactedDsn) -> str | None:
    engine = _admin_engine(dsn)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT system_identifier FROM pg_control_system()")
            )
    except Exception:
        return None
    finally:
        await engine.dispose()
    return None if value is None else str(value)


@pytest.mark.db
def test_session_lock_holds_with_unix_socket_query_dsn(
    require_postgres: RedactedDsn,
) -> None:
    """MEDIA 3: ?host=/socket&port= debe abrir el lock por la ruta del engine."""

    async def socket_params() -> tuple[str, str, str | None]:
        engine = _admin_engine(require_postgres)
        try:
            async with engine.connect() as connection:
                directories = await connection.scalar(
                    text("SHOW unix_socket_directories")
                )
                port = await connection.scalar(text("SHOW port"))
                ident = await connection.scalar(
                    text("SELECT system_identifier FROM pg_control_system()")
                )
        finally:
            await engine.dispose()
        if not directories:
            raise RuntimeError("Postgres no publica unix_socket_directories")
        return (
            str(directories).split(",")[0].strip(),
            str(port),
            None if ident is None else str(ident),
        )

    directory, port, tcp_ident = asyncio.run(socket_params())
    socket_path = Path(directory) / f".s.PGSQL.{port}"
    if not socket_path.is_socket():
        _skip_or_fail_unix_socket_lock(
            f"el socket Unix {socket_path} no está en el cliente "
            "(en compose el socket vive dentro del contenedor)"
        )
    socket_url = _unix_socket_lock_dsn(require_postgres, directory, port)
    socket_ident = asyncio.run(_postgres_system_identifier(RedactedDsn(socket_url)))
    if tcp_ident is None or socket_ident != tcp_ident:
        _skip_or_fail_unix_socket_lock(
            f"el socket Unix {socket_path} no es el mismo servidor que el DSN TCP "
            "(system_identifier distinto; limitación de topología)"
        )
    lock = _SessionAdvisoryLock(RedactedDsn(socket_url), _unique_lock_role("dsn_sock"))
    try:
        assert lock.start() is True
    finally:
        lock.stop()
    assert lock._thread.is_alive() is False


@pytest.mark.db
def test_session_lock_holds_with_query_user_password(
    require_postgres: RedactedDsn,
) -> None:
    """MEDIA 3: user/password en la query no deben romper el lock."""

    parsed = make_url(require_postgres.secret())
    query: dict[str, Any] = dict(parsed.query or {})

    def _query_first(key: str) -> str | None:
        raw = query.get(key)
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        return str(raw) if raw else None

    user = parsed.username or _query_first("user")
    password = parsed.password or _query_first("password")
    assert user and password
    query_url = (
        parsed.set(username=None, password=None)
        .update_query_dict({"user": user, "password": password})
        .render_as_string(hide_password=False)
    )
    lock = _SessionAdvisoryLock(RedactedDsn(query_url), _unique_lock_role("dsn_query"))
    try:
        assert lock.start() is True
    finally:
        lock.stop()


def test_own_schema_names_includes_public() -> None:
    """AG6: ``public`` está siempre, aunque no haya tablas con schema."""

    from app.migrations.autogen import own_schema_names

    assert "public" in own_schema_names()
