from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends, Query

from app.config import Settings, get_settings
from app.exceptions import Validation
from app.schemas.base import APIModel


class PaginationQuery(APIModel):
    """Parámetros de paginación (`page` / `pageSize`) ya validados."""

    page: int
    page_size: int


class Paginated[T](APIModel):
    """Listado paginado: ítems más metadatos `page`, `pageSize` y `total`."""

    items: list[T]
    page: int
    page_size: int
    total: int


def pagination_query(
    settings: Annotated[Settings, Depends(get_settings)],
    page: Annotated[
        int | None,
        Query(
            ge=1,
            description="Número de página, empezando en 1.",
            examples=[1],
        ),
    ] = None,
    page_size: Annotated[
        int | None,
        Query(
            alias="pageSize",
            ge=1,
            description="Tamaño de página.",
            examples=[20],
        ),
    ] = None,
) -> PaginationQuery:
    resolved_page = page if page is not None else settings.pagination_default_page
    resolved_size = (
        page_size if page_size is not None else settings.pagination_default_page_size
    )
    invalid: list[str] = []
    if resolved_page > settings.pagination_max_page:
        invalid.append("page")
    if resolved_size > settings.pagination_max_page_size:
        invalid.append("pageSize")
    if invalid:
        raise Validation(fields=invalid)
    return PaginationQuery(page=resolved_page, page_size=resolved_size)


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
