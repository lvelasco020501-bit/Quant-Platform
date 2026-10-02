"""M37 — why Risk V2 destroys this edge, and whether risk can be controlled more compatibly.

**Pre-declaration, committed before a single ablation result existed.** Research only. Nothing
here is deployed, nothing touches the VPS, production Risk, production execution, production
strategies, or B2's and regime_trend's parameters. Every strategy parameter is reached by
reference through M36 and M34 to M29's own probe objects, so this module restates none of them.

M36 measured the problem this milestone explains. On untouched signal timelines the combined
portfolio returned 29.34% a year at 31.87% drawdown, Calmar 0.92. Run on the positions the
certified engine actually took, the same portfolio returned 9.01% at 46.56%, Calmar 0.19 --
with 4.86 times the turnover, 1.70 times the fees, and a cost-stress result that goes negative
at double cost. The question M37 asks is *which part of Risk V2 does that*, and it asks it by
ablation rather than by search.

**Phase 1 is ablation, not tuning.** Start from the Risk V2 configuration M36 ran and disable
exactly one mechanism at a time. No threshold is moved, no parameter is improved, nothing is
optimised. A variant either has a mechanism or does not.

Three structural facts about that plan were established by reading the risk layer *before*
running anything, and all three limit what the ablation can claim:

* **The loss-streak breaker was already off.** M36 ran the reference latch policy every screen
  since M13 has used, and that policy sets ``max_consecutive_losses=None``. Disabling it is a
  no-op: the configuration comes out equal to the baseline by object identity. It is kept in
  the variant set anyway, as a control -- it must reproduce the baseline bit for bit, and if it
  does not, the harness is wrong and no other row can be trusted. Mechanism 8 therefore cannot
  be a cause of anything M36 measured.
* **The initial stop is not independently ablatable.** ``RiskConfiguration`` refuses a risk
  budget without a stop distance, because the budget measures risk *from* the stop; removing
  the stop alone raises ``ValueError``. So the stop is ablated jointly with sizing, and its own
  marginal effect is read as the difference between that joint variant and the sizing-only
  variant. This is a property of the configuration's coherence invariant, not a choice.
* **Re-entry after a stop cannot be ablated at all.** There is no cooldown, no re-entry delay
  and no post-stop state anywhere in the risk layer -- the grep is empty. Re-entry is not a
  mechanism Risk V2 has; it is what *happens* when a stop closes a position while the
  strategy's own condition is still true. It is therefore measured on every variant and
  disabled on none, and a cooldown is available only as a phase-2 alternative.

**The baseline is its own control.** The ablation runs on M16's six markets rather than M36's
thirty, because the engine is O(n squared) and nine variants over thirty markets is not
affordable. That universe is pre-existing and was not chosen by any result, but a six-market
portfolio at breadth six never selects -- it always holds all six -- so none of M37's numbers
are comparable with M36's headline figures. Every comparison in this milestone is
variant-against-baseline *within* M37, and the baseline variant re-measures the reference point
on this universe so that it can be.

**Phase 2 runs only if phase 1 finds a dominant cause**, and tests one pre-declared structural
alternative at a time. If no mechanism clears the primary-cause rule below, Risk V2 is not
dominated by any single part of itself, and the milestone stops and closes -- which is the
instruction, not a fallback.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel
from quantplatform.research.m16 import ASSETS
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import COST_STRESS_MULTIPLIERS, MIN_CALMAR
from quantplatform.research.m30 import ONE_WAY_COST_BASIS_POINTS, OOS_START
from quantplatform.research.m34 import SLEEVES, TIMEFRAME
from quantplatform.research.m36 import CANDIDATE

__all__ = [
    "ABLATIONS",
    "ASSETS_M37",
    "CAGR_GAP_RECOVERY_FOR_PRIMARY",
    "CANDIDATE",
    "COST_STRESS_MULTIPLIERS",
    "EDGE_CONSERVATION_FOR_ALTERNATIVE",
    "MAX_PRIMARY_MECHANISMS",
    "MIN_CALMAR",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "SCREEN_MAX_DRAWDOWN",
    "SLEEVES",
    "TIMEFRAME",
    "TURNOVER_REDUCTION_FOR_PRIMARY",
    "UNIVERSE_M37",
    "Ablation",
    "Alternative",
    "Mechanism",
    "MechanismRow",
    "Rejection",
    "is_primary",
    "primary_causes",
    "survives",
]


ASSETS_M37: Final[tuple[str, ...]] = tuple(asset.raw for asset in ASSETS)
"""M16's six markets, taken by reference from M16's own catalogue rather than retyped.

Pre-existing and not selected by any M37 result. The reason for six rather than M36's thirty is
cost and nothing else: the engine is O(n squared), and nine variants over thirty markets would
cost roughly twenty-five hours of CPU against this universe's eight and a half."""

UNIVERSE_M37: Final[int] = len(ASSETS_M37)
"""Six, which is also the breadth -- so this portfolio never selects, it always holds all six.
That is the reason M37's levels are not comparable with M36's, stated as a constant."""


class Mechanism(StrEnum):
    """The parts of Risk V2 M37 enumerates. Declared from the configuration, not guessed."""

    SIZING = "sizing"
    INITIAL_STOP = "initial_stop"
    BREAK_EVEN = "break_even"
    TRAILING_STOP = "trailing_stop"
    TAKE_PROFIT = "take_profit"
    TIME_STOP = "time_stop"
    DRAWDOWN_BREAKER = "drawdown_breaker"
    LOSS_STREAK_BREAKER = "loss_streak_breaker"
    RE_ENTRY = "re_entry"


ALREADY_INACTIVE: Final[frozenset[Mechanism]] = frozenset({Mechanism.LOSS_STREAK_BREAKER})
"""Mechanisms the M36 configuration did not have switched on, so ablating them proves nothing.

The reference latch policy sets ``max_consecutive_losses=None``. Its variant is retained as a
harness control that must come out identical to the baseline."""

NOT_ABLATABLE: Final[frozenset[Mechanism]] = frozenset({Mechanism.RE_ENTRY})
"""Mechanisms with no switch to turn off. Re-entry is a consequence of a stop firing while the
strategy still wants the position, not a configurable behaviour; it is measured everywhere."""

COUPLED_TO_SIZING: Final[frozenset[Mechanism]] = frozenset({Mechanism.INITIAL_STOP})
"""Mechanisms the configuration refuses to ablate alone. The risk budget measures risk from the
initial stop, so removing the stop removes risk-based sizing with it."""


class Ablation(DomainModel):
    """One variant: the baseline with a declared set of fields overridden, and nothing else."""

    key: str
    label: str
    disables: tuple[Mechanism, ...]
    """Which mechanisms this variant removes. Empty for the baseline."""

    expected_identical_to_baseline: bool = False
    """Whether this variant must reproduce the baseline exactly. True only for the control."""

    @property
    def is_control(self) -> bool:
        """Return whether this variant exists to validate the harness rather than to measure."""
        return self.expected_identical_to_baseline


ABLATIONS: Final[tuple[Ablation, ...]] = (
    Ablation(key="BASE", label="Risk V2 as M36 ran it", disables=()),
    Ablation(key="A", label="no risk-based sizing (stop kept)", disables=(Mechanism.SIZING,)),
    Ablation(
        key="B",
        label="no initial stop, which the config couples to sizing",
        disables=(Mechanism.INITIAL_STOP, Mechanism.SIZING),
    ),
    Ablation(key="C", label="no break-even move", disables=(Mechanism.BREAK_EVEN,)),
    Ablation(key="D", label="no trailing stop", disables=(Mechanism.TRAILING_STOP,)),
    Ablation(key="E", label="no take profit", disables=(Mechanism.TAKE_PROFIT,)),
    Ablation(key="F", label="no time stop", disables=(Mechanism.TIME_STOP,)),
    Ablation(
        key="G",
        label="no drawdown or daily-loss breakers",
        disables=(Mechanism.DRAWDOWN_BREAKER,),
    ),
    Ablation(
        key="H",
        label="control: no loss-streak breaker, which was already off",
        disables=(Mechanism.LOSS_STREAK_BREAKER,),
        expected_identical_to_baseline=True,
    ),
)
"""Nine variants: the baseline, seven real ablations, and one control.

``B`` disables two mechanisms because the configuration will not let it disable one; the
initial stop's own marginal effect is ``B`` minus ``A``, declared here rather than derived
later. No variant moves a threshold."""


TURNOVER_REDUCTION_FOR_PRIMARY: Final[Decimal] = Decimal("0.25")
"""How much of the baseline's turnover a single ablation must remove to be called primary.

A quarter. Set before any ablation ran, and set against a measured gap rather than in the
abstract: M36's certified basis turned over 4.86 times the signal basis, so a mechanism that
accounts for a meaningful share of that has to move turnover substantially, not detectably."""

CAGR_GAP_RECOVERY_FOR_PRIMARY: Final[Decimal] = Decimal("0.25")
"""How much of the baseline-to-signals CAGR gap a single ablation must recover to be primary.

The same quarter, applied to the other half of the question. Turnover alone would not do: a
mechanism could churn without costing return, and a mechanism could cost return without
churning. A primary cause has to do both."""

MAX_PRIMARY_MECHANISMS: Final[int] = 2
"""At most two mechanisms may be carried into phase 2, as the milestone requires. If more than
two clear the rule, the two with the largest CAGR-gap recovery are taken -- ranked by the
measure the milestone is about, not by turnover."""

EDGE_CONSERVATION_FOR_ALTERNATIVE: Final[Decimal] = Decimal("0.50")
"""How much of the baseline-to-signals CAGR gap a phase-2 alternative must recover to continue.

Half, against a quarter for merely identifying a cause. An alternative is proposing to replace
part of a working safety system, so it is held to a higher bar than a diagnosis is."""


class MechanismRow(DomainModel):
    """One ablation's measured result, with no judgement applied."""

    key: str
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    turnover: Decimal
    fees: Decimal
    re_entries: int
    """Entries taken while the strategy's own condition had not lapsed since the prior exit."""

    stops: int
    out_of_sample_return: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None


def is_primary(row: MechanismRow, *, base: MechanismRow, signals_annual: Decimal) -> bool:
    """Return whether disabling this mechanism alone accounts for a primary share of the damage.

    Both conditions, declared above and fixed before any result: the variant removes at least a
    quarter of the baseline's turnover, and it recovers at least a quarter of the distance
    between the baseline's annual return and the signal basis's.

    A variant whose turnover or CAGR is missing cannot clear the rule, rather than clearing it
    by default. Where the baseline already equals the signal basis there is no gap to recover
    and nothing can be primary.
    """
    if row.annual is None or base.annual is None or base.turnover <= 0:
        return False
    gap = signals_annual - base.annual
    if gap <= 0:
        return False
    turnover_cut = (base.turnover - row.turnover) / base.turnover
    recovered = (row.annual - base.annual) / gap
    return (
        turnover_cut >= TURNOVER_REDUCTION_FOR_PRIMARY
        and recovered >= CAGR_GAP_RECOVERY_FOR_PRIMARY
    )


def primary_causes(
    rows: tuple[MechanismRow, ...], *, base: MechanismRow, signals_annual: Decimal
) -> tuple[str, ...]:
    """Return the keys of the mechanisms that clear the primary-cause rule, at most two.

    Ranked by recovered CAGR gap, descending, so the cut to two is made on the measure the
    milestone is about. The baseline and the control are never candidates: the baseline is the
    thing being explained, and the control disables a mechanism that was already off.
    """
    controls = {a.key for a in ABLATIONS if a.is_control or not a.disables}
    qualified = [
        row
        for row in rows
        if row.key not in controls and is_primary(row, base=base, signals_annual=signals_annual)
    ]
    ranked = sorted(
        qualified,
        key=lambda row: (row.annual or Decimal(0)) - (base.annual or Decimal(0)),
        reverse=True,
    )
    return tuple(row.key for row in ranked[:MAX_PRIMARY_MECHANISMS])


class Rejection(StrEnum):
    """Why a phase-2 alternative does not survive. Declared before any alternative was run."""

    NO_LARGE_LOSS_PROTECTION = "no_large_loss_protection"
    TURNOVER_NOT_REDUCED = "turnover_not_reduced"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"
    COST_FRAGILE = "cost_fragile"
    DRAWDOWN = "drawdown"
    INTRODUCES_RATCHET = "introduces_ratchet"
    MORE_FRAGILE = "more_fragile"
    EDGE_NOT_CONSERVED = "edge_not_conserved"


class Alternative(DomainModel):
    """One pre-declared structural alternative, measured and judged."""

    key: str
    label: str
    keeps_catastrophic_stop: bool
    """Whether some price level still closes the position before a loss becomes unbounded.

    The milestone's first criterion. An alternative that merely removes protection is not an
    alternative, so this is a property of the design and is declared, not measured."""

    introduces_ratchet: bool
    """Whether the alternative adds state that, once entered, price action alone cannot leave.

    A latch is the example. Declared from the design for the same reason as the stop: whether a
    mechanism can release is a fact about how it is written, not about what a sample showed."""

    row: MechanismRow


def survives(
    alternative: Alternative, *, base: MechanismRow, signals_annual: Decimal
) -> tuple[bool, tuple[Rejection, ...]]:
    """Return whether an alternative clears every pre-declared criterion, and what it failed.

    Every condition the milestone names, in one place, evaluated against the baseline measured
    on M37's own universe rather than against M36's figures.
    """
    row = alternative.row
    reasons: list[Rejection] = []
    if not alternative.keeps_catastrophic_stop:
        reasons.append(Rejection.NO_LARGE_LOSS_PROTECTION)
    if alternative.introduces_ratchet:
        reasons.append(Rejection.INTRODUCES_RATCHET)
    if base.turnover <= 0 or (
        (base.turnover - row.turnover) / base.turnover < TURNOVER_REDUCTION_FOR_PRIMARY
    ):
        reasons.append(Rejection.TURNOVER_NOT_REDUCED)
    if row.out_of_sample_return is None or row.out_of_sample_return <= 0:
        reasons.append(Rejection.OUT_OF_SAMPLE_NEGATIVE)
    if (
        row.annual_at_double_cost is None
        or row.annual_at_double_cost <= 0
        or row.annual_at_triple_cost is None
        or row.annual_at_triple_cost <= 0
    ):
        reasons.append(Rejection.COST_FRAGILE)
    if row.max_drawdown > SCREEN_MAX_DRAWDOWN:
        reasons.append(Rejection.DRAWDOWN)
    if row.calmar_ratio is None or row.calmar_ratio < MIN_CALMAR:
        reasons.append(Rejection.MORE_FRAGILE)
    gap = signals_annual - (base.annual if base.annual is not None else Decimal(0))
    recovered = (
        Decimal(0)
        if gap <= 0 or row.annual is None or base.annual is None
        else (row.annual - base.annual) / gap
    )
    if recovered < EDGE_CONSERVATION_FOR_ALTERNATIVE:
        reasons.append(Rejection.EDGE_NOT_CONSERVED)
    return not reasons, tuple(reasons)
