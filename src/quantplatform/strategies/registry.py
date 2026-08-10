"""Strategy registry.

Orchestration resolves strategies by identifier through a registry instance, so adding a
strategy means adding a class and listing it in :data:`BUILTIN_STRATEGIES` — no
orchestration code changes. The registry is an ordinary object rather than a module-level
singleton, which keeps construction explicit and tests fully isolated.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final

from pydantic import ValidationError

from quantplatform.core.errors import (
    StrategyAlreadyRegisteredError,
    StrategyNotFoundError,
    StrategyParameterError,
)
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.ema_trend import EmaTrendStrategy

__all__ = ["BUILTIN_STRATEGIES", "StrategyRegistry", "build_default_registry"]

BUILTIN_STRATEGIES: Final[tuple[type[BaseStrategy], ...]] = (EmaTrendStrategy,)
"""Strategies shipped with the platform.

One, deliberately. A paper run needs a strategy to exercise the chain, and more than one
would invite a comparison this platform is not yet set up to make honestly.
"""


class StrategyRegistry:
    """Maps strategy identifiers to implementations."""

    def __init__(self) -> None:
        self._entries: dict[str, type[BaseStrategy]] = {}

    def register(self, strategy_class: type[BaseStrategy]) -> type[BaseStrategy]:
        """Add a strategy class to the registry.

        Args:
            strategy_class: Concrete subclass of
                :class:`~quantplatform.strategies.base.BaseStrategy` declaring ``METADATA``.

        Returns:
            The registered class, so this may also be used as a decorator.

        Raises:
            StrategyAlreadyRegisteredError: If the identifier is already taken.
            StrategyParameterError: If the class does not declare metadata.
        """
        metadata = getattr(strategy_class, "METADATA", None)
        if not isinstance(metadata, StrategyMetadata):
            raise StrategyParameterError(
                "strategy class must declare a STRATEGY METADATA class attribute",
                strategy_class=strategy_class.__name__,
            )
        if metadata.strategy_id in self._entries:
            raise StrategyAlreadyRegisteredError(
                "a strategy with this identifier is already registered",
                strategy_id=metadata.strategy_id,
                existing=self._entries[metadata.strategy_id].__name__,
                incoming=strategy_class.__name__,
            )
        self._entries[metadata.strategy_id] = strategy_class
        return strategy_class

    def get(self, strategy_id: str) -> type[BaseStrategy]:
        """Return the registered class for an identifier.

        Args:
            strategy_id: Registry identifier.

        Returns:
            The registered strategy class.

        Raises:
            StrategyNotFoundError: If the identifier is unknown.
        """
        try:
            return self._entries[strategy_id]
        except KeyError as exc:
            raise StrategyNotFoundError(
                "no strategy is registered under this identifier",
                strategy_id=strategy_id,
                available=sorted(self._entries),
            ) from exc

    def metadata_for(self, strategy_id: str) -> StrategyMetadata:
        """Return the declared metadata of a registered strategy.

        Args:
            strategy_id: Registry identifier.

        Returns:
            The strategy's metadata.

        Raises:
            StrategyNotFoundError: If the identifier is unknown.
        """
        return self.get(strategy_id).METADATA

    def create(self, strategy_id: str, parameters: Mapping[str, Any] | None = None) -> BaseStrategy:
        """Instantiate a registered strategy from raw parameter values.

        Args:
            strategy_id: Registry identifier.
            parameters: Raw parameter mapping, validated against the declared schema.

        Returns:
            A ready-to-use strategy instance.

        Raises:
            StrategyNotFoundError: If the identifier is unknown.
            StrategyParameterError: If the parameters fail the declared schema.
        """
        strategy_class = self.get(strategy_id)
        schema = strategy_class.METADATA.parameter_schema
        try:
            validated = schema.model_validate(dict(parameters or {}))
        except ValidationError as exc:
            raise StrategyParameterError(
                "strategy parameters failed validation",
                strategy_id=strategy_id,
                errors=exc.errors(include_url=False),
            ) from exc
        return strategy_class(validated)

    def list_metadata(self) -> tuple[StrategyMetadata, ...]:
        """Return the metadata of every registered strategy, ordered by identifier."""
        return tuple(self._entries[key].METADATA for key in sorted(self._entries))

    def __contains__(self, strategy_id: object) -> bool:
        """Return whether an identifier is registered."""
        return strategy_id in self._entries

    def __len__(self) -> int:
        """Return the number of registered strategies."""
        return len(self._entries)

    def __iter__(self) -> Iterator[str]:
        """Iterate over registered identifiers in sorted order."""
        return iter(sorted(self._entries))


def build_default_registry() -> StrategyRegistry:
    """Construct a registry populated with the built-in strategies.

    Returns:
        A new registry instance; callers own it and may register further strategies.
    """
    registry = StrategyRegistry()
    for strategy_class in BUILTIN_STRATEGIES:
        registry.register(strategy_class)
    return registry
