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
from app.schemas.roles import (
    CoursePermissionsResponse,
    SystemRoleResponse,
    SystemRoleUpdate,
)

__all__ = [
    "AuthMeResponse",
    "AuthSessionResponse",
    "CoursePermissionsResponse",
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
    "SystemRoleResponse",
    "SystemRoleUpdate",
]
