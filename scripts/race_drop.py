"""Dos procesos borran a la vez roles ``em_prod_tests_*`` huérfanos.

Uso:
  python scripts/race_drop.py [N]   planta N huérfanos y lanza dos drops
  python scripts/race_drop.py --drop-only
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _admin_dsn() -> object:
    from app.db import RedactedDsn
    from tests.conftest import resolve_test_database_url

    return RedactedDsn(resolve_test_database_url())


def _drop_only() -> None:
    from tests.conftest import drop_production_test_role

    drop_production_test_role(_admin_dsn())


def _plant(count: int) -> list[str]:
    from sqlalchemy import text

    from tests.conftest import _admin_engine, _pg_ident, _pg_literal

    token = secrets.token_hex(4)
    names = [
        f"em_prod_tests_{os.getpid()}_race_{token}_{index}"
        for index in range(1, count + 1)
    ]
    password = os.environ.get("EM_RACE_DROP_PASSWORD") or secrets.token_urlsafe(24)
    admin = _admin_dsn()

    async def plant() -> None:
        engine = _admin_engine(admin)
        try:
            async with engine.connect() as connection:
                for name in names:
                    role = _pg_ident(name)
                    exists = await connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
                        {"name": name},
                    )
                    if exists is not None:
                        continue
                    await connection.execute(
                        text(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER "
                            f"PASSWORD {_pg_literal(password)}"
                        )
                    )
                    await connection.execute(
                        text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
                    )
        finally:
            await engine.dispose()

    asyncio.run(plant())
    return names


def _leftover_planted(names: list[str]) -> list[str]:
    from sqlalchemy import text

    from tests.conftest import _admin_engine

    admin = _admin_dsn()

    async def listed() -> list[str]:
        engine = _admin_engine(admin)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT rolname FROM pg_roles "
                            "WHERE rolname LIKE 'em_prod_tests\\_%' "
                            "ESCAPE '\\'"
                        )
                    )
                ).all()
        finally:
            await engine.dispose()
        planted = set(names)
        return [name for (name,) in rows if name in planted]

    return asyncio.run(listed())


def _run_pair() -> tuple[list[int], str]:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    outputs: list[str] = []
    codes: list[int] = []
    workers = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--drop-only"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    for worker in workers:
        out, _ = worker.communicate(timeout=90)
        outputs.append(out)
        codes.append(int(worker.returncode if worker.returncode is not None else 1))
    return codes, "".join(outputs)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--drop-only":
        _drop_only()
        return 0
    count = int(argv[0]) if argv else int(os.environ.get("EM_RACE_DROP_N", "5"))
    if count < 0:
        raise SystemExit("N debe ser >= 0")
    planted = _plant(count) if count else []
    codes, output = _run_pair()
    leftovers = _leftover_planted(planted) if planted else []
    print(
        f"exits={codes} leftovers={len(leftovers)} planted={len(planted)}",
        flush=True,
    )
    if output.strip():
        print(output, end="", flush=True)
    if codes != [0, 0] or leftovers:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
