"""Usuario persistido. El rol de sistema vive en ``system_role_id``."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import PersistentModel


class SystemRole(StrEnum):
    """Roles de sistema. Enums en snake_case (convención #91)."""

    ALUMNO = "alumno"
    PROFESOR = "profesor"
    PROFESOR_ADJUNTO = "profesor_adjunto"
    ADMIN = "admin"


class User(PersistentModel):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "is_admin = (system_role_id = 'admin')",
            name="is_admin_matches_system_role",
        ),
    )

    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        server_default=text("''"),
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    system_role_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("system_roles.id"),
        nullable=False,
        default=SystemRole.ALUMNO,
        server_default=text("'alumno'"),
        index=True,
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    email_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_site_admin(self) -> bool:
        """Fuente de verdad de ``isAdmin``: el rol, no la columna ``is_admin``."""

        return self.system_role_id == SystemRole.ADMIN

    @property
    def system_role(self) -> SystemRole:
        try:
            return SystemRole(self.system_role_id)
        except ValueError:
            return SystemRole.ALUMNO
