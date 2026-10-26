from typing import Literal

from pydantic import Field

from app.schemas.base import APIModel


class HealthResponse(APIModel):
    """Respuesta de `GET /api/v1/health` (y del alias de liveness `GET /health`)."""

    status: Literal["ok"] = Field(examples=["ok"])
