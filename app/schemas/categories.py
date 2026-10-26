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
_BIDI_EMBED = frozenset(
    {chr(code) for code in range(0x202A, 0x202F)}
    | {chr(code) for code in range(0x2066, 0x206A)}
)
_FLAG_SEQUENCE = re.compile("\U0001f3f4[\U000e0020-\U000e007e]+\U000e007f")
# Unicode 16/17 Cf salvo ZWJ, ZWNJ y tags de bandera (E0020-E007F).
_CF_STRIP_CHARS = (
    "\u00ad\u0600\u0601\u0602\u0603\u0604\u0605\u061c\u06dd\u070f"
    "\u0890\u0891\u08e2\u180e\u200b\u200e\u200f\u202a\u202b\u202c"
    "\u202d\u202e\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068"
    "\u2069\u206a\u206b\u206c\u206d\u206e\u206f\ufeff\ufff9\ufffa"
    "\ufffb\U000110bd\U000110cd\U00013430\U00013431\U00013432"
    "\U00013433\U00013434\U00013435\U00013436\U00013437\U00013438"
    "\U00013439\U0001343a\U0001343b\U0001343c\U0001343d\U0001343e"
    "\U0001343f\U0001bca0\U0001bca1\U0001bca2\U0001bca3\U0001d173"
    "\U0001d174\U0001d175\U0001d176\U0001d177\U0001d178\U0001d179"
    "\U0001d17a\U000e0001"
)
_CF_CHARS = _CF_STRIP_CHARS
_CF_TABLE = str.maketrans("", "", _CF_STRIP_CHARS)
_NFC_LENGTH_NOTE = "Longitud máxima tras NFC."


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _tags_are_only_in_flags(value: str) -> bool:
    covered = [False] * len(value)
    for match in _FLAG_SEQUENCE.finditer(value):
        for index in range(match.start(), match.end()):
            covered[index] = True
    return not any(
        char in _TAG_CF and not covered[index] for index, char in enumerate(value)
    )


def _reject_forbidden_chars(value: str) -> None:
    if "\x00" in value:
        raise ValueError("nul")
    if _has_surrogate(value):
        raise ValueError("surrogate")
    if "\ufffc" in value:
        raise ValueError("object replacement")
    if any(char in _BIDI_EMBED for char in value):
        raise ValueError("bidi embed")
    if not _tags_are_only_in_flags(value):
        raise ValueError("loose tag")


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
    """Clave de unicidad: sin Cf de relleno, con ZWJ/ZWNJ y tags de bandera."""

    return _WHITESPACE.sub(" ", value.translate(_CF_TABLE)).strip()


def normalize_category_name(value: str) -> str:
    """NFC, recorta Cf/espacios de borde, colapsa espacios; permite Cf internos."""

    _reject_forbidden_chars(value)
    normalized = unicodedata.normalize("NFC", value)
    _reject_forbidden_chars(normalized)
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
    _reject_forbidden_chars(stripped)
    if len(stripped) > _DESCRIPTION_MAX:
        raise ValueError("description too long")
    return stripped


def _schema_create_name(schema: JsonDict) -> None:
    schema["type"] = "string"
    schema["minLength"] = 1
    schema["maxLength"] = _NAME_MAX
    schema["description"] = _NFC_LENGTH_NOTE


def _schema_update_name(schema: JsonDict) -> None:
    schema.pop("anyOf", None)
    schema.pop("nullable", None)
    schema["type"] = "string"
    schema["minLength"] = 1
    schema["maxLength"] = _NAME_MAX
    schema["description"] = _NFC_LENGTH_NOTE


def _schema_description(schema: JsonDict) -> None:
    options = schema.get("anyOf")
    if isinstance(options, list):
        for item in options:
            if isinstance(item, dict) and item.get("type") == "string":
                item["maxLength"] = _DESCRIPTION_MAX
                item["description"] = _NFC_LENGTH_NOTE
        return
    if schema.get("type") == "string":
        schema["maxLength"] = _DESCRIPTION_MAX
        schema["description"] = _NFC_LENGTH_NOTE


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
