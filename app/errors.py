from collections.abc import Mapping, Sequence
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic.alias_generators import to_camel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.exceptions import AppError
from app.schemas.errors import ErrorResponse, FieldError

STATUS_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    500: "internal_error",
}


def loc_to_field(loc: Sequence[object]) -> str:
    """Convierte `loc` de Pydantic a un path camelCase (`pageSize`, `displayName`)."""

    parts: list[str] = []
    for item in loc:
        if item in {"body", "query", "path", "header"}:
            continue
        if isinstance(item, int):
            parts.append(str(item))
            continue
        text = str(item)
        parts.append(to_camel(text) if text.isidentifier() else text)
    return ".".join(parts) or "request"


def error_body(
    error: str,
    message: str | None = None,
    *,
    fields: list[FieldError] | None = None,
    code: str | None = None,
) -> dict[str, object]:
    return ErrorResponse(
        error=error,
        message=message,
        fields=fields,
        code=code,
    ).model_dump(by_alias=True, exclude_none=True)


def _detail_as_error(status_code: int, detail: object) -> dict[str, object]:
    if isinstance(detail, Mapping):
        payload = cast(Mapping[str, Any], detail)
        error = str(
            payload.get("error") or STATUS_ERROR_CODES.get(status_code, "error")
        )
        raw_fields = payload.get("fields")
        fields: list[FieldError] | None = None
        if isinstance(raw_fields, list):
            fields = [
                FieldError.model_validate(item)
                for item in raw_fields
                if isinstance(item, Mapping)
            ] or None
        message = payload.get("message")
        extra_code = payload.get("code")
        return error_body(
            error,
            str(message) if message is not None else None,
            fields=fields,
            code=str(extra_code) if extra_code is not None else None,
        )

    if isinstance(detail, str) and detail:
        mapped = {
            "Not Found": "not_found",
            "Unauthorized": "unauthorized",
            "Forbidden": "forbidden",
            "Method Not Allowed": "method_not_allowed",
        }
        if detail in mapped:
            return error_body(mapped[detail], detail)
        if " " not in detail:
            return error_body(detail)
        return error_body(
            STATUS_ERROR_CODES.get(status_code, "error"),
            detail,
        )

    return error_body(STATUS_ERROR_CODES.get(status_code, "error"))


def register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                exc.error,
                exc.message,
                fields=exc.fields,
                code=exc.code,
            ),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            FieldError(
                field=loc_to_field(cast(Sequence[object], err["loc"])),
                message=err["msg"],
            )
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error",
                "La petición no es válida.",
                fields=fields,
            ),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_detail_as_error(exc.status_code, exc.detail),
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        leak = get_settings().app_env == "development"
        message = str(exc) if leak else "Error interno del servidor."
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", message),
        )
