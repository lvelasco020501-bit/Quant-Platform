"""System health and circuit breaker domain models."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from quantplatform.core.enums import (
    CircuitBreakerReason,
    ReconciliationStatus,
    SystemState,
)
from quantplatform.core.models.base import DomainModel, Text, UtcDatetime

__all__ = ["CircuitBreakerStatus", "ComponentHealth", "HealthStatus"]


class ComponentHealth(DomainModel):
    """Health of one subsystem, such as the data feed, the venue or the database."""

    name: Text
    healthy: bool
    detail: Text | None = None
    checked_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_detail(self) -> Self:
        """Require an explanation whenever a component reports itself unhealthy."""
        if not self.healthy and self.detail is None:
            msg = "an unhealthy component must explain why"
            raise ValueError(msg)
        return self


class CircuitBreakerStatus(DomainModel):
    """State of a single automatic halt condition."""

    reason: CircuitBreakerReason
    tripped: bool
    detail: Text | None = None
    tripped_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _validate_trip(self) -> Self:
        """Require trip metadata exactly when the breaker is tripped."""
        if self.tripped and (self.tripped_at is None or self.detail is None):
            msg = "a tripped circuit breaker requires tripped_at and detail"
            raise ValueError(msg)
        if not self.tripped and self.tripped_at is not None:
            msg = "an untripped circuit breaker must not carry tripped_at"
            raise ValueError(msg)
        return self


class HealthStatus(DomainModel):
    """Aggregate operating state of the platform.

    A halted system never returns to trading on its own: recovery requires an explicit
    operator action that produces a new status.
    """

    state: SystemState
    checked_at: UtcDatetime
    components: tuple[ComponentHealth, ...] = ()
    circuit_breakers: tuple[CircuitBreakerStatus, ...] = ()
    reconciliation_status: ReconciliationStatus
    last_bar_close_time: UtcDatetime | None = None
    data_age_seconds: int | None = Field(default=None, ge=0)
    clock_skew_seconds: float | None = None
    consecutive_api_failures: int = Field(default=0, ge=0)
    halt_reason: Text | None = None

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        """Check that the reported state is consistent with its evidence."""
        reasons = [breaker.reason for breaker in self.circuit_breakers]
        if len(set(reasons)) != len(reasons):
            msg = "each circuit breaker reason may appear at most once"
            raise ValueError(msg)
        if self.state is SystemState.HALTED and self.halt_reason is None:
            msg = "a halted system must record why it halted"
            raise ValueError(msg)
        if self.state is not SystemState.HALTED and self.halt_reason is not None:
            msg = "only a halted system may carry a halt_reason"
            raise ValueError(msg)
        if self.state is SystemState.HEALTHY:
            if self.tripped_breakers:
                msg = "a system with tripped circuit breakers is not healthy"
                raise ValueError(msg)
            if any(not component.healthy for component in self.components):
                msg = "a system with unhealthy components is not healthy"
                raise ValueError(msg)
            if not self.reconciliation_status.allows_trading:
                msg = "a system that is not reconciled is not healthy"
                raise ValueError(msg)
        return self

    @property
    def tripped_breakers(self) -> tuple[CircuitBreakerStatus, ...]:
        """Return every circuit breaker currently tripped."""
        return tuple(breaker for breaker in self.circuit_breakers if breaker.tripped)

    @property
    def allows_trading(self) -> bool:
        """Return whether new orders may be submitted in this state."""
        return self.state.allows_new_orders and not self.tripped_breakers
