"""Strategy contract, registry and the ports the platform exposes."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from quantplatform.core.enums import (
    AlertSeverity,
    MarketType,
    PositionState,
    SignalAction,
    Timeframe,
)
from quantplatform.core.errors import (
    StrategyAlreadyRegisteredError,
    StrategyContextError,
    StrategyNotFoundError,
    StrategyParameterError,
)
from quantplatform.core.events import AlertRaised
from quantplatform.core.ids import deterministic_uuid
from quantplatform.core.interfaces import EventPublisher, Strategy
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.monitoring.publishers import (
    CompositeEventPublisher,
    InMemoryEventPublisher,
    LoggingEventPublisher,
)
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.registry import (
    BUILTIN_STRATEGIES,
    StrategyRegistry,
    build_default_registry,
)
from tests.factories import ANCHOR, DummyParams, DummyStrategy, make_context


def _metadata_payload(**overrides: object) -> dict[str, object]:
    """Build an explicit metadata payload; the schema field is not round-trippable."""
    base: dict[str, object] = {
        "strategy_id": "dummy_trend",
        "version": "1.0.0",
        "name": "Dummy trend",
        "description": "Test metadata.",
        "required_history": 2,
        "required_features": ("ema_fast",),
        "supported_timeframes": (Timeframe.H1,),
        "supported_market_types": (MarketType.SPOT,),
        "parameter_schema": DummyParams,
    }
    return {**base, **overrides}


# --- Metadata ------------------------------------------------------------------------------------


def test_metadata_declares_the_full_contract() -> None:
    metadata = DummyStrategy.METADATA
    assert metadata.strategy_id == "dummy_trend"
    assert metadata.version == "1.0.0"
    assert metadata.required_history == 2
    assert metadata.required_features == ("ema_fast",)
    assert metadata.supported_timeframes == (Timeframe.H1,)
    assert metadata.supported_market_types == (MarketType.SPOT,)
    assert metadata.parameter_schema is DummyParams
    assert metadata.operates_intrabar is False
    assert metadata.allows_short is False


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "latest", ""])
def test_metadata_requires_a_semantic_version(version: str) -> None:
    with pytest.raises(ValidationError):
        StrategyMetadata.model_validate(_metadata_payload(version=version))


def test_metadata_rejects_duplicate_declarations() -> None:
    payload = _metadata_payload(supported_timeframes=(Timeframe.H1, Timeframe.H1))
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        StrategyMetadata.model_validate(payload)


def test_metadata_support_check() -> None:
    metadata = DummyStrategy.METADATA
    assert metadata.supports(timeframe=Timeframe.H1, market_type=MarketType.SPOT)
    assert not metadata.supports(timeframe=Timeframe.M5, market_type=MarketType.SPOT)
    assert not metadata.supports(timeframe=Timeframe.H1, market_type=MarketType.PERPETUAL)


def test_short_capable_strategy_needs_a_shortable_market() -> None:
    with pytest.raises(ValidationError, match="shortable market type"):
        StrategyMetadata.model_validate(_metadata_payload(allows_short=True))


# --- Contract enforcement ------------------------------------------------------------------------


def test_strategy_satisfies_the_port() -> None:
    strategy: Strategy = DummyStrategy(DummyParams())
    assert strategy.metadata.strategy_id == "dummy_trend"
    assert isinstance(strategy.parameters, DummyParams)


def test_strategy_rejects_foreign_parameters() -> None:
    class OtherParams(BaseModel):
        value: int = 1

    with pytest.raises(StrategyParameterError, match="parameter schema"):
        DummyStrategy(OtherParams())


def test_generate_is_pure_and_deterministic() -> None:
    strategy = DummyStrategy(DummyParams())
    context = make_context(closes=(Decimal(100), Decimal(110)))
    first = strategy.generate(context)
    second = strategy.generate(context)
    assert len(first) == 1
    assert first[0].signal_id == second[0].signal_id
    assert first[0].action is SignalAction.ENTER_LONG
    assert first[0].reason


def test_signals_never_predate_their_bar() -> None:
    strategy = DummyStrategy(DummyParams())
    context = make_context(closes=(Decimal(100), Decimal(110)))
    signal = strategy.generate(context)[0]
    assert signal.bar_close_time == context.as_of
    assert signal.generated_at == context.as_of


def test_strategy_may_decline_to_act() -> None:
    strategy = DummyStrategy(DummyParams())
    assert strategy.generate(make_context(closes=(Decimal(110), Decimal(100)))) == ()


def test_context_validation_requires_sufficient_history() -> None:
    strategy = DummyStrategy(DummyParams())
    with pytest.raises(StrategyContextError, match="insufficient history"):
        strategy.validate_context(make_context(closes=(Decimal(100),)))


def test_context_validation_requires_declared_features() -> None:
    strategy = DummyStrategy(DummyParams())
    context = make_context(closes=(Decimal(100), Decimal(110)), features={})
    with pytest.raises(StrategyContextError, match="required features are missing"):
        strategy.validate_context(context)


def test_context_validation_rejects_an_unsupported_timeframe() -> None:
    strategy = DummyStrategy(DummyParams())
    context = make_context(closes=(Decimal(100), Decimal(110)), timeframe=Timeframe.M15)
    with pytest.raises(StrategyContextError, match="does not support"):
        strategy.validate_context(context)


def test_a_valid_context_passes_validation() -> None:
    strategy = DummyStrategy(DummyParams())
    strategy.validate_context(make_context(closes=(Decimal(100), Decimal(110))))


def test_closed_bar_guarantee_is_enforced_by_the_context_type() -> None:
    # No strategy can be handed an open bar, because the context refuses to carry one.
    assert all(bar.is_closed for bar in make_context().bars)
    assert DummyStrategy.METADATA.operates_intrabar is False


def test_strategy_cannot_emit_a_short_signal_without_declaring_it() -> None:
    strategy = DummyStrategy(DummyParams())
    context = make_context()
    with pytest.raises(StrategyContextError, match="without declaring short support"):
        strategy.build_signal(
            context=context,
            action=SignalAction.ENTER_SHORT,
            confidence=Decimal("0.5"),
            reason="short attempt",
        )


def test_position_state_carries_no_account_financials() -> None:
    context = make_context(position_state=PositionState.LONG)
    assert context.position_state is PositionState.LONG
    assert not hasattr(context, "cash")


# --- Registry ------------------------------------------------------------------------------------


def test_the_default_registry_carries_the_declared_builtin_strategies() -> None:
    # Was "empty before phase six", then "exactly one" until M9c.3b's research harness made
    # comparing two strategies honestly possible. Now it's exactly the two declared —
    # ema_trend (frozen as the benchmark) and breakout — named explicitly so a third
    # appearing here is a deliberate edit to this test, not a silent drift.
    assert len(BUILTIN_STRATEGIES) == 2
    assert len(build_default_registry()) == 2
    assert "ema_trend" in build_default_registry()
    assert "breakout" in build_default_registry()


def test_registry_registers_and_resolves() -> None:
    registry = StrategyRegistry()
    registry.register(DummyStrategy)
    assert "dummy_trend" in registry
    assert len(registry) == 1
    assert registry.get("dummy_trend") is DummyStrategy
    assert registry.metadata_for("dummy_trend") is DummyStrategy.METADATA
    assert list(registry) == ["dummy_trend"]
    assert registry.list_metadata() == (DummyStrategy.METADATA,)


def test_registry_refuses_duplicate_identifiers() -> None:
    registry = StrategyRegistry()
    registry.register(DummyStrategy)
    with pytest.raises(StrategyAlreadyRegisteredError):
        registry.register(DummyStrategy)


def test_registry_reports_unknown_identifiers() -> None:
    registry = StrategyRegistry()
    with pytest.raises(StrategyNotFoundError):
        registry.get("missing")


def test_registry_refuses_classes_without_metadata() -> None:
    class Undeclared(BaseStrategy):
        def generate(self, context: StrategyContext) -> Sequence[Signal]:
            return ()

    registry = StrategyRegistry()
    with pytest.raises(StrategyParameterError, match="METADATA"):
        registry.register(Undeclared)


def test_registry_validates_parameters_on_creation() -> None:
    registry = StrategyRegistry()
    registry.register(DummyStrategy)
    strategy = registry.create("dummy_trend", {"fast_period": 3})
    assert isinstance(strategy, DummyStrategy)
    assert strategy.parameters.model_dump()["fast_period"] == 3


def test_registry_rejects_invalid_parameters() -> None:
    registry = StrategyRegistry()
    registry.register(DummyStrategy)
    with pytest.raises(StrategyParameterError, match="failed validation"):
        registry.create("dummy_trend", {"fast_period": "not-an-int"})


def test_registries_are_independent() -> None:
    first = StrategyRegistry()
    second = StrategyRegistry()
    first.register(DummyStrategy)
    assert len(first) == 1
    assert len(second) == 0


def test_adding_a_strategy_needs_no_orchestration_change() -> None:
    class SecondParams(BaseModel):
        window: int = 3

    class SecondStrategy(BaseStrategy):
        METADATA: ClassVar[StrategyMetadata] = StrategyMetadata(
            strategy_id="second_strategy",
            version="0.1.0",
            name="Second",
            description="Another strategy registered without touching orchestration.",
            required_history=1,
            required_features=(),
            supported_timeframes=(Timeframe.H1,),
            supported_market_types=(MarketType.SPOT,),
            parameter_schema=SecondParams,
        )

        def generate(self, context: StrategyContext) -> Sequence[Signal]:
            return ()

    registry = StrategyRegistry()
    for strategy_class in (DummyStrategy, SecondStrategy):
        registry.register(strategy_class)
    assert list(registry) == ["dummy_trend", "second_strategy"]


# --- Event publishers ----------------------------------------------------------------------------


async def test_in_memory_publisher_records_events() -> None:
    publisher: EventPublisher = InMemoryEventPublisher()
    event = AlertRaised(
        event_id=deterministic_uuid("event", "alert", "1"),
        occurred_at=ANCHOR,
        source="test",
        correlation_id=None,
        severity=AlertSeverity.WARNING,
        title="test alert",
        detail="detail",
    )
    await publisher.publish(event)
    await publisher.publish_many([event])
    assert isinstance(publisher, InMemoryEventPublisher)
    assert len(publisher.events) == 2
    publisher.clear()
    assert not publisher.events


async def test_composite_publisher_fans_out() -> None:
    first = InMemoryEventPublisher()
    second = InMemoryEventPublisher()
    composite: EventPublisher = CompositeEventPublisher(first, second, LoggingEventPublisher())
    event = AlertRaised(
        event_id=deterministic_uuid("event", "alert", "2"),
        occurred_at=ANCHOR,
        source="test",
        correlation_id=None,
        severity=AlertSeverity.INFO,
        title="fan out",
        detail="detail",
    )
    await composite.publish(event)
    after_publish = first.events
    assert len(after_publish) == 1
    assert after_publish[0] is event
    assert second.events == after_publish

    await composite.publish_many([event])
    assert len(first.events) == 2
    assert len(second.events) == 2


async def test_logging_publisher_emits_every_event() -> None:
    publisher = LoggingEventPublisher()
    event = AlertRaised(
        event_id=deterministic_uuid("event", "alert", "3"),
        occurred_at=ANCHOR,
        source="test",
        correlation_id=deterministic_uuid("correlation", "run-1"),
        severity=AlertSeverity.CRITICAL,
        title="halted",
        detail="drawdown breach",
    )
    await publisher.publish(event)
    await publisher.publish_many([event, event])
