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

from quantplatform.core.enums import (
    CircuitBreakerReason,
    RiskActionKind,
    RiskCheckCode,
    RiskCheckSeverity,
    RiskCheckStatus,
    RiskOutcome,
    StopKind,
)
from quantplatform.core.models.base import (
    DomainModel,
    StrategyId,
    Symbol,
    Text,
    UtcDatetime,
)
from quantplatform.core.models.health import HealthStatus
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.orders import ApprovedOrder
from quantplatform.core.models.portfolio import PortfolioSnapshot
from quantplatform.core.numeric import Money, NonNegativeMoney, Price, Quantity, Rate

__all__ = [
    "CircuitBreakerState",
    "PositionRiskState",
    "RiskAction",
    "RiskBudget",
    "RiskCheckResult",
    "RiskContext",
    "RiskDecision",
    "StopSpecification",
]


class RiskCheckResult(DomainModel):
    """Outcome of a single risk check, recorded for auditability.

    Every check the engine evaluates is recorded, including the ones that pass and the
    ones that are skipped, so a decision can be reproduced and explained after the fact.
    """

    code: RiskCheckCode
    status: RiskCheckStatus
    severity: RiskCheckSeverity = RiskCheckSeverity.BLOCKING
    """Whether a failure of this check rejects the intent or is merely recorded."""

    sequence: int = Field(default=0, ge=0)
    """Position of this check in the engine's fixed evaluation order.

    Recorded so a decision's check list can be replayed in the exact order it was produced
    even after being serialised, sorted or stored, which is what makes two runs comparable
    check by check rather than only by final outcome.
    """

    message: Text
    observed: Money | None = None
    limit: Money | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    """Structured, non-sensitive context for this check, beyond the single observed/limit
    pair — for example the requested and normalised quantity of a rounding check. Values are
    strings so the record serialises losslessly; never place credentials or infrastructure
    detail here."""

    evaluated_at: UtcDatetime

    @property
    def passed(self) -> bool:
        """Return whether the check did not block the order."""
        return self.status is not RiskCheckStatus.FAILED

    @property
    def blocks(self) -> bool:
        """Return whether this result must reject the intent.

        Only a failed *blocking* check does. A failed advisory check is reported and
        deliberately does not veto, which is what lets the engine record a concern without
        that record silently becoming a rejection.
        """
        return self.status is RiskCheckStatus.FAILED and self.severity is (
            RiskCheckSeverity.BLOCKING
        )


class RiskContext(DomainModel):
    """Everything the risk engine observes when evaluating an order intent.

    The context is assembled by orchestration and passed in, so the engine performs no
    input or output of its own and its decisions are reproducible from a stored context.
    Which checks consume which fields is the responsibility of the risk engine itself.

    Exercised against the Phase 4 risk engine, which consumes every field below. Optional
    metrics (``spread_basis_points``, ``realized_volatility``) are genuinely optional: the
    engine records a check for each and, under a strict configuration, rejects when one it
    needs is absent rather than assuming a value.
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
    open_order_symbols: frozenset[Symbol] = frozenset()
    """Symbols that already have a working order, used to detect a conflicting order.

    Distinct from :attr:`open_order_count`, which bounds total venue capacity: an account may
    permit several working orders overall while still refusing a second one on the symbol an
    intent targets.
    """

    pending_buy_notional: dict[Symbol, Money] = Field(default_factory=dict)
    """Worst-case quote-asset value already committed by working **buy** orders, per symbol.

    Exposure a decision has authorised but that has not yet reached a position. Without it,
    two intents evaluated between one another's fills each see the same untouched headroom
    and are both approved, together exceeding limits neither breached alone. A symbol present
    here with no open position is also treated as a position about to exist, so the
    position-count limit cannot be walked past the same way.

    Pending **sells** are deliberately absent: a sale reduces exposure, and counting it as
    committed value would make the account look more exposed the more it was unwinding.
    """

    approved_orders_last_hour: int = Field(ge=0)
    """Decisions in the last hour that authorised an order — approved or resized.

    Rejections are excluded: refusing an intent consumes no venue capacity, and counting it
    would let a burst of bad signals lock out the good one behind them. A replayed decision
    is counted once, when it was first made.
    """

    approved_orders_today: int = Field(ge=0)
    """Decisions today that authorised an order; same accounting as
    :attr:`approved_orders_last_hour`."""
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

    @property
    def symbol_rules_age_seconds(self) -> float:
        """Return how stale the venue trading rules are at :attr:`as_of`."""
        return (self.as_of - self.symbol_rules.updated_at).total_seconds()


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
        if not self.blocking_failures:
            msg = "a rejected decision must record at least one failed blocking check"
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
        if self.blocking_failures:
            msg = "an approved decision must not contain failed blocking risk checks"
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
        """Return every check that failed, blocking or advisory."""
        return tuple(check for check in self.checks if not check.passed)

    @property
    def blocking_failures(self) -> tuple[RiskCheckResult, ...]:
        """Return every failed check that actually vetoes the intent.

        The structural safety property is stated against *this* set, not
        :attr:`failed_checks`: an approved order cannot coexist with a failed blocking
        check, while a failed advisory check is recorded and does not prevent approval.
        """
        return tuple(check for check in self.checks if check.blocks)

    @property
    def ordered_checks(self) -> tuple[RiskCheckResult, ...]:
        """Return the checks in the engine's fixed evaluation order."""
        return tuple(sorted(self.checks, key=lambda check: check.sequence))

    @property
    def is_executable(self) -> bool:
        """Return whether the decision produced an order that may be submitted."""
        return self.outcome.is_approved and self.approved_order is not None


# --- Risk V2 contracts ------------------------------------------------------------------------
#
# Nothing below is consumed by the risk engine, the broker or the backtest loop yet. That is
# deliberate and is the milestone's whole safety argument: a contract no execution path reads
# cannot change how a run behaves, so V1 equivalence is structural rather than asserted.
#
# What they exist to fix is a specific, observed failure. Over seven days in week 5 a single
# entry committed roughly 95% of the account with no protection of any kind behind it — no
# stop, no risk budget, and no mechanism by which the risk engine could close what the
# strategy had opened. The engine's drawdown limits were configured and working; they simply
# only gate the *next* intent, and can say nothing about the position already losing money.


class StopSpecification(DomainModel):
    """Where a position stops losing money, in a form the strategy does not own.

    Exactly one way of naming the level is permitted per specification. Carrying both an
    absolute price and a relative distance would give the enforcement layer two sources of
    truth for one trigger, and whichever it chose the other would be a silent lie about
    where the stop actually sits.
    """

    kind: StopKind

    trigger_price: Price | None = None
    """Absolute level, when it is known at intent time."""

    distance_bps: NonNegativeMoney | None = None
    """Distance from entry in basis points, for a stop whose level follows the fill."""

    max_holding_seconds: int | None = Field(default=None, gt=0)
    """Maximum time in position, for :attr:`StopKind.TIME`."""

    activated_at: UtcDatetime | None = None
    """When a trailing or break-even stop armed; ``None`` while it is still dormant."""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the stop can actually be evaluated.

        Raises:
            ValueError: If the specification names no level, names two, or omits the
                duration a time stop is defined by.
        """
        if self.kind is StopKind.TIME:
            if self.max_holding_seconds is None:
                msg = "a time stop requires max_holding_seconds"
                raise ValueError(msg)
            return self
        if self.trigger_price is not None and self.distance_bps is not None:
            msg = "a stop names either a trigger_price or a distance_bps, not both"
            raise ValueError(msg)
        if self.trigger_price is None and self.distance_bps is None:
            msg = "a stop requires either a trigger_price or a distance_bps"
            raise ValueError(msg)
        return self


class RiskBudget(DomainModel):
    """How much of the account a single position may destroy, and how large it may become.

    The distinction this model draws — between what may be *spent* and what may be *lost* —
    is the one week 5 did not have. ``entry_fraction`` sizes a position as a share of equity
    regardless of where its stop sits; :attr:`risk_per_trade_pct` sizes it so that being
    stopped out costs a known, bounded amount.
    """

    risk_per_trade_pct: Rate = Field(gt=0, le=Decimal("0.5"))
    """Fraction of equity a single stop-out may cost.

    Bounded well below 1.0 on purpose: at 1.0 a single losing trade ends the account, which
    is not a risk limit but a permission slip. The ceiling here is a structural sanity bound,
    not a recommendation — the operative value is configuration, chosen with evidence.
    """

    max_position_exposure_pct: Rate = Field(gt=0, le=1)
    """Largest share of equity a single position's marked value may reach."""

    min_stop_distance_bps: NonNegativeMoney
    """Below this, a stop is inside the noise and would be hit by spread alone."""

    max_stop_distance_bps: NonNegativeMoney
    """Above this, risk-based sizing produces a position too small to be meaningful."""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the stop-distance window is usable.

        Raises:
            ValueError: If the bounds cross or meet, leaving no admissible distance.
        """
        if self.min_stop_distance_bps >= self.max_stop_distance_bps:
            msg = "min_stop_distance_bps must be strictly below max_stop_distance_bps"
            raise ValueError(msg)
        return self


class PositionRiskState(DomainModel):
    """What a position was protected by, and what it actually risked.

    Recorded per position and intended to be persisted alongside it. Without this, a
    resumed process knows it holds 0.1185 BTC but not what stop that position was opened
    under, nor what its R-multiple should be measured against — which is exactly the state
    week 5 ended in.
    """

    symbol: Symbol
    stop: StopSpecification
    risk_amount: NonNegativeMoney = Field(gt=0)
    """Quote-asset amount at risk at entry: the denominator of every R-multiple.

    Strictly positive. A recorded risk of zero would make R undefined by division and would
    claim protection where none was purchased.
    """

    entry_price: Price
    highest_price_seen: Price | None = None
    """Favourable extreme since entry, for a trailing stop. ``None`` until first marked."""

    opened_at: UtcDatetime


class CircuitBreakerState(DomainModel):
    """Whether trading is halted, why, and what would be needed to resume.

    A tripped breaker does not clear itself. Automatic un-tripping would turn a stop into a
    delay, and the condition that tripped it is precisely the condition under which a
    process should not be deciding on its own that things have improved — the same
    discipline :meth:`~quantplatform.marketdata.feed.BinanceSpotMarketDataFeed.resynchronize`
    already applies to a data gap.
    """

    tripped_at: UtcDatetime | None = None
    reason: CircuitBreakerReason | None = None
    consecutive_losses: int = Field(default=0, ge=0)
    daily_loss: NonNegativeMoney = Decimal(0)
    resets_at: UtcDatetime | None = None
    """Earliest instant an operator-initiated reset may be accepted, when one is scheduled."""

    @property
    def is_tripped(self) -> bool:
        """Return whether trading is currently halted by this breaker."""
        return self.tripped_at is not None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check a halt is explicable and ordered in time.

        Raises:
            ValueError: If the breaker is tripped without a reason, or resets before it
                tripped.
        """
        if self.tripped_at is not None and self.reason is None:
            msg = "a tripped circuit breaker must record its reason"
            raise ValueError(msg)
        if self.tripped_at is None and self.reason is not None:
            msg = "an untripped circuit breaker must not carry a reason"
            raise ValueError(msg)
        if (
            self.resets_at is not None
            and self.tripped_at is not None
            and self.resets_at < self.tripped_at
        ):
            msg = "resets_at must not precede tripped_at"
            raise ValueError(msg)
        return self


class RiskAction(DomainModel):
    """What risk has decided must happen to exposure that already exists.

    The contract that lets risk *act* rather than only refuse. It is narrow by design: risk
    may reduce, close or stop opening. It may never open a position, size one up, or choose
    an instrument — a risk engine able to do those things would be a second strategy that
    nobody researched and no benchmark measures.
    """

    kind: RiskActionKind
    symbol: Symbol | None = None
    quantity: Quantity | None = None
    reason: Text
    triggered_by: RiskCheckCode | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the action names everything its kind needs to be carried out.

        Raises:
            ValueError: If an action that touches a position does not identify it, or a
                partial reduction does not say by how much.
        """
        if self.kind in (RiskActionKind.CLOSE, RiskActionKind.REDUCE) and self.symbol is None:
            msg = f"a {self.kind.value} action must name the symbol it acts on"
            raise ValueError(msg)
        if self.kind is RiskActionKind.REDUCE and self.quantity is None:
            msg = "a reduce action must state the quantity to close"
            raise ValueError(msg)
        return self
