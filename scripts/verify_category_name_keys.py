#!/usr/bin/env python3
"""Recalcula name_key de categories y avisa diferencias o colisiones.

Uso:
  uv run python scripts/verify_category_name_keys.py
  uv run python scripts/verify_category_name_keys.py --fix

``--fix`` renombra duplicados con el sufijo `` (dup N)`` y recalcula las
claves en una sola transacción. Lee DATABASE_URL (pydantic-settings).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


async def _run(*, fix: bool) -> int:
    from app.config import get_settings
    from app.services.categories import audit_category_name_keys, fix_category_name_keys

    settings = get_settings()
    engine = create_async_engine(settings.resolved_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            assert isinstance(session, AsyncSession)
            if fix:
                async with session.begin():
                    report = await fix_category_name_keys(session)
            else:
                report = await audit_category_name_keys(session)
    finally:
        await engine.dispose()
    for item in report.mismatches:
        print(
            f"DIFF {item.id} stored={item.stored!r} expected={item.expected!r} "
            f"name={item.name!r}"
        )
    for item in report.collisions:
        print(f"COLLISION parent={item.parent_id} key={item.key!r} ids={item.ids}")
    if report.ok():
        print("name_key: ok")
        return 0
    print(
        f"name_key: {len(report.mismatches)} diferencias, "
        f"{len(report.collisions)} colisiones"
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fix",
        action="store_true",
        help='Renombra duplicados con " (dup N)" y recalcula name_key',
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(fix=args.fix)))


if __name__ == "__main__":
    main()
