from app.schemas.errors import FieldError


class AppError(Exception):
    """Error de aplicación con el sobre que consume el frontend."""

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str | None = None,
        *,
        fields: list[FieldError] | None = None,
        code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.message = message
        self.fields = fields
        self.code = code
        super().__init__(message or error)
