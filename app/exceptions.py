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


class Unauthorized(DomainError):
    """401 {error: unauthorized}."""

    status_code = 401
    error = "unauthorized"


class InvalidCredentials(DomainError):
    """401 {error: invalid_credentials}."""

    status_code = 401
    error = "invalid_credentials"


class AccountSuspended(DomainError):
    """403 {error: account_suspended}."""

    status_code = 403
    error = "account_suspended"


class EmailTaken(DomainError):
    """409 {error: email_taken}."""

    status_code = 409
    error = "email_taken"


class Conflict(DomainError):
    """409 {error: conflict}."""

    status_code = 409
    error = "conflict"


class SystemRoleProtected(Conflict):
    """409 {error: system_role_protected}."""

    error = "system_role_protected"


class RoleInUse(Conflict):
    """409 {error: role_in_use}."""

    error = "role_in_use"


class RoleNotAssignable(Conflict):
    """409 {error: role_not_assignable}."""

    error = "role_not_assignable"


class CategoryNotEmpty(Conflict):
    """409 {error: category_not_empty}."""

    error = "category_not_empty"


class CategoryNameTaken(Conflict):
    """409 {error: category_name_taken}."""

    error = "category_name_taken"


class DuplicateShortName(Conflict):
    """409 {error: duplicate_short_name}."""

    error = "duplicate_short_name"


class CourseArchived(Conflict):
    """409 {error: course_archived}."""

    error = "course_archived"
