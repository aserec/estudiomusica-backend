import re
import unicodedata
from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator
from pydantic.config import JsonDict

from app.schemas.base import APIModel
from app.schemas.unicode_props import (
    ASSIGNED,
    EMOJI_MODIFIER_BASE,
    EMOJI_PRESENTATION,
    EXTENDED_PICTOGRAPHIC,
    JOINING_D,
    JOINING_L,
    JOINING_R,
    JOINING_T,
)

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
# RGI de subdivisión UTS #51: Inglaterra, Escocia, Gales.
_RGI_SUBDIVISION_TAGS = ("gbeng", "gbsct", "gbwls")
_FLAG_SEQUENCE = re.compile(
    "\U0001f3f4(?:"
    + "|".join(
        "".join(chr(0xE0000 + ord(char)) for char in tag)
        for tag in _RGI_SUBDIVISION_TAGS
    )
    + ")\U000e007f"
)
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
_CF_TABLE = str.maketrans("", "", _CF_STRIP_CHARS)
_NFC_LENGTH_NOTE = "Longitud máxima tras NFC."
_ZWJ = "\u200d"
_ZWNJ = "\u200c"
_VIRAMA_CCC = 9
_VS15 = "\ufe0e"
_VS16 = "\ufe0f"
_VARIATION_SELECTORS = frozenset({_VS15, _VS16})


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    low, high = 0, len(ranges) - 1
    while low <= high:
        mid = (low + high) // 2
        start, end = ranges[mid]
        if code < start:
            high = mid - 1
        elif code > end:
            low = mid + 1
        else:
            return True
    return False


def _joining_type(char: str) -> str:
    code = ord(char)
    if _in_ranges(code, JOINING_L):
        return "L"
    if _in_ranges(code, JOINING_D):
        return "D"
    if _in_ranges(code, JOINING_R):
        return "R"
    if _in_ranges(code, JOINING_T):
        return "T"
    return "U"


def _last_vs16(value: str, start: int, end: int, step: int) -> bool:
    """El último selector de variación (VS15/VS16) manda: VS15 anula VS16."""

    last: str | None = None
    cursor = start
    while 0 <= cursor < len(value) and (end - cursor) * step > 0:
        char = value[cursor]
        if char in _VARIATION_SELECTORS:
            last = char
        cursor += step
    return last == _VS16


def _skip_transparent(value: str, index: int, step: int) -> tuple[int, bool]:
    cursor = index + step
    while 0 <= cursor < len(value) and _joining_type(value[cursor]) == "T":
        cursor += step
    if step < 0:
        saw_vs16 = _last_vs16(value, cursor + 1, index, 1)
    else:
        saw_vs16 = _last_vs16(value, index + 1, cursor, 1)
    return cursor, saw_vs16


def _is_emoji_zwj_side(char: str, saw_vs16: bool) -> bool:
    code = ord(char)
    if not _in_ranges(code, EXTENDED_PICTOGRAPHIC):
        return False
    return bool(_in_ranges(code, EMOJI_PRESENTATION) or saw_vs16)


def _keep_zwj(value: str, index: int) -> bool:
    if index > 0 and unicodedata.combining(value[index - 1]) == _VIRAMA_CCC:
        return True
    left, left_vs = _skip_transparent(value, index, -1)
    right, _ = _skip_transparent(value, index, 1)
    right_vs = False
    if not (0 <= left < len(value) and 0 <= right < len(value)):
        return False
    # UTS #51: el tono (1F3FB..1F3FF) solo cuenta tras Emoji_Modifier_Base;
    # el VS16 de presentación va *detrás* del carácter.
    if 0x1F3FB <= ord(value[left]) <= 0x1F3FF:
        if left < 1 or not _in_ranges(ord(value[left - 1]), EMOJI_MODIFIER_BASE):
            return False
        left -= 1
        left_vs = True
    if not 0 <= left < len(value):
        return False
    if right + 1 < len(value) and 0x1F3FB <= ord(value[right + 1]) <= 0x1F3FF:
        if _in_ranges(ord(value[right]), EMOJI_MODIFIER_BASE):
            right_vs = True
    else:
        trail = right + 1
        while trail < len(value) and value[trail] in _VARIATION_SELECTORS:
            trail += 1
        right_vs = right_vs or _last_vs16(value, right + 1, trail, 1)
    return _is_emoji_zwj_side(value[left], left_vs) and _is_emoji_zwj_side(
        value[right], right_vs
    )


def _keep_zwnj(value: str, index: int) -> bool:
    if index > 0 and unicodedata.combining(value[index - 1]) == _VIRAMA_CCC:
        return True
    left, _ = _skip_transparent(value, index, -1)
    right, _ = _skip_transparent(value, index, 1)
    if not (0 <= left < len(value) and 0 <= right < len(value)):
        return False
    return _joining_type(value[left]) in {"L", "D"} and _joining_type(value[right]) in {
        "R",
        "D",
    }


def _keep_joiner(value: str, index: int, char: str) -> bool:
    if char == _ZWJ:
        return _keep_zwj(value, index)
    return _keep_zwnj(value, index)


def _keep_significant_joiners(value: str) -> str:
    """ZWJ/ZWNJ: CONTEXTJ RFC 5892 A.1 (ZWNJ) y A.2 (ZWJ tras virama).

    El ZWJ entre emoji es extensión de UTS #51, no de IDNA.
    """

    return "".join(
        char
        for index, char in enumerate(value)
        if char not in {_ZWJ, _ZWNJ} or _keep_joiner(value, index, char)
    )


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
    if any(not _in_ranges(ord(char), ASSIGNED) for char in value):
        raise ValueError("unassigned")
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


_RI_START = 0x1F1E6
_RI_END = 0x1F1FF
_TAG_TERM = "\U000e007f"


def _is_grapheme_extend(char: str) -> bool:
    code = ord(char)
    if unicodedata.category(char) in {"Mn", "Me", "Mc"}:
        return True
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return True
    if 0x1F3FB <= code <= 0x1F3FF:
        return True
    return char in {_ZWJ, _ZWNJ, _TAG_TERM} or char in _TAG_CF


def _next_grapheme(value: str, start: int) -> int:
    if start >= len(value):
        return start
    index = start + 1
    if (
        _RI_START <= ord(value[start]) <= _RI_END
        and index < len(value)
        and _RI_START <= ord(value[index]) <= _RI_END
    ):
        index += 1
    while index < len(value) and _is_grapheme_extend(value[index]):
        joined = value[index] == _ZWJ
        index += 1
        if (
            joined
            and index < len(value)
            and _in_ranges(ord(value[index]), EXTENDED_PICTOGRAPHIC)
        ):
            index += 1
    return index


def clip_to_grapheme_limit(value: str, limit: int) -> str:
    """Corta por límite de grafema; no parte tags 🏴…󠁿 ni ZWJ/tono/VS."""

    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    end = 0
    while True:
        nxt = _next_grapheme(value, end)
        if nxt > limit or nxt == end:
            break
        end = nxt
    return value[:end]


def category_name_key(value: str) -> str:
    """Clave de unicidad: sin Cf de relleno; ZWJ/ZWNJ solo donde importan.

    NFC es defensa para INSERT SQL en NFD; las rutas HTTP ya normalizan.
    """

    normalized = unicodedata.normalize("NFC", value).translate(_CF_TABLE)
    return _WHITESPACE.sub(" ", _keep_significant_joiners(normalized)).strip()


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
