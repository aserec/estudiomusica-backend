import functools
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.config import get_settings
from app.db import dispose_engine
from app.errors import register_exception_handlers
from app.middleware import (
    ApiErrorMiddleware,
    BodySizeLimitMiddleware,
    RejectedPreflightMiddleware,
)
from app.routers import (
    admin_roles_router,
    admin_users_router,
    auth_router,
    courses_router,
    examples_router,
    health_router,
    users_router,
)
from app.schemas.errors import ErrorResponse

logger = logging.getLogger(__name__)

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
- Postgres: `GET /api/v1/health/db` → `{"db":"ok"}` o `503` `{"db":"down"}`.
- Auth: `POST /api/v1/auth/signup|login|logout` y `GET /api/v1/auth/me`.
- Roles: `GET|PATCH|DELETE /api/v1/admin/system-roles` y
  `GET /api/v1/courses/{course_id}/me/permissions`.
- Admin users: `GET|PUT|DELETE /api/v1/admin/users` y
  `GET /api/v1/users/search`.
"""

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": (
            "Liveness (`/api/v1/health`) y readiness de Postgres (`/api/v1/health/db`)."
        ),
    },
    {
        "name": "auth",
        "description": (
            "Registro, login, logout y sesión. Cookie HttpOnly + JWT HS256."
        ),
    },
    {
        "name": "admin",
        "description": (
            "Roles de sistema y administración de usuarios. "
            "Requiere `site:manage-admins`. "
            "PATCH de system-roles (excepción al PUT de #91); "
            "identidad de usuario va por PUT."
        ),
    },
    {
        "name": "users",
        "description": (
            "Búsqueda de usuarios para matricular. "
            "Admin o profesor de un curso. Paginación page/pageSize."
        ),
    },
    {
        "name": "courses",
        "description": (
            "Permisos del usuario actual en un curso. "
            "La matrícula se asigna en #403; aquí solo se lee."
        ),
    },
    {
        "name": "examples",
        "description": "Demostración de convenciones. No es lógica de negocio.",
    },
]

if TYPE_CHECKING:
    app: FastAPI


def _package_version() -> str:
    try:
        return version("estudiomusica-backend")
    except PackageNotFoundError:
        return "0.0.0"


@asynccontextmanager
async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def _cors_origin_regex(origins: list[str]) -> str | None:
    if not origins or "*" in origins:
        return None
    return "(?i)^(?:" + "|".join(re.escape(origin) for origin in origins) + ")$"


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
        lifespan=lifespan,
        contact={
            "name": "EstudioMusica",
            "url": "https://estudiomusica.com",
        },
        openapi_tags=OPENAPI_TAGS,
    )
    if settings.expose_errors and settings.app_env == "production":
        logger.warning(
            "EXPOSE_ERRORS=true en production; los 500 incluirán detalles internos"
        )
    register_exception_handlers(application, settings)
    application.add_middleware(BodySizeLimitMiddleware, settings=settings)
    application.add_middleware(ApiErrorMiddleware, settings=settings)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_origin_regex=_cors_origin_regex(settings.cors_origin_list()),
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_method_list(),
        allow_headers=settings.cors_header_list(),
        expose_headers=["X-API-Version"],
    )
    application.add_middleware(RejectedPreflightMiddleware, settings=settings)

    api_router = APIRouter(prefix="/api/v1")
    api_router.include_router(health_router)
    api_router.include_router(auth_router)
    api_router.include_router(admin_roles_router)
    api_router.include_router(admin_users_router)
    api_router.include_router(users_router)
    api_router.include_router(courses_router)
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


@functools.cache
def get_app() -> FastAPI:
    return create_app()


def __getattr__(name: str) -> object:
    """Expone `app` de forma perezosa para Uvicorn/FastAPI CLI.

    Importar este módulo no instancia Settings, así un APP_ENV inválido
    en la shell no rompe la recogida de tests.
    """
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), "app"])
