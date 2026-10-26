"""CRUD de categorías de curso. Deniega por defecto en el router."""

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    CategoryNameTaken,
    CategoryNotEmpty,
    NotFound,
    TransientDbConflict,
    Validation,
    conflict_from_sqlstate,
    pg_sqlstate,
)
from app.models import Category
from app.schemas.categories import (
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    category_name_key,
    clip_to_grapheme_limit,
)

_NAME_CONSTRAINTS = (
    "uq_categories_root_name_active",
    "uq_categories_parent_id_name_key",
)
_TRANSIENT_ATTEMPTS = 3


def to_category_response(row: Category) -> CategoryResponse:
    return CategoryResponse(
        id=row.id,
        name=row.name,
        parent_id=row.parent_id,
        description=row.description,
        sort_order=row.sort_order,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _integrity_name(exc: IntegrityError) -> str:
    """``constraint_name`` de asyncpg en ``orig`` o en ``orig.__cause__``."""

    orig = exc.orig
    if orig is None:
        return ""
    name = getattr(orig, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    cause = getattr(orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    return ""


def _raise_from_integrity(exc: IntegrityError) -> NoReturn:
    if _integrity_name(exc) in _NAME_CONSTRAINTS:
        raise CategoryNameTaken() from exc
    raise exc


def _raise_from_db(exc: DBAPIError) -> NoReturn:
    """40P01/40001 → TransientDbConflict; el resto de IntegrityError como siempre."""

    mapped = conflict_from_sqlstate(pg_sqlstate(exc))
    if mapped is not None:
        raise mapped from exc
    if isinstance(exc, IntegrityError):
        _raise_from_integrity(exc)
    raise exc


async def _run_with_transient_retry[T](
    session: AsyncSession, operation: Callable[[], Awaitable[T]]
) -> T:
    """Reintenta la unidad de trabajo hasta 2 veces si Postgres aborta."""

    last: TransientDbConflict | None = None
    for attempt in range(_TRANSIENT_ATTEMPTS):
        try:
            return await operation()
        except TransientDbConflict as exc:
            last = exc
            await session.rollback()
            if attempt + 1 < _TRANSIENT_ATTEMPTS:
                await asyncio.sleep(0.01 + random.random() * 0.02)
    assert last is not None
    raise last


async def _lock_category(session: AsyncSession, category_id: UUID) -> Category | None:
    try:
        return await session.scalar(
            select(Category)
            .where(Category.id == category_id, Category.deleted_at.is_(None))
            .with_for_update()
        )
    except DBAPIError as exc:
        _raise_from_db(exc)


async def _lock_categories_ordered(
    session: AsyncSession, *category_ids: UUID
) -> dict[UUID, Category | None]:
    """FOR UPDATE en orden de UUID para no deadlock al mover dos raíces."""

    found: dict[UUID, Category | None] = {}
    try:
        self_id = category_ids[0] if category_ids else None
        parents = set(category_ids[1:])
        for category_id in sorted(set(category_ids)):
            share = category_id in parents and category_id != self_id
            found[category_id] = await session.scalar(
                select(Category)
                .where(Category.id == category_id, Category.deleted_at.is_(None))
                .with_for_update(read=share)
            )
    except DBAPIError as exc:
        _raise_from_db(exc)
    return found


async def _next_sort_order(session: AsyncSession, parent_id: UUID | None) -> int:
    try:
        query = select(func.coalesce(func.max(Category.sort_order), -1) + 1).where(
            Category.deleted_at.is_(None)
        )
        if parent_id is None:
            query = query.where(Category.parent_id.is_(None))
        else:
            query = query.where(Category.parent_id == parent_id)
        with session.no_autoflush:
            value = await session.scalar(query)
        return int(value or 0)
    except DBAPIError as exc:
        _raise_from_db(exc)


async def _name_taken(
    session: AsyncSession,
    name: str,
    parent_id: UUID | None,
    *,
    exclude_id: UUID | None = None,
) -> bool:
    query = select(Category.id).where(
        Category.deleted_at.is_(None),
        Category.name_key == category_name_key(name),
    )
    if parent_id is None:
        query = query.where(Category.parent_id.is_(None))
    else:
        query = query.where(Category.parent_id == parent_id)
    if exclude_id is not None:
        query = query.where(Category.id != exclude_id)
    try:
        with session.no_autoflush:
            return await session.scalar(query.limit(1)) is not None
    except DBAPIError as exc:
        _raise_from_db(exc)


async def _has_active_children(session: AsyncSession, category_id: UUID) -> bool:
    try:
        with session.no_autoflush:
            return (
                await session.scalar(
                    select(Category.id)
                    .where(
                        Category.parent_id == category_id,
                        Category.deleted_at.is_(None),
                    )
                    .limit(1)
                )
                is not None
            )
    except DBAPIError as exc:
        _raise_from_db(exc)


async def _assert_parent_is_root(
    session: AsyncSession,
    parent_id: UUID,
) -> Category:
    """Un solo nivel: el padre tiene que ser una raíz vigente."""

    try:
        with session.no_autoflush:
            parent = await session.scalar(
                select(Category)
                .where(Category.id == parent_id, Category.deleted_at.is_(None))
                .with_for_update(read=True)
            )
    except DBAPIError as exc:
        _raise_from_db(exc)
    return _parent_is_root(parent)


def _parent_is_root(
    parent: Category | None, category_id: UUID | None = None
) -> Category:
    if parent is None or parent.parent_id is not None:
        raise Validation(fields=["parentId"])
    if category_id is not None and parent.id == category_id:
        raise Validation(fields=["parentId"])
    return parent


async def list_categories(session: AsyncSession) -> list[Category]:
    try:
        result = await session.scalars(
            select(Category)
            .where(Category.deleted_at.is_(None))
            .order_by(Category.sort_order, Category.name, Category.id)
        )
        return list(result)
    except DBAPIError as exc:
        _raise_from_db(exc)


async def create_category(session: AsyncSession, body: CategoryCreate) -> Category:
    async def once() -> Category:
        try:
            if body.parent_id is not None:
                await _assert_parent_is_root(session, body.parent_id)
            if await _name_taken(session, body.name, body.parent_id):
                raise CategoryNameTaken()
            sort_order = (
                body.sort_order
                if body.sort_order is not None
                else await _next_sort_order(session, body.parent_id)
            )
            row = Category(
                name=body.name,
                name_key=category_name_key(body.name),
                parent_id=body.parent_id,
                description=body.description,
                sort_order=sort_order,
            )
            async with session.begin_nested():
                session.add(row)
                await session.flush()
            await session.refresh(row)
            return row
        except DBAPIError as exc:
            _raise_from_db(exc)

    return await _run_with_transient_retry(session, once)


async def update_category(
    session: AsyncSession, category_id: UUID, body: CategoryUpdate
) -> Category:
    fields = body.model_fields_set
    next_name_update = body.name
    next_sort: int | None = None
    if "name" in fields and next_name_update is None:
        raise Validation(fields=["name"])
    if "sort_order" in fields:
        if body.sort_order is None:
            raise Validation(fields=["sortOrder"])
        next_sort = body.sort_order
    changing_parent = "parent_id" in fields

    async def once() -> Category:
        try:
            parent: Category | None = None
            if changing_parent and body.parent_id is not None:
                locked = await _lock_categories_ordered(
                    session, category_id, body.parent_id
                )
                row = locked.get(category_id)
                parent = locked.get(body.parent_id)
            else:
                row = await _lock_category(session, category_id)
            if row is None:
                raise NotFound()
            next_name = row.name if next_name_update is None else next_name_update
            next_parent = row.parent_id
            if changing_parent:
                if body.parent_id is not None:
                    if await _has_active_children(session, category_id):
                        raise Validation(fields=["parentId"])
                    _parent_is_root(parent, category_id)
                next_parent = body.parent_id
            if await _name_taken(
                session, next_name, next_parent, exclude_id=category_id
            ):
                raise CategoryNameTaken()
            if "name" in fields:
                row.name = next_name
            row.name_key = category_name_key(next_name)
            if changing_parent:
                row.parent_id = next_parent
            if "description" in fields:
                row.description = body.description
            if next_sort is not None:
                row.sort_order = next_sort
            async with session.begin_nested():
                await session.flush()
            await session.refresh(row)
            return row
        except DBAPIError as exc:
            _raise_from_db(exc)

    return await _run_with_transient_retry(session, once)


async def delete_category(session: AsyncSession, category_id: UUID) -> None:
    async def once() -> None:
        try:
            row = await _lock_category(session, category_id)
            if row is None:
                raise NotFound()
            if await _has_active_children(session, category_id):
                raise CategoryNotEmpty()
            row.deleted_at = datetime.now(UTC)
            await session.flush()
        except DBAPIError as exc:
            _raise_from_db(exc)

    await _run_with_transient_retry(session, once)


@dataclass(frozen=True)
class NameKeyMismatch:
    id: UUID
    name: str
    stored: str
    expected: str


@dataclass(frozen=True)
class NameKeyCollision:
    key: str
    parent_id: UUID | None
    ids: tuple[UUID, ...]


@dataclass(frozen=True)
class NameKeyAudit:
    mismatches: tuple[NameKeyMismatch, ...]
    collisions: tuple[NameKeyCollision, ...]

    def ok(self) -> bool:
        return not self.mismatches and not self.collisions


async def audit_category_name_keys(session: AsyncSession) -> NameKeyAudit:
    """Recalcula name_key de todas las filas; avisa diferencias y colisiones."""

    rows = list(await session.scalars(select(Category)))
    mismatches: list[NameKeyMismatch] = []
    live: list[Category] = []
    for row in rows:
        expected = category_name_key(row.name)
        if row.name_key != expected:
            mismatches.append(
                NameKeyMismatch(
                    id=row.id,
                    name=row.name,
                    stored=row.name_key,
                    expected=expected,
                )
            )
        if row.deleted_at is None:
            live.append(row)
    folded = await _citext_fold(session, [category_name_key(row.name) for row in live])
    groups: dict[tuple[UUID | None, str], list[UUID]] = {}
    for row, key in zip(live, folded, strict=True):
        groups.setdefault((row.parent_id, key), []).append(row.id)
    collisions = tuple(
        NameKeyCollision(key=key, parent_id=parent_id, ids=tuple(ids))
        for (parent_id, key), ids in groups.items()
        if len(ids) > 1
    )
    return NameKeyAudit(mismatches=tuple(mismatches), collisions=collisions)


async def _citext_fold(session: AsyncSession, values: Sequence[str]) -> list[str]:
    """``lower(citext)`` de Postgres: el mismo criterio que el índice único."""

    if not values:
        return []
    result = await session.execute(
        text(
            "SELECT lower(u::citext) FROM unnest(CAST(:vals AS text[])) "
            "WITH ORDINALITY AS t(u, ord) ORDER BY ord"
        ),
        {"vals": list(values)},
    )
    return [row[0] for row in result]


def _dup_name(name: str, number: int) -> str:
    suffix = f" (dup {number})"
    budget = 120 - len(suffix)
    if budget < 0:
        return suffix[:120]
    if len(name) <= budget:
        return name.rstrip() + suffix
    return clip_to_grapheme_limit(name, budget).rstrip() + suffix


async def fix_category_name_keys(session: AsyncSession) -> NameKeyAudit:
    """Renombra duplicados con `` (dup N)`` y recalcula name_key.

    Una sola transacción: el caller hace commit (el script usa ``begin()``).
    """

    await session.execute(text("LOCK TABLE categories IN EXCLUSIVE MODE"))
    rows = list(await session.scalars(select(Category).order_by(Category.id)))
    live = [row for row in rows if row.deleted_at is None]
    folded = await _citext_fold(session, [category_name_key(row.name) for row in live])
    groups: dict[tuple[UUID | None, str], list[Category]] = {}
    for row, key in zip(live, folded, strict=True):
        groups.setdefault((row.parent_id, key), []).append(row)
    taken: dict[tuple[UUID | None, str], Category] = {}
    for (parent_id, key), members in groups.items():
        members.sort(
            key=lambda item: (
                item.sort_order,
                category_name_key(item.name) != item.name,
                item.id,
            )
        )
        taken[(parent_id, key)] = members[0]
    for members in groups.values():
        if len(members) < 2:
            continue
        planned: list[str] = []
        for number, row in enumerate(members[1:], start=1):
            planned.extend(
                _dup_name(row.name, suffix) for suffix in range(number, number + 8)
            )
        folded_plan = await _citext_fold(
            session, [category_name_key(name) for name in planned]
        )
        fold_of = dict(zip(planned, folded_plan, strict=True))
        for number, row in enumerate(members[1:], start=1):
            suffix = number
            while True:
                candidate = _dup_name(row.name, suffix)
                if candidate not in fold_of:
                    fold_of[candidate] = (
                        await _citext_fold(session, [category_name_key(candidate)])
                    )[0]
                new_key = fold_of[candidate]
                clash = taken.get((row.parent_id, new_key))
                if clash is None:
                    row.name = candidate
                    taken[(row.parent_id, new_key)] = row
                    break
                suffix += 1
    changing: list[tuple[Category, str]] = []
    for row in rows:
        expected = category_name_key(row.name)
        if row.name_key != expected:
            changing.append((row, expected))
    if changing:
        try:
            for row, _expected in changing:
                row.name_key = str(row.id)
            await session.flush()
            for row, expected in changing:
                row.name_key = expected
            await session.flush()
        except DBAPIError as exc:
            _raise_from_db(exc)
    return await audit_category_name_keys(session)
