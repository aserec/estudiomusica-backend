from collections.abc import Sequence
from typing import Annotated

from fastapi import Query
from pydantic import Field

from app.schemas.base import APIModel

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PaginationQuery(APIModel):
    """Parámetros de paginación (`page` / `pageSize`) ya validados."""

    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=MAX_PAGE_SIZE)


class Paginated[T](APIModel):
    """Listado paginado: ítems más metadatos `page`, `pageSize` y `total`."""

    items: list[T]
    page: int
    page_size: int
    total: int


def pagination_query(
    page: Annotated[
        int,
        Query(
            ge=1,
            description="Número de página, empezando en 1.",
            examples=[1],
        ),
    ] = DEFAULT_PAGE,
    page_size: Annotated[
        int,
        Query(
            alias="pageSize",
            ge=1,
            le=MAX_PAGE_SIZE,
            description="Tamaño de página.",
            examples=[20],
        ),
    ] = DEFAULT_PAGE_SIZE,
) -> PaginationQuery:
    return PaginationQuery(page=page, page_size=page_size)


def paginate[T](items: Sequence[T], query: PaginationQuery) -> Paginated[T]:
    """Corta `items` según `page`/`pageSize` y rellena los metadatos."""

    total = len(items)
    start = (query.page - 1) * query.page_size
    page_items = list(items[start : start + query.page_size])
    return Paginated(
        items=page_items,
        page=query.page,
        page_size=query.page_size,
        total=total,
    )
