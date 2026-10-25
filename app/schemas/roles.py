from datetime import datetime

from pydantic import Field, field_validator

from app.permissions.types import CourseRole, Permission
from app.schemas.base import APIModel


class SystemRoleResponse(APIModel):
    id: str
    name: str
    sort_order: int
    is_system: bool
    active: bool
    created_at: datetime
    updated_at: datetime


class SystemRoleUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name required")
        return stripped


class CoursePermissionsResponse(APIModel):
    course_role: CourseRole | None = None
    permissions: list[Permission]
