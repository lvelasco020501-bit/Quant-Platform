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

**One alternative, not a family.** No variant of this is prepared, and no second distance. If
this fails its criteria the milestone reports that and proposes a Risk V3 for research only; it
does not go looking for a distance that passes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from quantplatform.core.models.base import DomainModel
from quantplatform.research.m37 import Mechanism

if TYPE_CHECKING:
    from decimal import Decimal

__all__ = ["ALTERNATIVES", "CATASTROPHIC_STOP_SOURCE", "AlternativeSpec", "stop_distance_for"]


CATASTROPHIC_STOP_SOURCE: Final[str] = "risk_budget.max_stop_distance_bps"
"""Where the safety stop's distance comes from: the deployed budget's own declared maximum.

Recorded as a string rather than as a number so the provenance travels with the milestone. A
2000 written here would be indistinguishable from a 2000 someone liked the look of."""


class AlternativeSpec(DomainModel):
    """One structural alternative: what it removes, what it keeps, and what it claims."""

    key: str
    label: str
    removes: tuple[Mechanism, ...]
    """The stop *modifications* this alternative deletes. The stop itself is never removed."""

    widen_stop_to_budget_maximum: bool
    """Whether the surviving stop is moved out to the budget's declared maximum distance."""

    keeps_catastrophic_stop: bool
    """Whether some price level still closes the position before a loss becomes unbounded.

    Declared from the design, not measured: whether a stop exists is a fact about the
    configuration, and a sample that happened never to reach it would not make it absent."""

    introduces_ratchet: bool
    """Whether this adds state that, once entered, price action alone cannot leave.

    A static stop at a fixed distance has none: it does not move, it does not latch, and there
    is no condition it can enter that only time or an operator can release. Trailing *is* a
    ratchet by construction -- it never retreats -- which is part of why it is removed here."""


ALTERNATIVES: Final[tuple[AlternativeSpec, ...]] = (
    AlternativeSpec(
        key="ALT1",
        label="strategy exit primary, hard safety stop at the budget's maximum distance",
        removes=(
            Mechanism.BREAK_EVEN,
            Mechanism.TRAILING_STOP,
            Mechanism.TAKE_PROFIT,
            Mechanism.TIME_STOP,
        ),
        widen_stop_to_budget_maximum=True,
        keeps_catastrophic_stop=True,
        introduces_ratchet=False,
    ),
)
"""Exactly one alternative. Risk-based sizing and the breakers are both kept: phase 1 showed
that removing sizing is what crippled variant A, and that the breakers never fired."""


def stop_distance_for(budget_maximum: Decimal, current: Decimal) -> Decimal:
    """Return the safety stop's distance, which is the budget's maximum and nothing else.

    A function rather than a constant so the provenance is enforced instead of described: it
    cannot return a number that is not the budget's own maximum.

    Raises:
        ValueError: If the budget's maximum is not wider than the stop being replaced. A
            "catastrophe brake" no further out than the trade-managing stop it replaces would
            be the same mechanism under a new name.
    """
    if budget_maximum <= current:
        msg = (
            f"the budget's maximum stop distance ({budget_maximum} bps) is not wider than the "
            f"stop it would replace ({current} bps), so there is no catastrophic stop to move "
            f"out to and this alternative does not exist for this configuration"
        )
        raise ValueError(msg)
    return budget_maximum
