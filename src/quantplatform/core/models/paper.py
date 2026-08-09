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
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.core.numeric import Fee, Money

__all__ = ["PaperSessionState"]


class PaperSessionState(DomainModel):
    """Everything needed to resume a paper session where it left off."""

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

    feed_baseline: FeedMetricsSnapshot | None = None
    """The feed reading the current reporting day started from.

    Persisted because the feed's counters are cumulative and survive nothing: a session
    that resumed with a zero baseline would report every candle since the feed started as
    if it had all happened today. ``None`` means no day has been reported yet, which is
    restored as a zero baseline — the same state a fresh session begins in.
    """

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
