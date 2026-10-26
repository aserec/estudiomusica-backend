class DomainError(Exception):
    """Error de dominio con el sobre `{error, code?, fields?}`."""

    status_code: int = 500
    error: str = "internal_error"

    def __init__(
        self,
        *,
        fields: list[str] | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        self.fields = fields
        self.code = code
        self.message = message
        super().__init__(message or self.error)


class AccessDenied(DomainError):
    """403 {error: forbidden}."""

    status_code = 403
    error = "forbidden"


class NotFound(DomainError):
    """404 {error: not_found}."""

    status_code = 404
    error = "not_found"


class Validation(DomainError):
    """400 {error: validation, fields: [...]}."""

    status_code = 400
    error = "validation"

    def __init__(
        self,
        fields: list[str],
        *,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(fields=fields, code=code, message=message)


class PayloadTooLarge(DomainError):
    """413 {error: payload_too_large}."""

    status_code = 413
    error = "payload_too_large"
