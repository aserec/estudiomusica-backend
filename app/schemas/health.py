from typing import Literal

from pydantic import Field

from app.schemas.base import APIModel


class HealthResponse(APIModel):
    """Respuesta de `GET /api/v1/health` (y del alias de liveness `GET /health`)."""

    status: Literal["ok"] = Field(examples=["ok"])


class DatabaseHealthResponse(APIModel):
    """Respuesta de la sonda de Postgres (`GET /api/v1/health/db`).

    El contrato de la issue es ``{"db":"ok"}`` / ``{"db":"down"}``, no el
    sobre de error de negocio. Así las sondas no dependen de ``error``.
    """

    db: Literal["ok", "down"] = Field(examples=["ok"])
