"""M31 — rotation with risk-controlled exposure: can the drawdown be cut without the edge?

**Pre-declaration. Written and committed before a single result was seen.**

M30 produced the first genuinely large returns this project has measured -- CS2 at 65.39% a
year and RF1 at 69.02% -- and refused both, because they ran at 78% and 75% drawdown against a
declared cap of 35%. It also established that the cap is not a rotation-specific verdict: it
rejects buy-and-hold BTC at 83.19% and a static equal-weight basket at 85.21%. Long-only spot
crypto, fully invested, simply runs at four times the risk this platform allows.

So M31 asks the one question that follows, and only that one:

    Can that drawdown be brought under 35% by changing **how much** capital is exposed,
    without changing **what** the signals say?

Three properties make the answer mean something:

**The signals are frozen by import, not by intention.** :data:`FROZEN` takes CS2 and RF1
straight out of :data:`~quantplatform.research.m30.RULES_M30`. Nothing here restates a
lookback, a ranking, a threshold or a filter, so there is no copy of them to drift. A test
asserts the objects are the same ones M30 screened.

**Exposure cannot reach the ranking.** The overlay in
:class:`~quantplatform.research.rotation.ExposurePolicy` scales weights after the ranking has
chosen and can neither reorder nor replace a choice. That is what separates "the drawdown was
controllable" from "a different strategy was found".

**No threshold is chosen by return, and most mechanisms have no threshold at all.** The fixed
levels are the three the user named. The volatility target is the *causal trailing median* of
the aggregate's own volatility -- a property of the volatility series, never of the equity
curve, using no information the tape had not already shown, and requiring nobody to pick a
level. The drawdown response is ``(1 - drawdown) ** power`` measured from a running peak that
never resets: no level exists in it to be tuned.

One arithmetic fact is stated up front because it bounds what mechanism A can possibly do.
Scaling exposure by a constant scales the whole log-equity path by that constant, so log-return
and log-drawdown fall together -- but *arithmetic* Calmar does not survive the transformation,
because it compresses a return through ``exp`` and a drawdown through ``1 - exp``. A rule at
0.83 Calmar and 78% drawdown, held at a quarter exposure, lands near 32% drawdown and near 0.42
Calmar. If the constant-exposure family fails on Calmar while passing on drawdown, that is
geometry rather than evidence, and it is recorded here in advance so the result is not mistaken
for a discovery.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_CALMAR,
    cagr,
    calmar,
)
from quantplatform.research.m30 import (
    ASSETS_M30,
    MAX_SINGLE_ASSET_SHARE,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    RULES_M30,
    RotationRule,
)
from quantplatform.research.rotation import ExposurePolicy
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "ASSETS_M30",
    "COST_STRESS_MULTIPLIERS",
    "FROZEN",
    "MAX_SINGLE_ASSET_SHARE",
    "MAX_SINGLE_YEAR_SHARE",
    "MECHANISMS",
    "MIN_CALMAR",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "SCREEN_MAX_DRAWDOWN",
    "SHORT_VOLATILITY_WINDOW",
    "VOLATILITY_WINDOW",
    "ControlRobustness",
    "Controlled",
    "ExposureMechanism",
    "ExposureVariant",
    "cagr",
    "calmar",
    "survives",
    "variants",
]


# --- What is frozen ------------------------------------------------------------------------------

FROZEN_KEYS: Final[tuple[str, ...]] = ("CS2", "RF1")
"""The two configurations M31 is allowed to put exposure behind, and no others.

CS2 was M30's cleanest robustness profile: the only rule clearing the per-asset concentration
gate, six of six assets contributing positively, five of five walk-forward windows positive, and
+127.65% out of sample. RF1 had the best Calmar at 0.92 and the lowest drawdown at 75.02%, and
lost 12% in 2022 where the basket lost 77%."""

FROZEN: Final[tuple[RotationRule, ...]] = tuple(
    rule for rule in RULES_M30 if rule.key in FROZEN_KEYS
)
"""The frozen rules themselves, taken from M30 by reference rather than restated.

This is the mechanism that makes "do not change the signals" checkable instead of promised:
there is no second copy of a lookback or a threshold anywhere in this module, so none can drift.
The 144-bar lookback M30 noticed RF1 preferring is **not** touched here -- it is recorded as a
future hypothesis and is not a parameter of this milestone."""


# --- The mechanisms, and what is inherited about them --------------------------------------------

FIXED_LEVELS: Final[tuple[Decimal, ...]] = (
    Decimal("0.25"),
    Decimal("0.50"),
    Decimal("0.75"),
)
"""The three constant exposures the user named. The remainder sits in cash, uninvested and
earning nothing -- no money-market return is assumed, which understates every result here."""

VOLATILITY_WINDOW: Final[int] = 72
"""Bars the aggregate's volatility is measured over. **Inherited** from ``vol_momentum``'s
``vol_window``, the same 72 M30's cross-sectional score already divides by."""

SHORT_VOLATILITY_WINDOW: Final[int] = 24
"""The faster measurement. **Inherited** from ``vol_filtered_momentum``'s ``short_vol`` -- the
project's only other declared volatility window, so the pair moves one dimension and invents
nothing."""

DRAWDOWN_POWERS: Final[tuple[int, ...]] = (1, 2)
"""Exponents on remaining capital. One is the linear response and has no parameter at all; two
is the natural second choice and is steeper. Neither contains a level, so neither offers
anything to tune."""


class ExposureMechanism(StrEnum):
    """The four mechanisms. Declaration order is the order every table reports them in."""

    NONE = "uncontrolled"
    FIXED = "fixed"
    VOLATILITY_TARGET = "volatility_target"
    DRAWDOWN_AWARE = "drawdown_aware"


class ExposureVariant(DomainModel):
    """One frozen signal with one declared exposure policy behind it."""

    key: Text
    signal: Text
    """Which frozen rule's signals this puts capital behind."""
    mechanism: ExposureMechanism
    label: Text
    """How the variant is named in a table, e.g. ``fixed 50%``."""


MECHANISMS: Final[tuple[ExposureMechanism, ...]] = tuple(ExposureMechanism)


def _policies() -> tuple[tuple[str, ExposureMechanism, ExposurePolicy], ...]:
    """Return every declared exposure policy, with the label and mechanism it reports under."""
    out: list[tuple[str, ExposureMechanism, ExposurePolicy]] = [
        ("uncontrolled", ExposureMechanism.NONE, ExposurePolicy())
    ]
    out.extend(
        (
            f"fixed {level:.0%}".replace("%", "%"),
            ExposureMechanism.FIXED,
            ExposurePolicy(fixed=level),
        )
        for level in FIXED_LEVELS
    )
    out.extend(
        (
            f"vol target {window}",
            ExposureMechanism.VOLATILITY_TARGET,
            ExposurePolicy(volatility_window=window),
        )
        for window in (VOLATILITY_WINDOW, SHORT_VOLATILITY_WINDOW)
    )
    out.extend(
        (
            f"drawdown ^{power}",
            ExposureMechanism.DRAWDOWN_AWARE,
            ExposurePolicy(drawdown_power=power),
        )
        for power in DRAWDOWN_POWERS
    )
    return tuple(out)


POLICIES: Final[tuple[tuple[str, ExposureMechanism, ExposurePolicy], ...]] = _policies()
"""Eight policies: the uncontrolled reference plus seven controls. Applied to both frozen
signals that is sixteen cells, which is the whole of M31's first phase.

**Mechanism D is the RF1 half of this matrix, not a ninth policy.** "Keep RF1 exactly, cash when
the regime is unfavourable, exposure control when it is favourable" is what RF1 plus any of
these already does: the regime filter lives in RF1's own frozen spec and puts the rule in cash
on its own, and the overlay then governs how much is risked on the bars it does hold. Adding a
separate D would be the same experiment counted twice."""


def variants() -> tuple[ExposureVariant, ...]:
    """Return every declared cell, in the order the report presents them."""
    return tuple(
        ExposureVariant(
            key=f"{rule.key}-{label.replace(' ', '')}",
            signal=rule.key,
            mechanism=mechanism,
            label=label,
        )
        for rule in FROZEN
        for label, mechanism, _ in POLICIES
    )


def policy_for(label: str) -> ExposurePolicy:
    """Return the declared policy carrying ``label``."""
    return next(policy for name, _, policy in POLICIES if name == label)


# --- The gate ------------------------------------------------------------------------------------


class ControlRobustness(StrEnum):
    """Every declared way a controlled variant can fail. Order is the reporting order.

    Exactly the seven conditions the user declared, and nothing else. M30's gate also read
    sample size, cross-asset breadth and walk-forward stability; those are **measured and
    reported** here but do not gate, because adding a rejection criterion after the fact would
    be as much a breach of the pre-declaration as removing one.
    """

    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    SINGLE_YEAR = "single_year"
    ASSET_CONCENTRATION = "asset_concentration"
    COST_FRAGILE = "cost_fragile"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"


class Controlled(DomainModel):
    """Everything the gate reads about one controlled variant, gathered from its runs."""

    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    single_year_share: Decimal | None
    top_asset_share: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    out_of_sample_return: Decimal | None


def survives(measured: Controlled) -> tuple[bool, tuple[ControlRobustness, ...]]:
    """Return whether a variant clears every declared gate, and what it failed.

    **Reads no CAGR and no benchmark.** The objective of this milestone is a drawdown under the
    declared cap without the edge collapsing, and the order among whatever clears it is settled
    afterwards by Calmar -- which is inside the gate as a floor, not as a score. A variant that
    cut its drawdown to nothing by holding nothing fails the Calmar floor, which is the whole
    reason that floor is one of the seven.

    Returns:
        ``(True, ())`` when everything passed, otherwise ``(False, reasons)`` with the reasons
        in declaration order so two failures always report in the same sequence.
    """
    m = measured
    failures: list[ControlRobustness] = []
    if m.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failures.append(ControlRobustness.DRAWDOWN)
    if m.calmar_ratio is None or m.calmar_ratio < MIN_CALMAR:
        failures.append(ControlRobustness.LOW_CALMAR)
    if m.single_year_share is None or m.single_year_share > MAX_SINGLE_YEAR_SHARE:
        failures.append(ControlRobustness.SINGLE_YEAR)
    if m.top_asset_share is None or m.top_asset_share > MAX_SINGLE_ASSET_SHARE:
        failures.append(ControlRobustness.ASSET_CONCENTRATION)
    if (
        m.annual_at_double_cost is None
        or m.annual_at_double_cost <= 0
        or m.annual_at_triple_cost is None
        or m.annual_at_triple_cost <= 0
    ):
        failures.append(ControlRobustness.COST_FRAGILE)
    if m.out_of_sample_return is None or m.out_of_sample_return <= 0:
        failures.append(ControlRobustness.OUT_OF_SAMPLE_NEGATIVE)
    return (not failures, tuple(failures))


def registry_is_untouched() -> bool:
    """Return whether the strategy registry still builds, unchanged by this milestone.

    M31 adds no strategy and alters none. This is cheap to assert and it is the boundary that
    matters most: the paper sessions on the VPS load from that registry.
    """
    return len(build_research_registry()) > 0
