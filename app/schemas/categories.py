import re
import unicodedata
from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator
from pydantic.config import JsonDict

from app.schemas.base import APIModel

_NAME_MAX = 120
_DESCRIPTION_MAX = 2000
_WHITESPACE = re.compile(r"\s+", re.UNICODE)
_INVISIBLE_FILLERS = frozenset("\u115f\u2800\u3164")


def _is_visible(char: str) -> bool:
    if char in _INVISIBLE_FILLERS:
        return False
    return unicodedata.category(char)[0] not in {"C", "Z", "M"}


def _has_format_char(value: str) -> bool:
    return any(unicodedata.category(char) == "Cf" for char in value)


def normalize_category_name(value: str) -> str:
    """NFC, colapsa espacios, recorta y rechaza vacíos, NUL, Cf e invisibles."""

    if "\x00" in value:
        raise ValueError("nul")
    normalized = unicodedata.normalize("NFC", value)
    if _has_format_char(normalized):
        raise ValueError("invisible")
    collapsed = _WHITESPACE.sub(" ", normalized).strip()
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
    if not stripped:
        return None
    if len(stripped) > _DESCRIPTION_MAX:
        raise ValueError("description too long")
    return stripped


def _schema_string_not_nullable(schema: JsonDict) -> None:
    schema.pop("anyOf", None)
    schema.pop("nullable", None)
    schema["type"] = "string"


def _schema_int_not_nullable(schema: JsonDict) -> None:
    schema.pop("anyOf", None)
    schema.pop("nullable", None)
    schema["type"] = "integer"
    schema["minimum"] = 0
    schema["maximum"] = 1_000_000


class CategoryResponse(APIModel):
    id: UUID
    name: str
    parent_id: UUID | None
    description: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime


class CategoryCreate(APIModel):
    name: str = Field(min_length=1)
    parent_id: UUID | None = None
    description: str | None = None
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

    name: str | None = Field(
        default=None, json_schema_extra=_schema_string_not_nullable
    )
    parent_id: UUID | None = None
    description: str | None = None
    sort_order: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        json_schema_extra=_schema_int_not_nullable,
    )

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
