from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.config import get_settings
from app.errors import register_exception_handlers
from app.routers import examples, health
from app.schemas.errors import ErrorResponse
from app.schemas.examples import ExampleItem
from app.schemas.pagination import Paginated

OPENAPI_DESCRIPTION = """
Backend FastAPI de EstudioMusica.

## Convenciones

- Prefijo global: `/api/v1` (`api_router` montado en `create_app()`).
- Claves JSON en **camelCase**. Códigos de error y valores de enum en **snake_case**.
- Actualizaciones con **PUT**, salvo el PATCH de autoguardado de intentos de cuestionario.
- Errores: `{ "error": "not_found", "message": "...", "fields": [...], "code": "..." }`.
- Listados: `{ "items": [...], "page": 1, "pageSize": 20, "total": 0 }`.
- CORS: orígenes en `CORS_ORIGINS` (por defecto el Next.js local).
"""

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness bajo `/api/v1`."},
    {
        "name": "examples",
        "description": "Rutas de demostración de convenciones. No son lógica de negocio.",
    },
]


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=OPENAPI_DESCRIPTION,
        contact={
            "name": "EstudioMusica",
            "url": "https://estudiomusica.com",
        },
        openapi_tags=OPENAPI_TAGS,
        servers=[
            {
                "url": "http://127.0.0.1:8000",
                "description": "Servidor local",
            }
        ],
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_method_list(),
        allow_headers=settings.cors_header_list(),
    )
    register_exception_handlers(application)

    api_router = APIRouter(prefix=settings.api_v1_prefix)
    api_router.include_router(health.router)
    api_router.include_router(examples.router)
    application.include_router(api_router)

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
            servers=application.servers,
            contact=application.contact,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ErrorResponse"] = ErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components["PaginatedExampleItem"] = Paginated[ExampleItem].model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        application.openapi_schema = schema
        return schema

    application.openapi = custom_openapi  # type: ignore[method-assign]
    return application


app = create_app()
