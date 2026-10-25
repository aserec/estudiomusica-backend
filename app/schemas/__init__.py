"""Esquemas Pydantic de entrada y salida de la API."""

from app.schemas.errors import ErrorResponse
from app.schemas.examples import ExampleCreate, ExampleItem
from app.schemas.health import HealthResponse
from app.schemas.pagination import Paginated, PaginationQuery

__all__ = [
    "ErrorResponse",
    "ExampleCreate",
    "ExampleItem",
    "HealthResponse",
    "Paginated",
    "PaginationQuery",
]
