"""Modelos de persistencia SQLAlchemy 2 (async)."""

from app.models.auth_session import AuthSession
from app.models.base import (
    Base,
    PersistentMixin,
    PersistentModel,
    attach_updated_at_trigger,
    updated_at_trigger_name,
    updated_at_trigger_sql,
)
from app.models.course_membership import CourseMembership
from app.models.system_role import SystemRoleRow
from app.models.user import SystemRole, User

__all__ = [
    "AuthSession",
    "Base",
    "CourseMembership",
    "PersistentMixin",
    "PersistentModel",
    "SystemRole",
    "SystemRoleRow",
    "User",
    "attach_updated_at_trigger",
    "updated_at_trigger_name",
    "updated_at_trigger_sql",
]
