"""Esquemas Pydantic de entrada y salida de la API."""

from app.schemas.auth import (
    AuthMeResponse,
    AuthSessionResponse,
    LogInRequest,
    PublicUser,
    SignUpRequest,
)
from app.schemas.categories import CategoryCreate, CategoryResponse, CategoryUpdate
from app.schemas.errors import ErrorResponse
from app.schemas.examples import ExampleCreate, ExampleItem
from app.schemas.health import DatabaseHealthResponse, HealthResponse
from app.schemas.pagination import Paginated, PaginationQuery

__all__ = [
    "AuthMeResponse",
    "AuthSessionResponse",
    "CategoryCreate",
    "CategoryResponse",
    "CategoryUpdate",
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
]
