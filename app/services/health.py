from app.schemas.health import HealthResponse


def check_health() -> HealthResponse:
    """Comprueba que el proceso responde. Sin dependencias externas."""

    return HealthResponse(status="ok")
