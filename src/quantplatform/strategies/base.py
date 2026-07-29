"""Base class every strategy implementation extends.

A strategy is a pure function of closed market data. It cannot download data, read
balances, submit orders, touch credentials, write to the database, modify the portfolio or
observe whether it is running in a backtest or against a live venue. Enforcing that here,
in one place, is what allows the same implementation to run unchanged in every execution
mode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel

from quantplatform.core.enums import SignalAction
from quantplatform.core.errors import StrategyContextError, StrategyParameterError
from quantplatform.core.ids import deterministic_uuid
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata

__all__ = ["BaseStrategy"]


class BaseStrategy(ABC):
    """Abstract strategy with contract enforcement and deterministic signal construction.

    Subclasses declare a :attr:`METADATA` class attribute and implement :meth:`generate`.

    Args:
        parameters: Instance of the parameter model declared in the metadata.

    Raises:
        StrategyParameterError: If ``parameters`` is not an instance of the declared schema.
    """

    METADATA: ClassVar[StrategyMetadata]

    def __init__(self, parameters: BaseModel) -> None:
        metadata = type(self).METADATA
        if not isinstance(parameters, metadata.parameter_schema):
            raise StrategyParameterError(
                "parameters do not match the declared parameter schema",
                strategy_id=metadata.strategy_id,
                expected=metadata.parameter_schema.__name__,
                received=type(parameters).__name__,
            )
        self._parameters = parameters

    @property
    def metadata(self) -> StrategyMetadata:
        """Return the strategy's self-description."""
        return type(self).METADATA

    @property
    def parameters(self) -> BaseModel:
        """Return the validated parameters this instance was constructed with."""
        return self._parameters

    @abstractmethod
    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Produce signals for the closed bar at ``context.as_of``.

        Args:
            context: Market and feature context containing only closed bars.

        Returns:
            Zero or more signals; an empty result means no opinion.
        """

    def validate_context(self, context: StrategyContext) -> None:
        """Check that a context satisfies everything the strategy declared it needs.

        Orchestration calls this before :meth:`generate` so that a misconfigured run fails
        loudly instead of producing signals from insufficient history.

        Bar closure is not re-checked here because
        :class:`~quantplatform.core.models.signals.StrategyContext` refuses to hold an open
        bar at all; that guarantee is enforced by the type, not by strategy discipline.

        Args:
            context: Context about to be passed to :meth:`generate`.

        Raises:
            StrategyContextError: If the market, timeframe, history depth or feature set
                does not satisfy the declared contract.
        """
        metadata = self.metadata
        if not metadata.supports(timeframe=context.timeframe, market_type=context.market_type):
            raise StrategyContextError(
                "strategy does not support this market and timeframe combination",
                strategy_id=metadata.strategy_id,
                timeframe=context.timeframe.value,
                market_type=context.market_type.value,
            )
        if context.history_length < metadata.required_history:
            raise StrategyContextError(
                "insufficient history for the strategy",
                strategy_id=metadata.strategy_id,
                required=metadata.required_history,
                available=context.history_length,
            )
        missing = tuple(
            feature for feature in metadata.required_features if feature not in context.features
        )
        if missing:
            raise StrategyContextError(
                "required features are missing from the context",
                strategy_id=metadata.strategy_id,
                missing=list(missing),
            )

    def build_signal(
        self,
        *,
        context: StrategyContext,
        action: SignalAction,
        confidence: Decimal,
        reason: str,
        features: Mapping[str, Decimal] | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Signal:
        """Construct a signal with a deterministic identifier.

        The identifier is derived from the strategy, symbol, bar and action, so replaying
        the same data produces byte-identical records and a backtest stays reproducible.

        Args:
            context: Context the signal was derived from.
            action: Directional action.
            confidence: Score on the unit interval.
            reason: Human-readable explanation, required for auditability.
            features: Feature values that justified the decision.
            metadata: Additional string annotations.

        Returns:
            The constructed signal.

        Raises:
            StrategyContextError: If the strategy emits a short signal without declaring
                short support.
        """
        declared = self.metadata
        if action.requires_short_selling and not declared.allows_short:
            raise StrategyContextError(
                "strategy emitted a short signal without declaring short support",
                strategy_id=declared.strategy_id,
            )
        signal_id = deterministic_uuid(
            "signal",
            declared.strategy_id,
            declared.version,
            context.symbol,
            context.as_of.isoformat(),
            action.value,
        )
        return Signal(
            signal_id=signal_id,
            strategy_id=declared.strategy_id,
            strategy_version=declared.version,
            symbol=context.symbol,
            market_type=context.market_type,
            timeframe=context.timeframe,
            bar_close_time=context.as_of,
            generated_at=context.as_of,
            action=action,
            confidence=confidence,
            reason=reason,
            features=dict(features or {}),
            metadata=dict(metadata or {}),
        )
