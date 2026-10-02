"""An ablation harness for Risk V2, measured without changing a line of Risk.

M37 needs two things production Risk does not record. The first is *why* each position closed:
``RiskAction`` carries a :class:`~quantplatform.core.enums.RiskCheckCode`, but ``ClosedTrade``
does not persist it, so a finished run says how many trades there were and not what ended them.
The second is *which stop level* ended them -- a protective-stop exit may be the initial stop,
the break-even stop or the trailing stop, and those are three different mechanisms wearing one
exit reason.

Both are read by observation rather than by modification. :class:`AblationRiskEngine` is a
research-only subclass that overrides one public method, ``evaluate_open_positions``, delegates
the decision to the standard engine unchanged, and then records what came back. It never
changes an action, never adds one, never suppresses one, and never approves anything the
underlying engine refused -- the same shape, and the same fail-closed argument, as
:class:`~quantplatform.research.latch_policy.LatchPolicyRiskEngine`.

The stop kind is read from the ``position_risk`` mapping the caller passes in, which is the
record the engine itself tested, so the attribution is the engine's own view of the position
and not a reconstruction of it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.engine import StandardRiskEngine

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from quantplatform.core.models.market import MarketBar
    from quantplatform.core.models.portfolio import Position
    from quantplatform.core.models.risk import PositionRiskState, RiskAction

__all__ = ["AblationRiskEngine", "ExitTally", "re_entries", "stretches"]


@dataclass
class ExitTally:
    """What ended each position over one run. Read after the run; never consulted during it."""

    by_code: Counter[str] = field(default_factory=Counter)
    """Exits keyed by the risk check that triggered them: protective stop, target, time stop."""

    by_stop_kind: Counter[str] = field(default_factory=Counter)
    """The same exits keyed by the kind of stop the position was carrying when it ended.

    This is what separates the three mechanisms that share the protective-stop exit reason: a
    ``hard`` stop is the initial one, ``break_even`` is the stop after it moved to recover
    costs, and ``trailing`` is the stop that follows the favourable extreme.
    """

    forced_exits: int = 0
    """Total positions closed by a risk action rather than by the strategy's own exit."""

    @property
    def protective_stops(self) -> int:
        """Return exits caused by a stop level being reached, of whatever kind."""
        return self.by_code.get("protective_stop", 0)


class AblationRiskEngine(StandardRiskEngine):
    """The standard risk engine, with every forced exit it decides recorded as it happens."""

    def __init__(self, *, config: RiskConfiguration) -> None:
        """Bind the engine to its limits and start an empty tally."""
        super().__init__(config=config)
        self._tally = ExitTally()

    @property
    def tally(self) -> ExitTally:
        """Return what has ended positions so far."""
        return self._tally

    def evaluate_open_positions(
        self,
        *,
        positions: Sequence[Position],
        position_risk: Mapping[str, PositionRiskState],
        bar: MarketBar,
        require_protection: bool = False,
    ) -> tuple[RiskAction, ...]:
        """Decide exactly as the standard engine would, and record what it decided.

        The actions are returned untouched. Recording happens after the decision, so nothing
        here can change which positions close or when.
        """
        actions = super().evaluate_open_positions(
            positions=positions,
            position_risk=position_risk,
            bar=bar,
            require_protection=require_protection,
        )
        for action in actions:
            self._tally.forced_exits += 1
            if action.triggered_by is not None:
                self._tally.by_code[action.triggered_by.value] += 1
            symbol = action.symbol
            state = position_risk.get(symbol) if symbol is not None else None
            if state is not None:
                self._tally.by_stop_kind[state.stop.kind.value] += 1
        return actions


def stretches(mask: Sequence[bool]) -> list[tuple[int, int]]:
    """Return each held stretch of a timeline as a half-open index range."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, held in enumerate(mask):
        if held and start is None:
            start = index
        elif not held and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(mask)))
    return spans


def re_entries(engine: Sequence[bool], signal: Sequence[bool]) -> int:
    """Count entries taken while the strategy's own condition had never lapsed.

    This is the churn M37 is about: risk closed a position, the strategy went on wanting it the
    entire time the account was flat, and risk let it back in. Measured as an engine stretch
    whose whole gap from the previous stretch lies inside an unbroken signal -- so an entry
    after the signal genuinely turned off and on again is *not* counted. That distinction is
    the point: the first is a mechanism fighting the strategy, the second is the strategy
    changing its mind, and conflating them would attribute the strategy's own decisions to
    Risk V2.
    """
    spans = stretches(engine)
    count = 0
    for (_, prior_end), (start, _) in pairwise(spans):
        gap = signal[prior_end:start]
        if gap and all(gap):
            count += 1
    return count
