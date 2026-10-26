"""Servicios de aplicación.

La lógica de negocio llega en issues posteriores. Aquí solo hay
el chequeo de salud y un listado fijo para demostrar paginación.
"""

from app.services.health import check_health

__all__ = ["check_health"]
