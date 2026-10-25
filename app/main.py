from collections.abc import Awaitable, Callable
from importlib.metadata import version
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.responses import Response

from app.config import get_settings
from app.errors import register_exception_handlers
from app.routers import examples_router, health_router
from app.schemas.errors import ErrorResponse
from app.schemas.examples import ExampleItem
from app.schemas.pagination import Paginated

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


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=version("estudiomusica-backend"),
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

    @application.middleware("http")
    async def api_version_header(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-API-Version"] = "v1"
        return response

    api_router = APIRouter(prefix="/api/v1")
    api_router.include_router(health_router)
    api_router.include_router(examples_router)
    application.include_router(api_router)

    # Liveness en la raíz para sondas. El contrato de la API es GET /api/v1/health.
    application.include_router(health_router)

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
