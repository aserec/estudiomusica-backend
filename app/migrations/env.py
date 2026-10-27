"""Configuración async de Alembic.

El DSN sale de ``sqlalchemy.url`` (alembic.ini o ``set_main_option``)
si está definido; si no, de ``Settings.resolved_database_url()``.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import asyncpg_url_and_connect_args, normalize_asyncpg_ssl_url
from app.migrations.autogen import include_name, process_revision_directives
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_database_url() -> str:
    """DSN crudo. ``run_async_migrations`` lo pasa por
    ``asyncpg_url_and_connect_args``: ``sslmode`` → ``ssl`` (también
    ``verify-full`` / ``require`` sin ``sslrootcert``) y ``sslrootcert``
    → ``SSLContext``. Sin eso, asyncpg caería en ``prefer``.
    """

    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    if configured:
        return normalize_asyncpg_ssl_url(configured)
    get_settings.cache_clear()
    return normalize_asyncpg_ssl_url(get_settings().resolved_database_url())


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_server_default=True,
        include_schemas=True,
        include_name=include_name,
        process_revision_directives=process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
        include_schemas=True,
        include_name=include_name,
        process_revision_directives=process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url, connect_args = asyncpg_url_and_connect_args(get_database_url())
    server_settings = dict(connect_args.get("server_settings") or {})
    app_name = (os.environ.get("ALEMBIC_APPLICATION_NAME") or "").strip()
    if app_name:
        server_settings["application_name"] = app_name
    lock_timeout = (os.environ.get("ALEMBIC_LOCK_TIMEOUT") or "").strip()
    if lock_timeout:
        server_settings["lock_timeout"] = lock_timeout
    if server_settings:
        connect_args["server_settings"] = server_settings
    connectable = create_async_engine(
        url,
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
