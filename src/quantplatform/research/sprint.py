"""The M13 strategy discovery sprint: everything decided before the first result.

What makes a sprint honest is not how many strategies it tries but whether the thing being
judged was chosen before or after looking. So everything that could otherwise drift toward a
flattering answer is fixed here, in code, before anything ran:

* **One canonical configuration per strategy**, set by convention — three days of hourly
  bars, a 20-bar band at two sigmas, RSI 14 at 30 — never by a result of that strategy.
* **Two neighbours either side**, which exist only to measure fragility. A neighbour is never
  a candidate: the canonical configuration is judged, and a strategy whose neighbourhood
  collapses is judged fragile, not re-tuned.
* **The in-sample / out-of-sample split and the walk-forward windows**, declared here. The
  walk-forward reuses M10c's four windows so this sprint and that one describe the same
  periods.
* **The verdict**, a mechanical function of the evidence with its thresholds stated below.
  Return enters it only as "above zero" and, at the top tier, "above the benchmarks", so no
  reading of a table afterwards can move a strategy up by its headline number.

**What the out-of-sample label does and does not mean.** The dataset is the same year M10c
used, and people have looked at that year. Nobody had looked at *these rules* on it: the
out-of-sample window is clean with respect to the parameter choices below, which were made
before it was run, and for nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Self

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.core.models.base import DomainModel, StrategyId, Text
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.folds import Fold, WalkForwardPlan, WindowSpec
from quantplatform.research.stress import StressScenario
from quantplatform.risk.config import RiskConfiguration

if TYPE_CHECKING:
    from quantplatform.backtesting.metrics import PerformanceSummary

__all__ = [
    "CANDIDATES",
    "IN_SAMPLE_WINDOW",
    "MEANINGFUL_TRADE_SAMPLE",
    "OUT_OF_SAMPLE_WINDOW",
    "STRESS_LABELS",
    "WALK_FORWARD_FOLDS",
    "Evidence",
    "Family",
    "RiskScenario",
    "Scorecard",
    "SprintCandidate",
    "Verdict",
    "definition_for",
    "deployed_definition_for",
    "judge",
    "narrowed",
    "research_risk_variant",
    "stress_scenarios",
    "walk_forward_plan",
]

MEANINGFUL_TRADE_SAMPLE: Final[int] = 30
"""Closed trades below which every ratio is flagged LOW SAMPLE — the platform's existing
threshold, used unchanged."""

PROMISING_MAX_DRAWDOWN: Final[Decimal] = Decimal("0.15")
CANDIDATE_MAX_DRAWDOWN: Final[Decimal] = Decimal("0.10")
CANDIDATE_MIN_PROFIT_FACTOR: Final[Decimal] = Decimal("1.2")
PROMISING_WALK_FORWARD_SHARE: Final[Decimal] = Decimal("0.5")
CANDIDATE_WALK_FORWARD_SHARE: Final[Decimal] = Decimal("0.75")


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


IN_SAMPLE_WINDOW: Final[WindowSpec] = WindowSpec(start=_utc(2025, 9, 1), end=_utc(2026, 5, 1))
OUT_OF_SAMPLE_WINDOW: Final[WindowSpec] = WindowSpec(start=_utc(2026, 5, 1), end=_utc(2026, 9, 1))

WALK_FORWARD_FOLDS: Final[tuple[Fold, ...]] = tuple(
    Fold(
        index=index,
        train=WindowSpec(start=train_start, end=test_start),
        test=WindowSpec(start=test_start, end=test_end),
    )
    for index, (train_start, test_start, test_end) in enumerate(
        (
            (_utc(2025, 9, 1), _utc(2025, 10, 16), _utc(2025, 12, 1)),
            (_utc(2025, 12, 1), _utc(2026, 1, 16), _utc(2026, 3, 1)),
            (_utc(2026, 3, 1), _utc(2026, 4, 16), _utc(2026, 6, 1)),
            (_utc(2026, 6, 1), _utc(2026, 7, 16), _utc(2026, 9, 1)),
        )
    )
)
"""M10c's four windows, reused so the two sprints describe the same periods. Nothing is
fitted on a training window; it runs the same configuration and is reported as context."""

STRESS_LABELS: Final[tuple[str, ...]] = (
    "fees x2",
    "slippage x3",
    "fees x2 + slippage x3 + spread 5bps",
)


class Family(StrEnum):
    """Which question a strategy is asking of the market."""

    BENCHMARK = "benchmark"
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    REGIME = "regime"


class RiskScenario(StrEnum):
    """Which risk configuration a run was judged under. The two are never interchangeable.

    ``DEPLOYED`` is exactly what paper runs, latching breakers included. ``RESEARCH_VARIANT``
    is identical except that its two latching breakers do not latch, and exists only to study
    how often a strategy trades and whether it has edge on its own. It is not Risk V2 and is
    never reported as though it were.
    """

    DEPLOYED = "deployed"
    RESEARCH_VARIANT = "research_variant"

    @property
    def label(self) -> str:
        """Return the name this scenario is reported under."""
        if self is RiskScenario.DEPLOYED:
            return "DEPLOYED RISK V2"
        return "RESEARCH VARIANT (non-latching breakers, not Risk V2)"


class Verdict(StrEnum):
    """What the evidence entitles a strategy to."""

    REJECT = "REJECT"
    WEAK = "WEAK"
    PROMISING = "PROMISING"
    PAPER_CANDIDATE = "PAPER CANDIDATE"


Params = tuple[tuple[Text, Text], ...]


def _p(**values: object) -> Params:
    return tuple((key, str(value)) for key, value in values.items())


class SprintCandidate(DomainModel):
    """One strategy the sprint judges, at the configuration chosen before looking."""

    strategy_id: StrategyId
    family: Family
    params: Params
    neighbours: tuple[Params, ...] = ()
    rationale: Text


CANDIDATES: Final[tuple[SprintCandidate, ...]] = (
    SprintCandidate(
        strategy_id="ema_trend",
        family=Family.BENCHMARK,
        params=_p(fast_period=20, slow_period=50),
        rationale="The frozen benchmark, re-run under the research profile so it is compared "
        "like for like.",
    ),
    SprintCandidate(
        strategy_id="breakout",
        family=Family.BENCHMARK,
        params=_p(entry_lookback=20, exit_lookback=10),
        rationale="The strategy in the current paper session, re-run under the same profile.",
    ),
    SprintCandidate(
        strategy_id="momentum_roc",
        family=Family.TREND,
        params=_p(lookback=72),
        neighbours=(_p(lookback=48), _p(lookback=96)),
        rationale="Three days of hourly bars: a conventional short horizon for time-series "
        "momentum.",
    ),
    SprintCandidate(
        strategy_id="ema_slope",
        family=Family.TREND,
        params=_p(period=50, slope_bars=10),
        neighbours=(_p(period=40, slope_bars=10), _p(period=60, slope_bars=10)),
        rationale="The benchmark's slow average, read by its direction instead of a crossing.",
    ),
    SprintCandidate(
        strategy_id="breakout_trend",
        family=Family.TREND,
        params=_p(entry_lookback=20, exit_lookback=10, trend_period=200),
        neighbours=(
            _p(entry_lookback=20, exit_lookback=10, trend_period=150),
            _p(entry_lookback=20, exit_lookback=10, trend_period=250),
        ),
        rationale="The paper strategy unchanged, plus the conventional 200-period trend filter.",
    ),
    SprintCandidate(
        strategy_id="vol_momentum",
        family=Family.TREND,
        params=_p(lookback=72, vol_window=72, threshold="1.0"),
        neighbours=(
            _p(lookback=72, vol_window=72, threshold="0.75"),
            _p(lookback=72, vol_window=72, threshold="1.25"),
        ),
        rationale="Momentum that must exceed one sigma of its own horizon's noise.",
    ),
    SprintCandidate(
        strategy_id="zscore_revert",
        family=Family.MEAN_REVERSION,
        params=_p(window=48, entry_z=-2, exit_z=0),
        neighbours=(_p(window=36, entry_z=-2, exit_z=0), _p(window=60, entry_z=-2, exit_z=0)),
        rationale="Two days of hourly bars, two sigmas out, back to the mean.",
    ),
    SprintCandidate(
        strategy_id="bollinger_revert",
        family=Family.MEAN_REVERSION,
        params=_p(window=20, band_z=2),
        neighbours=(_p(window=16, band_z=2), _p(window=24, band_z=2)),
        rationale="Bollinger's own 20 and 2, with a close back inside the band as confirmation.",
    ),
    SprintCandidate(
        strategy_id="rsi_reversal",
        family=Family.MEAN_REVERSION,
        params=_p(period=14, oversold=30, exit_level=50),
        neighbours=(
            _p(period=10, oversold=30, exit_level=50),
            _p(period=18, oversold=30, exit_level=50),
        ),
        rationale="Wilder's 14 and 30, exiting at the midline.",
    ),
    SprintCandidate(
        strategy_id="regime_trend",
        family=Family.REGIME,
        params=_p(lookback=72, er_window=72, er_min="0.30"),
        neighbours=(
            _p(lookback=72, er_window=72, er_min="0.25"),
            _p(lookback=72, er_window=72, er_min="0.35"),
        ),
        rationale="The momentum rule, allowed to enter only when the efficiency ratio says "
        "trending.",
    ),
    SprintCandidate(
        strategy_id="regime_revert",
        family=Family.REGIME,
        params=_p(window=48, entry_z=-2, exit_z=0, er_window=72, er_max="0.20"),
        neighbours=(
            _p(window=48, entry_z=-2, exit_z=0, er_window=72, er_max="0.15"),
            _p(window=48, entry_z=-2, exit_z=0, er_window=72, er_max="0.25"),
        ),
        rationale="The z-score rule, allowed to enter only when the efficiency ratio says ranging.",
    ),
    SprintCandidate(
        strategy_id="regime_switch",
        family=Family.REGIME,
        params=_p(
            lookback=72,
            window=48,
            entry_z=-2,
            exit_z=0,
            er_window=72,
            er_trend="0.30",
            er_range="0.20",
        ),
        neighbours=(
            _p(
                lookback=72,
                window=48,
                entry_z=-2,
                exit_z=0,
                er_window=72,
                er_trend="0.25",
                er_range="0.15",
            ),
            _p(
                lookback=72,
                window=48,
                entry_z=-2,
                exit_z=0,
                er_window=72,
                er_trend="0.35",
                er_range="0.25",
            ),
        ),
        rationale="Both regime rules combined: momentum in a trend, reversion in a range.",
    ),
    SprintCandidate(
        strategy_id="vol_filtered_momentum",
        family=Family.REGIME,
        params=_p(lookback=72, short_vol=24, long_vol=168, max_ratio="1.5"),
        neighbours=(
            _p(lookback=72, short_vol=24, long_vol=168, max_ratio="1.25"),
            _p(lookback=72, short_vol=24, long_vol=168, max_ratio="1.75"),
        ),
        rationale="Momentum that stands aside while a day's volatility runs 50% above the week's.",
    ),
)


# --- Definitions ---------------------------------------------------------------------------------


def research_risk_variant(deployed: RiskConfiguration) -> RiskConfiguration:
    """Return the research variant: the deployed configuration with its latches released.

    **This is not Risk V2.** It is a research instrument, reported under
    :attr:`RiskScenario.RESEARCH_VARIANT` and never under Risk V2's name.

    Why it exists: under the deployed configuration the consecutive-loss breaker latched in
    the first weeks of the year and refused 3986 of 4008 decisions for the rest of it. In a
    live session a latched breaker means a person reviews and resets; a year-long backtest
    has no person, so the latch silently truncates the sample to its first handful of trades
    and says nothing about the strategy. Everything else — sizing, stop, break-even,
    trailing, take-profit, time stop, daily loss, costs — is left exactly as deployed.

    A result here answers "does this strategy have edge by itself". Whether it survives the
    policy paper actually runs is answered only by :func:`deployed_definition_for`.
    """
    return RiskConfiguration.model_validate(
        {**deployed.model_dump(), "max_consecutive_losses": None, "latch_total_drawdown": False}
    )


def _revalidated(definition: ExperimentDefinition) -> ExperimentDefinition:
    """Re-run every validator on a definition assembled by copying.

    ``model_copy`` skips validation, so on its own it would let a narrowed benchmark claim to
    be out-of-sample. ``model_dump`` is no way back in either: it carries computed fields — a
    symbol's price and quantity precision — that the strict models refuse. The canonical JSON
    form is the one this codebase already guarantees can be read back, so the round trip goes
    through that.
    """
    return ExperimentDefinition.model_validate_json(canonical_json(definition))


def _candidate_definition(
    candidate: SprintCandidate,
    *,
    base: ExperimentDefinition,
    strategy_version: str,
    risk: RiskConfiguration,
    name: str,
) -> ExperimentDefinition:
    role = (
        ExperimentRole.BENCHMARK
        if candidate.family is Family.BENCHMARK
        else ExperimentRole.IN_SAMPLE
    )
    strategy = StrategySpec(
        strategy_id=candidate.strategy_id,
        strategy_version=strategy_version,
        params=candidate.params,
    )
    return _revalidated(
        base.model_copy(update={"name": name, "strategy": strategy, "risk": risk, "role": role})
    )


def definition_for(
    candidate: SprintCandidate, *, base: ExperimentDefinition, strategy_version: str
) -> ExperimentDefinition:
    """Return the full-year definition under the research variant — not Risk V2."""
    return _candidate_definition(
        candidate,
        base=base,
        strategy_version=strategy_version,
        risk=research_risk_variant(base.risk),
        name=f"m13-{candidate.strategy_id}",
    )


def deployed_definition_for(
    candidate: SprintCandidate, *, base: ExperimentDefinition, strategy_version: str
) -> ExperimentDefinition:
    """Return the full-year definition under the deployed Risk V2, exactly as paper runs it."""
    return _candidate_definition(
        candidate,
        base=base,
        strategy_version=strategy_version,
        risk=base.risk,
        name=f"m13-{candidate.strategy_id}-deployed-risk-v2",
    )


def narrowed(
    definition: ExperimentDefinition, window: WindowSpec, role: ExperimentRole
) -> ExperimentDefinition:
    """Return a definition restricted to one window and claiming one role."""
    dataset = definition.dataset.model_copy(update={"start": window.start, "end": window.end})
    return _revalidated(definition.model_copy(update={"dataset": dataset, "role": role}))


def walk_forward_plan(definition: ExperimentDefinition) -> WalkForwardPlan:
    """Return M10c's four folds, bound to this definition."""
    return WalkForwardPlan(base_experiment_id=definition.experiment_id, folds=WALK_FORWARD_FOLDS)


def stress_scenarios(base: ExperimentDefinition) -> tuple[StressScenario, ...]:
    """Return the three cost scenarios, in :data:`STRESS_LABELS` order. Only costs move."""
    risk = base.risk
    policy = risk.execution_policy
    fee = policy.fee.basis_points
    slip = policy.slippage.basis_points

    def with_costs(fee_bps: Decimal, slip_bps: Decimal) -> RiskConfiguration:
        costs = policy.model_dump()
        costs["fee"]["basis_points"] = fee_bps
        costs["slippage"]["basis_points"] = slip_bps
        return RiskConfiguration.model_validate({**risk.model_dump(), "execution_policy": costs})

    wider = BacktestConfig.model_validate(
        {**base.backtest.model_dump(), "assumed_spread_basis_points": Decimal(5)}
    )
    return (
        StressScenario(risk=with_costs(fee * 2, slip)),
        StressScenario(risk=with_costs(fee, slip * 3)),
        StressScenario(risk=with_costs(fee * 2, slip * 3), backtest=wider),
    )


# --- Evidence and verdict -----------------------------------------------------------------------


class Scorecard(DomainModel):
    """The KPIs one run is reported with. ``None`` means not computable, never zero."""

    total_return: Decimal
    max_drawdown: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    trades: int
    fees: Decimal
    slippage: Decimal
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    time_in_market: Decimal | None
    max_consecutive_losses: int

    @property
    def low_sample(self) -> bool:
        """Return whether too few trades closed for any ratio here to mean much."""
        return self.trades < MEANINGFUL_TRADE_SAMPLE

    @classmethod
    def from_performance(cls, performance: PerformanceSummary) -> Self:
        """Read a scorecard off a run's performance summary."""
        trades = performance.trades
        total = performance.total_return
        if total is None:
            total = performance.final_equity / performance.initial_equity - 1
        return cls(
            total_return=total,
            max_drawdown=performance.max_drawdown,
            profit_factor=trades.profit_factor,
            expectancy=trades.expectancy,
            trades=trades.count,
            fees=performance.commission_paid,
            slippage=performance.slippage_paid,
            win_rate=trades.win_rate,
            average_win=trades.average_win,
            average_loss=trades.average_loss,
            time_in_market=performance.time_in_market,
            max_consecutive_losses=trades.max_consecutive_losses,
        )


class Evidence(DomainModel):
    """Everything the verdict is allowed to look at. Unknown fails its check."""

    full: Scorecard
    out_of_sample_return: Decimal | None
    walk_forward_positive_share: Decimal | None
    walk_forward_median_return: Decimal | None
    stress_worst_return: Decimal | None
    neighbours_min_profit_factor: Decimal | None
    """The weaker neighbour's profit factor. ``None`` only when neither lost a trade; a
    neighbour that traded nothing is recorded as zero by the runner, not as ``None``."""

    benchmark_best_return: Decimal | None
    deployed_return: Decimal | None
    """Full-year return under :attr:`RiskScenario.DEPLOYED`. Everything else in this record
    is measured under the research variant."""


def _above(value: Decimal | None, floor: Decimal) -> bool:
    return value is not None and value > floor


def judge(evidence: Evidence) -> Verdict:
    """Return the verdict the evidence supports, by the thresholds on this module.

    * **REJECT** — nothing traded, or it lost money, or its profit factor is below one.
    * **WEAK** — positive, but fails any robustness check: fewer than
      :data:`MEANINGFUL_TRADE_SAMPLE` trades, a non-positive out-of-sample window, fewer than
      half the walk-forward windows positive or a non-positive median window, any stress
      scenario losing money, a neighbour with a profit factor below one, or a drawdown above
      15%.
    * **PROMISING** — passes every robustness check.
    * **PAPER CANDIDATE** — promising, and at least three of four walk-forward windows
      positive, drawdown at most 10%, profit factor at least 1.2, a full-year return above
      both benchmarks', **and a positive full-year return under the deployed Risk V2**.

    Every check but the last is measured under the research variant. The last is what stops
    a strategy that only looks good because its breakers were relaxed: it can reach
    PROMISING, never PAPER CANDIDATE, until it survives the policy paper would run it under.
    """
    card = evidence.full
    profit_factor = card.profit_factor
    if card.trades == 0 or card.total_return <= 0:
        return Verdict.REJECT
    if profit_factor is not None and profit_factor < 1:
        return Verdict.REJECT

    robust = (
        not card.low_sample
        and _above(evidence.out_of_sample_return, Decimal(0))
        and evidence.walk_forward_positive_share is not None
        and evidence.walk_forward_positive_share >= PROMISING_WALK_FORWARD_SHARE
        and _above(evidence.walk_forward_median_return, Decimal(0))
        and _above(evidence.stress_worst_return, Decimal(0))
        and (
            evidence.neighbours_min_profit_factor is None
            or evidence.neighbours_min_profit_factor >= 1
        )
        and card.max_drawdown <= PROMISING_MAX_DRAWDOWN
    )
    if not robust:
        return Verdict.WEAK

    candidate = (
        evidence.walk_forward_positive_share is not None
        and evidence.walk_forward_positive_share >= CANDIDATE_WALK_FORWARD_SHARE
        and card.max_drawdown <= CANDIDATE_MAX_DRAWDOWN
        and (profit_factor is None or profit_factor >= CANDIDATE_MIN_PROFIT_FACTOR)
        and evidence.benchmark_best_return is not None
        and card.total_return > evidence.benchmark_best_return
        and _above(evidence.deployed_return, Decimal(0))
    )
    return Verdict.PAPER_CANDIDATE if candidate else Verdict.PROMISING
