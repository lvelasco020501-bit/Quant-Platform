"""The M15 protocol: six and a half years, three timeframes, and risk converted explicitly.

M14 answered its questions on one year and reused every risk parameter unchanged across
timeframes, so a seven-day time stop became 168 *days* on daily bars and a 300 bps stop sized
for an hour's noise sat inside a single day's range. This module fixes both before anything
runs: the dataset spans 2020-01-01 to 2026-09-15, and each timeframe-dependent risk parameter is
converted by a stated rule, with every other field classified as deliberately unchanged.

**The conversions** (from the 1h production configuration, :data:`RISK_CONVERSIONS`):

* **Price distances** — initial stop, break-even, trailing activation and distance,
  take-profit, and the risk budget's minimum and maximum stop — scale with **√(bar length /
  1h)**. Under a random walk the typical price excursion over an interval grows with the square
  root of its length, so the stop sits the same number of "bars of noise" away on every
  timeframe. Times 2 at 4h, times √24 ≈ 4.899 at 1d, rounded half-even to whole basis points.
* **The per-bar volatility limit** scales the same way, being a per-bar standard deviation;
  rounded half-even to four places.
* **The time stop keeps its time, not its bar count**: 168 hourly bars are seven days, so 42
  four-hour bars and 7 daily ones.
* **Everything else is unchanged, each for a stated reason**: risk per trade and exposure caps
  are policies on capital; daily loss and daily drawdown are per calendar day; total drawdown
  and its latch are on equity; the loss streak counts trades; rate limits and staleness are in
  wall-clock time; spread, buffers and costs are per order. Keeping 1% risk per trade with a
  wider stop means a smaller position — about a third of equity at 1h, under a tenth at 1d —
  which is intended: it is the same risk, not the same exposure.

None of this was chosen by looking at a result. The rules are conventions, fixed here, and
tested; the strategies keep M13's canonical parameters in bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from typing import Final

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION
from quantplatform.core.enums import Timeframe
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.folds import Fold, WindowSpec
from quantplatform.research.latch_policy import LatchPolicy, risk_configuration_for
from quantplatform.research.m14 import POLICIES as M14_POLICIES
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m14 import STUDY_STRATEGIES as M14_STRATEGIES
from quantplatform.research.sprint import Family, SprintCandidate, Verdict
from quantplatform.risk.config import RiskConfiguration
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "DATASET_SOURCE",
    "DATA_END",
    "DATA_START",
    "MIN_PAPER_TRADES",
    "MIN_TRADES_PER_TEST_WINDOW",
    "POLICIES",
    "RISK_CONVERSIONS",
    "RISK_RATIONALE",
    "STUDY_STRATEGIES",
    "TIMEFRAMES",
    "YEARLY_WINDOWS",
    "Conversion",
    "cap_by_sample",
    "risk_for_timeframe",
    "study_definition",
    "walk_forward_folds",
]

DATA_START: Final[datetime] = datetime(2020, 1, 1, tzinfo=UTC)
DATA_END: Final[datetime] = datetime(2026, 9, 15, tzinfo=UTC)
"""The M15 dataset: Binance Vision BTCUSDT spot 1h, the last bar opening 2026-09-14T23:00Z."""

DATASET_SOURCE: Final[str] = "binance_vision_m15"
TIMEFRAMES: Final[tuple[Timeframe, ...]] = (Timeframe.H1, Timeframe.H4, Timeframe.D1)

YEARLY_WINDOWS: Final[tuple[WindowSpec, ...]] = tuple(
    WindowSpec(
        start=max(DATA_START, datetime(year, 1, 1, tzinfo=UTC)),
        end=min(DATA_END, datetime(year + 1, 1, 1, tzinfo=UTC)),
    )
    for year in range(DATA_START.year, DATA_END.year + 1)
)
"""Calendar years, 2026 to the end of the data. The subperiods stability is judged over, and
the unit every 1h run is made in: the backtest engine validates its whole history on every bar,
so one 1h run over six years would take hours, while a year takes minutes."""


def walk_forward_folds() -> tuple[Fold, ...]:
    """Return six folds: train on one calendar year, test on the next. Nothing is fitted."""
    return tuple(
        Fold(index=i, train=YEARLY_WINDOWS[i], test=YEARLY_WINDOWS[i + 1])
        for i in range(len(YEARLY_WINDOWS) - 1)
    )


POLICIES: Final[tuple[LatchPolicy, ...]] = (
    REFERENCE,
    *(p for p in M14_POLICIES if p.key in {"A", "C", "D"}),
)
"""The research reference (no latch, not Risk V2), production (A), M14's recommendation (C)
and the hybrid it tied with (D). B is dropped: M14 showed it barely protects."""

_STUDIED: Final[tuple[str, ...]] = ("regime_trend", "rsi_reversal", "ema_trend_mtf", "breakout_mtf")
STUDY_STRATEGIES: Final[tuple[SprintCandidate, ...]] = tuple(
    next(c for c in M14_STRATEGIES if c.strategy_id == sid) for sid in _STUDIED
)

MIN_PAPER_TRADES: Final[int] = 100
MIN_TRADES_PER_TEST_WINDOW: Final[int] = 5
"""A PAPER CANDIDATE needs at least 100 closed trades over the whole period and at least five
in every walk-forward test year. The verdict function asks for 30; that separates a result
from noise, not a strategy from luck."""


class Conversion(StrEnum):
    """How one risk field changes with the timeframe."""

    PRICE_DISTANCE = "price distance: x sqrt(bar / 1h), rounded half-even to whole bps"
    PER_BAR_DEVIATION = "per-bar deviation: x sqrt(bar / 1h), rounded half-even to 4 places"
    HOLDING_TIME = "holding time: same time, so bars x (1h / bar)"
    STOP_BOUNDS = "risk budget: stop bounds as price distances, sizing unchanged"
    UNCHANGED = "unchanged"


RISK_RATIONALE: Final[Mapping[str, str]] = {
    "initial_stop_distance_bps": "a price distance",
    "break_even_activation_bps": "a price distance",
    "trailing_activation_bps": "a price distance",
    "trailing_distance_bps": "a price distance",
    "take_profit_distance_bps": "a price distance",
    "max_volatility": "a per-bar standard deviation",
    "max_holding_bars": "a holding time, stated in bars",
    "risk_budget": (
        "min/max stop are price distances; risk per trade and exposure are capital policy"
    ),
    "max_daily_loss_pct": "per calendar day",
    "max_daily_drawdown_pct": "per calendar day",
    "max_total_drawdown_pct": "on equity, not on time",
    "latch_total_drawdown": "set by the latch policy, not by the timeframe",
    "max_consecutive_losses": "counts trades; set by the latch policy",
    "max_orders_per_day": "wall-clock rate limit",
    "max_orders_per_hour": "wall-clock rate limit",
    "stale_market_data_seconds": "wall-clock age, measured at the bar close on every timeframe",
    "stale_symbol_rules_seconds": "wall-clock age of the venue rules",
    "max_spread_bps": "per order",
    "market_buy_buffer_bps": "per order",
    "additional_market_buy_safety_bps": "per order",
    "execution_policy": "fees and slippage per order; M15 keeps them identical everywhere",
    "max_portfolio_exposure_pct": "capital policy",
    "max_symbol_exposure": "capital policy",
    "max_order_notional": "capital policy",
    "max_open_positions": "a count",
    "max_open_orders": "a count",
    "max_consecutive_api_failures": "a count",
    "require_stop_on_entry": "a rule, not a quantity",
    "strict_missing_metrics": "a rule, not a quantity",
    "allow_degraded_state": "a rule, not a quantity",
    "allow_market_orders": "a rule, not a quantity",
    "allow_limit_orders": "a rule, not a quantity",
    "allowed_time_in_force": "a rule, not a quantity",
}

_CONVERTED: Final[Mapping[str, Conversion]] = {
    "initial_stop_distance_bps": Conversion.PRICE_DISTANCE,
    "break_even_activation_bps": Conversion.PRICE_DISTANCE,
    "trailing_activation_bps": Conversion.PRICE_DISTANCE,
    "trailing_distance_bps": Conversion.PRICE_DISTANCE,
    "take_profit_distance_bps": Conversion.PRICE_DISTANCE,
    "max_volatility": Conversion.PER_BAR_DEVIATION,
    "max_holding_bars": Conversion.HOLDING_TIME,
    "risk_budget": Conversion.STOP_BOUNDS,
}
RISK_CONVERSIONS: Final[Mapping[str, Conversion]] = {
    name: _CONVERTED.get(name, Conversion.UNCHANGED) for name in RiskConfiguration.model_fields
}
"""Every field of :class:`RiskConfiguration`, classified. A test holds this to the model's own
field list, so a field added later fails the test instead of being carried over silently."""

_BASE: Final[Timeframe] = Timeframe.H1


def _factor(timeframe: Timeframe) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        return (Decimal(timeframe.seconds) / Decimal(_BASE.seconds)).sqrt()


def _bps(value: Decimal | None, factor: Decimal) -> Decimal | None:
    if value is None:
        return None
    return (value * factor).to_integral_value(rounding=ROUND_HALF_EVEN)


def risk_for_timeframe(deployed: RiskConfiguration, timeframe: Timeframe) -> RiskConfiguration:
    """Return the 1h production configuration converted to ``timeframe`` by the stated rules."""
    if timeframe is _BASE:
        return deployed
    factor = _factor(timeframe)
    update: dict[str, object] = {
        name: _bps(getattr(deployed, name), factor)
        for name, rule in _CONVERTED.items()
        if rule is Conversion.PRICE_DISTANCE
    }
    update["max_volatility"] = (deployed.max_volatility * factor).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_EVEN
    )
    if deployed.max_holding_bars is not None:
        held = Decimal(deployed.max_holding_bars * _BASE.seconds) / Decimal(timeframe.seconds)
        update["max_holding_bars"] = max(1, int(held.to_integral_value(rounding=ROUND_HALF_EVEN)))
    if deployed.risk_budget is not None:
        budget = deployed.risk_budget.model_dump()
        budget["min_stop_distance_bps"] = _bps(deployed.risk_budget.min_stop_distance_bps, factor)
        budget["max_stop_distance_bps"] = _bps(deployed.risk_budget.max_stop_distance_bps, factor)
        update["risk_budget"] = budget
    return RiskConfiguration.model_validate({**deployed.model_dump(), **update})


def study_definition(
    candidate: SprintCandidate,
    *,
    base: ExperimentDefinition,
    timeframe: Timeframe,
    policy: LatchPolicy,
    window: WindowSpec | None,
) -> ExperimentDefinition:
    """Return the definition for one strategy, timeframe, policy and window (``None``: all)."""
    version = build_research_registry().metadata_for(candidate.strategy_id).version
    span = window if window is not None else WindowSpec(start=DATA_START, end=DATA_END)
    label = "full" if window is None else str(span.start.year)
    role = (
        ExperimentRole.BENCHMARK
        if candidate.family is Family.BENCHMARK
        else ExperimentRole.IN_SAMPLE
    )
    dataset = base.dataset.model_copy(
        update={
            "timeframe": timeframe,
            "start": span.start,
            "end": span.end,
            "source": DATASET_SOURCE,
        }
    )
    copy = base.model_copy(
        update={
            "name": f"m15-{candidate.strategy_id}-{timeframe.value}-{policy.key}-{label}",
            "strategy": StrategySpec(
                strategy_id=candidate.strategy_id, strategy_version=version, params=candidate.params
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": timeframe}),
            "risk": risk_configuration_for(policy, risk_for_timeframe(base.risk, timeframe)),
            "role": role,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def cap_by_sample(verdict: Verdict, *, trades: int, window_trades: list[int]) -> Verdict:
    """Lower PAPER CANDIDATE to PROMISING when the sample cannot carry it. Never raises one."""
    if verdict is not Verdict.PAPER_CANDIDATE:
        return verdict
    thin = (
        trades < MIN_PAPER_TRADES
        or not window_trades
        or any(count < MIN_TRADES_PER_TEST_WINDOW for count in window_trades)
    )
    return Verdict.PROMISING if thin else verdict
