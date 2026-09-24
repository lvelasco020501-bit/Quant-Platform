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

from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, ClassVar, Final, Self

from pydantic import BaseModel, Field, model_validator

from quantplatform.core.enums import PositionState, SignalAction
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.breakout import BreakoutStrategy
from quantplatform.strategies.ema_trend import EmaTrendStrategy
from quantplatform.strategies.parametric import (
    FROZEN,
    STUDIED_TIMEFRAMES,
    ParametricStrategy,
    Ratio,
    Window,
    metadata_for,
)
from quantplatform.strategies.registry import StrategyRegistry, build_default_registry

__all__ = [
    "MULTI_TIMEFRAME_BENCHMARKS",
    "RESEARCH_STRATEGIES",
    "BollingerReversionStrategy",
    "BreakoutMultiTimeframe",
    "EmaSlopeStrategy",
    "EmaTrendMultiTimeframe",
    "MomentumStrategy",
    "ParametricStrategy",
    "RegimeReversionStrategy",
    "RegimeSwitchStrategy",
    "RegimeTrendStrategy",
    "RsiReversalStrategy",
    "VolFilteredMomentumStrategy",
    "VolScaledMomentumStrategy",
    "ZScoreReversionStrategy",
    "build_research_registry",
]

# --- Trend / momentum -------------------------------------------------------------------------


class MomentumParameters(BaseModel):
    """Parameters for :class:`MomentumStrategy`."""

    model_config = FROZEN
    lookback: Window


class MomentumStrategy(ParametricStrategy):
    """Long while the close is above its value N bars ago; flat while below."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    period: Annotated[int, Field(ge=2, le=200)]
    slope_bars: Annotated[int, Field(ge=1, le=100)]


class EmaSlopeStrategy(ParametricStrategy):
    """Long while an EMA is rising and price is above it."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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


class VolScaledMomentumParameters(BaseModel):
    """Parameters for :class:`VolScaledMomentumStrategy`."""

    model_config = FROZEN
    lookback: Window
    vol_window: Window
    threshold: Annotated[Decimal, Field(gt=0, le=10)]


class VolScaledMomentumStrategy(ParametricStrategy):
    """Momentum that must clear its own noise before it counts."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    window: Annotated[int, Field(ge=3, le=1000)]
    entry_z: Annotated[Decimal, Field(lt=0)]
    exit_z: Decimal

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.entry_z >= self.exit_z:
            msg = "entry_z must sit below exit_z, or the rule would exit before it entered"
            raise ValueError(msg)
        return self


class ZScoreReversionStrategy(ParametricStrategy):
    """Buys a close stretched below its mean; sells it back at the mean."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    window: Annotated[int, Field(ge=3, le=1000)]
    band_z: Annotated[Decimal, Field(gt=0, le=10)]


class BollingerReversionStrategy(ParametricStrategy):
    """Buys the close back inside the lower band, not the first touch below it."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    period: Annotated[int, Field(ge=2, le=500)]
    oversold: Annotated[Decimal, Field(gt=0, lt=100)]
    exit_level: Annotated[Decimal, Field(gt=0, lt=100)]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.oversold >= self.exit_level:
            msg = "oversold must sit below exit_level"
            raise ValueError(msg)
        return self


class RsiReversalStrategy(ParametricStrategy):
    """Buys an oversold RSI, sells it back at the exit level."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    lookback: Window
    er_window: Window
    er_min: Ratio


class RegimeTrendStrategy(ParametricStrategy):
    """Momentum, entered only while the market is moving efficiently."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    window: Annotated[int, Field(ge=3, le=1000)]
    entry_z: Annotated[Decimal, Field(lt=0)]
    exit_z: Decimal
    er_window: Window
    er_max: Ratio

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.entry_z >= self.exit_z:
            msg = "entry_z must sit below exit_z"
            raise ValueError(msg)
        return self


class RegimeReversionStrategy(ParametricStrategy):
    """Z-score reversion, entered only while the market is ranging."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    lookback: Window
    window: Annotated[int, Field(ge=3, le=1000)]
    entry_z: Annotated[Decimal, Field(lt=0)]
    exit_z: Decimal
    er_window: Window
    er_trend: Ratio
    er_range: Ratio

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.entry_z >= self.exit_z:
            msg = "entry_z must sit below exit_z"
            raise ValueError(msg)
        if self.er_range >= self.er_trend:
            msg = "er_range must sit below er_trend, or a bar could be both regimes at once"
            raise ValueError(msg)
        return self


class RegimeSwitchStrategy(ParametricStrategy):
    """Momentum in a trend, reversion in a range, silence in between.

    **Stateless, so the exit follows the current regime rather than the entry's.** A strategy
    here cannot remember which rule opened its position. While the market is ranging the
    reversion exit applies; otherwise the momentum exit does. A reversion entry whose market
    starts trending therefore switches to the momentum exit — stated, not hidden.
    """

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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

    model_config = FROZEN
    lookback: Window
    short_vol: Window
    long_vol: Window
    max_ratio: Annotated[Decimal, Field(gt=0, le=10)]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.short_vol >= self.long_vol:
            msg = "short_vol must be shorter than long_vol"
            raise ValueError(msg)
        return self


class VolFilteredMomentumStrategy(ParametricStrategy):
    """Momentum that declines to enter into a volatility spike."""

    METADATA: ClassVar[StrategyMetadata] = metadata_for(
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


# --- Benchmarks on slower timeframes -----------------------------------------------------------


def _widened(metadata: StrategyMetadata, strategy_id: str) -> StrategyMetadata:
    """Return a benchmark's contract under a research id, allowed on the studied timeframes."""
    declared = dict(metadata)
    declared["strategy_id"] = strategy_id
    declared["supported_timeframes"] = STUDIED_TIMEFRAMES
    declared["description"] = (
        f"{metadata.description} Research copy of {metadata.strategy_id}, identical rule, "
        "also allowed on 4h and 1d."
    )
    return StrategyMetadata(**declared)


class EmaTrendMultiTimeframe(EmaTrendStrategy):
    """EMA20/50 exactly as the benchmark, allowed on 4h and 1d for the timeframe study.

    A subclass rather than an edit: the production class stays hourly-only and pinned, and
    this copy carries its own id, so no result from a slower timeframe can ever be filed
    under the frozen benchmark's name.
    """

    METADATA: ClassVar[StrategyMetadata] = _widened(EmaTrendStrategy.METADATA, "ema_trend_mtf")


class BreakoutMultiTimeframe(BreakoutStrategy):
    """Donchian 20/10 exactly as the paper strategy, allowed on 4h and 1d. See above."""

    METADATA: ClassVar[StrategyMetadata] = _widened(BreakoutStrategy.METADATA, "breakout_mtf")


MULTI_TIMEFRAME_BENCHMARKS: Final[tuple[type[BaseStrategy], ...]] = (
    EmaTrendMultiTimeframe,
    BreakoutMultiTimeframe,
)
"""Research copies of the benchmarks. Registered for research only, never for paper."""


RESEARCH_STRATEGIES: Final[tuple[type[ParametricStrategy], ...]] = (
    MomentumStrategy,
    EmaSlopeStrategy,
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
    for strategy_class in (*RESEARCH_STRATEGIES, *MULTI_TIMEFRAME_BENCHMARKS):
        registry.register(strategy_class)
    return registry
