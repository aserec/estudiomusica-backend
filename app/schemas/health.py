from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Respuesta de `GET /health`."""

    status: Literal["ok"] = Field(examples=["ok"])
