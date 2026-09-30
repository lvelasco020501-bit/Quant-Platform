"""Driving a frozen strategy over one market to recover *when* it is long, and nothing else.

A portfolio of several rules over thirty markets needs one thing from each pair: the stretches
during which that rule wants to be in that market. It does not need the rule's own position
sizing, because the portfolio's declared allocation replaces it.

**Why this exists rather than the engine.** ``BacktestEngine`` is O(n squared) in history length
-- measured here at 3.5s, 13.7s and 55.4s for 2k, 4k and 8k bars, which extrapolates to 339s for
a full 4h series and 5.6 hours for sixty of them. That is not a corner worth cutting quietly, so
it is cut loudly: this module runs the **same frozen strategy objects** against the **same
production feature pipeline** the engine builds, in one pass, and a companion script holds its
output to the engine's own trade list. Nothing here reimplements a strategy, a feature or an
indicator.

**What it deliberately leaves out, and what that costs.** Risk V2. No breaker, no stop, no order
rejection, no venue minimum. A strategy's signal is not the same thing as a fill, and this module
produces the former. ``scripts/m34_verify.py`` measures the difference across twelve series
instead of leaving it to be assumed, and the answer is that it is large:

* 321 of the engine's 322 trades open inside one of the stretches this module reports, so the two
  agree about when a rule speaks.
* The engine turns over **1.5 to 10.5 times** as often, because its stop goes flat inside a
  stretch the strategy still wants and the strategy then re-enters within it.
* Position state is an input to these strategies, so a stop can also produce an entry the
  unstopped rule never takes. One trade in 322 did exactly that.

Anything built on this module is therefore a study of a rule's own entry and exit logic, not of
the deployed strategy. That is a statement to put in a report, not a footnote to bury.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol

from quantplatform.core.enums import MarketType, PositionState, SignalAction
from quantplatform.core.models.base import DomainModel, UtcDatetime
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.signals import StrategyContext

if TYPE_CHECKING:
    from quantplatform.core.interfaces import FeaturePipeline
    from quantplatform.core.models.market import MarketBar
    from quantplatform.strategies.base import BaseStrategy

__all__ = ["Interval", "PipelineFactory", "long_intervals", "long_mask", "permissive_rules"]


class PipelineFactory(Protocol):
    """Builds the feature pipeline a strategy declared it needs.

    Declared rather than imported, for the same reason
    :class:`~quantplatform.research.runner.BacktestFactory` is: research may not reach into
    orchestration, because assembling a pipeline is a composition decision and the architecture
    allows exactly one place to make it. The caller passes ``features_for``.
    """

    def __call__(self, strategy: BaseStrategy) -> FeaturePipeline:
        """Return a pipeline producing the features this strategy requires."""
        ...


_EPOCH: Final[datetime] = datetime(2017, 1, 1, tzinfo=UTC)
"""Stamp on the placeholder venue rules: before any bar in any dataset here, and read by nothing.
It exists because the model requires it."""

_TINY: Final[Decimal] = Decimal("0.00000001")


class Interval(DomainModel):
    """One stretch during which a rule wanted to be long, by the bar it decided on."""

    entered_at: UtcDatetime
    exited_at: UtcDatetime | None
    """``None`` when the run ended with the rule still long."""


def permissive_rules(symbol: str) -> SymbolRules:
    """Return venue rules loose enough to constrain nothing.

    Signal generation reads none of these -- a strategy is handed the rules so that it *could*,
    and none of the frozen rules here does. They are permissive rather than realistic so a
    missing rules file for a pool market cannot silently change a signal, and a test asserts that
    two different rule sets produce identical signals.
    """
    base, quote = symbol.split("/")
    return SymbolRules(
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        market_type=MarketType.SPOT,
        price_tick=_TINY,
        quantity_step=_TINY,
        min_quantity=_TINY,
        min_notional=_TINY,
        source="m34_signal_driver",
        updated_at=_EPOCH,
    )


def long_intervals(
    strategy: BaseStrategy, bars: Sequence[MarketBar], *, pipelines: PipelineFactory
) -> tuple[Interval, ...]:
    """Return every stretch this strategy wanted to be long over ``bars``.

    Args:
        strategy: A built, frozen strategy. Its own metadata decides how much history it sees.
        bars: One market's closed bars in ascending open-time order.
        pipelines: Builds the feature pipeline; supplied by a composition root so this module
            reads the production one without importing it.

    Returns:
        Intervals keyed by the *decision* bar: ``entered_at`` is the open time of the bar whose
        close produced the entry signal, and ``exited_at`` the bar whose close produced the exit.
        What a portfolio then does about that is the portfolio's business, not this module's.
    """
    if not bars:
        return ()
    pipeline = pipelines(strategy)
    depth = max(strategy.metadata.required_history, pipeline.required_history)
    rules = permissive_rules(bars[0].symbol)
    state = PositionState.FLAT
    opened: datetime | None = None
    out: list[Interval] = []
    for index, bar in enumerate(bars):
        window = tuple(bars[max(0, index + 1 - depth) : index + 1])
        context = StrategyContext(
            symbol=bar.symbol,
            market_type=bar.market_type,
            timeframe=bar.timeframe,
            as_of=bar.close_time,
            bars=window,
            features=dict(pipeline.compute(window)),
            position_state=state,
            symbol_rules=rules,
        )
        for signal in strategy.generate(context):
            if signal.action is SignalAction.ENTER_LONG and state is PositionState.FLAT:
                state, opened = PositionState.LONG, bar.open_time
            elif signal.action is SignalAction.EXIT_LONG and state is PositionState.LONG:
                if opened is not None:
                    out.append(Interval(entered_at=opened, exited_at=bar.open_time))
                state, opened = PositionState.FLAT, None
    if opened is not None:
        out.append(Interval(entered_at=opened, exited_at=None))
    return tuple(out)


def long_mask(intervals: Sequence[Interval], grid: Sequence[datetime]) -> tuple[bool, ...]:
    """Return, per grid slot, whether the rule was long having decided at that slot.

    The entry slot is included and the exit slot is not: a rule that says "leave" on a bar is not
    long over the bar that follows, which is the one a portfolio would have held it for.
    """
    out: list[bool] = []
    for stamp in grid:
        held = any(
            stamp >= interval.entered_at
            and (interval.exited_at is None or stamp < interval.exited_at)
            for interval in intervals
        )
        out.append(held)
    return tuple(out)
