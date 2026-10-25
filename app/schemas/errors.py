from app.schemas.base import APIModel


class FieldError(APIModel):
    """Error de un campo concreto, tal como espera el frontend (`fields`)."""

    field: str
    message: str


class ErrorResponse(APIModel):
    """Sobre de error uniforme.

    `error` es el código legible por máquina (snake_case). `fields` y `code`
    son opcionales: el frontend ya los consume en SM3 y en validación.
    """

    error: str
    message: str | None = None
    fields: list[FieldError] | None = None
    code: str | None = None
