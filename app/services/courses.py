"""CRUD de cursos. Deniega por defecto; el router aplica la matriz de #94."""

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import (
    AccessDenied,
    CourseArchived,
    DuplicateShortName,
    NotFound,
    Validation,
)
from app.models import Category, Course, User
from app.models.course import CourseLevel, DefaultGroupMode
from app.permissions.matrix import effective_permissions
from app.permissions.types import Permission
from app.schemas.courses import (
    CourseArchive,
    CourseCreate,
    CourseDuplicate,
    CourseResponse,
    CourseUpdate,
)
from app.schemas.pagination import Paginated, PaginationQuery
from app.services.permissions import load_user_course_context

_SHORT_NAME_CONSTRAINTS = ("uq_courses_short_name_active",)


def to_course_response(row: Course) -> CourseResponse:
    return CourseResponse(
        id=row.id,
        short_name=row.short_name,
        name=row.name,
        summary=row.summary,
        category_id=row.category_id,
        start_date=row.start_date,
        end_date=row.end_date,
        visible=row.visible,
        level=CourseLevel(row.level),
        default_group_mode=DefaultGroupMode(row.default_group_mode),
        deleted_at=row.deleted_at,
        archived_at=row.archived_at,
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
    if any(item in name for item in _SHORT_NAME_CONSTRAINTS):
        raise DuplicateShortName() from exc
    if "end_not_before_start" in name:
        raise Validation(fields=["endDate"]) from exc
    if "category_id" in name:
        raise Validation(fields=["categoryId"]) from exc
    if "ck_courses_level" in name or name.endswith("_level"):
        raise Validation(fields=["level"]) from exc
    if "default_group_mode" in name:
        raise Validation(fields=["defaultGroupMode"]) from exc
    raise DuplicateShortName() from exc


def _assert_date_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise Validation(fields=["endDate"])


def _ilike_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _lock_course(session: AsyncSession, course_id: UUID) -> Course | None:
    return await session.scalar(
        select(Course)
        .where(Course.id == course_id, Course.deleted_at.is_(None))
        .with_for_update()
    )


async def _assert_category(
    session: AsyncSession, category_id: UUID | None
) -> Category | None:
    if category_id is None:
        return None
    category = await session.scalar(
        select(Category)
        .where(Category.id == category_id, Category.deleted_at.is_(None))
        .with_for_update()
    )
    if category is None:
        raise Validation(fields=["categoryId"])
    return category


async def _short_name_taken(
    session: AsyncSession,
    short_name: str,
    *,
    exclude_id: UUID | None = None,
) -> bool:
    query = select(Course.id).where(
        Course.deleted_at.is_(None),
        Course.short_name == short_name,
    )
    if exclude_id is not None:
        query = query.where(Course.id != exclude_id)
    return await session.scalar(query.limit(1)) is not None


async def assert_course_permission(
    session: AsyncSession,
    user: User,
    course: Course,
    permission: Permission,
) -> None:
    """Deniega por defecto. Curso oculto sin ``course:view`` → 404."""

    course_role = await load_user_course_context(session, user, course.id)
    granted = effective_permissions(
        user.system_role, course_role, settings=get_settings()
    )
    if Permission.COURSE_VIEW not in granted:
        if not course.visible:
            raise NotFound()
        raise AccessDenied()
    if permission not in granted:
        raise AccessDenied()


async def get_course_for_view(
    session: AsyncSession, user: User, course_id: UUID
) -> Course:
    course = await session.scalar(
        select(Course).where(Course.id == course_id, Course.deleted_at.is_(None))
    )
    if course is None:
        raise NotFound()
    await assert_course_permission(session, user, course, Permission.COURSE_VIEW)
    return course


async def list_admin_courses(
    session: AsyncSession,
    pagination: PaginationQuery,
    *,
    query: str | None = None,
    archived: bool | None = None,
) -> Paginated[CourseResponse]:
    filters: list[ColumnElement[bool]] = [Course.deleted_at.is_(None)]
    if archived is True:
        filters.append(Course.archived_at.is_not(None))
    elif archived is False:
        filters.append(Course.archived_at.is_(None))
    needle = query.strip() if query else ""
    if needle:
        pattern = _ilike_pattern(needle)
        filters.append(
            or_(
                Course.short_name.ilike(pattern, escape="\\"),
                Course.name.ilike(pattern, escape="\\"),
            )
        )
    total = await session.scalar(
        select(func.count()).select_from(Course).where(*filters)
    )
    offset = (pagination.page - 1) * pagination.page_size
    rows = await session.scalars(
        select(Course)
        .where(*filters)
        .order_by(Course.name, Course.id)
        .offset(offset)
        .limit(pagination.page_size)
    )
    return Paginated(
        items=[to_course_response(row) for row in rows],
        page=pagination.page,
        page_size=pagination.page_size,
        total=int(total or 0),
    )


async def create_course(session: AsyncSession, body: CourseCreate) -> Course:
    _assert_date_range(body.start_date, body.end_date)
    await _assert_category(session, body.category_id)
    if await _short_name_taken(session, body.short_name):
        raise DuplicateShortName()
    row = Course(
        short_name=body.short_name,
        name=body.name,
        summary=body.summary,
        category_id=body.category_id,
        start_date=body.start_date,
        end_date=body.end_date,
        visible=body.visible,
        level=body.level,
        default_group_mode=body.default_group_mode,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        _raise_from_integrity(exc)
    await session.refresh(row)
    return row


async def update_course(
    session: AsyncSession, user: User, course_id: UUID, body: CourseUpdate
) -> Course:
    _assert_date_range(body.start_date, body.end_date)
    row = await _lock_course(session, course_id)
    if row is None:
        raise NotFound()
    await assert_course_permission(session, user, row, Permission.COURSE_TEACH)
    if row.archived_at is not None:
        raise CourseArchived()
    await _assert_category(session, body.category_id)
    if await _short_name_taken(session, body.short_name, exclude_id=course_id):
        raise DuplicateShortName()
    row.short_name = body.short_name
    row.name = body.name
    row.summary = body.summary
    row.category_id = body.category_id
    row.start_date = body.start_date
    row.end_date = body.end_date
    row.visible = body.visible
    row.level = body.level
    row.default_group_mode = body.default_group_mode
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        _raise_from_integrity(exc)
    await session.refresh(row)
    return row


async def set_course_archived(
    session: AsyncSession, user: User, course_id: UUID, body: CourseArchive
) -> Course:
    row = await _lock_course(session, course_id)
    if row is None:
        raise NotFound()
    await assert_course_permission(session, user, row, Permission.COURSE_TEACH)
    if body.archived:
        if row.archived_at is None:
            row.archived_at = datetime.now(UTC)
    else:
        row.archived_at = None
    await session.flush()
    await session.refresh(row)
    return row


async def copy_course_contents(
    session: AsyncSession, source: Course, target: Course
) -> None:
    """Punto de extensión para #402 (secciones) y siguientes (actividades).

    Hoy no hay tablas de contenido. El duplicado copia solo la ficha
    del curso: sin matrículas ni notas. #402+ rellenarán este hook.
    """

    _ = (session, source, target)


async def duplicate_course(
    session: AsyncSession, course_id: UUID, body: CourseDuplicate
) -> Course:
    source = await _lock_course(session, course_id)
    if source is None:
        raise NotFound()
    if await _short_name_taken(session, body.short_name):
        raise DuplicateShortName()
    end_date = source.end_date
    if end_date is not None and end_date < body.start_date:
        end_date = None
    row = Course(
        short_name=body.short_name,
        name=body.name,
        summary=source.summary,
        category_id=source.category_id,
        start_date=body.start_date,
        end_date=end_date,
        visible=source.visible,
        level=source.level,
        default_group_mode=source.default_group_mode,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        _raise_from_integrity(exc)
    await copy_course_contents(session, source, row)
    await session.refresh(row)
    return row


async def delete_course(session: AsyncSession, course_id: UUID) -> None:
    row = await _lock_course(session, course_id)
    if row is None:
        raise NotFound()
    row.deleted_at = datetime.now(UTC)
    await session.flush()
