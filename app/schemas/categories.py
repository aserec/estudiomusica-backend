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
_INVISIBLE_FILLERS = frozenset("\u115f\u1160\u2800\u3164")
_EDGE_SPACE = frozenset(" \t\n\r\f\v")
_TAG_CF = frozenset(chr(code) for code in range(0xE0020, 0xE0080)) | {"\U000e0001"}


def _cf_characters() -> str:
    return "".join(
        chr(code) for code in range(0x110000) if unicodedata.category(chr(code)) == "Cf"
    )


_CF_CHARS = _cf_characters()
_CF_TABLE = str.maketrans("", "", _CF_CHARS)


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _reject_forbidden_chars(value: str) -> None:
    if "\x00" in value:
        raise ValueError("nul")
    if _has_surrogate(value):
        raise ValueError("surrogate")


def _is_edge_trim(char: str) -> bool:
    if char in _TAG_CF:
        return False
    return unicodedata.category(char) == "Cf" or char.isspace() or char in _EDGE_SPACE


def _strip_cf_and_space(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and _is_edge_trim(value[start]):
        start += 1
    while end > start and _is_edge_trim(value[end - 1]):
        end -= 1
    return value[start:end]


def _is_visible(char: str) -> bool:
    if char in _INVISIBLE_FILLERS:
        return False
    return unicodedata.category(char)[0] not in {"C", "Z", "M"}


def category_name_key(value: str) -> str:
    """Clave de unicidad: sin Cf, espacios colapsados. Compara en citext."""

    return _WHITESPACE.sub(" ", value.translate(_CF_TABLE)).strip()


def category_name_key_function_sql() -> str:
    """SQL de ``category_name_key(citext)`` alineado con ``category_name_key``."""

    return (
        "CREATE FUNCTION category_name_key(value citext) "
        "RETURNS citext "
        "LANGUAGE sql "
        "IMMUTABLE "
        "STRICT "
        "PARALLEL SAFE "
        "AS $fn$ "
        "SELECT btrim(regexp_replace("
        f"translate(value::text, $cf${_CF_CHARS}$cf$, ''), "
        r"'\s+', ' ', 'g'"
        "))::citext "
        "$fn$"
    )


def normalize_category_name(value: str) -> str:
    """NFC, recorta Cf/espacios de borde, colapsa espacios; permite Cf internos."""

    _reject_forbidden_chars(value)
    normalized = unicodedata.normalize("NFC", value)
    collapsed = _WHITESPACE.sub(" ", _strip_cf_and_space(normalized))
    if not collapsed or not any(_is_visible(char) for char in collapsed):
        raise ValueError("name required")
    if len(collapsed) > _NAME_MAX:
        raise ValueError("name too long")
    return collapsed


def normalize_category_description(value: str | None) -> str | None:
    if value is None:
        return None
    _reject_forbidden_chars(value)
    stripped = unicodedata.normalize("NFC", value).strip()
    if not stripped:
        return None
    if len(stripped) > _DESCRIPTION_MAX:
        raise ValueError("description too long")
    return stripped


def _schema_create_name(schema: JsonDict) -> None:
    schema["type"] = "string"
    schema["minLength"] = 1
    schema["maxLength"] = _NAME_MAX


def _schema_update_name(schema: JsonDict) -> None:
    schema.pop("anyOf", None)
    schema.pop("nullable", None)
    schema["type"] = "string"
    schema["minLength"] = 1
    schema["maxLength"] = _NAME_MAX


def _schema_description(schema: JsonDict) -> None:
    options = schema.get("anyOf")
    if isinstance(options, list):
        for item in options:
            if isinstance(item, dict) and item.get("type") == "string":
                item["maxLength"] = _DESCRIPTION_MAX
        return
    if schema.get("type") == "string":
        schema["maxLength"] = _DESCRIPTION_MAX


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
    name: str = Field(min_length=1, json_schema_extra=_schema_create_name)
    parent_id: UUID | None = None
    description: str | None = Field(default=None, json_schema_extra=_schema_description)
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

    name: str | None = Field(default=None, json_schema_extra=_schema_update_name)
    parent_id: UUID | None = None
    description: str | None = Field(default=None, json_schema_extra=_schema_description)
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
