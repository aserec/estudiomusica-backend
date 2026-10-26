import unicodedata
import uuid

from pydantic import EmailStr, Field, field_validator

from app.models.user import SystemRole
from app.schemas.base import APIModel


def normalize_email(value: object) -> object:
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
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return unicodedata.normalize("NFC", stripped)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_signup_email(cls, value: object) -> object:
        return normalize_email(value)


class LogInRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128, repr=False)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_login_email(cls, value: object) -> object:
        return normalize_email(value)


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
