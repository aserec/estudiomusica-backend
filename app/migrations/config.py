"""Configuración de Alembic usable desde el paquete instalado.

El ``alembic.ini`` de la raíz del repo no viaja en el wheel
(``packages = ["app"]``). Este helper construye un ``Config``
programático con ``script_location = app:migrations``.
"""

from alembic.config import Config


def make_alembic_config(database_url: str | None = None) -> Config:
    """Config de Alembic sin depender de ``alembic.ini`` en el CWD.

    Desde el paquete instalado::

        from alembic import command
        from app.migrations.config import make_alembic_config

        command.upgrade(make_alembic_config(), "head")

    Si ``database_url`` es ``None``, ``env.py`` lee ``DATABASE_URL``.
    """

    config = Config()
    config.set_main_option("script_location", "app:migrations")
    config.set_main_option("prepend_sys_path", ".")
    config.set_main_option("path_separator", "os")
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config
