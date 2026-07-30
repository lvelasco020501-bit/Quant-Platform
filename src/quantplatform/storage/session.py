"""Async engine and session construction from typed configuration.

The engine is built once from :class:`~quantplatform.config.settings.DatabaseSettings` by
the composition root (CLI, tests, or a future orchestration entry point) and handed to
repositories as an injected dependency; nothing in this module reads the environment
directly.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from quantplatform.config.settings import DatabaseSettings

__all__ = ["create_engine", "create_session_factory"]


def _enforce_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Turn on SQLite's foreign key enforcement, which is off by default.

    PostgreSQL enforces foreign keys unconditionally. SQLite does not unless asked, so a
    mapping that violates a foreign key would pass on SQLite and fail only in production —
    exactly the kind of divergence that makes a SQLite-backed test suite untrustworthy.
    Enabling the pragma keeps referential integrity identical on both backends.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _connection_record: Any) -> None:  # noqa: ANN401
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Build an async SQLAlchemy engine from database settings.

    Pool sizing is only meaningful for a server-based database: SQLite's async driver
    either rejects those keyword arguments outright (for an in-memory database, which is
    restricted to a single static connection) or has no server-side pool to size, so they
    are only passed through for non-SQLite dialects.

    Args:
        settings: Validated database configuration.

    Returns:
        An unconnected async engine; the caller owns its lifecycle and must dispose of it.
    """
    dsn = settings.dsn.get_secret_value()
    is_sqlite = make_url(dsn).get_dialect().name == "sqlite"
    if is_sqlite:
        engine = create_async_engine(dsn, echo=settings.echo)
        _enforce_sqlite_foreign_keys(engine)
        return engine
    return create_async_engine(
        dsn,
        echo=settings.echo,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        connect_args={
            "options": f"-c statement_timeout={settings.statement_timeout_seconds * 1000}"
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to an engine.

    ``expire_on_commit`` is disabled because repository methods construct and return
    plain domain models before the surrounding transaction commits; ORM attribute access
    must not trigger an implicit refresh after that point.

    Args:
        engine: Engine produced by :func:`create_engine`.

    Returns:
        A callable that produces new :class:`~sqlalchemy.ext.asyncio.AsyncSession` instances.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
