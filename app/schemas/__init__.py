"""Esquemas Pydantic de entrada y salida de la API."""

from app.schemas.auth import (
    AuthMeResponse,
    AuthSessionResponse,
    LogInRequest,
    PublicUser,
    SignUpRequest,
)
from app.schemas.errors import ErrorResponse
from app.schemas.examples import ExampleCreate, ExampleItem
from app.schemas.health import DatabaseHealthResponse, HealthResponse
from app.schemas.pagination import Paginated, PaginationQuery
from app.schemas.users import (
    AdminFlagUpdate,
    AdminUserIdentityUpdate,
    AdminUserStatus,
    AdminUserView,
    SuspensionUpdate,
    UserSearchHit,
)

__all__ = [
    "AdminFlagUpdate",
    "AdminUserIdentityUpdate",
    "AdminUserStatus",
    "AdminUserView",
    "AuthMeResponse",
    "AuthSessionResponse",
    "DatabaseHealthResponse",
    "ErrorResponse",
    "ExampleCreate",
    "ExampleItem",
    "HealthResponse",
    "LogInRequest",
    "Paginated",
    "PaginationQuery",
    "PublicUser",
    "SignUpRequest",
    "SuspensionUpdate",
    "UserSearchHit",
]
