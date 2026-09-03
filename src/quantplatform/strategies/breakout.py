"""A Donchian breakout strategy: momentum, not trend-following.

**What this is for.** EMA20/50 covers trend-following; breakout is a structurally different
signal — it reacts to price making a new extreme rather than to two averages crossing — and it
is simple enough to audit by hand, which is the whole point of trying it before anything more
elaborate. It is deliberately the same shape of strategy as
:mod:`quantplatform.strategies.ema_trend`: long-only, one position, no sizing, no stops of its
own. All of that lives in Risk.

**No claim is made about profitability.** ``entry_lookback=20`` and ``exit_lookback=10`` are
an operational default, chosen to make the class constructible and nothing else — the same
status ``ema_trend``'s 20/50 had before a single bar of research ran against it. Nothing here
has been backtested, and nothing here should be read as though it had.

**Entry breaks its own level, never the one it is compared against.** The level a bar is
tested against is built from :class:`~quantplatform.features.DonchianChannelFeatures`, which
excludes that bar from its own window unconditionally — see that class's docstring for why.
This module never reads raw bars to build a level; it only ever reads the feature already
computed that way, plus the current bar's own already-closed high/low, which is not
look-ahead because the bar has already closed by the time a decision is made about it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.errors import StrategyParameterError
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.strategies.base import BaseStrategy

__all__ = ["BreakoutParameters", "BreakoutStrategy"]

_SIGNAL_CONFIDENCE: Decimal = Decimal("0.6")
"""Fixed confidence attached to every signal, for the same reason ema_trend's is fixed: a
breakout is a binary condition, and any number derived from how far price cleared the level
would be an invented probability dressed up as a measurement."""


class BreakoutParameters(BaseModel):
    """Typed parameters for :class:`BreakoutStrategy`.

    Neither lookback has a default. ``ema_trend``'s periods default to 20/50 because those
    numbers are the frozen benchmark's own identity; breakout has no equivalent claim to
    default to, and constructing one without stating both lookbacks explicitly is refused
    rather than quietly answered with a number that could be mistaken for a recommendation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    entry_lookback: int = Field(ge=2, le=500)
    exit_lookback: int = Field(ge=2, le=500)

    @property
    def entry_feature(self) -> str:
        """Return the feature name carrying the entry breakout level."""
        return f"donchian_high_{self.entry_lookback}"

    @property
    def exit_feature(self) -> str:
        """Return the feature name carrying the exit breakdown level."""
        return f"donchian_low_{self.exit_lookback}"


class BreakoutStrategy(BaseStrategy):
    """Enters long on a new N-bar high, exits on a new M-bar low.

    Deterministic: the same window of closed bars always produces the same signal. No
    randomness, no adaptation, no fitting, and no memory between calls — the decision is a
    pure function of the features and the current bar it is handed.
    """

    METADATA: ClassVar[StrategyMetadata] = StrategyMetadata(
        strategy_id="breakout",
        version="1.0.0",
        name="Donchian breakout",
        description=(
            "Long-only spot momentum: enter when the current bar's high breaks above the "
            "prior 20-bar high, exit when its low breaks below the prior 10-bar low. An "
            "operational default to make the strategy runnable, not a claim of edge."
        ),
        required_history=21,
        required_features=("donchian_high_20", "donchian_low_10"),
        supported_timeframes=(Timeframe.H1,),
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=BreakoutParameters,
        operates_intrabar=False,
        allows_short=False,
    )

    def __init__(self, parameters: BaseModel) -> None:
        """Build the strategy and check its parameters against its declared contract.

        :class:`~quantplatform.core.models.strategy.StrategyMetadata` is a class attribute,
        so ``required_features`` cannot vary with an instance's lookbacks. Configuring, say,
        15 and 5 would leave the strategy reading ``donchian_high_15``/``donchian_low_5``
        while its contract still promised the 20/10 pair — the engine's check would pass,
        the pipeline would supply the declared pair, and the strategy would find neither of
        the features it actually wanted and fall silent for the rest of the run.

        Refusing at construction turns that into an immediate, readable failure. The
        lookbacks remain typed configuration; changing them means also updating the declared
        metadata, which is the honest cost of a class-level contract — the same one
        :class:`~quantplatform.strategies.ema_trend.EmaTrendStrategy` already carries.

        Raises:
            StrategyParameterError: If the configured lookbacks are not the ones this
                strategy's metadata declares.
        """
        super().__init__(parameters)
        typed = self._typed_parameters()
        declared = set(type(self).METADATA.required_features)
        wanted = {typed.entry_feature, typed.exit_feature}
        if wanted != declared:
            raise StrategyParameterError(
                "the configured lookbacks do not match the features this strategy declares; "
                "update METADATA.required_features and required_history to match",
                strategy_id=type(self).METADATA.strategy_id,
                configured=sorted(wanted),
                declared=sorted(declared),
            )

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Return at most one signal for the bar being decided on.

        Args:
            context: Closed bars and features up to and including the decided bar.

        Returns:
            One entry signal, one exit signal, or nothing at all. Silence is the common
            case: a breakout system that spoke on every bar would be a different strategy.
        """
        parameters = self._typed_parameters()
        latest = context.latest_bar

        if context.position_state is PositionState.FLAT:
            # `entry_level is None` means the window is too short for the level. The
            # contract check has already confirmed the pipeline *can* produce it, so this
            # is warm-up, not misconfiguration, and warm-up is not something to trade
            # through — `None > x` would raise, so the check is required, not defensive.
            entry_level = context.features.get(parameters.entry_feature)
            if entry_level is not None and latest.high > entry_level:
                return (
                    self.build_signal(
                        context=context,
                        action=SignalAction.ENTER_LONG,
                        confidence=_SIGNAL_CONFIDENCE,
                        reason=(
                            f"high {latest.high} broke the {parameters.entry_lookback}-bar "
                            f"high {entry_level}"
                        ),
                        features={parameters.entry_feature: entry_level},
                    ),
                )
            return ()

        if context.position_state is PositionState.LONG:
            exit_level = context.features.get(parameters.exit_feature)
            if exit_level is not None and latest.low < exit_level:
                return (
                    self.build_signal(
                        context=context,
                        action=SignalAction.EXIT_LONG,
                        confidence=_SIGNAL_CONFIDENCE,
                        reason=(
                            f"low {latest.low} broke the {parameters.exit_lookback}-bar "
                            f"low {exit_level}"
                        ),
                        features={parameters.exit_feature: exit_level},
                    ),
                )
            return ()

        return ()

    def _typed_parameters(self) -> BreakoutParameters:
        """Return the validated parameters this instance was built with."""
        parameters = self.parameters
        if not isinstance(parameters, BreakoutParameters):  # pragma: no cover - defensive
            msg = "BreakoutStrategy requires BreakoutParameters"
            raise TypeError(msg)
        return parameters
