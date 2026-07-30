"""Domain event invariants.

Scoped to the events this repository's tests have not previously covered. Ingestion
lifecycle events (``IngestionStarted``/``Completed``/``Failed``,
``DataQualityIssueDetected``) are already exercised through the Phase 2 ingestion-service
integration tests and are not duplicated here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

import quantplatform.core.events as events_module
from quantplatform.core.enums import EventType
from quantplatform.core.events import PositionChanged
from quantplatform.core.ids import deterministic_uuid
from quantplatform.core.models.orders import Fill
from quantplatform.core.models.portfolio import Position
from tests.factories import ANCHOR, make_fill, make_position


def _payload(model: Fill | Position, **overrides: object) -> dict[str, object]:
    """Dump a model to a re-validatable payload, dropping computed fields."""
    data = model.model_dump()
    for computed in type(model).model_computed_fields:
        data.pop(computed, None)
    return {**data, **overrides}


def _event(**overrides: object) -> PositionChanged:
    defaults: dict[str, object] = {
        "event_id": deterministic_uuid("event", "position_changed", "1"),
        "occurred_at": ANCHOR,
        "source": "test",
        "correlation_id": None,
        "previous_position": None,
        "position": make_position(quantity=Decimal("0.1")),
        "fill": make_fill(),
    }
    return PositionChanged.model_validate({**defaults, **overrides})


def test_position_changed_declares_its_event_type() -> None:
    event = _event()
    assert event.event_type is EventType.POSITION_CHANGED


def test_position_changed_is_immutable() -> None:
    event = _event()
    with pytest.raises(ValidationError):
        event.position = make_position(quantity=Decimal("0.2"))  # type: ignore[misc]


def test_opening_a_position_carries_no_previous_position() -> None:
    event = _event(previous_position=None, position=make_position(quantity=Decimal("0.1")))
    assert event.previous_position is None
    assert event.position.is_open


def test_opening_requires_the_previous_position_to_be_omitted_or_absent() -> None:
    # previous_position may only be None when the fill opened the position; a flat
    # resulting position with no previous state is a nonsensical no-op event.
    with pytest.raises(ValidationError, match="only be omitted when the fill opened"):
        _event(previous_position=None, position=make_position(quantity=Decimal(0)))


def test_increasing_a_position_carries_both_states() -> None:
    previous = make_position(quantity=Decimal("0.1"))
    increased = make_position(quantity=Decimal("0.2"))
    event = _event(previous_position=previous, position=increased)
    assert event.previous_position is not None
    assert event.position.quantity > event.previous_position.quantity


def test_reducing_a_position_carries_both_states() -> None:
    previous = make_position(quantity=Decimal("0.2"))
    reduced = make_position(quantity=Decimal("0.1"))
    event = _event(previous_position=previous, position=reduced)
    assert event.previous_position is not None
    assert event.position.quantity < event.previous_position.quantity


def test_closing_a_position_leaves_it_flat() -> None:
    previous = make_position(quantity=Decimal("0.1"))
    closed = make_position(quantity=Decimal(0))
    event = _event(previous_position=previous, position=closed)
    assert event.previous_position is not None
    assert event.previous_position.is_open
    assert not event.position.is_open


def test_fill_must_match_the_resulting_position_symbol() -> None:
    mismatched_fill = Fill.model_validate(_payload(make_fill(), symbol="ETH/USDT"))
    with pytest.raises(ValidationError, match="fill must be for the same symbol"):
        _event(fill=mismatched_fill)


def test_previous_position_must_match_the_resulting_position_symbol() -> None:
    previous = make_position(quantity=Decimal("0.1"))
    mismatched_previous = Position.model_validate(
        _payload(previous, symbol="ETH/USDT", base_asset="ETH")
    )
    with pytest.raises(ValidationError, match="previous_position must be for the same symbol"):
        _event(
            previous_position=mismatched_previous, position=make_position(quantity=Decimal("0.2"))
        )


def test_no_separate_position_lifecycle_event_classes_exist() -> None:
    # One generic event covers every lifecycle point, mirroring OrderStatusChanged.
    for name in ("PositionOpened", "PositionIncreased", "PositionReduced", "PositionClosed"):
        assert not hasattr(events_module, name)
