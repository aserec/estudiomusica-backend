from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.config import get_settings
from app.errors import register_exception_handlers
from app.middleware import (
    ApiErrorMiddleware,
    BodySizeLimitMiddleware,
    RejectedPreflightMiddleware,
)
from app.routers import examples_router, health_router
from app.schemas.errors import ErrorResponse

OPENAPI_DESCRIPTION = """
Backend FastAPI de EstudioMusica.

## Convenciones

- Prefijo global: `/api/v1` (`api_router` montado en `create_app()`).
- Claves JSON en **camelCase**. Códigos de error y valores de enum en **snake_case**.
- Actualizaciones con **PUT**, salvo el PATCH de autoguardado del cuestionario.
- Errores: `{ "error": "not_found", "code": "...", "fields": ["campo"] }`.
- Listados: `{ "items": [...], "page": 1, "pageSize": 20, "total": 0 }`.
- Versión de API: prefijo `/api/v1` y cabecera `X-API-Version: v1`.
- CORS: orígenes en `CORS_ORIGINS` (por defecto el Next.js local).
"""

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness. Contrato: `/api/v1/health`."},
    {
        "name": "examples",
        "description": "Demostración de convenciones. No es lógica de negocio.",
    },
]


def _package_version() -> str:
    try:
        return version("estudiomusica-backend")
    except PackageNotFoundError:
        return "0.0.0"


def _rewrite_openapi_errors(schema: dict[str, Any]) -> None:
    error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
    error_400 = {
        "description": "Petición inválida",
        "content": {"application/json": {"schema": error_ref}},
    }
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict):
                continue
            if "422" in responses:
                del responses["422"]
                responses["400"] = error_400
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    error_schema = ErrorResponse.model_json_schema()
    error_schema.pop("$defs", None)
    error_schema.pop("$schema", None)
    components["ErrorResponse"] = error_schema
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=_package_version(),
        description=OPENAPI_DESCRIPTION,
        contact={
            "name": "EstudioMusica",
            "url": "https://estudiomusica.com",
        },
        openapi_tags=OPENAPI_TAGS,
    )
    register_exception_handlers(application)
    application.add_middleware(BodySizeLimitMiddleware)
    application.add_middleware(ApiErrorMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_method_list(),
        allow_headers=settings.cors_header_list(),
        expose_headers=["X-API-Version"],
    )
    application.add_middleware(RejectedPreflightMiddleware)

    api_router = APIRouter(prefix="/api/v1")
    api_router.include_router(health_router)
    if settings.example_routes_enabled():
        api_router.include_router(examples_router)
    application.include_router(api_router)

    # Liveness en la raíz para sondas. El contrato de la API es GET /api/v1/health.
    application.include_router(health_router, include_in_schema=False)

    def custom_openapi() -> dict[str, Any]:
        if application.openapi_schema:
            return application.openapi_schema
        schema = get_openapi(
            title=application.title,
            version=application.version,
            openapi_version=application.openapi_version,
            description=application.description,
            routes=application.routes,
            tags=application.openapi_tags,
            contact=application.contact,
        )
        _rewrite_openapi_errors(schema)
        application.openapi_schema = schema
        return schema

    application.openapi = custom_openapi  # type: ignore[method-assign]
    return application


def __getattr__(name: str) -> FastAPI:
    """Expone `app` de forma perezosa para Uvicorn/FastAPI CLI.

    Importar este módulo no instancia Settings, así un APP_ENV inválido
    en la shell no rompe la recogida de tests.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
