"""Routers HTTP.

El prefijo global `/api/v1` lo aplica `create_app()` al montar `api_router`.
`GET /health` en la raíz es un alias de liveness; el contrato de la API
es `GET /api/v1/health`.
"""

from app.routers.examples import router as examples_router
from app.routers.health import router as health_router

__all__ = ["examples_router", "health_router"]
