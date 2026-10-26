from fastapi import APIRouter

from app.schemas.health import HealthResponse
from app.services.health import check_health

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Comprobar que el servicio está en pie",
)
async def get_health() -> HealthResponse:
    return check_health()
