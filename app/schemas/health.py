from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Respuesta de `GET /api/v1/health` (y del alias de liveness `GET /health`)."""

    status: Literal["ok"] = Field(examples=["ok"])
