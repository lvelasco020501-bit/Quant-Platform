"""SQLAlchemy implementation of the market-data unit of work.

Implements :class:`~quantplatform.core.interfaces.DataUnitOfWork` over an
:class:`~sqlalchemy.ext.asyncio.AsyncSession`. Entering the scope opens a session, leaving
it closes that session, and the transaction is only durable if :meth:`commit` was called
explicitly — an exception, or simply forgetting to commit, discards everything staged.
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from quantplatform.core.interfaces import IngestionRunRepository, MarketBarRepository
from quantplatform.storage.repository import (
    SqlAlchemyIngestionRunRepository,
    SqlAlchemyMarketBarRepository,
)

__all__ = ["SqlAlchemyDataUnitOfWork"]


class SqlAlchemyDataUnitOfWork:
    """A transactional scope over the market-data repositories.

    Args:
        session_factory: Produces the session backing one scope. A fresh session is opened
            per scope so that a rolled-back attempt leaves no residue in a session reused
            by the next one.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._bars: SqlAlchemyMarketBarRepository | None = None
        self._runs: SqlAlchemyIngestionRunRepository | None = None

    @property
    def bars(self) -> MarketBarRepository:
        """Return the market bar repository bound to this transaction."""
        if self._bars is None:
            msg = "unit of work is not active; use it as an async context manager"
            raise RuntimeError(msg)
        return self._bars

    @property
    def runs(self) -> IngestionRunRepository:
        """Return the ingestion run repository bound to this transaction."""
        if self._runs is None:
            msg = "unit of work is not active; use it as an async context manager"
            raise RuntimeError(msg)
        return self._runs

    async def __aenter__(self) -> SqlAlchemyDataUnitOfWork:
        """Open a session and bind the repositories to it."""
        self._session = self._session_factory()
        self._bars = SqlAlchemyMarketBarRepository(self._session)
        self._runs = SqlAlchemyIngestionRunRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back anything uncommitted, then close the session."""
        session = self._session
        self._session = None
        self._bars = None
        self._runs = None
        if session is None:  # pragma: no cover - defensive, __aenter__ always sets it
            return
        try:
            await session.rollback()
        finally:
            await session.close()

    async def commit(self) -> None:
        """Commit everything staged in this scope."""
        if self._session is None:
            msg = "unit of work is not active; use it as an async context manager"
            raise RuntimeError(msg)
        await self._session.commit()

    async def rollback(self) -> None:
        """Discard everything staged in this scope."""
        if self._session is None:
            msg = "unit of work is not active; use it as an async context manager"
            raise RuntimeError(msg)
        await self._session.rollback()
