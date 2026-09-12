"""Research-only strategies for the M13 discovery sprint.

**None of these can reach a paper session.** Paper trading resolves strategies through
:func:`~quantplatform.strategies.registry.build_default_registry`, whose contents an
architecture test pins line for line. These are registered only in
:func:`build_research_registry`, which the research harness is handed explicitly. Promoting
one to paper is a separate decision with its own review, never a side effect of having been
tested.

**No claim of edge is made here, and no parameter was chosen by looking at data.** The
canonical values live in :mod:`quantplatform.research.sprint`, fixed before the first run;
this module only defines what each rule does with the numbers it is given.

**The contract follows the parameters.** The existing strategies declare their features on
the class, so constructing one with anything but its default is refused — which is why M10c
could not run a sensitivity sweep at all. The engine reads ``strategy.metadata`` per
instance, so these derive ``required_features`` and ``required_history`` from their own
parameters at construction. A neighbour is then just a different set of numbers, built
through the same registry and checked by the same engine contract.

Every rule is long-only and stateless, and treats a missing feature as silence: a pipeline
omits a value its window cannot yet support, and warm-up is not something to trade through.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, ClassVar, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.registry import StrategyRegistry, build_default_registry

__all__ = [
    "RESEARCH_STRATEGIES",
    "BollingerReversionStrategy",
    "EmaSlopeStrategy",
    "MomentumStrategy",
    "RegimeReversionStrategy",
    "RegimeSwitchStrategy",
    "RegimeTrendStrategy",
    "ResearchStrategy",
    "RsiReversalStrategy",
    "TrendFilteredBreakoutStrategy",
    "VolFilteredMomentumStrategy",
    "VolScaledMomentumStrategy",
    "ZScoreReversionStrategy",
    "build_research_registry",
    "warm_up",
]

_CONFIDENCE: Final[Decimal] = Decimal("0.6")
"""Fixed, as in every other strategy here: each rule is a binary condition, and a number
derived from how far past its threshold a value sits would be an invented probability."""

_VERSION: Final[str] = "0.1.0"
_FEATURE_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?P<kind>[a-z_]+?)_(?P<first>[0-9]+)(?:_(?P<second>[0-9]+))?$"
)
_SAME_WINDOW: Final[frozenset[str]] = frozenset({"sma", "stdev", "zscore"})
_ONE_BAR_MORE: Final[frozenset[str]] = frozenset(
    {"roc", "zscore_prev", "rvol", "rsi", "er", "donchian_high", "donchian_low"}
)
_EMA_SEED_MULTIPLE: Final[int] = 5

_FROZEN: Final[ConfigDict] = ConfigDict(frozen=True, extra="forbid", validate_default=True)
_Window = Annotated[int, Field(ge=2, le=1000)]
_Ratio = Annotated[Decimal, Field(ge=0, le=1)]


def warm_up(name: str) -> int:
    """Return how many bars a feature needs, ending at the bar being decided on.

    This package may not import the features package, so it states warm-up itself. A test
    holds this answer to the pipeline's own for every configuration the sprint runs.

    Raises:
        ValueError: If the name is not one any research strategy declares.
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


def _metadata(
    strategy_id: str,
    name: str,
    description: str,
    schema: type[BaseModel],
    features: tuple[str, ...],
) -> StrategyMetadata:
    """Return class-level metadata describing the canonical configuration."""
    return StrategyMetadata(
        strategy_id=strategy_id,
        version=_VERSION,
        name=name,
        description=description,
        required_history=max(warm_up(feature) for feature in features),
        required_features=features,
        supported_timeframes=(Timeframe.H1,),
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=schema,
        operates_intrabar=False,
        allows_short=False,
    )


class ResearchStrategy(BaseStrategy):
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
                confidence=_CONFIDENCE,
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


# --- Trend / momentum -------------------------------------------------------------------------


class MomentumParameters(BaseModel):
    """Parameters for :class:`MomentumStrategy`."""

    model_config = _FROZEN
    lookback: _Window


class MomentumStrategy(ResearchStrategy):
    """Long while the close is above its value N bars ago; flat while below."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "momentum_roc",
        "N-bar momentum",
        "Long-only time-series momentum: hold while the N-bar return is positive.",
        MomentumParameters,
        ("roc_72",),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the return feature for the configured lookback."""
        return (f"roc_{self._typed(MomentumParameters).lookback}",)

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter on a positive N-bar return, exit on a negative one."""
        (name,) = self._names
        values = self._read(context, name)
        if values is None:
            return ()
        (roc,) = values
        if context.position_state is PositionState.FLAT and roc > 0:
            return self._enter(context, f"{name} {roc} is positive", {name: roc})
        if context.position_state is PositionState.LONG and roc < 0:
            return self._exit(context, f"{name} {roc} turned negative", {name: roc})
        return ()


class EmaSlopeParameters(BaseModel):
    """Parameters for :class:`EmaSlopeStrategy`."""

    model_config = _FROZEN
    period: Annotated[int, Field(ge=2, le=200)]
    slope_bars: Annotated[int, Field(ge=1, le=100)]


class EmaSlopeStrategy(ResearchStrategy):
    """Long while an EMA is rising and price is above it."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "ema_slope",
        "EMA slope",
        "Long-only trend filter: enter when the EMA is rising and price sits above it, exit "
        "when the EMA turns down.",
        EmaSlopeParameters,
        ("emab_50", "emaslope_50_10"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the bounded EMA and its slope for the configured period."""
        p = self._typed(EmaSlopeParameters)
        return (f"emab_{p.period}", f"emaslope_{p.period}_{p.slope_bars}")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter on a rising EMA with price above it, exit when the EMA falls."""
        ema_name, slope_name = self._names
        values = self._read(context, ema_name, slope_name)
        if values is None:
            return ()
        ema, slope = values
        close = context.latest_bar.close
        seen = {ema_name: ema, slope_name: slope}
        if context.position_state is PositionState.FLAT and slope > 0 and close > ema:
            return self._enter(context, f"EMA rising ({slope}) with close {close} above it", seen)
        if context.position_state is PositionState.LONG and slope < 0:
            return self._exit(context, f"EMA turned down ({slope})", seen)
        return ()


class TrendFilteredBreakoutParameters(BaseModel):
    """Parameters for :class:`TrendFilteredBreakoutStrategy`."""

    model_config = _FROZEN
    entry_lookback: Annotated[int, Field(ge=2, le=500)]
    exit_lookback: Annotated[int, Field(ge=2, le=500)]
    trend_period: _Window


class TrendFilteredBreakoutStrategy(ResearchStrategy):
    """The Donchian breakout, taken only above a long simple average."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "breakout_trend",
        "Breakout with trend filter",
        "Long-only Donchian breakout that only enters while the close is above a long simple "
        "moving average; the exit is the plain breakdown and ignores the filter.",
        TrendFilteredBreakoutParameters,
        ("donchian_high_20", "donchian_low_10", "sma_200"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the two channel levels and the trend average."""
        p = self._typed(TrendFilteredBreakoutParameters)
        return (
            f"donchian_high_{p.entry_lookback}",
            f"donchian_low_{p.exit_lookback}",
            f"sma_{p.trend_period}",
        )

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter on a new high above the trend average; exit on a new low regardless."""
        high_name, low_name, trend_name = self._names
        bar = context.latest_bar
        if context.position_state is PositionState.FLAT:
            values = self._read(context, high_name, trend_name)
            if values is None:
                return ()
            level, trend = values
            if bar.high > level and bar.close > trend:
                return self._enter(
                    context,
                    f"high {bar.high} broke {level} with close above {trend_name} {trend}",
                    {high_name: level, trend_name: trend},
                )
            return ()
        if context.position_state is PositionState.LONG:
            values = self._read(context, low_name)
            if values is None:
                return ()
            (floor,) = values
            if bar.low < floor:
                return self._exit(context, f"low {bar.low} broke {floor}", {low_name: floor})
        return ()


class VolScaledMomentumParameters(BaseModel):
    """Parameters for :class:`VolScaledMomentumStrategy`."""

    model_config = _FROZEN
    lookback: _Window
    vol_window: _Window
    threshold: Annotated[Decimal, Field(gt=0, le=10)]


class VolScaledMomentumStrategy(ResearchStrategy):
    """Momentum that must clear its own noise before it counts."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "vol_momentum",
        "Volatility-scaled momentum",
        "Long-only momentum entered only when the N-bar return exceeds a multiple of the move "
        "realised volatility alone would produce over N bars; exits when the return turns "
        "negative.",
        VolScaledMomentumParameters,
        ("roc_72", "rvol_72"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the return and realised-volatility features."""
        p = self._typed(VolScaledMomentumParameters)
        return (f"roc_{p.lookback}", f"rvol_{p.vol_window}")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter when the move clears threshold x sigma x sqrt(N); exit when it reverses."""
        p = self._typed(VolScaledMomentumParameters)
        roc_name, vol_name = self._names
        if context.position_state is PositionState.FLAT:
            values = self._read(context, roc_name, vol_name)
            if values is None:
                return ()
            roc, vol = values
            hurdle = p.threshold * vol * Decimal(p.lookback).sqrt()
            if roc > hurdle:
                return self._enter(
                    context, f"{roc_name} {roc} cleared {hurdle}", {roc_name: roc, vol_name: vol}
                )
            return ()
        if context.position_state is PositionState.LONG:
            values = self._read(context, roc_name)
            if values is not None and values[0] < 0:
                return self._exit(
                    context, f"{roc_name} {values[0]} turned negative", {roc_name: values[0]}
                )
        return ()


# --- Mean reversion ---------------------------------------------------------------------------


class ZScoreReversionParameters(BaseModel):
    """Parameters for :class:`ZScoreReversionStrategy`."""

    model_config = _FROZEN
    window: Annotated[int, Field(ge=3, le=1000)]
    entry_z: Annotated[Decimal, Field(lt=0)]
    exit_z: Decimal

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.entry_z >= self.exit_z:
            msg = "entry_z must sit below exit_z, or the rule would exit before it entered"
            raise ValueError(msg)
        return self


class ZScoreReversionStrategy(ResearchStrategy):
    """Buys a close stretched below its mean; sells it back at the mean."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "zscore_revert",
        "Z-score reversion",
        "Long-only mean reversion: enter when the close is more than |entry_z| standard "
        "deviations below its rolling mean, exit when it returns to exit_z.",
        ZScoreReversionParameters,
        ("zscore_48",),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the z-score for the configured window."""
        return (f"zscore_{self._typed(ZScoreReversionParameters).window}",)

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter below entry_z, exit at or above exit_z."""
        p = self._typed(ZScoreReversionParameters)
        (name,) = self._names
        values = self._read(context, name)
        if values is None:
            return ()
        (z,) = values
        if context.position_state is PositionState.FLAT and z < p.entry_z:
            return self._enter(context, f"{name} {z} below {p.entry_z}", {name: z})
        if context.position_state is PositionState.LONG and z >= p.exit_z:
            return self._exit(context, f"{name} {z} back to {p.exit_z}", {name: z})
        return ()


class BollingerReversionParameters(BaseModel):
    """Parameters for :class:`BollingerReversionStrategy`."""

    model_config = _FROZEN
    window: Annotated[int, Field(ge=3, le=1000)]
    band_z: Annotated[Decimal, Field(gt=0, le=10)]


class BollingerReversionStrategy(ResearchStrategy):
    """Buys the close back inside the lower band, not the first touch below it."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "bollinger_revert",
        "Bollinger reversal",
        "Long-only: enter when the previous close was below the lower band and the current "
        "close is back inside it; exit at the middle band.",
        BollingerReversionParameters,
        ("zscore_20", "zscore_prev_20"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the current and previous z-scores for the band window."""
        window = self._typed(BollingerReversionParameters).window
        return (f"zscore_{window}", f"zscore_prev_{window}")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter on the close back inside the band, exit at the middle."""
        p = self._typed(BollingerReversionParameters)
        now_name, prev_name = self._names
        values = self._read(context, now_name, prev_name)
        if values is None:
            return ()
        now, prev = values
        seen = {now_name: now, prev_name: prev}
        lower = -p.band_z
        if context.position_state is PositionState.FLAT and prev < lower <= now:
            return self._enter(context, f"close back inside the band ({prev} -> {now})", seen)
        if context.position_state is PositionState.LONG and now >= 0:
            return self._exit(context, f"close reached the middle band ({now})", seen)
        return ()


class RsiReversalParameters(BaseModel):
    """Parameters for :class:`RsiReversalStrategy`."""

    model_config = _FROZEN
    period: Annotated[int, Field(ge=2, le=500)]
    oversold: Annotated[Decimal, Field(gt=0, lt=100)]
    exit_level: Annotated[Decimal, Field(gt=0, lt=100)]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.oversold >= self.exit_level:
            msg = "oversold must sit below exit_level"
            raise ValueError(msg)
        return self


class RsiReversalStrategy(ResearchStrategy):
    """Buys an oversold RSI, sells it back at the exit level."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "rsi_reversal",
        "RSI reversal",
        "Long-only: enter when the simple-average RSI is below oversold, exit above exit_level.",
        RsiReversalParameters,
        ("rsi_14",),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the RSI for the configured period."""
        return (f"rsi_{self._typed(RsiReversalParameters).period}",)

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter below oversold, exit above exit_level."""
        p = self._typed(RsiReversalParameters)
        (name,) = self._names
        values = self._read(context, name)
        if values is None:
            return ()
        (rsi,) = values
        if context.position_state is PositionState.FLAT and rsi < p.oversold:
            return self._enter(context, f"{name} {rsi} below {p.oversold}", {name: rsi})
        if context.position_state is PositionState.LONG and rsi > p.exit_level:
            return self._exit(context, f"{name} {rsi} above {p.exit_level}", {name: rsi})
        return ()


# --- Regime / volatility ------------------------------------------------------------------------


class RegimeTrendParameters(BaseModel):
    """Parameters for :class:`RegimeTrendStrategy`."""

    model_config = _FROZEN
    lookback: _Window
    er_window: _Window
    er_min: _Ratio


class RegimeTrendStrategy(ResearchStrategy):
    """Momentum, entered only while the market is moving efficiently."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "regime_trend",
        "Momentum in a trending regime",
        "Long-only momentum that only enters while the efficiency ratio says the market is "
        "trending; the exit does not wait for the regime.",
        RegimeTrendParameters,
        ("roc_72", "er_72"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the return and efficiency-ratio features."""
        p = self._typed(RegimeTrendParameters)
        return (f"roc_{p.lookback}", f"er_{p.er_window}")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter on positive momentum in a trend, exit on negative momentum."""
        p = self._typed(RegimeTrendParameters)
        roc_name, er_name = self._names
        if context.position_state is PositionState.FLAT:
            values = self._read(context, roc_name, er_name)
            if values is None:
                return ()
            roc, er = values
            if roc > 0 and er >= p.er_min:
                return self._enter(
                    context, f"{roc_name} {roc} in a trend (ER {er})", {roc_name: roc, er_name: er}
                )
            return ()
        if context.position_state is PositionState.LONG:
            values = self._read(context, roc_name)
            if values is not None and values[0] < 0:
                return self._exit(
                    context, f"{roc_name} {values[0]} turned negative", {roc_name: values[0]}
                )
        return ()


class RegimeReversionParameters(BaseModel):
    """Parameters for :class:`RegimeReversionStrategy`."""

    model_config = _FROZEN
    window: Annotated[int, Field(ge=3, le=1000)]
    entry_z: Annotated[Decimal, Field(lt=0)]
    exit_z: Decimal
    er_window: _Window
    er_max: _Ratio

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.entry_z >= self.exit_z:
            msg = "entry_z must sit below exit_z"
            raise ValueError(msg)
        return self


class RegimeReversionStrategy(ResearchStrategy):
    """Z-score reversion, entered only while the market is ranging."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "regime_revert",
        "Reversion in a ranging regime",
        "Long-only z-score reversion that only enters while the efficiency ratio says the "
        "market is ranging; the exit does not wait for the regime.",
        RegimeReversionParameters,
        ("zscore_48", "er_72"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the z-score and efficiency-ratio features."""
        p = self._typed(RegimeReversionParameters)
        return (f"zscore_{p.window}", f"er_{p.er_window}")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter a stretch in a range, exit at the mean."""
        p = self._typed(RegimeReversionParameters)
        z_name, er_name = self._names
        if context.position_state is PositionState.FLAT:
            values = self._read(context, z_name, er_name)
            if values is None:
                return ()
            z, er = values
            if er <= p.er_max and z < p.entry_z:
                return self._enter(
                    context, f"{z_name} {z} in a range (ER {er})", {z_name: z, er_name: er}
                )
            return ()
        if context.position_state is PositionState.LONG:
            values = self._read(context, z_name)
            if values is not None and values[0] >= p.exit_z:
                return self._exit(
                    context, f"{z_name} {values[0]} back to {p.exit_z}", {z_name: values[0]}
                )
        return ()


class RegimeSwitchParameters(BaseModel):
    """Parameters for :class:`RegimeSwitchStrategy`."""

    model_config = _FROZEN
    lookback: _Window
    window: Annotated[int, Field(ge=3, le=1000)]
    entry_z: Annotated[Decimal, Field(lt=0)]
    exit_z: Decimal
    er_window: _Window
    er_trend: _Ratio
    er_range: _Ratio

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.entry_z >= self.exit_z:
            msg = "entry_z must sit below exit_z"
            raise ValueError(msg)
        if self.er_range >= self.er_trend:
            msg = "er_range must sit below er_trend, or a bar could be both regimes at once"
            raise ValueError(msg)
        return self


class RegimeSwitchStrategy(ResearchStrategy):
    """Momentum in a trend, reversion in a range, silence in between.

    **Stateless, so the exit follows the current regime rather than the entry's.** A strategy
    here cannot remember which rule opened its position. While the market is ranging the
    reversion exit applies; otherwise the momentum exit does. A reversion entry whose market
    starts trending therefore switches to the momentum exit — stated, not hidden.
    """

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "regime_switch",
        "Regime switch",
        "Long-only: momentum while the efficiency ratio says trending, z-score reversion while "
        "it says ranging, nothing in between.",
        RegimeSwitchParameters,
        ("roc_72", "zscore_48", "er_72"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the return, z-score and efficiency-ratio features."""
        p = self._typed(RegimeSwitchParameters)
        return (f"roc_{p.lookback}", f"zscore_{p.window}", f"er_{p.er_window}")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Apply whichever rule the current regime calls for."""
        roc_name, z_name, er_name = self._names
        values = self._read(context, roc_name, z_name, er_name)
        if values is None:
            return ()
        roc, z, er = values
        decision = self._decide(context.position_state, roc=roc, z=z, er=er)
        if decision is None:
            return ()
        action, reason = decision
        return self._signal(context, action, reason, {roc_name: roc, z_name: z, er_name: er})

    def _decide(
        self, position: PositionState, *, roc: Decimal, z: Decimal, er: Decimal
    ) -> tuple[SignalAction, str] | None:
        """Return the action the current regime's rule calls for, if any."""
        p = self._typed(RegimeSwitchParameters)
        if position is PositionState.FLAT:
            if er >= p.er_trend and roc > 0:
                return SignalAction.ENTER_LONG, f"trend regime (ER {er}), momentum {roc}"
            if er <= p.er_range and z < p.entry_z:
                return SignalAction.ENTER_LONG, f"range regime (ER {er}), z-score {z}"
            return None
        ranging = er <= p.er_range
        if position is PositionState.LONG and (
            (ranging and z >= p.exit_z) or (not ranging and roc < 0)
        ):
            reason = (
                f"range regime, z-score {z} at the mean"
                if ranging
                else f"momentum {roc} turned negative"
            )
            return SignalAction.EXIT_LONG, reason
        return None


class VolFilteredMomentumParameters(BaseModel):
    """Parameters for :class:`VolFilteredMomentumStrategy`."""

    model_config = _FROZEN
    lookback: _Window
    short_vol: _Window
    long_vol: _Window
    max_ratio: Annotated[Decimal, Field(gt=0, le=10)]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.short_vol >= self.long_vol:
            msg = "short_vol must be shorter than long_vol"
            raise ValueError(msg)
        return self


class VolFilteredMomentumStrategy(ResearchStrategy):
    """Momentum that declines to enter into a volatility spike."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "vol_filtered_momentum",
        "Volatility-filtered momentum",
        "Long-only momentum that only enters while short-window realised volatility is at most "
        "max_ratio times the long-window one; the exit ignores the filter.",
        VolFilteredMomentumParameters,
        ("roc_72", "volratio_24_168"),
    )

    def feature_names(self) -> tuple[str, ...]:
        """Return the return and volatility-ratio features."""
        p = self._typed(VolFilteredMomentumParameters)
        return (f"roc_{p.lookback}", f"volratio_{p.short_vol}_{p.long_vol}")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Enter positive momentum in calm conditions, exit on negative momentum."""
        p = self._typed(VolFilteredMomentumParameters)
        roc_name, ratio_name = self._names
        if context.position_state is PositionState.FLAT:
            values = self._read(context, roc_name, ratio_name)
            if values is None:
                return ()
            roc, ratio = values
            if roc > 0 and ratio <= p.max_ratio:
                return self._enter(
                    context,
                    f"{roc_name} {roc} with volatility ratio {ratio}",
                    {roc_name: roc, ratio_name: ratio},
                )
            return ()
        if context.position_state is PositionState.LONG:
            values = self._read(context, roc_name)
            if values is not None and values[0] < 0:
                return self._exit(
                    context, f"{roc_name} {values[0]} turned negative", {roc_name: values[0]}
                )
        return ()


RESEARCH_STRATEGIES: Final[tuple[type[ResearchStrategy], ...]] = (
    MomentumStrategy,
    EmaSlopeStrategy,
    TrendFilteredBreakoutStrategy,
    VolScaledMomentumStrategy,
    ZScoreReversionStrategy,
    BollingerReversionStrategy,
    RsiReversalStrategy,
    RegimeTrendStrategy,
    RegimeReversionStrategy,
    RegimeSwitchStrategy,
    VolFilteredMomentumStrategy,
)
"""Every M13 research strategy. Deliberately not :data:`BUILTIN_STRATEGIES`."""


def build_research_registry() -> StrategyRegistry:
    """Return the default registry plus every research strategy.

    The benchmarks come from the default registry unchanged, so the research harness compares
    against exactly the classes paper trading runs.
    """
    registry = build_default_registry()
    for strategy_class in RESEARCH_STRATEGIES:
        registry.register(strategy_class)
    return registry
