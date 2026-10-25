"""Routers HTTP.

El prefijo global `/api/v1` lo aplica `create_app()` al montar `api_router`.
`GET /health` en la raíz es un alias de liveness; el contrato de la API
es `GET /api/v1/health`. `GET /api/v1/health/db` comprueba Postgres.
"""

from app.routers.admin_roles import router as admin_roles_router
from app.routers.admin_users import router as admin_users_router
from app.routers.auth import router as auth_router
from app.routers.courses import router as courses_router
from app.routers.examples import router as examples_router
from app.routers.health import router as health_router
from app.routers.users import router as users_router

__all__ = [
    "admin_roles_router",
    "admin_users_router",
    "auth_router",
    "courses_router",
    "examples_router",
    "health_router",
    "users_router",
]
