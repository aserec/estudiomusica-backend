import unicodedata
import uuid

from pydantic import EmailStr, Field, field_validator

from app.models.user import SystemRole
from app.schemas.base import APIModel


def _is_unicode_noncharacter(code: int) -> bool:
    return 0xFDD0 <= code <= 0xFDEF or (code & 0xFFFE) == 0xFFFE


def reject_disallowed_text(value: object) -> object:
    """Rechaza controles (Cc, incluido NUL) y no-caracteres Unicode."""

    if not isinstance(value, str):
        return value
    for char in value:
        if unicodedata.category(char) == "Cc":
            raise ValueError("control characters not allowed")
        if _is_unicode_noncharacter(ord(char)):
            raise ValueError("noncharacter not allowed")
    return value


def reject_nul(value: object) -> object:
    """Postgres rechaza ``\\u0000`` en texto (UTF8 0x00 → 500)."""

    return reject_disallowed_text(value)


def normalize_email(value: object) -> object:
    value = reject_nul(value)
    if isinstance(value, str):
        return value.strip().lower()
    return value


class SignUpRequest(APIModel):
    name: str = Field(min_length=1, max_length=161)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128, repr=False)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        reject_nul(value)
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return stripped

    @field_validator("email", mode="before")
    @classmethod
    def normalize_signup_email(cls, value: object) -> object:
        return normalize_email(value)

    @field_validator("password", mode="before")
    @classmethod
    def reject_nul_signup_password(cls, value: object) -> object:
        return reject_nul(value)


class LogInRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128, repr=False)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_login_email(cls, value: object) -> object:
        return normalize_email(value)

    @field_validator("password", mode="before")
    @classmethod
    def reject_nul_login_password(cls, value: object) -> object:
        return reject_nul(value)


class PublicUser(APIModel):
    """Usuario público. El frontend privado no está disponible; ver la PR."""

    id: uuid.UUID
    name: str
    email: EmailStr
    first_name: str
    last_name: str
    avatar_file_id: uuid.UUID | None = None
    email_notifications_enabled: bool


class AuthSessionResponse(APIModel):
    user: PublicUser


class AuthMeResponse(APIModel):
    user: PublicUser
    system_role: SystemRole
    is_admin: bool
