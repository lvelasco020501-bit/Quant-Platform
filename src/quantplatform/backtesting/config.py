"""Configuration for a single backtest run.

Everything that shapes a run and is not itself a component. The engine's collaborators — the
strategy, the feature pipeline, the risk engine, the broker and the portfolio — are injected
separately; what remains here is the account it starts with, how signals are sized into
intents, and the assumptions the performance summary is computed under.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantplatform.core.constants import DAYS_PER_YEAR, SECONDS_PER_DAY
from quantplatform.core.enums import ExecutionMode, Timeframe
from quantplatform.core.models.base import AssetCode, UtcDatetime
from quantplatform.core.numeric import NonNegativeMoney, Rate

__all__ = ["BacktestConfig"]

_SECONDS_PER_YEAR = SECONDS_PER_DAY * DAYS_PER_YEAR


class BacktestConfig(BaseModel):
    """Immutable settings for one backtest run."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    run_id: str = Field(default="backtest", min_length=1, max_length=64)
    """Stable label mixed into derived identifiers, so two configured runs over the same data
    do not collide while a repeat of the same run reproduces its ids exactly."""

    quote_asset: AssetCode = "USDT"
    initial_capital: NonNegativeMoney = Decimal(10_000)
    execution_mode: ExecutionMode = ExecutionMode.BACKTEST
    timeframe: Timeframe = Timeframe.H1

    entry_fraction: Rate = Decimal("0.95")
    """Share of current equity a long entry asks for, before the risk engine sizes it.

    A request, not an authorisation: the risk engine reduces it for balance, exposure and
    venue limits, and may reject it outright. Sizing lives there precisely so that a strategy
    — which cannot see the account at all — never decides how much to buy.
    """

    started_at: UtcDatetime | None = None
    """Instant the broker's clock starts at; defaults to the first bar's open time."""

    assumed_spread_basis_points: NonNegativeMoney | None = None
    """The spread the run assumes, in basis points, or ``None`` to assume nothing.

    An OHLCV bar records no bid and no ask, so a backtest cannot *measure* spread — it can
    only be told what to assume. That assumption is stated here, explicitly and recorded with
    the run, rather than defaulted to zero: a silent zero-spread assumption is one of the
    most reliable ways to make a strategy look profitable that is not.

    Leaving it ``None`` while the risk engine runs in strict-missing-metrics mode means every
    intent is refused for want of the metric. The engine checks for that combination before
    the run starts and says so, rather than producing a run of uniform rejections.
    """

    volatility_window: int = Field(default=20, ge=2)
    """Trailing closes used to measure realised volatility, which *is* computable from bars."""

    risk_free_rate: Rate = Decimal(0)
    """Annualised risk-free rate used by the Sharpe and Sortino ratios."""

    minimum_periods_for_ratios: int = Field(default=2, ge=2)
    """Bars of return history below which Sharpe and Sortino report "not computable".

    A ratio computed from one observation is not a weak estimate, it is arithmetic with no
    meaning; reporting nothing is more useful than reporting a number nobody should read.
    """

    @model_validator(mode="after")
    def _validate_capital(self) -> Self:
        """Require capital that can actually fund a trade.

        Raises:
            ValueError: If the account starts with nothing to trade.
        """
        if self.initial_capital <= 0:
            msg = "initial_capital must be strictly positive"
            raise ValueError(msg)
        return self

    @property
    def periods_per_year(self) -> Decimal:
        """Return how many bars of this timeframe fall in a year, for annualisation."""
        return Decimal(_SECONDS_PER_YEAR) / Decimal(self.timeframe.seconds)
