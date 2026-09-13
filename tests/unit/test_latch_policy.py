"""Latch policies, simulated without changing a line of Risk.

The deployed breaker latches forever after five consecutive losses. M13 showed that halts a
trend strategy within weeks. These tests pin the research-only wrapper that lets other
policies be measured: it overrides only ``StandardRiskEngine.assess``, it only ever adds or
releases *its own* streak breaker, and every other breaker passes through untouched.

The first test is the one everything rests on: with the permanent policy the wrapper must
reproduce the deployed engine bit for bit. If the wrapper's idea of a loss differed from the
engine's by one fee, that test fails.
"""

from __future__ import annotations

from datetime import timedelta
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
    risk_configuration_for,
)
from quantplatform.research.sprint import CANDIDATES, definition_for, narrowed
from quantplatform.strategies.research import build_research_registry
from tests.factories import ANCHOR, make_bars

DEPLOYED = Path(__file__).resolve().parents[1] / "fixtures" / "deployed_risk_v2_definition.json"
HOURS = 24 * 8

PERMANENT = LatchPolicy(
    key="A", label="permanent", streak_limit=5, cooldown=None, drawdown_latch_pct=Decimal("0.20")
)
COOLDOWN = LatchPolicy(
    key="B", label="cooldown", streak_limit=5, cooldown=timedelta(hours=24), drawdown_latch_pct=None
)


def _sawtooth(up: str = "0.001", down: str = "-0.0012") -> tuple[MarketBar, ...]:
    """Three small rises, three slightly larger falls: a short momentum rule loses every lap."""
    closes = [Decimal(100_000)]
    steps = [Decimal(up)] * 3 + [Decimal(down)] * 3
    for i in range(HOURS - 1):
        closes.append((closes[-1] * (1 + steps[i % 6])).quantize(Decimal("0.01")))
    return make_bars(tuple(closes))


def _definition(risk_policy: LatchPolicy | None) -> ExperimentDefinition:
    base = load_definition(DEPLOYED)
    candidate = next(c for c in CANDIDATES if c.strategy_id == "momentum_roc")
    candidate = candidate.model_copy(update={"params": (("lookback", "3"),)})
    full = definition_for(candidate, base=base, strategy_version="0.1.0")
    window = WindowSpec(start=ANCHOR, end=ANCHOR + timedelta(hours=HOURS))
    narrow = narrowed(full, window, ExperimentRole.IN_SAMPLE)
    risk = base.risk if risk_policy is None else risk_configuration_for(risk_policy, base.risk)
    return narrow.model_copy(update={"risk": risk})


def _run(
    policy: LatchPolicy | None, bars: tuple[MarketBar, ...] | None = None
) -> tuple[BacktestResult, LatchPolicyRiskEngine | None]:
    """Run a synthetic series; ``None`` means the deployed engine with no wrapper at all."""
    engines: list[LatchPolicyRiskEngine] = []

    def wrap(definition: ExperimentDefinition) -> LatchPolicyRiskEngine:
        assert policy is not None  # wrap is only installed when a policy was given
        engine = LatchPolicyRiskEngine(config=definition.risk, policy=policy)
        engines.append(engine)
        return engine

    factory = ExperimentEngineFactory(
        registry=build_research_registry(),
        features_for=features_for,
        quote_asset="USDT",
        risk_engine_for=None if policy is None else wrap,
    )
    result = factory(_definition(policy)).run(bars if bars is not None else _sawtooth())
    return result, (engines[0] if engines else None)


def test_the_permanent_policy_reproduces_the_deployed_engine_bit_for_bit() -> None:
    deployed, _ = _run(None)
    wrapped, engine = _run(PERMANENT)

    assert deployed.performance is not None
    assert wrapped.performance is not None
    assert deployed.performance.trades.count >= 5  # the series really does build a streak
    assert wrapped.performance == deployed.performance
    assert wrapped.trades == deployed.trades
    assert wrapped.equity_curve == deployed.equity_curve
    assert engine is not None
    assert len(engine.stats.pauses) == 1


def test_a_cooldown_pauses_then_trades_again_and_pauses_again() -> None:
    permanent, _ = _run(PERMANENT)
    cooled, engine = _run(COOLDOWN)

    assert permanent.performance is not None
    assert cooled.performance is not None
    assert cooled.performance.trades.count > permanent.performance.trades.count
    assert engine is not None
    assert len(engine.stats.pauses) >= 2
    for start, end in engine.stats.pauses[:-1]:
        assert end == start + timedelta(hours=24)


def test_a_new_streak_must_be_earned_after_a_release() -> None:
    # Released pauses are followed by at least five more losses before the next one: the
    # five losses that caused the first pause are never counted twice.
    _, engine = _run(COOLDOWN)
    assert engine is not None
    assert engine.stats.pauses
    for (_, released), (next_start, _) in pairwise(engine.stats.pauses):
        assert released is not None
        assert next_start - released >= timedelta(hours=1)


def test_the_wrapper_never_releases_a_drawdown_latch() -> None:
    # A steeper sawtooth drives the account through a 5% drawdown while the streak cooldown
    # keeps re-opening trading. 5% is the tightest latch Risk itself accepts: it refuses one
    # below the 5% daily drawdown limit. Once the drawdown latch trips, no cooldown may reopen
    # it, so no trade can open after it.
    hybrid = LatchPolicy(
        key="D",
        label="hybrid",
        streak_limit=5,
        cooldown=timedelta(hours=24),
        drawdown_latch_pct=Decimal("0.05"),
    )
    result, engine = _run(hybrid, _sawtooth(up="0.004", down="-0.005"))
    assert engine is not None
    tripped = [
        at for reason, at in engine.stats.trips if reason is CircuitBreakerReason.EXCESSIVE_DRAWDOWN
    ]
    assert tripped, "the series must actually reach the drawdown latch"
    assert all(trade.opened_at < min(tripped) for trade in result.trades)


def test_every_blocked_decision_is_counted() -> None:
    _, engine = _run(PERMANENT)
    assert engine is not None
    assert 0 < engine.stats.blocked <= engine.stats.assessed


def test_blocked_time_is_measured_from_the_pauses() -> None:
    _, engine = _run(COOLDOWN)
    assert engine is not None
    share = blocked_time_share(engine.stats, start=ANCHOR, end=ANCHOR + timedelta(hours=HOURS))
    assert Decimal(0) < share < Decimal(1)


def test_risk_configuration_maps_each_policy_to_the_breakers_it_needs() -> None:
    deployed = load_definition(DEPLOYED).risk
    permanent = risk_configuration_for(PERMANENT, deployed)
    drawdown = risk_configuration_for(
        LatchPolicy(
            key="C",
            label="drawdown",
            streak_limit=None,
            cooldown=None,
            drawdown_latch_pct=Decimal("0.10"),
        ),
        deployed,
    )

    assert permanent == deployed  # A is production, unchanged
    assert drawdown.max_consecutive_losses is None
    assert drawdown.latch_total_drawdown is True
    assert drawdown.max_total_drawdown_pct == Decimal("0.10")


def test_a_policy_cannot_cool_down_a_streak_it_does_not_track() -> None:
    with pytest.raises(ValueError, match="cooldown"):
        LatchPolicy(
            key="X",
            label="bad",
            streak_limit=None,
            cooldown=timedelta(hours=1),
            drawdown_latch_pct=None,
        )


def test_the_multi_timeframe_benchmark_is_the_same_rule_at_one_hour() -> None:
    registry = build_research_registry()
    original = registry.create("ema_trend", {})
    widened = registry.create("ema_trend_mtf", {})
    assert Timeframe.H4 in widened.metadata.supported_timeframes
    assert Timeframe.D1 in widened.metadata.supported_timeframes
    assert widened.metadata.required_features == original.metadata.required_features
