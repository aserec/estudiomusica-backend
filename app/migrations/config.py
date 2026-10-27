"""Configuración de Alembic usable desde el paquete instalado.

La fuente de verdad es ``app/alembic.ini`` (viaja en el wheel).
El ``alembic.ini`` de la raíz es un fichero regular (no un enlace)
con las mismas claves operativas (``script_location``,
``prepend_sys_path``, loggers) para que ``uv run alembic`` funcione
en el repo y el sdist no lleve symlinks. No es un delegado: es una
copia; la fuente empaquetada es ``app/alembic.ini``.
"""

from importlib.resources import files
from pathlib import Path

from alembic.config import Config


def packaged_alembic_ini() -> Path:
    return Path(str(files("app").joinpath("alembic.ini")))


def make_alembic_config(database_url: str | None = None) -> Config:
    """Config de Alembic leyendo el ini empaquetado.

    No añade el CWD a ``sys.path``. ``python -c`` / ``python -m alembic``
    desde un directorio con un ``app/`` local sí importan ese ``app``
    (``sys.path[0]``); hay que invocarlos desde un CWD sin ese paquete,
    o usar ``alembic -c app/alembic.ini`` / el ini empaquetado.

    Desde el paquete instalado, en un directorio sin ``app/`` local::

        from alembic import command
        from app.migrations.config import make_alembic_config

        command.upgrade(make_alembic_config(), "head")
    """

    config = Config(str(packaged_alembic_ini()))
    config.set_main_option("prepend_sys_path", "")
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config
