"""Event publisher implementations.

These are the transport-free publishers the platform needs from day one: one that records
events for assertions and replay, one that writes them to the structured log, and one that
fans out to several sinks. Durable persistence of events is the storage layer's concern.
"""

from __future__ import annotations

from collections.abc import Sequence

from quantplatform.core.events import DomainEvent
from quantplatform.core.logging_config import get_logger

__all__ = [
    "CompositeEventPublisher",
    "InMemoryEventPublisher",
    "LoggingEventPublisher",
]


class InMemoryEventPublisher:
    """Collects published events in order.

    Intended for tests, for backtests that assemble a run report in memory, and as a
    building block inside :class:`CompositeEventPublisher`.
    """

    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        """Record a single event."""
        self._events.append(event)

    async def publish_many(self, events: Sequence[DomainEvent]) -> None:
        """Record events in order."""
        self._events.extend(events)

    @property
    def events(self) -> tuple[DomainEvent, ...]:
        """Return every recorded event in publication order."""
        return tuple(self._events)

    def clear(self) -> None:
        """Discard all recorded events."""
        self._events.clear()


class LoggingEventPublisher:
    """Writes each event to the structured log.

    Args:
        logger_name: Logger namespace to publish under.
    """

    def __init__(self, logger_name: str = "monitoring.events") -> None:
        self._logger = get_logger(logger_name)

    async def publish(self, event: DomainEvent) -> None:
        """Emit one event as a structured log record."""
        self._logger.info(
            "domain_event",
            extra={
                "event_type": event.event_type.value,
                "event_id": str(event.event_id),
                "correlation_id": str(event.correlation_id) if event.correlation_id else None,
                "occurred_at": event.occurred_at.isoformat(),
                "source": event.source,
            },
        )

    async def publish_many(self, events: Sequence[DomainEvent]) -> None:
        """Emit events in order."""
        for event in events:
            await self.publish(event)


class CompositeEventPublisher:
    """Fans a published event out to several publishers, in declaration order.

    Args:
        publishers: Downstream publishers to notify.
    """

    def __init__(self, *publishers: InMemoryEventPublisher | LoggingEventPublisher) -> None:
        self._publishers = publishers

    async def publish(self, event: DomainEvent) -> None:
        """Forward one event to every downstream publisher."""
        for publisher in self._publishers:
            await publisher.publish(event)

    async def publish_many(self, events: Sequence[DomainEvent]) -> None:
        """Forward events to every downstream publisher."""
        for publisher in self._publishers:
            await publisher.publish_many(events)
