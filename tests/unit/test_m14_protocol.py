"""The M14 protocol: policies, timeframes and classifications declared before any run."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quantplatform.core.enums import Timeframe
from quantplatform.orchestration.research import load_definition
from quantplatform.research.m14 import (
    POLICIES,
    REFERENCE,
    STUDY_STRATEGIES,
    TIMEFRAMES,
    WALK_FORWARD_TIMEFRAMES,
    ProtectionEffect,
    classify_protection,
    cooldown_bars,
    study_definition,
)
from quantplatform.research.sprint import research_risk_variant
from quantplatform.strategies.research import build_research_registry

DEPLOYED = Path(__file__).resolve().parents[1] / "fixtures" / "deployed_risk_v2_definition.json"


def test_four_policies_and_a_reference_are_declared() -> None:
    assert [p.key for p in POLICIES] == ["A", "B", "C", "D"]
    assert REFERENCE.streak_limit is None
    assert REFERENCE.drawdown_latch_pct is None


def test_policy_a_is_production_risk_v2() -> None:
    a = next(p for p in POLICIES if p.key == "A")
    assert a.streak_limit == 5
    assert a.cooldown is None
    assert a.drawdown_latch_pct == Decimal("0.20")


def test_the_cooldown_is_one_day_on_every_timeframe() -> None:
    assert cooldown_bars(Timeframe.H1) == 24
    assert cooldown_bars(Timeframe.H4) == 6
    assert cooldown_bars(Timeframe.D1) == 1


def test_timeframes_studied_and_where_walk_forward_is_measurable() -> None:
    assert TIMEFRAMES == (Timeframe.H1, Timeframe.H4, Timeframe.D1)
    # 45-day test windows hold 45 daily bars, fewer than any strategy's warm-up.
    assert WALK_FORWARD_TIMEFRAMES == (Timeframe.H1, Timeframe.H4)


def test_the_study_covers_the_benchmarks_and_the_best_m13_families() -> None:
    ids = [c.strategy_id for c in STUDY_STRATEGIES]
    assert ids == [
        "ema_trend_mtf",
        "breakout_mtf",
        "regime_trend",
        "breakout_trend",
        "vol_momentum",
        "rsi_reversal",
    ]
    registry = build_research_registry()
    for candidate in STUDY_STRATEGIES:
        registry.create(candidate.strategy_id, dict(candidate.params))


def test_a_study_definition_names_its_timeframe_and_policy() -> None:
    base = load_definition(DEPLOYED)
    candidate = STUDY_STRATEGIES[2]
    h1 = study_definition(candidate, base=base, timeframe=Timeframe.H1, policy=POLICIES[0])
    h4 = study_definition(candidate, base=base, timeframe=Timeframe.H4, policy=POLICIES[0])
    ref = study_definition(candidate, base=base, timeframe=Timeframe.H4, policy=REFERENCE)

    assert h4.dataset.timeframe is Timeframe.H4
    assert h4.backtest.timeframe is Timeframe.H4
    assert len({h1.experiment_id, h4.experiment_id, ref.experiment_id}) == 3
    assert h1.risk == base.risk  # policy A is the deployed configuration
    assert ref.risk == research_risk_variant(base.risk)


def test_stopping_trading_is_not_called_reducing_losses() -> None:
    reference = {"trades": 100, "expectancy": Decimal("-10"), "max_drawdown": Decimal("0.20")}
    stopped = {"trades": 12, "expectancy": Decimal("-10"), "max_drawdown": Decimal("0.02")}
    better = {"trades": 80, "expectancy": Decimal("-4"), "max_drawdown": Decimal("0.12")}
    same = {"trades": 97, "expectancy": Decimal("-10.2"), "max_drawdown": Decimal("0.19")}
    worse = {"trades": 95, "expectancy": Decimal("-14"), "max_drawdown": Decimal("0.21")}

    assert classify_protection(stopped, reference) is ProtectionEffect.STOPS_TRADING
    assert classify_protection(better, reference) is ProtectionEffect.REDUCES_LOSSES
    assert classify_protection(same, reference) is ProtectionEffect.NO_EFFECT
    assert classify_protection(worse, reference) is ProtectionEffect.HURTS
