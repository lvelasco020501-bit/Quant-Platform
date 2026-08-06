"""Capturing and restoring a paper session across restarts.

The persistence *port* lives in :mod:`quantplatform.core.interfaces`; what lives here is the
translation between a running session's components and the immutable
:class:`~quantplatform.core.models.paper.PaperSessionState` a repository stores, plus an
in-memory repository for tests and local runs.

No SQL, no key-value store, no files. A composition root supplies the durable implementation;
keeping the choice out of here is what lets the session be tested without one.
"""

from __future__ import annotations

from quantplatform.core.errors import PaperSessionStateError
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.portfolio import Balance

__all__ = ["InMemoryPaperStateRepository", "restore_balances"]


class InMemoryPaperStateRepository:
    """A repository that forgets everything when the process ends.

    Exactly what a test wants and exactly what production does not: it satisfies the port so
    a session can be exercised end to end, while making its non-durability obvious at the call
    site rather than discovering it after a restart.
    """

    def __init__(self) -> None:
        self._states: dict[str, PaperSessionState] = {}

    def load(self, session_id: str) -> PaperSessionState | None:
        """Return the stored state for a session, or ``None``."""
        return self._states.get(session_id)

    def save(self, state: PaperSessionState) -> None:
        """Store a session's state, replacing any previous snapshot."""
        self._states[state.session_id] = state

    def delete(self, session_id: str) -> None:
        """Remove a session's stored state; a no-op when nothing was stored."""
        self._states.pop(session_id, None)

    def __len__(self) -> int:
        """Return how many sessions are held."""
        return len(self._states)


def restore_balances(state: PaperSessionState) -> tuple[Balance, ...]:
    """Return the balances a resumed session's portfolio should be constructed with.

    Open positions cannot be restored: :class:`SpotPortfolioEngine` is flat-start by
    construction (Phase 3A), and inventing a seeding path here would bypass the reconciliation
    invariant that keeps a position and its base balance in lockstep. A session holding an
    open position therefore refuses to resume rather than resuming into an account whose books
    do not agree with themselves — see :class:`~quantplatform.paper.session.PaperTradingSession`.

    Args:
        state: The snapshot being resumed from.

    Returns:
        The balances to seed the portfolio with.

    Raises:
        PaperSessionStateError: If the snapshot holds an open position.
    """
    open_positions = [position for position in state.positions if position.is_open]
    if open_positions:
        raise PaperSessionStateError(
            "cannot resume a session holding an open position: the portfolio engine is "
            "flat-start, and seeding one would bypass its reconciliation invariant",
            session_id=state.session_id,
            symbols=[position.symbol for position in open_positions],
        )
    return tuple(balance for balance in state.balances if balance.asset == state.quote_asset)
