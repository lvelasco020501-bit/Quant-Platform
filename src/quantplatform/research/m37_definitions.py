"""Turn M37's declared ablations into the configurations and definitions that measure them.

One mapping, from a :class:`~quantplatform.research.m37.Mechanism` to the configuration fields
that switch it off, and one builder that puts an ablated configuration into an otherwise
unchanged experiment definition. Nothing here chooses a threshold: every override is either
``None`` or the permissive bound of a field that has no ``None``, so a variant removes a
mechanism and never retunes it.

The definition is M36's, field for field, with two differences: the risk configuration is the
ablated one, and the name carries the variant key so two runs of the same pair under different
variants are different experiments rather than one cached twice.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

from quantplatform.core.enums import Timeframe
from quantplatform.research.definition import ExperimentDefinition, canonical_json
from quantplatform.research.latch_policy import risk_configuration_for
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m22 import _base
from quantplatform.research.m36_definitions import definition_for as m36_definition_for
from quantplatform.research.m37 import Mechanism
from quantplatform.research.m37_alternatives import stop_distance_for
from quantplatform.risk.config import RiskConfiguration

if TYPE_CHECKING:
    from quantplatform.research.m29 import Probe
    from quantplatform.research.m36_definitions import MarketRef
    from quantplatform.research.m37 import Ablation
    from quantplatform.research.m37_alternatives import AlternativeSpec

__all__ = [
    "OVERRIDES",
    "alternative_definition_for",
    "alternative_risk",
    "baseline_risk",
    "definition_for",
    "risk_for",
]


OVERRIDES: Final[dict[Mechanism, dict[str, object]]] = {
    Mechanism.SIZING: {"risk_budget": None},
    Mechanism.INITIAL_STOP: {"initial_stop_distance_bps": None},
    Mechanism.BREAK_EVEN: {"break_even_activation_bps": None},
    Mechanism.TRAILING_STOP: {"trailing_activation_bps": None, "trailing_distance_bps": None},
    Mechanism.TAKE_PROFIT: {"take_profit_distance_bps": None},
    Mechanism.TIME_STOP: {"max_holding_bars": None},
    Mechanism.DRAWDOWN_BREAKER: {
        # These three have no "off": the two drawdown limits are non-optional rates, so the
        # only way to remove the guard rather than move it is to raise it to its own bound,
        # where no equity path can reach it. The daily-loss limit is optional and is cleared.
        "max_total_drawdown_pct": Decimal(1),
        "max_daily_drawdown_pct": Decimal(1),
        "max_daily_loss_pct": None,
    },
    Mechanism.LOSS_STREAK_BREAKER: {"max_consecutive_losses": None},
}
"""Which configuration fields each mechanism lives in.

``Mechanism.RE_ENTRY`` is deliberately absent: there is no field to clear, which is the reason
M37 measures re-entry on every variant rather than ablating it."""


def baseline_risk(timeframe: Timeframe) -> RiskConfiguration:
    """Return the exact Risk V2 configuration M36 ran, at this timeframe.

    The deployed configuration converted by M15's rules and then given the reference latch
    policy, which is the pair every screen since M13 has used. Reached through the same
    functions M36 called rather than restated, so the baseline cannot drift from what it claims
    to reproduce.
    """
    return risk_configuration_for(REFERENCE, risk_for_timeframe(_base().risk, timeframe))


def risk_for(ablation: Ablation, timeframe: Timeframe) -> RiskConfiguration:
    """Return the baseline configuration with this variant's mechanisms switched off.

    Args:
        ablation: The declared variant. An empty ``disables`` returns the baseline itself.
        timeframe: The bar interval, which selects the risk conversion.

    Returns:
        A configuration differing from the baseline only in the fields this variant's
        mechanisms occupy.

    Raises:
        ValueError: If the resulting combination is one ``RiskConfiguration`` refuses -- which
            is how the coupling between the initial stop and risk-based sizing surfaces, rather
            than being worked around.
    """
    base = baseline_risk(timeframe)
    update: dict[str, object] = {}
    for mechanism in ablation.disables:
        update.update(OVERRIDES[mechanism])
    if not update:
        return base
    return RiskConfiguration.model_validate({**base.model_dump(), **update})


def definition_for(
    ref: MarketRef, probe: Probe, timeframe: Timeframe, ablation: Ablation
) -> ExperimentDefinition:
    """Return M36's definition for this pair, with this variant's risk and a name that says so.

    Args:
        ref: The market, its venue rules and its window.
        probe: Which frozen rule to run.
        timeframe: The bar interval.
        ablation: The variant whose configuration replaces the baseline's.

    Returns:
        A definition identical to M36's for this pair except for the risk configuration and the
        name, round-tripped through canonical JSON so it carries no computed field.
    """
    base = m36_definition_for(ref, probe, timeframe, latching=False)
    copy = base.model_copy(
        update={
            "name": f"m37-{ablation.key}-{base.name.removeprefix('m36-')}",
            "risk": risk_for(ablation, timeframe),
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def alternative_risk(spec: AlternativeSpec, timeframe: Timeframe) -> RiskConfiguration:
    """Return the baseline with this alternative's modifications removed and its stop widened.

    The surviving stop's distance comes from ``stop_distance_for``, which can only return the
    deployed budget's own declared maximum -- so the number cannot be something chosen for how
    it performed. Risk-based sizing is kept, which means the wider stop funds a proportionally
    smaller position; that is what sizing by risk does, and it is declared rather than avoided.

    Raises:
        ValueError: If the configuration has no risk budget to take a maximum distance from, or
            if that maximum is not wider than the stop it would replace.
    """
    base = baseline_risk(timeframe)
    if base.risk_budget is None or base.initial_stop_distance_bps is None:
        msg = (
            "this alternative replaces a trade-managing stop with a wider catastrophic one, so "
            "it needs both a risk budget to read a maximum distance from and a current stop to "
            "widen; a V1 configuration has neither"
        )
        raise ValueError(msg)
    update: dict[str, object] = {}
    for mechanism in spec.removes:
        update.update(OVERRIDES[mechanism])
    if spec.widen_stop_to_budget_maximum:
        update["initial_stop_distance_bps"] = stop_distance_for(
            base.risk_budget.max_stop_distance_bps, base.initial_stop_distance_bps
        )
    return RiskConfiguration.model_validate({**base.model_dump(), **update})


def alternative_definition_for(
    ref: MarketRef, probe: Probe, timeframe: Timeframe, spec: AlternativeSpec
) -> ExperimentDefinition:
    """Return M36's definition for this pair under one phase-2 alternative."""
    base = m36_definition_for(ref, probe, timeframe, latching=False)
    copy = base.model_copy(
        update={
            "name": f"m37-{spec.key}-{base.name.removeprefix('m36-')}",
            "risk": alternative_risk(spec, timeframe),
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))
