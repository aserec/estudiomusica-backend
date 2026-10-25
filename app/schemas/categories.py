from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import APIModel

_NAME_MAX = 120
_DESCRIPTION_MAX = 2000


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
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return stripped

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class CategoryUpdate(APIModel):
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    parent_id: UUID | None = None
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX)
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return stripped

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None
