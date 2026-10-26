from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.exceptions import AccessDenied, NotFound, PayloadTooLarge
from app.schemas.errors import ErrorResponse
from app.schemas.examples import ExampleCreate, ExampleItem
from app.schemas.pagination import (
    Paginated,
    PaginationQuery,
    paginate,
    pagination_query,
)
from app.services.examples import get_example_item, list_example_items

router = APIRouter(prefix="/examples", tags=["examples"])

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Petición inválida"},
    403: {"model": ErrorResponse, "description": "Acceso denegado"},
    404: {"model": ErrorResponse, "description": "No encontrado"},
    413: {"model": ErrorResponse, "description": "Cuerpo demasiado grande"},
}


@router.get(
    "",
    response_model=Paginated[ExampleItem],
    summary="Listar ejemplos paginados",
    description=(
        "Fixture de convenciones: no es un recurso de negocio. "
        "Acepta page y pageSize; responde items, page, pageSize y total."
    ),
    responses={400: ERROR_RESPONSES[400]},
)
async def list_examples(
    query: Annotated[PaginationQuery, Depends(pagination_query)],
) -> Paginated[ExampleItem]:
    return paginate(list_example_items(), query)


@router.get(
    "/denied",
    response_model=ExampleItem,
    summary="Demostrar AccessDenied",
    responses={403: ERROR_RESPONSES[403]},
)
async def read_denied_example() -> ExampleItem:
    raise AccessDenied()


@router.get(
    "/too-large",
    response_model=ExampleItem,
    summary="Demostrar PayloadTooLarge",
    responses={413: ERROR_RESPONSES[413]},
)
async def read_too_large_example() -> ExampleItem:
    raise PayloadTooLarge()


@router.get(
    "/{example_id}",
    response_model=ExampleItem,
    summary="Obtener un ejemplo",
    responses={404: ERROR_RESPONSES[404]},
)
async def read_example(example_id: str) -> ExampleItem:
    item = get_example_item(example_id)
    if item is None:
        raise NotFound()
    return item


@router.post(
    "",
    response_model=ExampleItem,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un ejemplo (solo validación)",
    description="No persiste nada: sirve para documentar y probar el error 400.",
    responses={400: ERROR_RESPONSES[400]},
)
async def create_example(body: ExampleCreate) -> ExampleItem:
    return ExampleItem(id="ex-new", display_name=body.display_name)


@router.put(
    "/{example_id}",
    response_model=ExampleItem,
    summary="Actualizar un ejemplo (PUT)",
    description=(
        "Las actualizaciones van por PUT. El único PATCH previsto "
        "es el autoguardado de cuestionario."
    ),
    responses={400: ERROR_RESPONSES[400], 404: ERROR_RESPONSES[404]},
)
async def update_example(example_id: str, body: ExampleCreate) -> ExampleItem:
    item = get_example_item(example_id)
    if item is None:
        raise NotFound()
    return ExampleItem(id=item.id, display_name=body.display_name)
