"""CRUD de categorías de curso. Deniega por defecto en el router."""

from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CategoryNameTaken, CategoryNotEmpty, NotFound, Validation
from app.models import Category
from app.schemas.categories import (
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    category_name_key,
)

# Un solo candado transaccional para todo el árbol (ticket #400).
_CATEGORY_TREE_LOCK_KEY = (400, 1)
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


async def _lock_category_tree(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:classid, :objid)"),
        {
            "classid": _CATEGORY_TREE_LOCK_KEY[0],
            "objid": _CATEGORY_TREE_LOCK_KEY[1],
        },
    )


async def _lock_category(session: AsyncSession, category_id: UUID) -> Category | None:
    return await session.scalar(
        select(Category)
        .where(Category.id == category_id, Category.deleted_at.is_(None))
        .with_for_update()
    )


async def _next_sort_order(session: AsyncSession, parent_id: UUID | None) -> int:
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
    with session.no_autoflush:
        return await session.scalar(query.limit(1)) is not None


async def _has_active_children(session: AsyncSession, category_id: UUID) -> bool:
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


async def _assert_parent_is_root(
    session: AsyncSession,
    parent_id: UUID,
    *,
    category_id: UUID | None = None,
) -> Category:
    """Un solo nivel: el padre tiene que ser una raíz vigente, no uno mismo."""

    if category_id is not None and parent_id == category_id:
        raise Validation(fields=["parentId"])
    with session.no_autoflush:
        parent = await session.scalar(
            select(Category)
            .where(Category.id == parent_id, Category.deleted_at.is_(None))
            .with_for_update(read=True)
        )
    if parent is None or parent.parent_id is not None:
        raise Validation(fields=["parentId"])
    return parent


async def list_categories(session: AsyncSession) -> list[Category]:
    result = await session.scalars(
        select(Category)
        .where(Category.deleted_at.is_(None))
        .order_by(Category.sort_order, Category.name, Category.id)
    )
    return list(result)


async def create_category(session: AsyncSession, body: CategoryCreate) -> Category:
    if body.parent_id is not None:
        await _lock_category_tree(session)
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
    fields = body.model_fields_set
    next_name_update = body.name
    next_sort = body.sort_order
    if "name" in fields and next_name_update is None:
        raise Validation(fields=["name"])
    if "sort_order" in fields and next_sort is None:
        raise Validation(fields=["sortOrder"])
    changing_parent = "parent_id" in fields
    if changing_parent:
        await _lock_category_tree(session)
    row = await _lock_category(session, category_id)
    if row is None:
        raise NotFound()
    next_name = next_name_update if "name" in fields else row.name
    if next_name is None:
        raise Validation(fields=["name"])
    next_parent = row.parent_id
    if changing_parent:
        if body.parent_id is not None:
            if await _has_active_children(session, category_id):
                raise Validation(fields=["parentId"])
            await _assert_parent_is_root(
                session, body.parent_id, category_id=category_id
            )
        next_parent = body.parent_id
    if await _name_taken(session, next_name, next_parent, exclude_id=category_id):
        raise CategoryNameTaken()
    if "name" in fields:
        row.name = next_name
    if changing_parent:
        row.parent_id = next_parent
    if "description" in fields:
        row.description = body.description
    if "sort_order" in fields and next_sort is not None:
        row.sort_order = next_sort
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        _raise_from_integrity(exc)
    await session.refresh(row)
    return row


async def delete_category(session: AsyncSession, category_id: UUID) -> None:
    await _lock_category_tree(session)
    row = await _lock_category(session, category_id)
    if row is None:
        raise NotFound()
    if await _has_active_children(session, category_id):
        raise CategoryNotEmpty()
    row.deleted_at = datetime.now(UTC)
    await session.flush()
