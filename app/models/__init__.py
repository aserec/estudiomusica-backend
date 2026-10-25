"""Modelos de persistencia SQLAlchemy 2 (async).

No hay tablas de negocio en este ticket. ``PersistentModel`` fija el
id UUID, ``created_at`` / ``updated_at`` y el borrado lógico ``deleted_at``.
"""

from app.models.base import (
    Base,
    PersistentMixin,
    PersistentModel,
    attach_updated_at_trigger,
    updated_at_trigger_name,
    updated_at_trigger_sql,
)

__all__ = [
    "Base",
    "PersistentMixin",
    "PersistentModel",
    "attach_updated_at_trigger",
    "updated_at_trigger_name",
    "updated_at_trigger_sql",
]
