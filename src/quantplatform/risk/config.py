"""Externalised risk limits.

Every threshold the risk engine enforces is configuration rather than code, so limits can be
tightened, audited and reproduced without touching decision logic. Nothing here has a
permissive fallback: an incoherent combination is refused at construction, because a risk
limit that silently defaults to something safe-looking is worse than no limit at all — it
reads as protection that was never actually applied.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantplatform.core.constants import ZERO
from quantplatform.core.enums import OrderType, TimeInForce
from quantplatform.core.models.execution_policy import MAX_BASIS_POINTS, ExecutionPolicy
from quantplatform.core.models.risk import RiskBudget
from quantplatform.core.numeric import Money, NonNegativeMoney, Rate

__all__ = ["RiskConfiguration"]

_MAX_BASIS_POINTS = MAX_BASIS_POINTS


class RiskConfiguration(BaseModel):
    """Complete set of limits the risk engine evaluates an intent against."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    # --- Operational gates ----------------------------------------------------------------

    allow_degraded_state: bool = False
    """Whether new orders may be approved while the system reports ``DEGRADED``.

    ``STARTING``, ``HALTED`` and ``RECONCILIATION_REQUIRED`` are never negotiable and have no
    corresponding flag: a system that is still starting has not established the state its
    checks read, and the other two are explicit stop conditions.
    """

    strict_missing_metrics: bool = True
    """Whether an absent optional metric rejects the intent.

    When ``True`` a missing spread or volatility reading is a rejection, because a guard that
    cannot be evaluated has not been passed. When ``False`` the check is recorded as skipped
    and the intent proceeds — appropriate only where the metric is known to be unavailable by
    design, such as a backtest over data that never carried a spread.
    """

    # --- Instrument and order permissions ---------------------------------------------------

    allow_market_orders: bool = True
    allow_limit_orders: bool = True
    allowed_time_in_force: tuple[TimeInForce, ...] = (TimeInForce.GTC, TimeInForce.IOC)

    # --- Frequency limits -------------------------------------------------------------------

    max_orders_per_hour: int = Field(default=5, ge=1)
    max_orders_per_day: int = Field(default=20, ge=1)

    # --- Position and exposure limits -------------------------------------------------------

    max_open_positions: int = Field(default=1, ge=1)
    max_open_orders: int = Field(default=1, ge=0)
    max_order_notional: NonNegativeMoney = Decimal(100_000)
    """Largest quote-denominated notional a single order may carry."""

    max_symbol_exposure: NonNegativeMoney = Decimal(100_000)
    """Largest marked value a single symbol's position may reach."""

    max_portfolio_exposure_pct: Rate = Decimal("0.95")
    """Largest share of equity that may be held as open positions."""

    # --- Drawdown limits ---------------------------------------------------------------------

    max_daily_drawdown_pct: Rate = Decimal("0.05")
    max_total_drawdown_pct: Rate = Decimal("0.20")

    # --- Risk V2 (declared, not yet enforced) --------------------------------------------------
    #
    # Every field below defaults to "not configured" rather than to a permissive-looking
    # value, and **nothing reads any of them yet**. Both facts are the milestone's safety
    # argument: an unconfigured engine is bit-for-bit the V1 engine, and a configured one
    # still is, until the enforcement milestones land. A limit that silently defaulted to
    # something safe-looking would be worse than no limit at all — it would read as
    # protection that was never actually applied, which is this module's opening premise.

    risk_budget: RiskBudget | None = None
    """How much a single position may lose, and how large it may become.

    ``None`` keeps V1 sizing: a fixed fraction of equity, blind to where any stop sits.
    That is the behaviour that let one entry commit ~95% of the account in week 5.
    """

    max_daily_loss_pct: Rate | None = Field(default=None, gt=0, le=1)
    """Realised loss within one reporting day that halts new entries.

    Distinct from :attr:`max_daily_drawdown_pct`, which measures a peak-to-trough decline in
    equity including open positions. This one counts money actually lost and booked.
    """

    max_consecutive_losses: int | None = Field(default=None, ge=1)
    """Losing trades in a row that halt new entries.

    A streak and a loss rate are different failures: five losses across a month is a
    strategy performing within expectation, and five in a row is a regime it did not
    anticipate.
    """

    initial_stop_distance_bps: NonNegativeMoney | None = Field(
        default=None, gt=0, le=_MAX_BASIS_POINTS
    )
    """How far below a long's entry its initial protective stop is placed, in basis points.

    Risk derives the stop; the strategy never proposes one. A strategy that chose its own
    survival level would be deciding how much of the account it may destroy, which is the
    separation this whole layer exists to enforce — and week 5's strategy had no exit at all
    beyond its own crossover, which is what the separation is being enforced against.

    ``None`` means no stop is derived, which is V1. Expressed as a distance rather than an
    absolute level because the level cannot be known before a price is: a distance applied
    to the reference price yields a trigger, whereas an absolute level configured in advance
    would be wrong for every price but one.
    """

    require_stop_on_entry: bool = False
    """Whether an intent that carries no :class:`~quantplatform.core.models.risk.StopSpecification`
    is refused.

    The single flag that turns "no naked entries" from an intention into an enforceable
    rule. ``False`` preserves V1, where no intent carried a stop because the concept did
    not exist.
    """

    # --- Market-condition guards --------------------------------------------------------------

    max_spread_bps: NonNegativeMoney = Field(default=Decimal(25), le=_MAX_BASIS_POINTS)
    max_volatility: Rate = Decimal("0.15")

    # --- Freshness budgets ---------------------------------------------------------------------

    stale_market_data_seconds: int = Field(default=90, ge=0)
    stale_symbol_rules_seconds: int = Field(default=86_400, ge=0)

    # --- Operational health ----------------------------------------------------------------------

    max_consecutive_api_failures: int = Field(default=3, ge=1)

    # --- Market-buy price cap ---------------------------------------------------------------------

    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    """The fee and slippage assumptions the executing adapter runs under.

    The **same object** the broker is constructed with, not a copy of its numbers. Mirroring
    them as separate scalars was the previous design, and two independently maintained values
    drift: the risk engine then funds a fee the broker does not charge, or approves a price
    cap the broker's own slippage immediately breaches. Sharing one policy makes that class
    of mismatch unrepresentable rather than merely discouraged.
    """

    market_buy_buffer_bps: NonNegativeMoney = Field(default=Decimal(50), le=_MAX_BASIS_POINTS)
    """Headroom above the reference price allowed for a market buy.

    Covers the executing adapter's slippage and any additional movement expected between
    valuing the intent and the fill landing. It deliberately does **not** carry a spread
    allowance: see :attr:`minimum_required_buffer_bps` for why adding one would double-count.
    """

    additional_market_buy_safety_bps: NonNegativeMoney = Field(default=ZERO, le=_MAX_BASIS_POINTS)
    """Extra headroom on top of :attr:`market_buy_buffer_bps`, for venues that move faster
    than the configured buffer alone anticipates."""

    @model_validator(mode="after")
    def _validate_coherence(self) -> Self:
        """Check that the limits are mutually consistent and actually permit trading.

        Raises:
            ValueError: If no order type is permitted, no time in force is permitted, the
                daily and total drawdown limits are inverted, the hourly and daily order
                limits are inverted, or the market-buy buffer would approve a price the
                broker's own slippage can already exceed.
        """
        if not self.allow_market_orders and not self.allow_limit_orders:
            msg = "at least one order type must be permitted"
            raise ValueError(msg)
        if not self.allowed_time_in_force:
            msg = "at least one time in force must be permitted"
            raise ValueError(msg)
        if self.max_total_drawdown_pct < self.max_daily_drawdown_pct:
            msg = "max_total_drawdown_pct must not be below max_daily_drawdown_pct"
            raise ValueError(msg)
        if self.max_orders_per_day < self.max_orders_per_hour:
            msg = "max_orders_per_day must not be below max_orders_per_hour"
            raise ValueError(msg)
        if self.max_symbol_exposure > ZERO and self.max_order_notional > self.max_symbol_exposure:
            msg = "max_order_notional must not exceed max_symbol_exposure"
            raise ValueError(msg)
        if self.total_market_buy_buffer_bps < self.minimum_required_buffer_bps:
            msg = (
                "the market-buy buffer must cover the execution policy's slippage; "
                "a cap below it would reject every market buy at execution time"
            )
            raise ValueError(msg)
        return self

    @property
    def minimum_required_buffer_bps(self) -> Money:
        """Return the smallest market-buy buffer that can still produce a usable cap.

        Exactly the executing adapter's slippage rate, and no spread term on top. The
        reference price a :class:`~quantplatform.core.models.risk.RiskContext` carries is a
        traded price from a closed bar, so the spread is already inside it; adding a spread
        allowance here would count it twice and inflate every cap. Spread is instead policed
        by its own guard, which rejects an intent outright when the book is too wide to
        trade.
        """
        return self.execution_policy.slippage.effective_basis_points

    @property
    def total_market_buy_buffer_bps(self) -> Money:
        """Return the full headroom applied above the reference price for a market buy."""
        return self.market_buy_buffer_bps + self.additional_market_buy_safety_bps

    def permits_order_type(self, order_type: OrderType) -> bool:
        """Return whether this configuration allows an order of this type at all."""
        if order_type is OrderType.MARKET:
            return self.allow_market_orders
        if order_type is OrderType.LIMIT:
            return self.allow_limit_orders
        return False
