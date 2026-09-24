"""M24 — B2 against regime_trend, through one protocol and one code path.

M23 gave B2 a PAPER CANDIDATE on BTC and said plainly what it had not shown: that B2 is
better than the rule the project already had. On BTC the incumbent posts +34.32% at a 4.41%
drawdown against B2's +31.31% at 6.34% — but those numbers come from M16, which ran a
different risk policy, no declared out-of-sample window and no neighbour set. Comparing them
as they stand would be comparing two protocols, not two strategies.

So this milestone changes nothing about either rule and everything about how they are asked.

**Both contenders are frozen.** B2 is :data:`~quantplatform.research.m23.CANDIDATE`, the
breakout at 40/20/400. ``regime_trend`` is M13's canonical configuration — lookback 72, er
window 72, er_min 0.30 — exactly what M15 and M16 ran. Neither is re-derived, retuned or
refitted per market.

**Everything the protocol supplies comes from M23 by import, not by copy.** The out-of-sample
boundary, both window functions, the neighbour step, and every failure threshold are the M23
objects themselves. A number that drifted here would drift for both contenders at once, and a
test pins the ones that matter anyway.

**One builder, both contenders.** :func:`contender_definition` constructs every run in this
milestone, and it takes a strategy id and parameters rather than a type. Routing B2 through
M22's ``Variant`` path and regime_trend through the ``SprintCandidate`` path would have reused
more code and introduced precisely the asymmetry this milestone exists to remove: two
definition builders differ in their names, their roles and their defaults, and any of those
could move a result. The duplication is deliberate and is the point.

**Neighbours use M23's rule on every axis of both rules.** A quarter in each direction, one
axis at a time. The axes differ because the rules differ — B2 has two window groups, while
regime_trend has one window group and one threshold — and that asymmetry is real, not a
choice: it is what the two strategies are. Applying the same *step* to whatever axes a rule
has is the closest thing to a fair perturbation that exists.

Note that on the threshold axis this is **stricter than M13's own neighbours**, which sat at
er_min 0.25 and 0.35; a quarter either way reaches 0.225 and 0.375. The wider test was kept
because it is the same rule as B2's, and picking the milder pre-existing pair for one
contender only would have been a thumb on the scale.

**No winner is declared by return.** The comparison answers six questions about consistency,
operability, cost survival, concentration, verdict and distinctness. Return enters only where
:func:`~quantplatform.research.sprint.judge` already lets it: above zero, and above the
benchmarks.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from quantplatform.core.enums import MarketType
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.folds import WindowSpec
from quantplatform.research.latch_policy import risk_configuration_for
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m16 import DATA_END, symbol_rules_for
from quantplatform.research.m22 import TIMEFRAME, asset_for
from quantplatform.research.m22 import _base as _deployed_base
from quantplatform.research.m23 import (
    CANDIDATE,
    MARKETS,
    NEIGHBOUR_STEP,
    OOS_START,
    in_sample_window,
    oos_window,
)
from quantplatform.research.sprint import CANDIDATES, Params
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "CONTENDERS",
    "INTEGER_PARAMS",
    "MARKETS",
    "OOS_START",
    "Contender",
    "Neighbour",
    "contender_definition",
    "contender_for",
    "full_window",
    "in_sample_window",
    "neighbour_definition",
    "neighbours_for",
    "oos_window",
    "scaled",
]

INTEGER_PARAMS: Final[frozenset[str]] = frozenset(
    {"entry_lookback", "exit_lookback", "trend_period", "lookback", "er_window"}
)
"""Parameters counted in bars. Everything else on an axis is a threshold and stays a
Decimal — scaling ``er_min`` through ``int()`` would round 0.225 to zero and silently turn a
neighbour into a rule that enters on every bar."""


class Contender(DomainModel):
    """One frozen strategy, and the axes a neighbour may move."""

    key: Text
    label: Text
    strategy_id: Text
    params: Params
    axes: tuple[tuple[Text, tuple[Text, ...]], ...]
    """Ordered so neighbour keys are stable across runs."""

    provenance: Text
    """Where these numbers were fixed, and by which milestone."""


def _m13(strategy_id: str) -> Params:
    return next(c for c in CANDIDATES if c.strategy_id == strategy_id).params


CONTENDERS: Final[tuple[Contender, ...]] = (
    Contender(
        key="B2",
        label="breakout 40/20 above a 400-bar trend filter",
        strategy_id=CANDIDATE.candidate.strategy_id,
        params=CANDIDATE.candidate.params,
        axes=(("channel", ("entry_lookback", "exit_lookback")), ("trend", ("trend_period",))),
        provenance="M13 canonical breakout, doubled mechanically in M22, validated in M23",
    ),
    Contender(
        key="RT",
        label="momentum entered only while the efficiency ratio says trending",
        strategy_id="regime_trend",
        params=_m13("regime_trend"),
        axes=(("horizon", ("lookback", "er_window")), ("regime", ("er_min",))),
        provenance="M13 canonical, unchanged through M14, M15 and M16",
    ),
)
"""Two rules, frozen. The order is declaration order and carries no ranking."""


def contender_for(key: str) -> Contender:
    """Return the contender with this key."""
    return next(c for c in CONTENDERS if c.key == key)


def full_window(market: str) -> WindowSpec:
    """Return the market's whole history — the two declared halves joined."""
    return WindowSpec(start=in_sample_window(market).start, end=oos_window(market).end)


# --- Neighbours ------------------------------------------------------------------------------


def scaled(value: str, name: str, direction: int) -> str:
    """Return ``value`` moved one step in ``direction``, as bars or as a threshold."""
    moved = Decimal(value) * (Decimal(1) + NEIGHBOUR_STEP * direction)
    return str(int(moved)) if name in INTEGER_PARAMS else str(moved)


class Neighbour(DomainModel):
    """One configuration a step from its contender. A fragility measurement, never a candidate."""

    key: Text
    contender: Text
    axis: Text
    direction: int
    params: Params


def neighbours_for(contender: Contender) -> tuple[Neighbour, ...]:
    """Return the four neighbours: each declared axis, a quarter either way.

    Raises:
        ValueError: If an axis names a parameter the contender does not have. A silently
            ignored axis would produce a "neighbour" identical to the candidate and report
            fragility that was never tested.
    """
    names = {name for name, _ in contender.params}
    out: list[Neighbour] = []
    for axis, members in contender.axes:
        missing = set(members) - names
        if missing:
            msg = f"axis {axis!r} names parameters {contender.key} does not have: {sorted(missing)}"
            raise ValueError(msg)
        for direction in (-1, 1):
            label = "down" if direction < 0 else "up"
            out.append(
                Neighbour(
                    key=f"{contender.key}-{axis}-{label}",
                    contender=contender.key,
                    axis=axis,
                    direction=direction,
                    params=tuple(
                        (name, scaled(value, name, direction) if name in members else value)
                        for name, value in contender.params
                    ),
                )
            )
    return tuple(out)


# --- One builder, both contenders --------------------------------------------------------------


def contender_definition(
    market: str,
    contender: Contender,
    window: WindowSpec,
    *,
    label: str,
    latching: bool = False,
    params: Params | None = None,
) -> ExperimentDefinition:
    """Return a definition for one contender, market and window.

    Args:
        market: Raw symbol, e.g. ``"BTCUSDT"``.
        contender: Which frozen rule to run.
        window: The period to run over.
        label: Goes into the name, and so into the experiment's identity — two windows of one
            rule on one market must never file under a single experiment.
        latching: ``True`` runs deployed Risk V2 with its breakers latching; ``False`` runs
            M13's research variant, which is that configuration with the latches released.
        params: Overrides the contender's own, for a neighbour. Never used to retune.
    """
    base = _deployed_base()
    version = build_research_registry().metadata_for(contender.strategy_id).version
    at_timeframe = risk_for_timeframe(base.risk, TIMEFRAME)
    risk = at_timeframe if latching else risk_configuration_for(REFERENCE, at_timeframe)
    dataset = base.dataset.model_copy(
        update={
            "symbol": asset_for(market).symbol,
            "symbol_rules": symbol_rules_for(asset_for(market)),
            "market_type": MarketType.SPOT,
            "timeframe": TIMEFRAME,
            "start": window.start,
            "end": min(window.end, DATA_END),
            "source": "binance_vision_m16",
        }
    )
    copy = base.model_copy(
        update={
            "name": f"m24-{market}-{contender.key}-{label}",
            "strategy": StrategySpec(
                strategy_id=contender.strategy_id,
                strategy_version=version,
                params=params if params is not None else contender.params,
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": TIMEFRAME}),
            "risk": risk,
            "role": ExperimentRole.IN_SAMPLE,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def neighbour_definition(
    market: str, contender: Contender, neighbour: Neighbour
) -> ExperimentDefinition:
    """Return a neighbour's definition over the market's whole history."""
    return contender_definition(
        market,
        contender,
        full_window(market),
        label=neighbour.key,
        params=neighbour.params,
    )
