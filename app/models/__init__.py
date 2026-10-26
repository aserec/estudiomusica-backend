"""Modelos de persistencia SQLAlchemy 2 (async)."""

from app.models.auth_session import AuthSession
from app.models.base import (
    Base,
    PersistentMixin,
    PersistentModel,
    attach_updated_at_trigger,
    qualify_table,
    quote_ident,
    updated_at_drop_trigger_sql,
    updated_at_trigger_name,
    updated_at_trigger_sql,
)
from app.models.category import Category
from app.models.course_membership import CourseMembership
from app.models.system_role import SystemRoleRow
from app.models.user import SystemRole, User

__all__ = [
    "AuthSession",
    "Base",
    "Category",
    "CourseMembership",
    "PersistentMixin",
    "PersistentModel",
    "SystemRole",
    "SystemRoleRow",
    "User",
    "attach_updated_at_trigger",
    "qualify_table",
    "quote_ident",
    "updated_at_drop_trigger_sql",
    "updated_at_trigger_name",
    "updated_at_trigger_sql",
]
