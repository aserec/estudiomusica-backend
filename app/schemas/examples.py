from pydantic import Field

from app.schemas.base import APIModel


class ExampleItem(APIModel):
    """Ítem de demostración (no es un recurso de negocio)."""

    id: str
    display_name: str


class ExampleCreate(APIModel):
    """Cuerpo de alta de ejemplo, para ejercitar validación 400."""

    display_name: str = Field(min_length=1, max_length=80)
