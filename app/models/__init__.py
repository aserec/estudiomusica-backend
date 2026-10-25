"""Modelos de persistencia SQLAlchemy 2 (async).

No hay tablas de negocio en este ticket. ``PersistentModel`` fija el
id UUID, ``created_at`` / ``updated_at`` y el borrado lógico ``deleted_at``.
"""

from app.models.base import Base, PersistentMixin, PersistentModel

__all__ = ["Base", "PersistentMixin", "PersistentModel"]
