"""Strategies whose declared contract is derived from their parameters.

The platform's first two strategies declare their features on the class, which is why M10c
could not sweep one: constructing an ``ema_trend`` with anything but its default was refused
by its own contract. The base class here reads ``feature_names()`` per instance instead, so
a different set of numbers is a different contract rather than a different class.

**This module is production infrastructure, not research.** It was extracted from
``strategies/research.py`` when ``breakout_trend`` was promoted to paper: a promoted strategy
must live in a module the registry can import, and the registry cannot import research —
``research`` imports ``registry``, so the arrow only goes one way. Nothing in the extraction
changed behaviour; the class, the warm-up arithmetic and the metadata derivation are the same
objects the research harness has been using since M13.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from quantplatform.core.enums import MarketType, SignalAction, Timeframe
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.strategies.base import BaseStrategy

__all__ = [
    "FROZEN",
    "ParametricStrategy",
    "Ratio",
    "Window",
    "metadata_for",
    "warm_up",
]

_FEATURE_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?P<kind>[a-z_]+?)_(?P<first>[0-9]+)(?:_(?P<second>[0-9]+))?$"
)
_SAME_WINDOW: Final[frozenset[str]] = frozenset({"sma", "stdev", "zscore"})
_ONE_BAR_MORE: Final[frozenset[str]] = frozenset(
    {"roc", "zscore_prev", "rvol", "rsi", "er", "donchian_high", "donchian_low"}
)
_EMA_SEED_MULTIPLE: Final[int] = 5

CONFIDENCE: Final[Decimal] = Decimal("0.6")
"""Fixed, as in every other strategy here: each rule is a binary condition, and a number
derived from how far past its threshold a value sits would be an invented probability."""

VERSION: Final[str] = "0.1.0"

STUDIED_TIMEFRAMES: Final[tuple[Timeframe, ...]] = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
"""These rules were measured on the slower timeframes M14 studied, and declare all three."""

FROZEN: Final[ConfigDict] = ConfigDict(frozen=True, extra="forbid", validate_default=True)
Window = Annotated[int, Field(ge=2, le=1000)]
Ratio = Annotated[Decimal, Field(ge=0, le=1)]


def warm_up(name: str) -> int:
    """Return how many bars a feature needs, ending at the bar being decided on.

    This package may not import the features package, so it states warm-up itself. A test
    holds this answer to the pipeline's own for every configuration that runs.

    Raises:
        ValueError: If the name is not one any parametric strategy declares.
    """
    match = _FEATURE_NAME.match(name)
    if match is None:
        msg = f"no warm-up rule for feature {name!r}"
        raise ValueError(msg)
    kind = match.group("kind")
    first = int(match.group("first"))
    second = int(match.group("second") or 0)
    if kind in _SAME_WINDOW:
        return first
    if kind in _ONE_BAR_MORE:
        return first + 1
    if kind == "emab":
        return _EMA_SEED_MULTIPLE * first
    if kind == "emaslope":
        return _EMA_SEED_MULTIPLE * first + second
    if kind == "volratio":
        return second + 1
    msg = f"no warm-up rule for feature {name!r}"
    raise ValueError(msg)


def metadata_for(
    strategy_id: str,
    name: str,
    description: str,
    schema: type[BaseModel],
    features: tuple[str, ...],
) -> StrategyMetadata:
    """Return class-level metadata describing the canonical configuration."""
    return StrategyMetadata(
        strategy_id=strategy_id,
        version=VERSION,
        name=name,
        description=description,
        required_history=max(warm_up(feature) for feature in features),
        required_features=features,
        supported_timeframes=STUDIED_TIMEFRAMES,
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=schema,
        operates_intrabar=False,
        allows_short=False,
    )


class ParametricStrategy(BaseStrategy):
    """A strategy whose declared contract is derived from its parameters."""

    def __init__(self, parameters: BaseModel) -> None:
        """Validate the parameters, then declare exactly the features they call for."""
        super().__init__(parameters)
        self._names = self.feature_names()
        declared = dict(type(self).METADATA)
        declared["required_features"] = self._names
        declared["required_history"] = max(warm_up(name) for name in self._names)
        self._metadata = StrategyMetadata(**declared)

    @property
    def metadata(self) -> StrategyMetadata:
        """Return this instance's contract, not the class's canonical one."""
        return self._metadata

    @abstractmethod
    def feature_names(self) -> tuple[str, ...]:
        """Return the features this instance's parameters call for."""

    def _typed[P: BaseModel](self, schema: type[P]) -> P:
        parameters = self.parameters
        if not isinstance(parameters, schema):  # pragma: no cover - the base class checked
            msg = f"{type(self).__name__} requires {schema.__name__}"
            raise TypeError(msg)
        return parameters

    @staticmethod
    def _read(context: StrategyContext, *names: str) -> tuple[Decimal, ...] | None:
        """Return every named feature, or ``None`` if any is missing."""
        values: list[Decimal] = []
        for name in names:
            value = context.features.get(name)
            if value is None:
                return None
            values.append(value)
        return tuple(values)

    def _signal(
        self,
        context: StrategyContext,
        action: SignalAction,
        reason: str,
        features: Mapping[str, Decimal],
    ) -> tuple[Signal, ...]:
        return (
            self.build_signal(
                context=context,
                action=action,
                confidence=CONFIDENCE,
                reason=reason,
                features=features,
            ),
        )

    def _enter(
        self, context: StrategyContext, reason: str, features: Mapping[str, Decimal]
    ) -> tuple[Signal, ...]:
        return self._signal(context, SignalAction.ENTER_LONG, reason, features)

    def _exit(
        self, context: StrategyContext, reason: str, features: Mapping[str, Decimal]
    ) -> tuple[Signal, ...]:
        return self._signal(context, SignalAction.EXIT_LONG, reason, features)
