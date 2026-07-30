"""Database fixtures for the integration tests.

These exercise the real SQLAlchemy repositories against a real database engine. SQLite is
used by default so the suite runs anywhere, and the custom
:class:`~quantplatform.storage.types.ExactNumeric` column type is what makes that faithful:
it stores exact decimal text on SQLite instead of SQLAlchemy's default binding through a C
double, which measurably loses precision. Point ``QP_TEST_DATABASE_DSN`` at a PostgreSQL
instance to run the identical tests against the production target.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from quantplatform.config.settings import DatabaseSettings
from quantplatform.storage.orm import Base
from quantplatform.storage.session import create_engine, create_session_factory

TEST_DSN_ENV_VAR = "QP_TEST_DATABASE_DSN"


@pytest.fixture
def database_settings(tmp_path: Path) -> DatabaseSettings:
    """Return settings for a throwaway database, honouring an external DSN when given."""
    dsn = os.environ.get(TEST_DSN_ENV_VAR) or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    return DatabaseSettings(dsn=SecretStr(dsn))


@pytest.fixture
async def engine(database_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    """Provide an engine over a schema created fresh for the test and dropped after."""
    built = create_engine(database_settings)
    async with built.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield built
    finally:
        async with built.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await built.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the test engine."""
    return create_session_factory(engine)
