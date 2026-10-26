from pydantic import Field

from app.schemas.base import APIModel


class ErrorResponse(APIModel):
    """Sobre de error unificado.

    Contrato de aserec/estudioMusica-webapp#391 y de las rutas Next
    (`{ error: string, code?: string, fields?: string[] }`).

    `message` es opcional y **no** forma parte de ese contrato. Solo se
    rellena si `EXPOSE_ERRORS=true` (opt-in; por defecto no se expone).
    """

    error: str
    code: str | None = None
    fields: list[str] | None = None
    message: str | None = Field(
        default=None,
        description=(
            "Opcional y fuera del contrato del frontend. Solo si EXPOSE_ERRORS=true."
        ),
    )
