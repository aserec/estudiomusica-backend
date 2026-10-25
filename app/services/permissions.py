"""Resolución de permisos de curso. Se reevalúa en cada petición."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourseMembership, User
from app.permissions.types import CourseRole


async def load_course_role(
    session: AsyncSession, user_id: UUID, course_id: UUID
) -> CourseRole | None:
    membership = await session.scalar(
        select(CourseMembership).where(
            CourseMembership.user_id == user_id,
            CourseMembership.course_id == course_id,
            CourseMembership.deleted_at.is_(None),
        )
    )
    if membership is None:
        return None
    return CourseRole(membership.course_role)


async def load_user_course_context(
    session: AsyncSession, user: User, course_id: UUID
) -> CourseRole | None:
    return await load_course_role(session, user.id, course_id)
