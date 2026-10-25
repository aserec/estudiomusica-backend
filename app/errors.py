import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic.alias_generators import to_camel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.exceptions import DomainError
from app.schemas.errors import ErrorResponse

STATUS_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation",
    500: "internal_error",
}

STARLETTE_DETAIL_SLUGS: dict[str, str] = {
    "Not Found": "not_found",
    "Unauthorized": "unauthorized",
    "Forbidden": "forbidden",
    "Method Not Allowed": "method_not_allowed",
}

KNOWN_ERROR_SLUGS = {
    "forbidden",
    "not_found",
    "unauthorized",
    "validation",
    "payload_too_large",
    "bad_request",
    "conflict",
    "internal_error",
}
MACHINE_SLUG = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
MAX_FIELDS = 20
MAX_FIELD_LEN = 64


def loc_to_field(loc: Sequence[object]) -> str:
    """Convierte `loc` de Pydantic a un path camelCase seguro."""

    parts: list[str] = []
    has_body = False
    for item in loc:
        if item in {"body", "query", "path", "header"}:
            has_body = has_body or item == "body"
            continue
        if isinstance(item, int):
            continue
        text = str(item)
        parts.append(to_camel(text) if text.isidentifier() else text)
    if not parts:
        return "body" if has_body else "request"
    return ".".join(parts)


def sanitize_fields(fields: list[str] | None) -> list[str] | None:
    if not fields:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in fields:
        name = raw[:MAX_FIELD_LEN]
        if not SAFE_FIELD.match(name):
            continue
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
        if len(cleaned) >= MAX_FIELDS:
            break
    return cleaned or None


def error_body(
    error: str,
    *,
    fields: list[str] | None = None,
    code: str | None = None,
    message: str | None = None,
) -> dict[str, object]:
    return ErrorResponse(
        error=error,
        fields=sanitize_fields(fields),
        code=code,
        message=message,
    ).model_dump(by_alias=True, exclude_none=True)


def _maybe_message(message: str | None) -> str | None:
    if message is None or not get_settings().expose_errors:
        return None
    return message


def _fields_from_detail(raw_fields: object) -> list[str] | None:
    if not isinstance(raw_fields, list):
        return None
    fields: list[str] = []
    for item in raw_fields:
        if isinstance(item, str):
            fields.append(item)
        elif isinstance(item, Mapping):
            payload = cast(Mapping[str, Any], item)
            name = payload.get("field") or payload.get("loc")
            if name is not None:
                fields.append(str(name))
    return fields or None


def _detail_as_error(status_code: int, detail: object) -> dict[str, object]:
    if isinstance(detail, Mapping):
        payload = cast(Mapping[str, Any], detail)
        error = str(
            payload.get("error") or STATUS_ERROR_CODES.get(status_code, "error")
        )
        extra_code = payload.get("code")
        extra_message = payload.get("message")
        message = None
        if extra_message is not None:
            message = _maybe_message(str(extra_message))
        return error_body(
            error,
            fields=_fields_from_detail(payload.get("fields")),
            code=str(extra_code) if extra_code is not None else None,
            message=message,
        )

    if isinstance(detail, str) and detail:
        if detail in STARLETTE_DETAIL_SLUGS:
            return error_body(STARLETTE_DETAIL_SLUGS[detail])
        if detail in KNOWN_ERROR_SLUGS or MACHINE_SLUG.match(detail):
            return error_body(detail)
        return error_body(STATUS_ERROR_CODES.get(status_code, "error"))

    return error_body(STATUS_ERROR_CODES.get(status_code, "error"))


def register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                exc.error,
                fields=exc.fields,
                code=exc.code,
                message=_maybe_message(exc.message),
            ),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: list[str] = []
        seen: set[str] = set()
        for err in exc.errors():
            name = loc_to_field(cast(Sequence[object], err["loc"]))
            if name not in seen:
                seen.add(name)
                fields.append(name)
        return JSONResponse(
            status_code=400,
            content=error_body("validation", fields=fields or ["body"]),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        status_code = 400 if exc.status_code == 422 else exc.status_code
        return JSONResponse(
            status_code=status_code,
            content=_detail_as_error(
                status_code if exc.status_code != 422 else 422, exc.detail
            ),
        )
