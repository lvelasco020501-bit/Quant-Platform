"""The Donchian breakout, taken only while price sits above a long simple average.

**Promoted to paper.** This rule spent M22, M23 and M24 in the research harness under the
name ``breakout_trend`` and at the numbers 40/20/400, and it is the strategy the M23 verdict
called a PAPER CANDIDATE on BTC. It lives here, rather than in ``strategies/research.py``,
because the registry the paper runner resolves through cannot import research — the arrow
goes the other way — and a promoted strategy has to be importable by the thing that runs it.

**The class is the one research measured, not a copy of it.** It was moved, not rewritten,
and ``strategies/research.py`` now imports it from here so there is exactly one
implementation. A test holds the two registries to that.

**Nothing about the rule changed on promotion, and nothing may.** Being registered for paper
is a decision about where a strategy may run; it is not a licence to retune it. The
parameters a session runs are supplied by configuration, and the ones M23 validated are
recorded in ``docs/m23_b2_final_validation.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, ClassVar

from pydantic import BaseModel, Field

from quantplatform.core.enums import PositionState
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.strategies.parametric import FROZEN, ParametricStrategy, Window, metadata_for

__all__ = ["TrendFilteredBreakoutParameters", "TrendFilteredBreakoutStrategy"]


class TrendFilteredBreakoutParameters(BaseModel):
    """Parameters for :class:`TrendFilteredBreakoutStrategy`."""

    model_config = FROZEN
    entry_lookback: Annotated[int, Field(ge=2, le=500)]
    exit_lookback: Annotated[int, Field(ge=2, le=500)]
    trend_period: Window


class TrendFilteredBreakoutStrategy(ParametricStrategy):
    """The Donchian breakout, taken only above a long simple average."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
        "breakout_trend",
        "Breakout with trend filter",
        "Long-only Donchian breakout that only enters while the close is above a long simple "
        "moving average; the exit is the plain breakdown and ignores the filter.",
        TrendFilteredBreakoutParameters,
        ("donchian_high_20", "donchian_low_10", "sma_200"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the two channel levels and the trend average."""
        p = self._typed(TrendFilteredBreakoutParameters)
        return (
            f"donchian_high_{p.entry_lookback}",
            f"donchian_low_{p.exit_lookback}",
            f"sma_{p.trend_period}",
        )

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter on a new high above the trend average; exit on a new low regardless."""
        high_name, low_name, trend_name = self._names
        bar = context.latest_bar
        if context.position_state is PositionState.FLAT:
            values = self._read(context, high_name, trend_name)
            if values is None:
                return ()
            level, trend = values
            if bar.high > level and bar.close > trend:
                return self._enter(
                    context,
                    f"high {bar.high} broke {level} with close above {trend_name} {trend}",
                    {high_name: level, trend_name: trend},
                )
            return ()
        if context.position_state is PositionState.LONG:
            values = self._read(context, low_name)
            if values is None:
                return ()
            (floor,) = values
            if bar.low < floor:
                return self._exit(context, f"low {bar.low} broke {floor}", {low_name: floor})
        return ()
