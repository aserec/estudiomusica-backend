from fastapi import APIRouter, Response, status

from app.schemas.health import DatabaseHealthResponse, HealthResponse
from app.services.health import check_database, check_health

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Comprobar que el servicio está en pie",
)
async def get_health() -> HealthResponse:
    return check_health()


@router.get(
    "/health/db",
    response_model=DatabaseHealthResponse,
    summary="Comprobar la conexión a Postgres",
    description=(
        'Sonda de readiness de la base de datos. Responde `{"db":"ok"}` '
        'si Postgres acepta un `SELECT 1`; si no, `503` con `{"db":"down"}`. '
        "No usa el sobre `{error, message}` de las rutas de negocio: el "
        "contrato de la issue es este cuerpo mínimo, sin credenciales."
    ),
    responses={
        200: {
            "description": "Postgres disponible.",
            "content": {"application/json": {"example": {"db": "ok"}}},
        },
        503: {
            "description": (
                "Postgres no disponible. Sin credenciales ni sobre de error."
            ),
            "content": {"application/json": {"example": {"db": "down"}}},
        },
    },
)
async def get_database_health(response: Response) -> DatabaseHealthResponse:
    result = await check_database()
    if result.db == "down":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
