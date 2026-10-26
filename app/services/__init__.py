"""Servicios de aplicación.

Reservado para la lógica de negocio. Este esqueleto no implementa
ningún caso de uso más allá del chequeo de salud.
"""

from app.services.health import check_health

__all__ = ["check_health"]
