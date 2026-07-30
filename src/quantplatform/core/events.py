"""Domain events emitted along the decision pipeline.

Events are the audit backbone. Every step of the mandatory flow — market data, features,
signal, intent, risk decision, order, fill, portfolio update, reconciliation, health —
emits an immutable event carrying a correlation id, which makes the full path from a bar
to a portfolio change reconstructable after the fact.
"""

from __future__ import annotations

from typing import ClassVar, Self
from uuid import UUID

from pydantic import Field, computed_field, model_validator

from quantplatform.core.enums import (
    AlertSeverity,
    CircuitBreakerReason,
    EventType,
    MarketType,
    OrderStatus,
    ReconciliationStatus,
    SystemState,
    Timeframe,
)
from quantplatform.core.models.base import DomainModel, Symbol, Text, UtcDatetime
from quantplatform.core.models.data import DataQualityFinding, IngestionRun
from quantplatform.core.models.health import HealthStatus
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.orders import Fill, Order, OrderIntent
from quantplatform.core.models.portfolio import PortfolioSnapshot, Position
from quantplatform.core.models.risk import RiskDecision
from quantplatform.core.models.signals import Signal
from quantplatform.core.numeric import Money

__all__ = [
    "AlertRaised",
    "CircuitBreakerTripped",
    "DataQualityIssueDetected",
    "DomainEvent",
    "FeaturesComputed",
    "FillReceived",
    "IngestionCompleted",
    "IngestionFailed",
    "IngestionStarted",
    "MarketBarReceived",
    "OrderIntentCreated",
    "OrderStatusChanged",
    "OrderSubmitted",
    "PortfolioUpdated",
    "PositionChanged",
    "ReconciliationCompleted",
    "RiskDecisionMade",
    "SignalGenerated",
    "SystemStateChanged",
]


class DomainEvent(DomainModel):
    """Base class for every domain event.

    Subclasses declare their :attr:`EVENT_TYPE`, which is serialised as the ``event_type``
    field so that persisted events remain self-describing.
    """

    EVENT_TYPE: ClassVar[EventType]

    event_id: UUID
    occurred_at: UtcDatetime
    source: Text
    correlation_id: UUID | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def event_type(self) -> EventType:
        """Return the stable event type identifier of this event class."""
        return type(self).EVENT_TYPE


class MarketBarReceived(DomainEvent):
    """A normalised bar was accepted by the data layer."""

    EVENT_TYPE: ClassVar[EventType] = EventType.MARKET_BAR_RECEIVED

    bar: MarketBar


class DataQualityIssueDetected(DomainEvent):
    """An integrity problem was detected on inbound market data.

    Carries the full :class:`~quantplatform.core.models.data.DataQualityFinding` rather
    than a loose ``symbol``/``timeframe``/``issue``/``detail`` tuple (the shape this event
    originally had in the phase 1 placeholder). Nothing outside this module consumed the
    old shape, so folding the finding in directly avoids keeping two parallel
    representations of the same observation in sync.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.DATA_QUALITY_ISSUE_DETECTED

    finding: DataQualityFinding


class IngestionStarted(DomainEvent):
    """A market-data ingestion run began."""

    EVENT_TYPE: ClassVar[EventType] = EventType.INGESTION_STARTED

    run_id: UUID
    source_id: Text
    source_path: Text
    expected_symbol: Symbol
    expected_market_type: MarketType
    expected_timeframe: Timeframe


class IngestionCompleted(DomainEvent):
    """A market-data ingestion run finished successfully, with or without findings."""

    EVENT_TYPE: ClassVar[EventType] = EventType.INGESTION_COMPLETED

    run: IngestionRun


class IngestionFailed(DomainEvent):
    """A market-data ingestion run failed; no bars from it were persisted."""

    EVENT_TYPE: ClassVar[EventType] = EventType.INGESTION_FAILED

    run: IngestionRun


class FeaturesComputed(DomainEvent):
    """Feature values were computed for a closed bar."""

    EVENT_TYPE: ClassVar[EventType] = EventType.FEATURES_COMPUTED

    symbol: Symbol
    timeframe: Timeframe
    as_of: UtcDatetime
    features: dict[str, Money] = Field(default_factory=dict)


class SignalGenerated(DomainEvent):
    """A strategy produced a signal."""

    EVENT_TYPE: ClassVar[EventType] = EventType.SIGNAL_GENERATED

    signal: Signal


class OrderIntentCreated(DomainEvent):
    """An actionable signal was converted into an order intent."""

    EVENT_TYPE: ClassVar[EventType] = EventType.ORDER_INTENT_CREATED

    intent: OrderIntent


class RiskDecisionMade(DomainEvent):
    """The risk engine evaluated an order intent."""

    EVENT_TYPE: ClassVar[EventType] = EventType.RISK_DECISION_MADE

    decision: RiskDecision


class OrderSubmitted(DomainEvent):
    """An approved order was handed to an execution adapter."""

    EVENT_TYPE: ClassVar[EventType] = EventType.ORDER_SUBMITTED

    order: Order


class OrderStatusChanged(DomainEvent):
    """An order transitioned to a new lifecycle state."""

    EVENT_TYPE: ClassVar[EventType] = EventType.ORDER_STATUS_CHANGED

    order: Order
    previous_status: OrderStatus


class FillReceived(DomainEvent):
    """A fill was reported for an order."""

    EVENT_TYPE: ClassVar[EventType] = EventType.FILL_RECEIVED

    fill: Fill


class PositionChanged(DomainEvent):
    """A fill changed a position's exposure.

    One generic event covers every lifecycle point — opened, increased, reduced, closed —
    the same way :class:`OrderStatusChanged` covers every order transition with a single
    class rather than one per transition. A consumer derives which happened by comparing
    ``previous_position`` and ``position``: opened when the former is ``None``, closed
    when the latter's quantity is zero, increased or reduced otherwise by comparing their
    quantities. There are deliberately no separate ``PositionOpened``, ``PositionReduced``
    or ``PositionClosed`` event classes.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.POSITION_CHANGED

    previous_position: Position | None
    """The position immediately before ``fill`` was applied; ``None`` only when this fill
    opened the position from flat (no prior lifecycle to report)."""

    position: Position
    """The position immediately after ``fill`` was applied."""

    fill: Fill
    """The fill that triggered this change."""

    @model_validator(mode="after")
    def _validate_change(self) -> Self:
        """Check the self-contained consistency rules this event can verify on its own.

        Cross-checking against fill history or portfolio-engine state (for example,
        confirming ``previous_position`` really was the prior stored state) is out of
        scope for a single immutable event and belongs to the future portfolio engine.
        """
        if self.fill.symbol != self.position.symbol:
            msg = "fill must be for the same symbol as the resulting position"
            raise ValueError(msg)
        if (
            self.previous_position is not None
            and self.previous_position.symbol != self.position.symbol
        ):
            msg = "previous_position must be for the same symbol as the resulting position"
            raise ValueError(msg)
        if self.previous_position is None and not self.position.is_open:
            msg = "previous_position may only be omitted when the fill opened the position"
            raise ValueError(msg)
        return self


class PortfolioUpdated(DomainEvent):
    """Portfolio accounting produced a new snapshot."""

    EVENT_TYPE: ClassVar[EventType] = EventType.PORTFOLIO_UPDATED

    snapshot: PortfolioSnapshot


class ReconciliationCompleted(DomainEvent):
    """Local state was compared against the execution venue."""

    EVENT_TYPE: ClassVar[EventType] = EventType.RECONCILIATION_COMPLETED

    status: ReconciliationStatus
    detail: Text
    discrepancy_count: int = Field(ge=0)


class CircuitBreakerTripped(DomainEvent):
    """An automatic halt condition fired."""

    EVENT_TYPE: ClassVar[EventType] = EventType.CIRCUIT_BREAKER_TRIPPED

    reason: CircuitBreakerReason
    detail: Text


class SystemStateChanged(DomainEvent):
    """The platform moved to a different operating state."""

    EVENT_TYPE: ClassVar[EventType] = EventType.SYSTEM_STATE_CHANGED

    previous_state: SystemState
    current_state: SystemState
    reason: Text
    health: HealthStatus | None = None


class AlertRaised(DomainEvent):
    """An operational alert was raised for human attention."""

    EVENT_TYPE: ClassVar[EventType] = EventType.ALERT_RAISED

    severity: AlertSeverity
    title: Text
    detail: Text
