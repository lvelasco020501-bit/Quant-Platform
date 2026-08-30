"""A round trip that ended, and what it should be measured against.

A trade is a position lifecycle that returned to flat — not a fill, and not a reduction. What
makes it worth a model rather than a number is the denominator: an R-multiple is meaningless
unless the risk it divides by is the risk *that* position opened with, and that figure is
deleted from the run's live state the moment the position goes flat.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.models.base import DomainModel, Symbol, UtcDatetime
from quantplatform.core.numeric import Money, NonNegativeMoney

__all__ = ["ClosedTrade"]


class ClosedTrade(DomainModel):
    """One completed round trip."""

    symbol: Symbol
    realized_pnl: Money
    """Cumulative realised profit or loss of the lifecycle, net of every fee it paid."""

    initial_risk_amount: NonNegativeMoney | None = None
    """What the position risked when it opened, or ``None`` when it recorded no risk.

    ``None`` rather than zero. Every run this platform completed before position risk existed
    produced trades with no denominator at all, and reporting zero would read as "this trade
    broke even against its risk" — a different and false claim.
    """

    opened_at: UtcDatetime
    closed_at: UtcDatetime

    @property
    def r_multiple(self) -> Decimal | None:
        """Return the result as a multiple of what was risked, if that is knowable."""
        if self.initial_risk_amount is None or self.initial_risk_amount <= ZERO:
            return None
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            return self.realized_pnl / self.initial_risk_amount
