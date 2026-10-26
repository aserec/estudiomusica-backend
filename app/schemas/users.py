import unicodedata
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import EmailStr, Field, StrictBool, field_validator, model_validator

from app.models.user import PERSON_NAME_MAX
from app.schemas.auth import (
    PERSON_NAME_RAW_MAX,
    PublicUser,
    normalize_email,
)
from app.schemas.base import APIModel


def reject_nul(value: str | None) -> str | None:
    """Postgres rechaza U+0000 en texto; FastAPI lo convertiría en 500."""

    if value is not None and "\x00" in value:
        raise ValueError("must not contain NUL")
    return value


# varchar(80) guarda NFC. El tope semántico es 80 *después* de NFC: U+0958
# (y otras exclusiones de composición) se alarga (1→2), y NFD como
# ``e\u0301`` se acorta. ``Field(max_length=80)`` sobre el crudo rechazaría
# 41×``é`` (cabe) y dejaría pasar 80×U+0958 (160 NFC → 500 en Postgres).
def _normalize_person_name(value: str, *, required_message: str | None) -> str:
    reject_nul(value)
    stripped = value.strip()
    if required_message is not None and not stripped:
        raise ValueError(required_message)
    normalized = unicodedata.normalize("NFC", stripped)
    if len(normalized) > PERSON_NAME_MAX:
        raise ValueError("too long")
    return normalized


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

    first_name: str | None = Field(default=None, max_length=PERSON_NAME_RAW_MAX)
    last_name: str | None = Field(default=None, max_length=PERSON_NAME_RAW_MAX)
    email: EmailStr | None = None

    @field_validator("first_name")
    @classmethod
    def strip_first_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_person_name(value, required_message="firstName required")

    @field_validator("last_name")
    @classmethod
    def strip_last_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_person_name(value, required_message=None)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_identity_email(cls, value: object) -> object:
        if isinstance(value, str):
            reject_nul(value)
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
