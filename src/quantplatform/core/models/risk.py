"""Risk decision domain models.

The models in this module encode the platform's central safety property: an
:class:`~quantplatform.core.models.orders.ApprovedOrder` can only exist inside a
:class:`RiskDecision`, and a decision carrying an approved order is rejected at
construction time unless every recorded check passed. The invariant is therefore
structural rather than a matter of engine discipline.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from quantplatform.core.enums import RiskCheckCode, RiskCheckStatus, RiskOutcome
from quantplatform.core.models.base import (
    DomainModel,
    StrategyId,
    Text,
    UtcDatetime,
)
from quantplatform.core.models.health import HealthStatus
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.orders import ApprovedOrder
from quantplatform.core.models.portfolio import PortfolioSnapshot
from quantplatform.core.numeric import Money, NonNegativeMoney, Price, Quantity

__all__ = ["RiskCheckResult", "RiskContext", "RiskDecision"]


class RiskCheckResult(DomainModel):
    """Outcome of a single risk check, recorded for auditability.

    Every check the engine evaluates is recorded, including the ones that pass and the
    ones that are skipped, so a decision can be reproduced and explained after the fact.
    """

    code: RiskCheckCode
    status: RiskCheckStatus
    message: Text
    observed: Money | None = None
    limit: Money | None = None
    evaluated_at: UtcDatetime

    @property
    def passed(self) -> bool:
        """Return whether the check did not block the order."""
        return self.status is not RiskCheckStatus.FAILED


class RiskContext(DomainModel):
    """Everything the risk engine observes when evaluating an order intent.

    The context is assembled by orchestration and passed in, so the engine performs no
    input or output of its own and its decisions are reproducible from a stored context.
    Which checks consume which fields is the responsibility of the risk engine itself.

    **Provisional pending Phase 4.** This field set was derived from the 27 risk checks the
    specification documents, written ahead of the risk engine that will actually consume
    them. It has not yet been exercised against a real implementation of every check, so it
    should be expected to change: Phase 4 may find a check that needs state not captured
    here (for example, a per-strategy exposure breakdown, or trailing values needed for a
    volatility check finer than a single ``realized_volatility`` scalar) and require adding
    or reshaping fields. Treat this model as a draft contract, not a stable interface, until
    the risk engine is implemented and its tests pass against it.
    """

    as_of: UtcDatetime
    health: HealthStatus
    snapshot: PortfolioSnapshot
    symbol_rules: SymbolRules
    reference_price: Price
    """Price used to value the intent, taken from the closed bar that produced the signal."""

    latest_bar_close_time: UtcDatetime
    latest_bar_is_closed: bool
    open_order_count: int = Field(ge=0)
    orders_in_last_hour: int = Field(ge=0)
    orders_today: int = Field(ge=0)
    day_start_equity: NonNegativeMoney
    peak_equity: NonNegativeMoney
    spread_basis_points: Money | None = None
    realized_volatility: Money | None = None
    consecutive_api_failures: int = Field(ge=0)
    known_idempotency_keys: frozenset[str] = frozenset()
    """Keys of decisions already processed, used to detect duplicate intents."""

    @property
    def data_age_seconds(self) -> float:
        """Return how stale the most recent bar is at :attr:`as_of`."""
        return (self.as_of - self.latest_bar_close_time).total_seconds()


class RiskDecision(DomainModel):
    """The risk engine's final verdict over an order intent."""

    decision_id: UUID
    intent_id: UUID
    strategy_id: StrategyId
    outcome: RiskOutcome
    checks: tuple[RiskCheckResult, ...] = Field(min_length=1)
    requested_quantity: Quantity | None = None
    approved_order: ApprovedOrder | None = None
    rejection_reasons: tuple[Text, ...] = ()
    decided_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        """Check that the outcome, the recorded checks and the approved order agree."""
        recorded_codes = [check.code for check in self.checks]
        if len(set(recorded_codes)) != len(recorded_codes):
            msg = "each risk check may be recorded at most once per decision"
            raise ValueError(msg)

        if self.outcome is RiskOutcome.REJECTED:
            self._validate_rejection()
        else:
            self._validate_approval()
        return self

    def _validate_rejection(self) -> None:
        """Check the invariants of a rejected decision.

        Raises:
            ValueError: If the rejection is unexplained or still carries an order.
        """
        if self.approved_order is not None:
            msg = "a rejected decision must not carry an approved order"
            raise ValueError(msg)
        if not self.failed_checks:
            msg = "a rejected decision must record at least one failed check"
            raise ValueError(msg)
        if not self.rejection_reasons:
            msg = "a rejected decision must record at least one rejection reason"
            raise ValueError(msg)

    def _validate_approval(self) -> None:
        """Check the invariants of an approved or resized decision.

        Raises:
            ValueError: If any check failed, the order is missing, or the linkage or the
                resized quantity is inconsistent.
        """
        if self.failed_checks:
            msg = "an approved decision must not contain failed risk checks"
            raise ValueError(msg)
        if self.rejection_reasons:
            msg = "an approved decision must not record rejection reasons"
            raise ValueError(msg)
        order = self.approved_order
        if order is None:
            msg = f"outcome {self.outcome} requires an approved order"
            raise ValueError(msg)
        if order.decision_id != self.decision_id:
            msg = "approved order must reference the decision that authorised it"
            raise ValueError(msg)
        if order.intent_id != self.intent_id:
            msg = "approved order must reference the evaluated intent"
            raise ValueError(msg)
        if order.strategy_id != self.strategy_id:
            msg = "approved order must reference the deciding strategy"
            raise ValueError(msg)
        self._validate_sizing(order.quantity)

    def _validate_sizing(self, approved_quantity: Decimal) -> None:
        """Check the approved quantity against the requested quantity.

        Args:
            approved_quantity: Quantity carried by the approved order.

        Raises:
            ValueError: If the outcome does not match the sizing change.
        """
        if self.requested_quantity is None:
            return
        unchanged = approved_quantity == self.requested_quantity
        if self.outcome is RiskOutcome.APPROVED and not unchanged:
            msg = "an approved decision must not change the requested quantity; use resized"
            raise ValueError(msg)
        if self.outcome is RiskOutcome.RESIZED and unchanged:
            msg = "a resized decision must change the requested quantity"
            raise ValueError(msg)
        if self.outcome is RiskOutcome.RESIZED and approved_quantity > self.requested_quantity:
            msg = "a resized decision must not increase the requested quantity"
            raise ValueError(msg)

    @property
    def failed_checks(self) -> tuple[RiskCheckResult, ...]:
        """Return every check that blocked the order."""
        return tuple(check for check in self.checks if not check.passed)

    @property
    def is_executable(self) -> bool:
        """Return whether the decision produced an order that may be submitted."""
        return self.outcome.is_approved and self.approved_order is not None
