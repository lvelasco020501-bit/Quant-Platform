"""Alembic migration environment.

The target database is resolved from the project's own typed configuration
(:func:`quantplatform.config.settings.load_settings`) rather than from ``alembic.ini``, so
overriding ``QP_DATABASE__DSN`` in the environment redirects migrations exactly the way it
redirects the running application — including to a throwaway SQLite database in tests,
with no special-casing here.

The engine is built here rather than through
:func:`quantplatform.storage.session.create_engine` because a migration has a different
connection lifecycle from the running application: it is a one-shot operation, so it uses
``NullPool`` and leaves no pooled connection to outlive the ``asyncio.run`` that drove it.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from quantplatform.config.settings import load_settings
from quantplatform.storage.orm import Base

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which switches off every logger that
    # already exists — including application loggers created at import time. A process that
    # runs a migration and then logs would go silent, which is the worst possible way for
    # logging to fail: nothing is raised and nothing is written.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _target_dsn() -> str:
    """Return the DSN migrations should connect to, from project settings."""
    return load_settings().database.dsn.get_secret_value()


def run_migrations_offline() -> None:
    """Emit migration SQL without a live database connection."""
    context.configure(
        url=_target_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations against an already-open synchronous-facing connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations over a single, unpooled connection, then dispose of the engine."""
    connectable = create_async_engine(_target_dsn(), poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
