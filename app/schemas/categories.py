import re
import unicodedata
from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import APIModel

_NAME_MAX = 120
_DESCRIPTION_MAX = 2000
_WHITESPACE = re.compile(r"\s+", re.UNICODE)


def _is_visible(char: str) -> bool:
    return unicodedata.category(char)[0] not in {"C", "Z"}


def normalize_category_name(value: str) -> str:
    """NFC, colapsa espacios, recorta y rechaza vacíos, NUL e invisibles."""

    if "\x00" in value:
        raise ValueError("nul")
    collapsed = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()
    if not collapsed or not any(_is_visible(char) for char in collapsed):
        raise ValueError("name required")
    if len(collapsed) > _NAME_MAX:
        raise ValueError("name too long")
    return collapsed


def normalize_category_description(value: str | None) -> str | None:
    if value is None:
        return None
    if "\x00" in value:
        raise ValueError("nul")
    stripped = unicodedata.normalize("NFC", value).strip()
    return stripped or None


class CategoryResponse(APIModel):
    id: UUID
    name: str
    parent_id: UUID | None
    description: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime


class CategoryCreate(APIModel):
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    parent_id: UUID | None = None
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX)
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_category_name(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return normalize_category_description(value)


class CategoryUpdate(APIModel):
    """PUT parcial: un campo omitido no cambia; ``null`` sí borra padre/description."""

    name: str | None = Field(default=None, min_length=1, max_length=_NAME_MAX)
    parent_id: UUID | None = None
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX)
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_category_name(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return normalize_category_description(value)
