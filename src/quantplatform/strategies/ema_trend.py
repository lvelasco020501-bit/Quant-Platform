"""A conservative EMA trend-following baseline.

**What this is for.** A paper run needs *a* strategy in order to exercise the chain end to
end — features, signals, sizing, risk, execution, accounting, reporting. This is that
strategy. It is deliberately the simplest thing that produces occasional, explicable
positions, so that when something looks wrong during an operational run the strategy is the
last place anyone needs to look.

**No claim is made about profitability.** Two exponential averages crossing is among the
most-published rules in existence and carries no edge on that account. Its virtues here are
narrow and entirely operational: it is deterministic, it holds no state between calls, it
needs one number from the market, and its behaviour on any window can be worked out by hand.

**Long-only spot, one position at a time.** It emits an entry when the fast average is above
the slow one and the account is flat, and an exit when the fast average falls below the slow
one and the account is long. Everything else is silence. It never sizes an order, never sees
a balance, never learns whether its last signal was executed, and cannot observe the
execution mode — all of which are properties of the strategy contract rather than of this
implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.errors import StrategyParameterError
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.strategies.base import BaseStrategy

__all__ = ["EmaTrendParameters", "EmaTrendStrategy"]

_SIGNAL_CONFIDENCE: Decimal = Decimal("0.6")
"""Fixed confidence attached to every signal.

Deliberately constant. A crossing is a binary condition, so any number derived from the
distance between the averages would be an invented probability dressed up as a measurement.
The risk engine's minimum-confidence check still applies; it simply always sees the same
value from this strategy.
"""


class EmaTrendParameters(BaseModel):
    """Typed parameters for :class:`EmaTrendStrategy`."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    fast_period: int = Field(default=20, ge=2, le=500)
    slow_period: int = Field(default=50, ge=3, le=1000)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the two periods describe a trend filter rather than noise.

        Raises:
            ValueError: If the fast period is not strictly shorter than the slow one. Equal
                periods would make the averages identical and the strategy silent forever,
                which is a configuration mistake that should not take a week to notice.
        """
        if self.fast_period >= self.slow_period:
            msg = "fast_period must be strictly shorter than slow_period"
            raise ValueError(msg)
        return self

    @property
    def fast_feature(self) -> str:
        """Return the feature name carrying the fast average."""
        return f"ema_{self.fast_period}"

    @property
    def slow_feature(self) -> str:
        """Return the feature name carrying the slow average."""
        return f"ema_{self.slow_period}"


class EmaTrendStrategy(BaseStrategy):
    """Enters long while a fast EMA sits above a slow one, exits when it falls below.

    Deterministic: the same window of closed bars always produces the same signal. No
    randomness, no adaptation, no fitting, and no memory between calls — the decision is a
    pure function of the features it is handed.
    """

    METADATA: ClassVar[StrategyMetadata] = StrategyMetadata(
        strategy_id="ema_trend",
        version="1.0.0",
        name="EMA trend",
        description=(
            "Long-only spot trend filter: hold while the fast exponential average is above "
            "the slow one, exit when it is below. A deterministic operational baseline, not "
            "a claim of edge."
        ),
        required_history=50,
        required_features=("ema_20", "ema_50"),
        supported_timeframes=(Timeframe.H1,),
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=EmaTrendParameters,
        operates_intrabar=False,
        allows_short=False,
    )

    def __init__(self, parameters: BaseModel) -> None:
        """Build the strategy and check its parameters against its declared contract.

        :class:`~quantplatform.core.models.strategy.StrategyMetadata` is a class attribute,
        so ``required_features`` cannot vary with an instance's periods. Configuring, say,
        9 and 21 would leave the strategy reading ``ema_9`` and ``ema_21`` while its
        contract still promised ``ema_20`` and ``ema_50`` — the engine's check would pass,
        the pipeline would supply the declared pair, and the strategy would find neither of
        the ones it wanted and fall silent for the rest of the run.

        Refusing at construction turns that into an immediate, readable failure. The
        periods remain typed configuration; changing them means also updating the declared
        metadata, which is the honest cost of a class-level contract.

        Raises:
            StrategyParameterError: If the configured periods are not the ones this
                strategy's metadata declares.
        """
        super().__init__(parameters)
        typed = self._typed_parameters()
        declared = set(type(self).METADATA.required_features)
        wanted = {typed.fast_feature, typed.slow_feature}
        if wanted != declared:
            raise StrategyParameterError(
                "the configured periods do not match the features this strategy declares; "
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
            case and is not a failure: a trend filter that spoke on every bar would be a
            different strategy.
        """
        parameters = self._typed_parameters()
        fast = context.features.get(parameters.fast_feature)
        slow = context.features.get(parameters.slow_feature)
        if fast is None or slow is None:
            # The window is too short for one of the averages. The contract check has
            # already confirmed the pipeline *can* produce them, so this is warm-up rather
            # than misconfiguration, and warm-up is not something to trade through.
            return ()

        if fast > slow and context.position_state is PositionState.FLAT:
            return (
                self.build_signal(
                    context=context,
                    action=SignalAction.ENTER_LONG,
                    confidence=_SIGNAL_CONFIDENCE,
                    reason=(
                        f"fast EMA({parameters.fast_period}) is above "
                        f"slow EMA({parameters.slow_period})"
                    ),
                    features={
                        parameters.fast_feature: fast,
                        parameters.slow_feature: slow,
                    },
                ),
            )
        if fast < slow and context.position_state is PositionState.LONG:
            return (
                self.build_signal(
                    context=context,
                    action=SignalAction.EXIT_LONG,
                    confidence=_SIGNAL_CONFIDENCE,
                    reason=(
                        f"fast EMA({parameters.fast_period}) is below "
                        f"slow EMA({parameters.slow_period})"
                    ),
                    features={
                        parameters.fast_feature: fast,
                        parameters.slow_feature: slow,
                    },
                ),
            )
        return ()

    def _typed_parameters(self) -> EmaTrendParameters:
        """Return the validated parameters this instance was built with."""
        parameters = self.parameters
        if not isinstance(parameters, EmaTrendParameters):  # pragma: no cover - defensive
            msg = "EmaTrendStrategy requires EmaTrendParameters"
            raise TypeError(msg)
        return parameters
