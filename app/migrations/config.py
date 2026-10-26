"""Configuración de Alembic usable desde el paquete instalado.

La fuente de verdad es ``app/alembic.ini`` (viaja en el wheel).
El ``alembic.ini`` de la raíz es un enlace a ese fichero para que
``uv run alembic`` siga funcionando en el repo.
"""

from importlib.resources import files
from pathlib import Path

from alembic.config import Config


def packaged_alembic_ini() -> Path:
    return Path(str(files("app").joinpath("alembic.ini")))


def make_alembic_config(database_url: str | None = None) -> Config:
    """Config de Alembic leyendo el ini empaquetado.

    No añade el CWD a ``sys.path``: un directorio ``app/`` local no
    puede sustituir al paquete instalado.

    Desde el paquete instalado::

        from alembic import command
        from app.migrations.config import make_alembic_config

        command.upgrade(make_alembic_config(), "head")
    """

    config = Config(str(packaged_alembic_ini()))
    config.set_main_option("prepend_sys_path", "")
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config
