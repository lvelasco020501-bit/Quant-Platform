"""Persistable state of a paper trading session.

A paper session runs for days or weeks against a live feed. A process restart in the middle
of one must not silently reset the account it was tracking, so this is the snapshot a
:class:`~quantplatform.core.interfaces.PaperStateRepository` stores and a resuming session
reads back.

**A snapshot, not a replay log.** Resuming by re-running every past decision would depend on
the strategy, the feed and the venue behaving identically the second time — which is exactly
the assumption a paper run exists to test. Storing the resulting balances and positions
instead means a resume starts from what the account actually became.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from quantplatform.core.enums import ExecutionMode
from quantplatform.core.models.base import AssetCode, DomainModel, Text, UtcDatetime
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.portfolio import Balance, Position
from quantplatform.core.models.risk import CircuitBreakerState, PositionRiskState
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.core.models.warm_start import WarmStartRecord
from quantplatform.core.numeric import Fee, Money

__all__ = ["CURRENT_SCHEMA_VERSION", "PaperSessionState"]

_RISK_SPLIT_SCHEMA_VERSION = 2
"""Version at which a position's risk became two figures rather than one."""

_WARM_START_SCHEMA_VERSION = 3
"""Version at which a snapshot began recording that its market context was restored."""

CURRENT_SCHEMA_VERSION = 3
"""Shape this code writes. Every snapshot it produces says so explicitly, so a reader
never has to infer a version from which fields happen to be present."""


class PaperSessionState(DomainModel):
    """Everything needed to resume a paper session where it left off."""

    schema_version: int = Field(default=1, ge=1)
    """Shape of this snapshot, so a reader never has to infer one.

    Absent means 1: the three sessions this platform has actually run predate every field
    added since, and they load because each of those fields defaults to what was historically
    true. That was luck rather than design — the next field whose default is *not* the
    historical truth would misread them in silence.

    Version 2 splits a position's risk into what it opened with and what it still carries. A
    version-1 snapshot cannot contain either, so one that claims to is refused rather than
    guessed at.

    Version 3 records whether the session's *market context* was restored at start-up. That
    record is audit only: it describes what happened and authorises nothing, and in
    particular it neither relaxes nor participates in the decision to resume an account.
    """

    session_id: Text
    strategy_id: Text
    execution_mode: ExecutionMode
    quote_asset: AssetCode

    started_at: UtcDatetime
    """When the session first began, preserved across every restart."""

    saved_at: UtcDatetime
    """When this snapshot was taken, from the session's injected clock."""

    balances: tuple[Balance, ...] = ()
    positions: tuple[Position, ...] = ()

    position_risk: tuple[PositionRiskState, ...] = ()
    """What each open position was protected by, and what it actually risked.

    Keyed by symbol, matching :attr:`positions` — spot holds one position per symbol, so
    the symbol is the association and no second identifier needs inventing.

    Empty for every session the platform has run, and deliberately empty rather than a
    record full of nulls: a
    :class:`~quantplatform.core.models.risk.PositionRiskState` requires a strictly positive
    ``risk_amount`` precisely so that it cannot claim protection it does not describe. A
    position with no quantified risk therefore has no entry here at all, which is a true
    statement, where an entry reporting ``None`` would be a misleading one.
    """

    breakers: tuple[CircuitBreakerState, ...] = ()
    """Every circuit breaker latched when this snapshot was taken, one entry per reason.

    Persisted so an operator can see *why* a session stopped, not so a process can carry the
    halt forward: a snapshot carrying one refuses to resume. That is the same fail-closed
    treatment :attr:`positions` and :attr:`position_risk` get, and for the same reason — the
    portfolio engine is flat-start, so a resumed process would rebuild an account with no
    memory of what halted it and begin trading again.

    Empty for every session the platform has run.
    """

    last_bar: MarketBar | None = None
    """The most recent bar the session finished processing.

    A resumed session refuses any bar that does not follow this one, which is what stops a
    replayed or backfilled feed from re-trading history the account has already lived through.
    """

    bars_processed: int = Field(default=0, ge=0)
    realized_pnl: Money = Decimal(0)
    total_fees: Fee = Decimal(0)
    restarts: int = Field(default=0, ge=0)
    """How many times this session has been resumed, recorded so an operator can tell a
    long-running session apart from one that keeps dying and coming back."""

    warm_start: WarmStartRecord | None = None
    """Audit of how this session's market context was obtained, when it was restored.

    Present only from :data:`_WARM_START_SCHEMA_VERSION`. It describes a start-up and
    authorises nothing: a snapshot carrying it is no more resumable than one without, and
    :attr:`financial_state_carried` neither reads it nor is affected by it. Keeping the two
    apart is the point — warm-start restores candles, resume restores an account, and a
    field that blurred them would be the first step towards a session that quietly traded a
    rebuilt account because its market history looked fine.
    """

    feed_baseline: FeedMetricsSnapshot | None = None
    """The feed reading the current reporting day started from.

    Persisted because the feed's counters are cumulative and survive nothing: a session
    that resumed with a zero baseline would report every candle since the feed started as
    if it had all happened today. ``None`` means no day has been reported yet, which is
    restored as a zero baseline — the same state a fresh session begins in.
    """

    @property
    def financial_state_carried(self) -> tuple[str, ...]:
        """Return every irrecoverable financial condition this snapshot holds.

        One definition, two consumers, deliberately. :meth:`PaperTradingSession.resume`
        refuses when this is non-empty because the portfolio engine is flat-start and a
        resumed process would trade a rebuilt account instead of this one; warm-start
        refuses to reuse such a session's market history because starting fresh from it
        would present an unreconciled account as a recovery. The judgement is the same
        judgement and the consequences differ, so copying the list into a second place would
        guarantee the two drift the day a seventh condition is added.

        Empty means the snapshot describes an account a fresh engine already matches.

        Returns:
            The conditions found, in a fixed order, phrased for an operator.
        """
        open_positions = [position for position in self.positions if position.is_open]
        reserved = [balance for balance in self.balances if balance.locked > Decimal(0)]
        blocking: list[str] = []
        if open_positions:
            blocking.append("an open position")
        if reserved:
            blocking.append("balance reserved against a working order")
        if self.realized_pnl != Decimal(0):
            blocking.append("realised pnl")
        if self.total_fees != Decimal(0):
            blocking.append("fees paid")
        if self.position_risk:
            blocking.append("recorded position risk")
        if self.breakers:
            blocking.append("a latched circuit breaker")
        return tuple(blocking)

    @model_validator(mode="after")
    def _validate_version(self) -> Self:
        """Check the snapshot's contents match the shape it claims.

        Raises:
            ValueError: If a version-1 snapshot carries state that only exists from version 2,
                which would leave a reader guessing what its numbers meant.
        """
        if self.schema_version < _WARM_START_SCHEMA_VERSION and self.warm_start is not None:
            msg = (
                "a snapshot below schema_version 3 cannot carry a warm-start record: the "
                "field arrived with version 3, and a snapshot claiming both is describing "
                "itself inconsistently"
            )
            raise ValueError(msg)
        if self.schema_version < _RISK_SPLIT_SCHEMA_VERSION and self.position_risk:
            msg = (
                "a snapshot at schema_version 1 cannot carry position risk: the split "
                "between initial and current risk arrived with version 2, and reading these "
                "numbers under either meaning would be a guess"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_timeline(self) -> Self:
        """Check that the snapshot describes a coherent point in time.

        Raises:
            ValueError: If the snapshot predates the session, or claims processed bars
                without recording which one was last.
        """
        if self.saved_at < self.started_at:
            msg = "saved_at must not precede the session start"
            raise ValueError(msg)
        if self.bars_processed > 0 and self.last_bar is None:
            msg = "a session that processed bars must record the last one"
            raise ValueError(msg)
        if self.last_bar is not None and not self.last_bar.is_closed:
            msg = "the last processed bar must be closed"
            raise ValueError(msg)
        return self
