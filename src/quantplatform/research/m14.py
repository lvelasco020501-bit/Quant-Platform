"""The M14 protocol: latch policies and timeframes, declared before any run.

Two questions, kept apart. **Part A** asks which way of protecting the account after losses
is safest while still letting a strategy trade; **Part B** asks whether a slower timeframe
improves the relation between edge and cost. Nothing here selects by return. Every policy,
timeframe, strategy and classification threshold is fixed below, and tests pin it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.enums import Timeframe
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.latch_policy import LatchPolicy, risk_configuration_for
from quantplatform.research.sprint import CANDIDATES, Family, SprintCandidate
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "COOLDOWN",
    "POLICIES",
    "REFERENCE",
    "STUDY_STRATEGIES",
    "TIMEFRAMES",
    "WALK_FORWARD_TIMEFRAMES",
    "ProtectionEffect",
    "classify_protection",
    "cooldown_bars",
    "study_definition",
]

COOLDOWN: Final[timedelta] = timedelta(hours=24)
"""One day, in time rather than bars, so a pause means the same thing on every timeframe:
24 bars at 1h, 6 at 4h, 1 at 1d."""

POLICIES: Final[tuple[LatchPolicy, ...]] = (
    LatchPolicy(
        key="A",
        label="permanent streak latch (production Risk V2)",
        streak_limit=5,
        cooldown=None,
        drawdown_latch_pct=Decimal("0.20"),
    ),
    LatchPolicy(
        key="B",
        label="streak cooldown 24h",
        streak_limit=5,
        cooldown=COOLDOWN,
        drawdown_latch_pct=Decimal("0.20"),
    ),
    LatchPolicy(
        key="C",
        label="drawdown latch 10%, no streak",
        streak_limit=None,
        cooldown=None,
        drawdown_latch_pct=Decimal("0.10"),
    ),
    LatchPolicy(
        key="D",
        label="streak cooldown 24h + drawdown latch 10%",
        streak_limit=5,
        cooldown=COOLDOWN,
        drawdown_latch_pct=Decimal("0.10"),
    ),
)
"""A is production and is the only one that exists outside research. B changes only what the
streak breaker does after it trips. C replaces the streak with a tighter drawdown latch —
tighter than production's 20% because it is now the only structural protection. D combines
both. The daily-loss breaker (3%, resets at midnight) is unchanged in all four."""

REFERENCE: Final[LatchPolicy] = LatchPolicy(
    key="REF",
    label="research variant: no latching breaker (not Risk V2)",
    streak_limit=None,
    cooldown=None,
    drawdown_latch_pct=None,
)
"""Not a candidate. The baseline each policy is compared against, so that "reduces losses"
can be told apart from "stops trading"; identical to M13's research variant."""

TIMEFRAMES: Final[tuple[Timeframe, ...]] = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
"""30m is not studied: the dataset is hourly, so no 30m bar can be built from it, and the
question is whether trading *less* often helps — 30m could only make costs worse."""

WALK_FORWARD_TIMEFRAMES: Final[tuple[Timeframe, ...]] = (Timeframe.H1, Timeframe.H4)
"""M10c's test windows are 45 days: 45 daily bars, fewer than any strategy's warm-up. Daily
walk-forward is reported as not measurable on one year of data rather than as zero folds."""


def cooldown_bars(timeframe: Timeframe) -> int:
    """Return how many bars of ``timeframe`` the cooldown lasts."""
    return int(COOLDOWN.total_seconds()) // timeframe.seconds


_BENCHMARK_ALIASES: Final[Mapping[str, str]] = {
    "ema_trend": "ema_trend_mtf",
    "breakout": "breakout_mtf",
}
_STUDIED: Final[tuple[str, ...]] = (
    "ema_trend",
    "breakout",
    "regime_trend",
    "breakout_trend",
    "vol_momentum",
    "rsi_reversal",
)
"""The benchmarks, M13's only non-rejected strategy, the two trend rules with the best gross
result, and the mean-reversion rule with the best gross result — so all three families that
M13 tested are represented, at their M13 canonical parameters."""


def _study_candidate(strategy_id: str) -> SprintCandidate:
    candidate = next(c for c in CANDIDATES if c.strategy_id == strategy_id)
    alias = _BENCHMARK_ALIASES.get(strategy_id)
    return candidate if alias is None else candidate.model_copy(update={"strategy_id": alias})


STUDY_STRATEGIES: Final[tuple[SprintCandidate, ...]] = tuple(_study_candidate(s) for s in _STUDIED)


def study_definition(
    candidate: SprintCandidate,
    *,
    base: ExperimentDefinition,
    timeframe: Timeframe,
    policy: LatchPolicy,
) -> ExperimentDefinition:
    """Return the full-year definition for one strategy, timeframe and policy.

    The policy's key is part of the name, and so of the experiment's identity: B and A share
    a risk configuration and differ only in what the wrapper does after a trip, and two
    different policies must never file their results under one experiment.
    """
    version = build_research_registry().metadata_for(candidate.strategy_id).version
    role = (
        ExperimentRole.BENCHMARK
        if candidate.family is Family.BENCHMARK
        else ExperimentRole.IN_SAMPLE
    )
    strategy = StrategySpec(
        strategy_id=candidate.strategy_id, strategy_version=version, params=candidate.params
    )
    dataset = base.dataset.model_copy(update={"timeframe": timeframe})
    backtest = base.backtest.model_copy(update={"timeframe": timeframe})
    copy = base.model_copy(
        update={
            "name": f"m14-{candidate.strategy_id}-{timeframe.value}-{policy.key}",
            "strategy": strategy,
            "dataset": dataset,
            "backtest": backtest,
            "risk": risk_configuration_for(policy, base.risk),
            "role": role,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


class ProtectionEffect(StrEnum):
    """What a protection policy actually did, relative to running without one."""

    STOPS_TRADING = "stops trading"
    REDUCES_LOSSES = "reduces losses"
    NO_EFFECT = "no effect"
    HURTS = "hurts"


_STOPPED_SHARE: Final[Decimal] = Decimal("0.5")
_EXPECTANCY_TOLERANCE: Final[Decimal] = Decimal("0.10")
_DRAWDOWN_IMPROVEMENT: Final[Decimal] = Decimal("0.9")


def classify_protection(
    policy: Mapping[str, object], reference: Mapping[str, object]
) -> ProtectionEffect:
    """Say whether a policy made trading safer or merely made it rarer.

    * **stops trading** — fewer than half the reference's trades. Whatever its return, the
      policy mostly bought safety by not participating.
    * **reduces losses** — at least half the trades, a better expectancy per trade (by more
      than 10% of the reference's), and a drawdown at least 10% smaller.
    * **hurts** — at least half the trades and a worse expectancy per trade.
    * **no effect** — anything else.
    """
    trades = int(str(policy["trades"]))
    ref_trades = int(str(reference["trades"]))
    if ref_trades > 0 and Decimal(trades) < Decimal(ref_trades) * _STOPPED_SHARE:
        return ProtectionEffect.STOPS_TRADING
    expectancy, ref_expectancy = policy.get("expectancy"), reference.get("expectancy")
    if expectancy is None or ref_expectancy is None:
        return ProtectionEffect.NO_EFFECT
    exp, ref = Decimal(str(expectancy)), Decimal(str(ref_expectancy))
    tolerance = abs(ref) * _EXPECTANCY_TOLERANCE
    drawdown = Decimal(str(policy["max_drawdown"]))
    ref_drawdown = Decimal(str(reference["max_drawdown"]))
    if exp > ref + tolerance and drawdown < ref_drawdown * _DRAWDOWN_IMPROVEMENT:
        return ProtectionEffect.REDUCES_LOSSES
    if exp < ref - tolerance:
        return ProtectionEffect.HURTS
    return ProtectionEffect.NO_EFFECT
