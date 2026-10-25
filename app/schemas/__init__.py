"""Esquemas Pydantic de entrada y salida de la API."""

from app.schemas.errors import ErrorResponse, FieldError
from app.schemas.examples import ExampleCreate, ExampleItem
from app.schemas.health import DatabaseHealthResponse, HealthResponse
from app.schemas.pagination import Paginated, PaginationQuery

__all__ = [
    "ErrorResponse",
    "ExampleCreate",
    "ExampleItem",
    "FieldError",
    "DatabaseHealthResponse",
    "HealthResponse",
    "Paginated",
    "PaginationQuery",
]
