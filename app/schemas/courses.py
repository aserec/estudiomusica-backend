from datetime import date, datetime
from uuid import UUID

from pydantic import Field, field_validator

from app.models.course import CourseLevel, DefaultGroupMode
from app.schemas.base import APIModel

_SHORT_NAME_MAX = 64
_NAME_MAX = 200
_SUMMARY_MAX = 2000


def _parse_optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if "T" in stripped:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00")).date()
        return date.fromisoformat(stripped)
    return value  # type: ignore[return-value]


class CourseResponse(APIModel):
    id: UUID
    short_name: str
    name: str
    summary: str | None
    category_id: UUID | None
    start_date: date | None
    end_date: date | None
    visible: bool
    level: CourseLevel
    default_group_mode: DefaultGroupMode
    deleted_at: datetime | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CourseCreate(APIModel):
    short_name: str = Field(min_length=1, max_length=_SHORT_NAME_MAX)
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    summary: str | None = Field(default=None, max_length=_SUMMARY_MAX)
    category_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    visible: bool = True
    level: CourseLevel = CourseLevel.BEGINNER
    default_group_mode: DefaultGroupMode = DefaultGroupMode.NONE

    @field_validator("short_name")
    @classmethod
    def strip_short_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("shortName required")
        return stripped

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return stripped

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_dates(cls, value: object) -> date | None:
        return _parse_optional_date(value)


class CourseUpdate(APIModel):
    short_name: str = Field(min_length=1, max_length=_SHORT_NAME_MAX)
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    summary: str | None = Field(default=None, max_length=_SUMMARY_MAX)
    category_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    visible: bool
    level: CourseLevel
    default_group_mode: DefaultGroupMode

    @field_validator("short_name")
    @classmethod
    def strip_short_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("shortName required")
        return stripped

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return stripped

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_dates(cls, value: object) -> date | None:
        return _parse_optional_date(value)


class CourseArchive(APIModel):
    archived: bool


class CourseDuplicate(APIModel):
    short_name: str = Field(min_length=1, max_length=_SHORT_NAME_MAX)
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    start_date: date

    @field_validator("short_name")
    @classmethod
    def strip_short_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("shortName required")
        return stripped

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return stripped

    @field_validator("start_date", mode="before")
    @classmethod
    def parse_start_date(cls, value: object) -> date | None:
        parsed = _parse_optional_date(value)
        if parsed is None:
            raise ValueError("startDate required")
        return parsed
