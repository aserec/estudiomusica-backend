"""Routers HTTP.

El prefijo global `/api/v1` lo aplica `create_app()` al montar `health_router`
(y, más adelante, el resto de recursos). `GET /health` en la raíz es un alias
de liveness; el contrato de la API es `GET /api/v1/health`.
"""

from app.routers.health import router as health_router

__all__ = ["health_router"]
