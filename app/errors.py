import http
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast, get_args, get_origin

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings
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
    503: "service_unavailable",
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
    "transient_db_conflict",
    "service_unavailable",
}
MACHINE_SLUG = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
POLLUTION_FIELDS = {"__proto__", "prototype", "constructor"}
MAX_FIELDS = 20
MAX_FIELD_LEN = 64


def status_error_slug(status_code: int) -> str:
    if status_code in STATUS_ERROR_CODES:
        return STATUS_ERROR_CODES[status_code]
    try:
        return http.HTTPStatus(status_code).name.lower()
    except ValueError:
        return "error"


def _annotation_aliases(annotation: object) -> dict[str, str]:
    aliases: dict[str, str] = {}
    origin = get_origin(annotation)
    if origin is dict:
        return aliases
    if origin is not None:
        for arg in get_args(annotation):
            aliases.update(_annotation_aliases(arg))
        return aliases
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        for name, field in annotation.model_fields.items():
            canonical = field.serialization_alias or field.alias or name
            aliases[name] = canonical
            if field.alias:
                aliases[field.alias] = canonical
            if field.serialization_alias:
                aliases[field.serialization_alias] = canonical
            if isinstance(field.validation_alias, str):
                aliases[field.validation_alias] = canonical
            aliases.update(_annotation_aliases(field.annotation))
    return aliases


def declared_aliases(request: Request) -> dict[str, str] | None:
    """Alias camelCase de campos declarados en la ruta, o None si no hay ruta."""

    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return None
    aliases: dict[str, str] = {}

    def walk(node: object) -> None:
        params = (
            *getattr(node, "query_params", ()),
            *getattr(node, "path_params", ()),
            *getattr(node, "header_params", ()),
            *getattr(node, "body_params", ()),
        )
        for param in params:
            _add_param_aliases(aliases, param)
        for child in getattr(node, "dependencies", ()):
            walk(child)

    walk(dependant)
    return aliases


def _add_param_aliases(aliases: dict[str, str], param: object) -> None:
    name = getattr(param, "name", None)
    alias = getattr(param, "alias", None) or name
    canonical = getattr(param, "serialization_alias", None) or alias
    if isinstance(name, str) and isinstance(canonical, str):
        aliases[name] = canonical
    if isinstance(alias, str) and isinstance(canonical, str):
        aliases[alias] = canonical
    field_info = getattr(param, "field_info", None)
    annotation = getattr(field_info, "annotation", None)
    aliases.update(_annotation_aliases(annotation))


def loc_to_field(
    loc: Sequence[object],
    aliases: dict[str, str] | None = None,
) -> str:
    """Convierte `loc` de Pydantic a un path de campos declarados (alias)."""

    parent = "request"
    parts: list[str] = []
    for item in loc:
        if item in {"body", "query", "path", "header"}:
            parent = "body" if item == "body" else str(item)
            continue
        if isinstance(item, int):
            continue
        text = str(item)
        if aliases is not None:
            if text in aliases:
                parts.append(aliases[text])
                parent = ".".join(parts)
            else:
                return ".".join(parts) if parts else parent
        else:
            parts.append(text)
    return ".".join(parts) if parts else parent


def sanitize_fields(fields: list[str] | None) -> list[str] | None:
    if not fields:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in fields:
        name = raw[:MAX_FIELD_LEN]
        if name in POLLUTION_FIELDS or name.startswith("__"):
            continue
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


def _maybe_message(message: str | None, expose_errors: bool) -> str | None:
    if message is None or not expose_errors:
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


def _detail_as_error(
    status_code: int,
    detail: object,
    *,
    expose_errors: bool,
) -> dict[str, object]:
    if isinstance(detail, Mapping):
        payload = cast(Mapping[str, Any], detail)
        error = str(payload.get("error") or status_error_slug(status_code))
        extra_code = payload.get("code")
        extra_message = payload.get("message")
        message = None
        if extra_message is not None:
            message = _maybe_message(str(extra_message), expose_errors)
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
        return error_body(status_error_slug(status_code))

    return error_body(status_error_slug(status_code))


def register_exception_handlers(application: FastAPI, settings: Settings) -> None:
    @application.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        headers = {"Retry-After": "1"} if exc.error == "transient_db_conflict" else None
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                exc.error,
                fields=exc.fields,
                code=exc.code,
                message=_maybe_message(exc.message, settings.expose_errors),
            ),
            headers=headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        aliases = declared_aliases(request)
        fields: list[str] = []
        seen: set[str] = set()
        for err in exc.errors():
            name = loc_to_field(cast(Sequence[object], err["loc"]), aliases)
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
        mapped = status_code if exc.status_code != 422 else 422
        return JSONResponse(
            status_code=status_code,
            content=_detail_as_error(
                mapped, exc.detail, expose_errors=settings.expose_errors
            ),
        )
