"""Building experiment definitions for markets M16 never catalogued.

Plumbing, not policy. It moves no threshold, chooses no number and changes no strategy; it exists
because M29's builder reaches for :func:`~quantplatform.research.m22.asset_for`, which knows only
the six markets M16 catalogued, and M36 phase 2 needs all twenty-seven that the corrected
universe can fund. Pointing the certified engine at the other twenty-one is what the first
attempt at phase 2 crashed on: ``StopIteration`` out of ``asset_for``, after twenty-two minutes
of a backtest that was never going to be joined by its sibling.

**The property that makes this safe is testable rather than asserted.** For the six markets M16
does catalogue, the definition built here must match M29's own in every field the engine reads:
strategy and parameters, dataset symbol and venue rules and window, backtest configuration, and
risk configuration. Only the experiment's name differs, because these are different experiments
and a name changes no behaviour. A test holds that equality, so a future edit here cannot quietly
make phase 2 incomparable with the milestones it is validating.

Venue rules come from M16's captured fixtures where they exist and from M36's own capture
otherwise. M16's are **not** re-read from a new source: three milestones of results rest on them.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import SymbolRules
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.latch_policy import risk_configuration_for
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m16 import ASSETS, DATA_END, symbol_rules_for
from quantplatform.research.m22 import _base
from quantplatform.strategies.research import build_research_registry

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar
    from quantplatform.research.m29 import Probe

__all__ = ["MARKET_RULES_DIR", "MarketRef", "definition_for", "market_ref", "pool_rules"]

_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
MARKET_RULES_DIR: Final[Path] = _ROOT / "data/raw/m36/rules"
"""Where ``scripts/m36_venue_rules.py`` wrote the rules M16 never captured."""


class MarketRef:
    """Everything a definition needs about one market, however it was catalogued."""

    __slots__ = ("raw", "rules", "start", "symbol")

    def __init__(self, raw: str, symbol: str, start: datetime, rules: SymbolRules) -> None:
        self.raw = raw
        self.symbol = symbol
        self.start = start
        self.rules = rules


def pool_rules(raw: str) -> SymbolRules:
    """Return the venue rules M36 captured for a market M16 did not."""
    path = MARKET_RULES_DIR / f"{raw}.json"
    if not path.exists():
        msg = (
            f"no venue rules for {raw}. The certified engine needs a market's tick, step and "
            f"minimum notional to decide whether an order is rejected, and order rejection is "
            f"one of the Risk V2 layers phase 2 claims to exercise -- so a placeholder here "
            f"would make that claim false. Run scripts/m36_venue_rules.py."
        )
        raise FileNotFoundError(msg)
    return SymbolRules.model_validate(json.loads(path.read_text(encoding="utf-8")))


def market_ref(raw: str, bars: tuple[MarketBar, ...]) -> MarketRef:
    """Resolve one market, preferring M16's catalogue where it has an entry.

    The start is M16's declared first complete month for its own six, and the first bar actually
    on disk for the rest. Those are the same kind of fact arrived at two ways, and the difference
    is recorded here rather than papered over: M16's is a curated boundary, M36's is wherever the
    archive begins.
    """
    catalogued = next((asset for asset in ASSETS if asset.raw == raw), None)
    if catalogued is not None:
        return MarketRef(raw, catalogued.symbol, catalogued.start, symbol_rules_for(catalogued))
    if not bars:
        msg = f"cannot resolve {raw}: no bars to take a start from"
        raise ValueError(msg)
    return MarketRef(raw, bars[0].symbol, bars[0].open_time, pool_rules(raw))


def definition_for(
    ref: MarketRef, probe: Probe, timeframe: Timeframe, *, latching: bool
) -> ExperimentDefinition:
    """Return the experiment definition for one frozen rule on one market.

    Mirrors :func:`~quantplatform.research.m29.definition_for` field for field. Risk is
    :func:`~quantplatform.research.m15.risk_for_timeframe` applied to the deployed Risk V2
    configuration, which is the conversion every run since M15 has used.

    Args:
        ref: The market, its venue rules and its window.
        probe: Which frozen rule to run.
        timeframe: The bar interval, which also selects the risk conversion.
        latching: ``False`` for the reference policy every prior screen used, which keeps the
            breakers and releases the latch; ``True`` for deployed Risk V2.

    Returns:
        A definition, round-tripped through canonical JSON so it carries no computed field.
    """
    base = _base()
    version = build_research_registry().metadata_for(probe.candidate.strategy_id).version
    at_timeframe = risk_for_timeframe(base.risk, timeframe)
    risk = at_timeframe if latching else risk_configuration_for(REFERENCE, at_timeframe)
    dataset = base.dataset.model_copy(
        update={
            "symbol": ref.symbol,
            "symbol_rules": ref.rules,
            "market_type": MarketType.SPOT,
            "timeframe": timeframe,
            "start": ref.start,
            "end": DATA_END,
            "source": "binance_vision_m16" if timeframe is not Timeframe.D1 else "m29_daily",
        }
    )
    suffix = "deployed" if latching else "ref"
    copy = base.model_copy(
        update={
            "name": (
                f"m36-{ref.raw}-{timeframe.value}-{probe.key}-"
                f"{probe.candidate.strategy_id}-{suffix}"
            ),
            "strategy": StrategySpec(
                strategy_id=probe.candidate.strategy_id,
                strategy_version=version,
                params=probe.candidate.params,
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": timeframe}),
            "risk": risk,
            "role": ExperimentRole.IN_SAMPLE,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))
