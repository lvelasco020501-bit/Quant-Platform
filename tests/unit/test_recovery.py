"""Recovery rules for the drawdown breaker, simulated without changing a line of Risk.

M16 found the defect these tests exist for: a *permanent* drawdown latch turns a continuous
result into a discontinuous one. XRP's latch tripped once in July 2020 and that market never
traded again — 27 trades — while the same run with slightly higher fees never tripped and
traded 146 times. A breaker that decides a decade on which side of a threshold the equity
passed one afternoon is not protection, it is a coin toss with a long memory.

So these tests pin three things: that the permanent rule still behaves exactly as it did
(nothing earlier in the repository may shift), that each recovery rule reopens the market
when it says it will, and that a reopened market measures its next drawdown from where it
restarted rather than from a peak it can no longer reach.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from quantplatform.backtesting.results import BacktestResult
from quantplatform.core.enums import CircuitBreakerReason, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory, load_definition
from quantplatform.research.definition import ExperimentDefinition, ExperimentRole
from quantplatform.research.folds import WindowSpec
from quantplatform.research.latch_policy import (
    LatchPolicy,
    LatchPolicyRiskEngine,
    blocked_time_share,
)
from quantplatform.research.latch_policy import (
    risk_configuration_for as latch_risk_configuration_for,
)
from quantplatform.research.recovery import (
    Recovery,
    RecoveryLatchRiskEngine,
    RecoveryPolicy,
    next_period_start,
    risk_configuration_for_recovery,
)
from quantplatform.research.sprint import CANDIDATES, definition_for, narrowed
from quantplatform.strategies.research import build_research_registry
from tests.factories import ANCHOR, make_bars

DEPLOYED = Path(__file__).resolve().parents[1] / "fixtures" / "deployed_risk_v2_definition.json"
BARS = 24 * 30
DRAWDOWN = Decimal("0.10")

PERMANENT = RecoveryPolicy(
    key="C", label="permanent", drawdown_pct=DRAWDOWN, recovery=Recovery.PERMANENT
)
COOLDOWN = RecoveryPolicy(
    key="E",
    label="cooldown",
    drawdown_pct=DRAWDOWN,
    recovery=Recovery.COOLDOWN,
    cooldown=timedelta(days=2),
)
RESTART = RecoveryPolicy(
    key="G",
    label="cooldown and restart",
    drawdown_pct=DRAWDOWN,
    recovery=Recovery.COOLDOWN_AND_RESTART,
    cooldown=timedelta(days=2),
)


def _falling(bars: int = BARS, timeframe: Timeframe = Timeframe.H1) -> tuple[MarketBar, ...]:
    """Three rises, three larger falls: a short momentum rule loses on every lap, forever."""
    closes = [Decimal(100_000)]
    steps = [Decimal("0.004")] * 3 + [Decimal("-0.005")] * 3
    for index in range(bars - 1):
        closes.append((closes[-1] * (1 + steps[index % 6])).quantize(Decimal("0.01")))
    return make_bars(tuple(closes), timeframe=timeframe)


def _definition(risk: object, bars: tuple[MarketBar, ...]) -> ExperimentDefinition:
    base = load_definition(DEPLOYED)
    candidate = next(c for c in CANDIDATES if c.strategy_id == "momentum_roc")
    candidate = candidate.model_copy(update={"params": (("lookback", "3"),)})
    full = definition_for(candidate, base=base, strategy_version="0.1.0")
    timeframe = bars[0].timeframe
    span = timedelta(seconds=timeframe.seconds * len(bars))
    narrow = narrowed(full, WindowSpec(start=ANCHOR, end=ANCHOR + span), ExperimentRole.IN_SAMPLE)
    return narrow.model_copy(
        update={
            "risk": risk,
            "backtest": narrow.backtest.model_copy(update={"timeframe": timeframe}),
            "dataset": narrow.dataset.model_copy(update={"timeframe": timeframe}),
        }
    )


def _run(
    policy: RecoveryPolicy, bars: tuple[MarketBar, ...] | None = None
) -> tuple[BacktestResult, RecoveryLatchRiskEngine]:
    """Run the synthetic series under one recovery policy."""
    series = bars if bars is not None else _falling()
    base = load_definition(DEPLOYED)
    risk = risk_configuration_for_recovery(policy, base.risk)
    engines: list[RecoveryLatchRiskEngine] = []

    def wrap(definition: ExperimentDefinition) -> RecoveryLatchRiskEngine:
        engine = RecoveryLatchRiskEngine(config=definition.risk, policy=policy)
        engines.append(engine)
        return engine

    factory = ExperimentEngineFactory(
        registry=build_research_registry(),
        features_for=features_for,
        quote_asset="USDT",
        risk_engine_for=wrap,
    )
    return factory(_definition(risk, series)).run(series), engines[0]


def _latched_at(engine: RecoveryLatchRiskEngine) -> list[tuple[datetime, datetime | None]]:
    return engine.stats.pauses


# --- Nothing earlier may shift -------------------------------------------------------------


def test_the_permanent_rule_reproduces_the_m14_wrapper_bit_for_bit() -> None:
    # M14, M15 and M16 all ran under the permanent latch, and their results are committed
    # with digests. If this milestone changed that rule by so much as a fee, this fails.
    old_policy = LatchPolicy(
        key="C", label="drawdown latch", streak_limit=None, drawdown_latch_pct=DRAWDOWN
    )
    bars = _falling()
    base = load_definition(DEPLOYED)
    engines: list[LatchPolicyRiskEngine] = []

    def wrap(definition: ExperimentDefinition) -> LatchPolicyRiskEngine:
        engine = LatchPolicyRiskEngine(config=definition.risk, policy=old_policy)
        engines.append(engine)
        return engine

    factory = ExperimentEngineFactory(
        registry=build_research_registry(),
        features_for=features_for,
        quote_asset="USDT",
        risk_engine_for=wrap,
    )
    old_risk = latch_risk_configuration_for(old_policy, base.risk)
    before = factory(_definition(old_risk, bars)).run(bars)
    after, _ = _run(PERMANENT, bars)

    assert before.performance is not None
    assert after.performance is not None
    assert before.performance.trades.count > 0, "the series must actually trade"
    assert after.performance == before.performance
    assert after.trades == before.trades
    assert after.equity_curve == before.equity_curve


def test_the_permanent_rule_never_reopens_the_market() -> None:
    result, engine = _run(PERMANENT)
    tripped = [start for start, _ in _latched_at(engine)]
    assert tripped, "the series must actually reach the drawdown latch"
    assert all(trade.opened_at < min(tripped) for trade in result.trades)
    assert all(end is None for _, end in _latched_at(engine))


# --- Each rule reopens when it says it will ------------------------------------------------


def test_a_cooldown_reopens_the_market_after_exactly_the_cooldown() -> None:
    _, engine = _run(COOLDOWN)
    episodes = _latched_at(engine)
    assert episodes, "the series must actually reach the drawdown latch"
    for start, end in episodes[:-1]:
        assert end == start + timedelta(days=2)


def test_a_cooldown_alone_cannot_reopen_a_market_that_is_still_under_water() -> None:
    # Corrected after seeing the mechanism: ending the halt is not the same as being allowed
    # to trade. The drawdown *limit* refuses new exposure while equity sits below the
    # threshold, whatever the latch says, so a cooldown that carries the old reference peak
    # reopens the market straight back into the same refusal. On a series that never
    # recovers, it therefore trades no more than the permanent rule does.
    permanent, _ = _run(PERMANENT)
    cooled, _ = _run(COOLDOWN)
    assert permanent.performance is not None
    assert cooled.performance is not None
    assert cooled.performance.trades.count == permanent.performance.trades.count


def test_the_restarted_market_trades_again_where_the_permanent_one_stops() -> None:
    permanent, _ = _run(PERMANENT)
    restarted, _ = _run(RESTART)
    assert permanent.performance is not None
    assert restarted.performance is not None
    assert restarted.performance.trades.count > permanent.performance.trades.count


def test_the_period_rule_reopens_on_the_calendar_boundary() -> None:
    policy = RecoveryPolicy(
        key="F", label="quarter reset", drawdown_pct=DRAWDOWN, recovery=Recovery.PERIOD
    )
    # 4h bars, long enough to cross a quarter boundary from the 1 January anchor.
    _, engine = _run(policy, _falling(bars=700, timeframe=Timeframe.H4))
    episodes = _latched_at(engine)
    assert episodes, "the series must actually reach the drawdown latch"
    for _, end in episodes[:-1]:
        assert end is not None
        # The halt ends on the first decision of the new quarter, not at midnight exactly.
        assert (end.month, end.day) in {(1, 1), (4, 1), (7, 1), (10, 1)}


def test_the_quarter_boundary_is_the_next_one_strictly_after_the_trip() -> None:
    assert next_period_start(datetime(2026, 1, 1, tzinfo=UTC)) == datetime(2026, 4, 1, tzinfo=UTC)
    assert next_period_start(datetime(2026, 2, 17, 9, tzinfo=UTC)) == datetime(
        2026, 4, 1, tzinfo=UTC
    )
    assert next_period_start(datetime(2026, 11, 30, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)


# --- Restarting measures from where it restarted -------------------------------------------


def test_without_a_restart_a_reopened_market_relatches_immediately() -> None:
    # The equity never recovers here, so a market reopened without a new high-water mark is
    # still 10% under its old peak and trips again at once. That is the trap policy G exists
    # to avoid: reopening into the same latch is not the same as being able to trade.
    _, engine = _run(COOLDOWN)
    episodes = _latched_at(engine)
    assert len(episodes) >= 2
    gaps = [
        later_start - end for (_, end), (later_start, _) in pairwise(episodes) if end is not None
    ]
    assert gaps
    assert min(gaps) <= timedelta(hours=1)


def test_a_restart_measures_the_next_drawdown_from_the_reopening() -> None:
    _, cooled = _run(COOLDOWN)
    _, restarted = _run(RESTART)
    # Same series, same threshold, same cooldown: the only difference is where the next
    # drawdown is measured from, and that alone must make the market latch less often.
    assert len(_latched_at(restarted)) < len(_latched_at(cooled))


def test_a_restarted_market_keeps_trading_after_reopening() -> None:
    result, engine = _run(RESTART)
    episodes = _latched_at(engine)
    assert episodes
    first_release = episodes[0][1]
    assert first_release is not None
    assert any(trade.opened_at > first_release for trade in result.trades)


def test_a_drawdown_the_run_actually_suffered_must_halt_the_market() -> None:
    # The bug this pins: the wrapper only sees the account when the strategy asks to trade,
    # while the engine tracks the peak on every bar. Keeping a private peak from those
    # samples under-measured every drawdown, so M17's first run recorded XRP with a 10.14%
    # drawdown and zero halts under a 10% breaker — a breaker that never fired. The
    # reference must therefore be the engine's own peak, not a subsample of it.
    for policy in (COOLDOWN, RESTART, PERMANENT):
        result, engine = _run(policy)
        assert result.performance is not None
        if result.performance.max_drawdown >= DRAWDOWN:
            assert _latched_at(engine), f"{policy.key} suffered the drawdown but never halted"


# --- Fail-closed ---------------------------------------------------------------------------


def test_the_engine_never_removes_a_breaker_it_does_not_govern() -> None:
    _, engine = _run(COOLDOWN)
    governed = {CircuitBreakerReason.EXCESSIVE_DRAWDOWN}
    assert all(reason not in governed for reason, _ in engine.stats.trips)


def test_every_blocked_decision_is_counted() -> None:
    _, engine = _run(PERMANENT)
    assert 0 < engine.stats.blocked <= engine.stats.assessed


def test_blocked_time_is_measured_from_the_latch_episodes() -> None:
    _, engine = _run(COOLDOWN)
    share = blocked_time_share(engine.stats, start=ANCHOR, end=ANCHOR + timedelta(hours=BARS))
    assert Decimal(0) < share < Decimal(1)


# --- The policy cannot be built incoherently -----------------------------------------------


def test_a_cooldown_recovery_needs_a_cooldown() -> None:
    with pytest.raises(ValueError, match="cooldown"):
        RecoveryPolicy(key="X", label="bad", drawdown_pct=DRAWDOWN, recovery=Recovery.COOLDOWN)


def test_a_rule_that_does_not_wait_cannot_carry_a_cooldown() -> None:
    with pytest.raises(ValueError, match="cooldown"):
        RecoveryPolicy(
            key="X",
            label="bad",
            drawdown_pct=DRAWDOWN,
            recovery=Recovery.PERIOD,
            cooldown=timedelta(days=2),
        )


def test_a_recovery_rule_needs_a_drawdown_to_recover_from() -> None:
    with pytest.raises(ValueError, match="drawdown"):
        RecoveryPolicy(
            key="X",
            label="bad",
            drawdown_pct=None,
            recovery=Recovery.COOLDOWN,
            cooldown=timedelta(days=2),
        )


def test_the_configuration_arms_the_breaker_the_policy_needs() -> None:
    deployed = load_definition(DEPLOYED).risk
    risk = risk_configuration_for_recovery(COOLDOWN, deployed)
    assert risk.latch_total_drawdown is True
    assert risk.max_total_drawdown_pct == DRAWDOWN
    assert risk.max_consecutive_losses is None
    assert risk.execution_policy == deployed.execution_policy, "costs are never touched"


# --- What each halt did to the reference ----------------------------------------------------


def test_each_halt_records_the_equity_and_reference_it_started_and_ended_on() -> None:
    # M18 needs this to tell a ratchet from a single fall: a chain of halts whose restart
    # reference steps down each time is the failure mode a moving high-water mark can have,
    # and it cannot be seen from halt counts alone.
    _, engine = _run(RESTART)
    episodes = engine.episodes
    assert episodes, "the series must actually reach the drawdown latch"
    first = episodes[0]
    assert first["began"] == _latched_at(engine)[0][0]
    assert first["equity_at_halt"] > 0
    assert first["reference_before"] >= first["equity_at_halt"]


def test_a_restart_lowers_the_reference_to_the_reopening_equity() -> None:
    _, engine = _run(RESTART)
    restarted = [e for e in engine.episodes if e["ended"] is not None]
    assert restarted
    for episode in restarted:
        assert episode["reference_after"] == episode["equity_at_release"]
        assert episode["reference_after"] < episode["reference_before"]


def test_carrying_the_reference_leaves_it_where_it_was() -> None:
    _, engine = _run(COOLDOWN)
    ended = [e for e in engine.episodes if e["ended"] is not None]
    assert ended
    for episode in ended:
        assert episode["reference_after"] == episode["reference_before"]


def test_the_permanent_latch_records_the_halt_it_never_releases() -> None:
    # It is the engine that latches here, not the wrapper. Recording it anyway is what keeps
    # a report from claiming the permanent rule "never halted" while the same run shows most
    # of its history blocked.
    _, engine = _run(PERMANENT)
    assert len(engine.episodes) == 1
    episode = engine.episodes[0]
    assert episode["ended"] is None
    assert episode["reference_after"] is None
    assert episode["equity_at_halt"] > 0
