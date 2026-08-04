"""The risk-to-broker contract, exercised with the real engine, broker and portfolio.

The guarantee under test: **an order the risk engine approves is one the broker can submit**,
under the same shared execution policy and against unchanged state. Each component is unit
tested on its own; what those tests cannot see is the seam between them, which is exactly
where a funding rule the risk engine models slightly differently from the broker shows up.

Every case below builds one :class:`ExecutionPolicy` and hands the same object to both sides,
which is how the platform is meant to be wired.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import (
    CommissionModel,
    ExecutionMode,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskCheckCode,
    RiskOutcome,
)
from quantplatform.core.models.execution_policy import ExecutionPolicy
from quantplatform.core.models.orders import ApprovedOrder
from quantplatform.core.models.risk import RiskDecision
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.execution.config import ExecutionConfig
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.risk.engine import StandardRiskEngine
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_balance,
    make_bar,
    make_execution_policy,
    make_intent,
    make_portfolio_engine,
    make_risk_config,
    make_risk_context,
    make_snapshot,
    make_symbol_rules,
)

_USDT = "USDT"
_BTC = "BTC"


class _Wiring:
    """One shared execution policy driving a real risk engine, broker and portfolio."""

    def __init__(
        self,
        *,
        policy: ExecutionPolicy,
        cash: Decimal,
        rules: object | None = None,
        **risk_overrides: object,
    ) -> None:
        self.policy = policy
        self.rules = rules if rules is not None else make_symbol_rules()
        self.symbols = {SYMBOL: self.rules}
        self.portfolio: SpotPortfolioEngine = make_portfolio_engine(
            symbols=self.symbols, initial_balances=(make_balance(free=cash),)
        )
        self.broker = SimulatedBroker(
            symbols=self.symbols,  # type: ignore[arg-type]
            portfolio=self.portfolio,
            execution_mode=ExecutionMode.PAPER,
            started_at=ANCHOR,
            config=ExecutionConfig(policy=policy),
        )
        self.engine = StandardRiskEngine(
            config=make_risk_config(execution_policy=policy, **risk_overrides)
        )
        self.cash = cash

    def decide(self, **intent_kwargs: object) -> RiskDecision:
        context = make_risk_context(
            snapshot=make_snapshot(cash=self.cash),
            symbol_rules=self.rules,  # type: ignore[arg-type]
        )
        return self.engine.evaluate(make_intent(**intent_kwargs), context)  # type: ignore[arg-type]

    def free_quote(self) -> Decimal:
        balance = self.portfolio.balance(_USDT)
        return balance.free if balance is not None else Decimal(0)

    def locked_quote(self) -> Decimal:
        balance = self.portfolio.balance(_USDT)
        return balance.locked if balance is not None else Decimal(0)


def _submit(wiring: _Wiring, order: ApprovedOrder) -> object:
    return wiring.broker.submit(order)


# --- 1. Market buy ---------------------------------------------------------------------------


def test_market_buy_reserves_the_exact_worst_case_and_fills_within_its_cap() -> None:
    policy = make_execution_policy(
        slippage_bps=Decimal(20),
        fee_model=CommissionModel.BASIS_POINTS,
        fee_basis_points=Decimal(10),
    )
    wiring = _Wiring(policy=policy, cash=Decimal(1_000_000), market_buy_buffer_bps=Decimal(30))

    decision = wiring.decide(quantity=Decimal("0.1"))
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.APPROVED
    assert order is not None
    cap = order.max_execution_price
    assert cap is not None

    submission = _submit(wiring, order)
    expected_reservation = cap * order.quantity + policy.fee.maximum_fee(cap * order.quantity)
    assert submission.accepted is True  # type: ignore[attr-defined]
    assert submission.reservation_delta == expected_reservation  # type: ignore[attr-defined]
    assert wiring.locked_quote() == expected_reservation

    result = wiring.broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert len(result.fills) == 1
    fill = result.fills[0]
    # The broker's own slippage moved the price, but never past what risk authorised.
    assert fill.price > Decimal(50_000)
    assert fill.price <= cap
    assert wiring.portfolio.positions()[0].quantity == order.quantity
    assert wiring.locked_quote() == Decimal(0)


def test_the_portfolio_receives_the_fill_exactly_once() -> None:
    wiring = _Wiring(policy=make_execution_policy(), cash=Decimal(1_000_000))
    decision = wiring.decide(quantity=Decimal("0.1"))
    assert decision.approved_order is not None
    _submit(wiring, decision.approved_order)

    wiring.broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))
    wiring.broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert len(wiring.broker.fills()) == 1
    assert wiring.portfolio.positions()[0].quantity == Decimal("0.1")


# --- 2. Limit buy ------------------------------------------------------------------------------


def test_limit_buy_price_and_quantity_are_accepted_by_the_broker_unchanged() -> None:
    policy = make_execution_policy(
        fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(10)
    )
    rules = make_symbol_rules(price_tick=Decimal("0.01"))
    wiring = _Wiring(policy=policy, cash=Decimal(1_000_000), rules=rules)

    intent = make_intent(quantity=Decimal("0.1"))
    payload = intent.model_dump()
    payload.update({"order_type": OrderType.LIMIT, "limit_price": Decimal("49999.007")})
    limit_intent = type(intent).model_validate(payload)
    decision = wiring.engine.evaluate(
        limit_intent,
        make_risk_context(snapshot=make_snapshot(cash=wiring.cash), symbol_rules=rules),
    )
    order = decision.approved_order
    assert order is not None
    assert order.limit_price == Decimal("49999.00")

    submission = _submit(wiring, order)

    assert submission.accepted is True  # type: ignore[attr-defined]
    assert submission.order.limit_price == order.limit_price  # type: ignore[attr-defined]
    assert submission.order.quantity == order.quantity  # type: ignore[attr-defined]
    notional = order.limit_price * order.quantity
    assert submission.reservation_delta == notional + policy.fee.maximum_fee(notional)  # type: ignore[attr-defined]


# --- 3. Resized order ---------------------------------------------------------------------------


def test_a_resized_order_is_reserved_and_executed_at_the_reduced_quantity() -> None:
    wiring = _Wiring(policy=make_execution_policy(), cash=Decimal(10_000))

    decision = wiring.decide(quantity=Decimal("1"))
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.quantity < Decimal("1")

    _submit(wiring, order)
    result = wiring.broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].quantity == order.quantity
    assert wiring.portfolio.positions()[0].quantity == order.quantity


# --- 4. Venue-rule rejection ----------------------------------------------------------------------


def test_a_sub_minimum_notional_intent_is_rejected_and_never_reaches_the_broker() -> None:
    rules = make_symbol_rules(min_notional=Decimal(1_000_000))
    wiring = _Wiring(policy=make_execution_policy(), cash=Decimal(1_000_000), rules=rules)

    decision = wiring.decide(quantity=Decimal("0.001"))

    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.approved_order is None
    assert RiskCheckCode.MINIMUM_NOTIONAL in {c.code for c in decision.blocking_failures}
    # Nothing was submittable, so the broker was never called and holds no state.
    assert wiring.broker.open_orders() == ()
    assert wiring.locked_quote() == Decimal(0)


# --- 5. Funding parity -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        make_execution_policy(),
        make_execution_policy(fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(25)),
        make_execution_policy(fee_model=CommissionModel.FLAT, flat_amount=Decimal(3)),
    ],
    ids=["no_fee", "basis_points", "flat"],
)
def test_the_broker_never_rejects_an_approved_order_for_insufficient_funds(
    policy: ExecutionPolicy,
) -> None:
    # The core guarantee: under one shared policy and unchanged state, risk funding and broker
    # reservation agree, so the broker cannot refuse what risk approved.
    for cash in (Decimal(120), Decimal(1_000), Decimal(5_000), Decimal(50_000)):
        wiring = _Wiring(
            policy=policy,
            cash=cash,
            market_buy_buffer_bps=Decimal(0),
            additional_market_buy_safety_bps=Decimal(0),
            max_portfolio_exposure_pct=Decimal(1),
        )
        decision = wiring.decide(quantity=Decimal("1"))
        order = decision.approved_order
        if order is None:
            assert decision.outcome is RiskOutcome.REJECTED
            continue
        submission = _submit(wiring, order)
        assert submission.accepted is True, (  # type: ignore[attr-defined]
            f"broker refused a risk-approved order at cash={cash} under {policy.fee.model}"
        )
        assert submission.order.status is OrderStatus.OPEN  # type: ignore[attr-defined]


def test_flat_commission_is_funded_by_risk_and_charged_once_by_the_broker() -> None:
    policy = make_execution_policy(fee_model=CommissionModel.FLAT, flat_amount=Decimal(3))
    wiring = _Wiring(
        policy=policy,
        cash=Decimal(5_000),
        market_buy_buffer_bps=Decimal(0),
        additional_market_buy_safety_bps=Decimal(0),
        max_portfolio_exposure_pct=Decimal(1),
    )

    decision = wiring.decide(quantity=Decimal("0.1"))
    order = decision.approved_order
    assert order is not None
    # Risk held back the flat fee, so the order is smaller than raw cash/price would allow.
    assert order.quantity < Decimal("0.1")

    _submit(wiring, order)
    result = wiring.broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].fee == Decimal(3)
    assert wiring.free_quote() >= Decimal(0)
    assert wiring.locked_quote() == Decimal(0)


def test_a_sell_approved_by_risk_is_submittable_and_settles() -> None:
    wiring = _Wiring(policy=make_execution_policy(), cash=Decimal(1_000_000))
    buy = wiring.decide(quantity=Decimal("0.1"))
    assert buy.approved_order is not None
    _submit(wiring, buy.approved_order)
    wiring.broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    snapshot = wiring.portfolio.snapshot(as_of=ANCHOR, mark_prices={SYMBOL: Decimal(50_000)})
    sell_decision = wiring.engine.evaluate(
        make_intent(
            side=OrderSide.SELL, quantity=Decimal("0.1"), signal_time=ANCHOR.replace(hour=2)
        ),
        make_risk_context(snapshot=snapshot, symbol_rules=wiring.rules),  # type: ignore[arg-type]
    )
    order = sell_decision.approved_order
    assert order is not None
    assert order.side is OrderSide.SELL

    submission = _submit(wiring, order)
    assert submission.accepted is True  # type: ignore[attr-defined]

    result = wiring.broker.process_bar(make_bar(index=1, open_price=Decimal(52_000)))
    assert result.fills[0].side is OrderSide.SELL
    assert wiring.portfolio.positions()[0].quantity == Decimal(0)


def test_time_of_check_to_time_of_use_is_the_brokers_to_enforce() -> None:
    # Documents the one guarantee the seam cannot make. Risk evaluates against a snapshot;
    # if the account is drained between evaluation and submission, the approval is stale.
    # The broker must still refuse atomically rather than overdraw, which is what makes the
    # window safe rather than merely narrow.
    wiring = _Wiring(policy=make_execution_policy(), cash=Decimal(1_000_000))
    decision = wiring.decide(quantity=Decimal("0.1"))
    order = decision.approved_order
    assert order is not None

    # State changes after evaluation: something else reserves nearly all the cash.
    wiring.portfolio.reserve(asset=_USDT, amount=Decimal(999_999), at=ANCHOR)

    submission = _submit(wiring, order)

    assert submission.accepted is False  # type: ignore[attr-defined]
    assert submission.order.status is OrderStatus.REJECTED  # type: ignore[attr-defined]
    assert submission.reservation_delta == Decimal(0)  # type: ignore[attr-defined]
    # The refusal changed nothing beyond recording the rejection.
    assert wiring.locked_quote() == Decimal(999_999)
