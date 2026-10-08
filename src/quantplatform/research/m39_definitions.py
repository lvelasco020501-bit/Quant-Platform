"""Turn M39's declared variants into experiment definitions. Plumbing, not policy.

Mirrors :func:`~quantplatform.research.m33.definition_for` field for field -- M33 is the only
prior milestone that ran a 1D screen, so its definition shape is the one M39 inherits rather
than a new one. No threshold is moved and no number is chosen here.

**Risk is the reference policy, as in every screen since M13.** The deployed configuration
latches its breakers forever after five consecutive losses, which M13 showed halts a trend rule
within weeks and leaves nothing to measure. The reference policy keeps every breaker and
releases the latch, so what is measured is the rule under Risk V2 rather than the latch. The
bias this introduces is one-directional and worth stating: latching can only ever *remove*
exposure, so its absence makes these runs show **more** return and **more** drawdown than a
deployed configuration would. A candidate that fails here would not be rescued by latching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quantplatform.core.enums import MarketType
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.latch_policy import risk_configuration_for
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m16 import DATA_END, symbol_rules_for
from quantplatform.research.m22 import _base, asset_for
from quantplatform.research.m39 import TIMEFRAME
from quantplatform.strategies.research import build_research_registry

if TYPE_CHECKING:
    from quantplatform.research.m39 import Variant

__all__ = ["definition_for"]


def definition_for(raw: str, variant: Variant) -> ExperimentDefinition:
    """Return the definition for one declared variant on one market at 1D.

    Args:
        raw: Market symbol as the dataset names it, e.g. ``"BTCUSDT"``.
        variant: Which of the six declared configurations to run.

    Returns:
        A definition, round-tripped through canonical JSON so it carries no computed field.
    """
    asset, base = asset_for(raw), _base()
    version = build_research_registry().metadata_for(variant.strategy_id).version
    risk = risk_configuration_for(REFERENCE, risk_for_timeframe(base.risk, TIMEFRAME))
    dataset = base.dataset.model_copy(
        update={
            "symbol": asset.symbol,
            "symbol_rules": symbol_rules_for(asset),
            "market_type": MarketType.SPOT,
            "timeframe": TIMEFRAME,
            "start": asset.start,
            "end": DATA_END,
            "source": "m30_daily",
        }
    )
    copy = base.model_copy(
        update={
            "name": (f"m39-{raw}-{TIMEFRAME.value}-{variant.key}-{variant.strategy_id}-risk_v2"),
            "strategy": StrategySpec(
                strategy_id=variant.strategy_id,
                strategy_version=version,
                params=variant.params,
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": TIMEFRAME}),
            "risk": risk,
            "role": ExperimentRole.IN_SAMPLE,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))
