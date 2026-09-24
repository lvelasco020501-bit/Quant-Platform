"""M23 — can B2 honestly become a paper candidate?

M22 ended WEAK on its best cell, and not because a test failed. Every check the verdict could
make, B2 on BTC passed: a 6.34% drawdown, a profit factor of 1.48, eight of nine walk-forward
windows positive, a worst cost scenario still at +10.68%, and +12.36% under deployed Risk V2.
What it lacked was an **out-of-sample window**, because M22 never declared one — M13 through
M16 did, and I did not. ``Evidence.out_of_sample_return`` stayed ``None``, and ``None`` fails
its check. Taking the last walk-forward fold as that window afterwards would have produced a
PAPER CANDIDATE off a +0.87% fold that reads -0.07% for B1; that reading was recorded as
post-hoc and refused. This milestone does the thing properly instead.

**The window, and what it is and is not clean with respect to.**
:data:`OOS_START` is a single fixed date, identical on all four markets, declared here before
any M23 run. The rule that produced it reads no result: the most recent stretch of data is
what a deployed rule meets next, one calendar boundary applies the same cut to four markets
with different histories, and it leaves every market at least three years in sample.

It is **not** a virgin window, and saying otherwise would be false. M22 ran B2 continuously
over the whole history, these dates included, and reported aggregate per-year counts — nine of
ten BTC years positive, seven of ten on ETH. What the window *is* clean with respect to is the
only thing that matters for a parameter claim: **no parameter was ever chosen by looking at
it.** B2's numbers are M13's canonical breakout values doubled by a mechanical rule, fixed
before M22 ran, and this milestone changes none of them.

**One candidate, four markets, the same numbers.** No optimisation, no per-market fitting, no
new family. If B2 fails, the breakout line closes; it does not get adjusted.

**Neighbours are computed, not picked.** Two axes — the channel and the trend filter — moved
:data:`NEIGHBOUR_STEP` in each direction, one axis at a time, by :func:`neighbour_params`. The
channel keeps its 2:1 entry-to-exit ratio because 40/20 is one channel described by two
numbers. Four neighbours is enough to separate "the horizon is fragile" from "the filter is
fragile" and few enough that nobody can call it a search.

**The failure conditions are the ones asked for**, and their thresholds are either the
platform's own or written down here with their reasoning. The operability floor in particular
invents nothing: it is :data:`~quantplatform.research.m15.MIN_TRADES_PER_TEST_WINDOW`, the
number the platform already demands of a walk-forward year, applied per calendar year to the
deployed run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.definition import ExperimentDefinition, canonical_json
from quantplatform.research.folds import WindowSpec
from quantplatform.research.m15 import MIN_TRADES_PER_TEST_WINDOW
from quantplatform.research.m16 import DATA_END
from quantplatform.research.m22 import (
    CANDIDATES_M22,
    Variant,
    asset_for,
    deployed_definition,
    study_definition,
)
from quantplatform.research.sprint import Params, SprintCandidate

__all__ = [
    "CANDIDATE",
    "MARKETS",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_DEPLOYED_TRADES_PER_YEAR",
    "MIN_MARKETS_POSITIVE",
    "MIN_OOS_RETURN",
    "MIN_YEARS_POSITIVE_SHARE",
    "NEIGHBOURS",
    "NEIGHBOUR_STEP",
    "OOS_START",
    "Failure",
    "Neighbour",
    "in_sample_window",
    "neighbour_definition",
    "neighbour_params",
    "oos_window",
    "windowed_definition",
]

OOS_START: Final[datetime] = datetime(2024, 1, 1, tzinfo=UTC)
"""The out-of-sample boundary. One date, every market, declared before any M23 run.

Two years and eight months of data sit beyond it — long enough in crypto to contain both a
drawdown and a recovery, short enough to leave BNB and BTC more than six years in sample and
SOL more than three."""

MARKETS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
"""The four M22 carried. ADA and XRP stay out of scope, as they were."""

CANDIDATE: Final[Variant] = next(v for v in CANDIDATES_M22 if v.key == "B2")
"""B2: ``breakout_trend`` at 40/20 with a 400-bar trend filter. Not re-derived, not retuned."""


# --- Windows -------------------------------------------------------------------------------------


def in_sample_window(market: str) -> WindowSpec:
    """Return the market's own history up to the declared boundary."""
    return WindowSpec(start=asset_for(market).start, end=OOS_START)


def oos_window(market: str) -> WindowSpec:
    """Return the out-of-sample window. Identical for every market, hence the unused argument."""
    del market
    return WindowSpec(start=OOS_START, end=DATA_END)


# --- Neighbours ----------------------------------------------------------------------------------

NEIGHBOUR_STEP: Final[Decimal] = Decimal("0.25")
"""A quarter either way. Small enough that a result which breaks is genuinely fragile, large
enough that surviving it is not just arithmetic rounding."""

_AXES: Final[dict[str, tuple[str, ...]]] = {
    "channel": ("entry_lookback", "exit_lookback"),
    "trend": ("trend_period",),
}


def neighbour_params(base: Params, axis: str, direction: int) -> Params:
    """Return ``base`` with one axis scaled by one step in ``direction``.

    The channel's two lookbacks move together, so 40/20 becomes 50/25 or 30/15 and never a
    shape the candidate is not a version of.

    Raises:
        ValueError: If ``axis`` is not one this milestone declared.
    """
    if axis not in _AXES:
        msg = f"no such axis {axis!r}: declare it as 'channel' or 'trend'"
        raise ValueError(msg)
    factor = Decimal(1) + (NEIGHBOUR_STEP * direction)
    moved = _AXES[axis]
    return tuple(
        (name, str(int(Decimal(value) * factor)) if name in moved else value)
        for name, value in base
    )


class Neighbour(DomainModel):
    """One configuration a step away from the candidate, used to measure fragility only."""

    key: Text
    axis: Text
    direction: int
    params: Params


def _neighbours() -> tuple[Neighbour, ...]:
    base = CANDIDATE.candidate.params
    out: list[Neighbour] = []
    for axis in _AXES:
        for direction in (-1, 1):
            label = "down" if direction < 0 else "up"
            out.append(
                Neighbour(
                    key=f"N-{axis}-{label}",
                    axis=axis,
                    direction=direction,
                    params=neighbour_params(base, axis, direction),
                )
            )
    return tuple(out)


NEIGHBOURS: Final[tuple[Neighbour, ...]] = _neighbours()
"""Four: the channel a quarter shorter and longer, the trend filter a quarter shorter and
longer. Never candidates — a neighbour exists to be a fragility measurement."""


# --- How the candidate is allowed to fail ---------------------------------------------------------


class Failure(StrEnum):
    """The ways B2 is declared to fail, fixed before the runs."""

    ONE_MARKET = "works only on BTC"
    THIN_OOS = "out-of-sample return is negative or nearly nil"
    STRESS = "the cost stress breaks the edge"
    FRAGILE = "small parameter changes destroy the result"
    ONE_YEAR = "the result rests on a single year"
    UNOPERABLE = "Risk V2 leaves it barely operable"


MIN_OOS_RETURN: Final[Decimal] = Decimal("0.03")
"""What "nearly nil" means, in a number, before the number arrives.

Over the declared window B2 should close somewhere near seventy trades at 1% risk each. Three
percent across two years and eight months is roughly one percent a year — a deliberately low
bar that still sits above what a handful of lucky trades produces. It is a floor on being
distinguishable from nothing, not a target."""

MIN_MARKETS_POSITIVE: Final[int] = 3
"""Of four. Two would let one market's era carry the result; four would fail a candidate for a
single market's idiosyncrasy. Three is the majority that cannot be one market."""

MAX_SINGLE_YEAR_SHARE: Final[Decimal] = Decimal("0.50")
"""No calendar year may be more than half of the whole result. A rule whose edge is one year
is a rule that was present for one move."""

MIN_YEARS_POSITIVE_SHARE: Final[Decimal] = Decimal("0.60")
"""And most years must pay. Concentration and consistency are different failures, so they get
different checks."""

MIN_DEPLOYED_TRADES_PER_YEAR: Final[int] = MIN_TRADES_PER_TEST_WINDOW
"""Not a new threshold. The platform already refuses PAPER CANDIDATE to anything with fewer
than five trades in a walk-forward test year; applied per calendar year to the deployed run,
it says when Risk V2 has gutted the sample rather than merely trimmed it."""


# --- Definitions ---------------------------------------------------------------------------


def windowed_definition(
    market: str, window: WindowSpec, *, label: str, latching: bool = False
) -> ExperimentDefinition:
    """Return the candidate's definition restricted to one window.

    The window is part of the name, and so of the experiment's identity: an in-sample and an
    out-of-sample run of one rule on one market must never file under a single experiment.
    """
    build = deployed_definition if latching else study_definition
    base = build(market, CANDIDATE)
    dataset = base.dataset.model_copy(update={"start": window.start, "end": window.end})
    copy = base.model_copy(update={"dataset": dataset, "name": f"m23-{market}-B2-{label}"})
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def neighbour_definition(market: str, neighbour: Neighbour) -> ExperimentDefinition:
    """Return a neighbour's definition over the market's whole history."""
    base = study_definition(market, CANDIDATE)
    spec = SprintCandidate(
        strategy_id=CANDIDATE.candidate.strategy_id,
        family=CANDIDATE.candidate.family,
        params=neighbour.params,
        rationale="Fragility measurement for M23. Never a candidate.",
    )
    copy = base.model_copy(
        update={
            "strategy": base.strategy.model_copy(update={"params": spec.params}),
            "name": f"m23-{market}-B2-{neighbour.key}",
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))
