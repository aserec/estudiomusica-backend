"""CRUD de categorías de curso. Deniega por defecto en el router."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    CategoryCycle,
    CategoryNameTaken,
    CategoryNotEmpty,
    NotFound,
    Validation,
)
from app.models import Category, Course
from app.schemas.categories import CategoryCreate, CategoryResponse, CategoryUpdate

_MAX_ANCESTOR_WALK = 64
_NAME_CONSTRAINTS = (
    "uq_categories_root_name_active",
    "uq_categories_parent_name_active",
)


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
    orig = exc.orig
    constraint = getattr(orig, "constraint_name", None)
    if isinstance(constraint, str) and constraint:
        return constraint
    diag = getattr(orig, "diag", None)
    if diag is not None:
        name = getattr(diag, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
    return str(orig) if orig is not None else str(exc)


def _raise_from_integrity(exc: IntegrityError) -> None:
    name = _integrity_name(exc)
    if any(item in name for item in _NAME_CONSTRAINTS):
        raise CategoryNameTaken() from exc
    if "parent_not_self" in name:
        raise CategoryCycle() from exc
    if "parent_id" in name:
        raise Validation(fields=["parentId"]) from exc
    raise CategoryNameTaken() from exc


async def _lock_category(session: AsyncSession, category_id: UUID) -> Category | None:
    return await session.scalar(
        select(Category)
        .where(Category.id == category_id, Category.deleted_at.is_(None))
        .with_for_update()
    )


async def _lock_categories(
    session: AsyncSession, *category_ids: UUID | None
) -> dict[UUID, Category]:
    unique = sorted({item for item in category_ids if item is not None})
    locked: dict[UUID, Category] = {}
    for category_id in unique:
        row = await _lock_category(session, category_id)
        if row is not None:
            locked[category_id] = row
    return locked


async def _next_sort_order(session: AsyncSession, parent_id: UUID | None) -> int:
    query = select(func.coalesce(func.max(Category.sort_order), -1) + 1).where(
        Category.deleted_at.is_(None)
    )
    if parent_id is None:
        query = query.where(Category.parent_id.is_(None))
    else:
        query = query.where(Category.parent_id == parent_id)
    value = await session.scalar(query)
    return int(value or 0)


async def _name_taken(
    session: AsyncSession,
    name: str,
    parent_id: UUID | None,
    *,
    exclude_id: UUID | None = None,
) -> bool:
    query = select(Category.id).where(
        Category.deleted_at.is_(None),
        Category.name == name,
    )
    if parent_id is None:
        query = query.where(Category.parent_id.is_(None))
    else:
        query = query.where(Category.parent_id == parent_id)
    if exclude_id is not None:
        query = query.where(Category.id != exclude_id)
    return await session.scalar(query.limit(1)) is not None


async def _assert_parent(
    session: AsyncSession, parent_id: UUID | None
) -> Category | None:
    if parent_id is None:
        return None
    parent = await _lock_category(session, parent_id)
    if parent is None:
        raise Validation(fields=["parentId"])
    return parent


async def _would_cycle(
    session: AsyncSession, category_id: UUID, new_parent_id: UUID | None
) -> bool:
    if new_parent_id is None:
        return False
    if new_parent_id == category_id:
        return True
    current: UUID | None = new_parent_id
    seen: set[UUID] = set()
    for _ in range(_MAX_ANCESTOR_WALK):
        if current is None:
            return False
        if current == category_id or current in seen:
            return True
        seen.add(current)
        row = await session.get(Category, current)
        if row is None or row.deleted_at is not None:
            return False
        current = row.parent_id
    return True


async def _has_active_children(session: AsyncSession, category_id: UUID) -> bool:
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


async def _has_courses(session: AsyncSession, category_id: UUID) -> bool:
    return (
        await session.scalar(
            select(Course.id)
            .where(
                Course.category_id == category_id,
                Course.deleted_at.is_(None),
            )
            .limit(1)
        )
        is not None
    )


async def list_categories(session: AsyncSession) -> list[Category]:
    result = await session.scalars(
        select(Category)
        .where(Category.deleted_at.is_(None))
        .order_by(Category.sort_order, Category.name, Category.id)
    )
    return list(result)


async def create_category(session: AsyncSession, body: CategoryCreate) -> Category:
    await _assert_parent(session, body.parent_id)
    if await _name_taken(session, body.name, body.parent_id):
        raise CategoryNameTaken()
    sort_order = (
        body.sort_order
        if body.sort_order is not None
        else await _next_sort_order(session, body.parent_id)
    )
    row = Category(
        name=body.name,
        parent_id=body.parent_id,
        description=body.description,
        sort_order=sort_order,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        _raise_from_integrity(exc)
    await session.refresh(row)
    return row


async def update_category(
    session: AsyncSession, category_id: UUID, body: CategoryUpdate
) -> Category:
    locked = await _lock_categories(session, category_id, body.parent_id)
    row = locked.get(category_id)
    if row is None:
        raise NotFound()
    if body.parent_id is not None and body.parent_id not in locked:
        raise Validation(fields=["parentId"])
    if await _would_cycle(session, category_id, body.parent_id):
        raise CategoryCycle()
    if await _name_taken(session, body.name, body.parent_id, exclude_id=category_id):
        raise CategoryNameTaken()
    row.name = body.name
    row.parent_id = body.parent_id
    row.description = body.description
    if body.sort_order is not None:
        row.sort_order = body.sort_order
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        _raise_from_integrity(exc)
    await session.refresh(row)
    return row


async def delete_category(session: AsyncSession, category_id: UUID) -> None:
    row = await _lock_category(session, category_id)
    if row is None:
        raise NotFound()
    if await _has_active_children(session, category_id) or await _has_courses(
        session, category_id
    ):
        raise CategoryNotEmpty()
    row.deleted_at = datetime.now(UTC)
    await session.flush()
