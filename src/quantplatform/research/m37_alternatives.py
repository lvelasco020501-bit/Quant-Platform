"""M37 phase 2: one pre-declared structural alternative, committed before it was run.

Phase 1's ablation pointed at a single place. The drawdown and daily-loss breakers never fired
at all -- variant G came out byte-identical to the baseline. Break-even, trailing, take-profit
and the time stop each moved turnover by at most a tenth and recovered at most 6.7% of the
distance to the signal basis. Only removing the protective stop itself changed anything, and
removing it changed everything: turnover fell 70%, re-entries fell from 1350 to 1, and the
portfolio returned more than the untouched signals.

That the stop's three parts could not be separated is not a gap in the measurement -- it is the
finding. Break-even and trailing are not stops of their own; they are *moves of the one stop
level* the initial distance creates. The exit tally shows them sharing the work almost exactly
in thirds (break-even 34.1%, trailing 33.0%, hard 32.9%), so removing any one hands its exits to
the other two and the churn is unchanged. There is one mechanism here wearing three names.

**The alternative.** The milestone permits "strategy exit as the primary exit plus a hard safety
stop", and that is what this is: the stop stays, moved out to a distance where it is a
catastrophe brake rather than a trade manager, and every stop *modification* is removed so the
strategy's own exit decides when a winning position ends.

**Where the distance comes from, since this must not be a search.** It is
``max_stop_distance_bps`` from the deployed risk budget: 2000 basis points, the widest stop that
configuration already declares permissible. It was not chosen by looking at a return, it was not
tuned, and it is not mine -- it was already in the configuration M36 ran, as the bound the
budget clamps a stop to. One number, taken from the system being studied.

**What it costs, stated before measuring.** Risk-based sizing divides the risk budget by the
stop distance, so a stop three and a third times wider funds a position three and a third times
smaller: 5% of equity against the baseline's 16.7%. That is not a side effect to be engineered
around, it is what sizing by risk *means*, and it may well be enough to fail the edge-conservation
criterion on its own. The alternative is measured as declared either way.

**One alternative, not a family.** No variant of this is prepared to chase a return. If an
alternative fails its *criteria* the milestone reports that and proposes a Risk V3 for research
only; it does not go looking for a distance that performs better.

**ALT1 turned out not to be constructible, and that is recorded rather than quietly replaced.**
Run as declared, it produced zero trades on all twelve pairs: 211 entry rejections reading "the
stop is further than max_stop_distance_bps permits". The engine derives the stop *price* from the
reference price, rounds it to the venue tick *away* from entry -- which is the conservative
direction and correct -- then re-derives the distance from that rounded level and compares it to
the budget's bound. So a stop configured at exactly the bound almost always realises marginally
outside it and the entry is refused: 12 of 16 sampled price/tick combinations reject, and in
practice every real price does. ``max_stop_distance_bps`` reads as an inclusive bound and behaves
as an exclusive one. That is a second concrete incompatibility, independent of phase 1's, and it
belongs in the Risk V3 proposal.

**ALT2 is the re-declaration, and it changes feasibility rather than ambition.** The distance now
comes from this project's own doubling convention -- the one M22 uses for ``Horizon.DOUBLED``,
and M29 and M30 for their sensitivity neighbours -- applied to the stop being replaced: 600 bps
doubled is 1200. It is not a number chosen for how it performed, it is the established way this
codebase widens a parameter, and it was committed before ALT2 was run. It was checked only for
*feasibility* beforehand (22 trades against ALT1's zero, with no budget rejections), never for
return.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from quantplatform.core.models.base import DomainModel
from quantplatform.research.m37 import Mechanism

if TYPE_CHECKING:
    from decimal import Decimal

__all__ = [
    "ALTERNATIVES",
    "INFEASIBLE",
    "AlternativeSpec",
    "StopRule",
    "stop_distance_for",
]


class StopRule(StrEnum):
    """Where a safety stop's distance comes from. A rule, never a number.

    Recorded as provenance rather than as a value so it travels with the milestone: a 1200
    written down would be indistinguishable from a 1200 someone liked the look of.
    """

    BUDGET_MAXIMUM = "risk_budget.max_stop_distance_bps"
    """The deployed budget's own declared maximum. Not constructible -- see the module docstring."""

    DOUBLED = "baseline_stop_distance_doubled"
    """The stop being replaced, doubled, by this project's own neighbour convention."""


INFEASIBLE: Final[dict[str, str]] = {
    "ALT1": (
        "zero trades on all twelve pairs: 211 entry rejections for a stop further than "
        "max_stop_distance_bps permits. The engine rounds the stop price away from entry and "
        "then re-derives the distance, so a stop configured at the bound realises outside it."
    )
}
"""Alternatives that could not be built, and why. Kept so the attempt stays on the record."""


class AlternativeSpec(DomainModel):
    """One structural alternative: what it removes, what it keeps, and what it claims."""

    key: str
    label: str
    removes: tuple[Mechanism, ...]
    """The stop *modifications* this alternative deletes. The stop itself is never removed."""

    stop_rule: StopRule
    """Which declared rule supplies the surviving stop's distance."""

    keeps_catastrophic_stop: bool
    """Whether some price level still closes the position before a loss becomes unbounded.

    Declared from the design, not measured: whether a stop exists is a fact about the
    configuration, and a sample that happened never to reach it would not make it absent."""

    introduces_ratchet: bool
    """Whether this adds state that, once entered, price action alone cannot leave.

    A static stop at a fixed distance has none: it does not move, it does not latch, and there
    is no condition it can enter that only time or an operator can release. Trailing *is* a
    ratchet by construction -- it never retreats -- which is part of why it is removed here."""


_REMOVES: Final[tuple[Mechanism, ...]] = (
    Mechanism.BREAK_EVEN,
    Mechanism.TRAILING_STOP,
    Mechanism.TAKE_PROFIT,
    Mechanism.TIME_STOP,
)
"""The stop *modifications* both alternatives delete. The stop itself is never removed, and
neither is sizing: phase 1 showed that removing the budget is what crippled variant A."""

ALTERNATIVES: Final[tuple[AlternativeSpec, ...]] = (
    AlternativeSpec(
        key="ALT1",
        label="strategy exit primary, safety stop at the budget's maximum (not constructible)",
        removes=_REMOVES,
        stop_rule=StopRule.BUDGET_MAXIMUM,
        keeps_catastrophic_stop=True,
        introduces_ratchet=False,
    ),
    AlternativeSpec(
        key="ALT2",
        label="strategy exit primary, safety stop at twice the baseline distance",
        removes=_REMOVES,
        stop_rule=StopRule.DOUBLED,
        keeps_catastrophic_stop=True,
        introduces_ratchet=False,
    ),
)
"""Two entries, one measurable. ALT1 is retained because an attempt that could not be built is
part of the record, and its failure is itself a finding; ALT2 is the same structure at a
feasible distance. The breakers are kept in both, phase 1 having shown they never fire."""


def stop_distance_for(rule: StopRule, *, budget_maximum: Decimal, current: Decimal) -> Decimal:
    """Return the safety stop's distance from a declared rule, never from a chosen value.

    A function rather than a constant so the provenance is enforced instead of described: every
    branch derives its answer from the configuration under study or from this project's own
    convention, and there is no branch that simply returns a literal.

    Raises:
        ValueError: If the resulting distance is not wider than the stop being replaced -- a
            "catastrophe brake" no further out than the trade manager it replaces is the same
            mechanism renamed -- or if it exceeds what the budget permits, which is ALT1's
            recorded failure rather than something to work around silently.
    """
    distance = budget_maximum if rule is StopRule.BUDGET_MAXIMUM else current * 2
    if distance <= current:
        msg = (
            f"{rule.value} gives {distance} bps, which is not wider than the stop it would "
            f"replace ({current} bps), so there is no catastrophic stop to move out to and this "
            f"alternative does not exist for this configuration"
        )
        raise ValueError(msg)
    if distance > budget_maximum:
        msg = (
            f"{rule.value} gives {distance} bps, beyond the budget's maximum of "
            f"{budget_maximum} bps, so risk-based sizing would refuse every entry"
        )
        raise ValueError(msg)
    return distance
