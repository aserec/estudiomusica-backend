import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import EmailStr, Field, StrictBool, field_validator, model_validator

from app.schemas.auth import PublicUser, normalize_email
from app.schemas.base import APIModel


class AdminUserStatus(StrEnum):
    """Filtro de listado. Enums en snake_case (convención #91)."""

    ALL = "all"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class AdminUserView(PublicUser):
    """PublicUser más metadatos de administración. Sin password hash."""

    suspended_at: datetime | None = None
    deleted_at: datetime | None = None
    system_role_id: str


class AdminUserIdentityUpdate(APIModel):
    """Edición de identidad. PUT parcial: al menos un campo."""

    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    email: EmailStr | None = None

    @field_validator("first_name")
    @classmethod
    def strip_first_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("firstName required")
        return stripped

    @field_validator("last_name")
    @classmethod
    def strip_last_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("email", mode="before")
    @classmethod
    def normalize_identity_email(cls, value: object) -> object:
        return normalize_email(value)

    @model_validator(mode="after")
    def require_one_field(self) -> AdminUserIdentityUpdate:
        if self.first_name is None and self.last_name is None and self.email is None:
            raise ValueError("firstName, lastName or email required")
        return self


class AdminFlagUpdate(APIModel):
    is_admin: StrictBool


class SuspensionUpdate(APIModel):
    suspended: StrictBool


class UserSearchHit(APIModel):
    """Resultado de búsqueda para matricular. Sin hashes ni fechas internas."""

    id: uuid.UUID
    name: str
    email: EmailStr | None = None
    first_name: str
    last_name: str
