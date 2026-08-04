"""Phase 5: the orchestration chain, exercised with every real component.

Data, features, strategy, risk, broker and portfolio are the production classes throughout.
The only doubles are the strategies themselves — a strategy is the thing under test in a real
run, so here it is the thing that must be predictable.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from quantplatform.core.enums import (
    CommissionModel,
    MarketType,
    OrderSide,
    PositionState,
    SignalAction,
    Timeframe,
)
from quantplatform.core.errors import (
    ConfigurationError,
    DataIntegrityError,
    StrategyContextError,
    StrategyError,
)
from quantplatform.core.events import FillReceived, OrderStatusChanged, RiskDecisionMade
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.features import MovingAverageFeatures, NullFeaturePipeline
from quantplatform.strategies.base import BaseStrategy
from tests.factories import (
    SYMBOL,
    make_backtest,
    make_backtest_config,
    make_bar,
    make_bars,
    make_execution_policy,
    make_symbol_rules,
)

_USDT = "USDT"
_BTC = "BTC"


class _Params(BaseModel):
    """Strategies here take no parameters."""


def _metadata(
    strategy_id: str,
    *,
    required_history: int = 1,
    required_features: tuple[str, ...] = (),
) -> StrategyMetadata:
    return StrategyMetadata(
        strategy_id=strategy_id,
        version="1.0.0",
        name=strategy_id,
        description="Deterministic strategy used to exercise the orchestration chain.",
        required_history=required_history,
        required_features=required_features,
        supported_timeframes=(Timeframe.H1,),
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=_Params,
        operates_intrabar=False,
        allows_short=False,
    )


class Silent(BaseStrategy):
    """Never has an opinion."""

    METADATA: ClassVar[StrategyMetadata] = _metadata("silent")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        _ = context
        return ()


_WARMUP_BARS = 3
"""Closes needed before realised volatility exists; the risk engine refuses to trade until
it does, so a strategy that signals earlier is testing rejection rather than execution."""


class BuyOnce(BaseStrategy):
    """Enters long once warm-up is complete and never acts again."""

    METADATA: ClassVar[StrategyMetadata] = _metadata("buy_once")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        if context.position_state is PositionState.FLAT and context.history_length == _WARMUP_BARS:
            return (
                self.build_signal(
                    context=context,
                    action=SignalAction.ENTER_LONG,
                    confidence=Decimal("0.9"),
                    reason="first bar entry",
                ),
            )
        return ()


class BuyThenSell(BaseStrategy):
    """Enters once warm-up is complete and exits as soon as it is holding something."""

    METADATA: ClassVar[StrategyMetadata] = _metadata("buy_then_sell")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        if context.position_state is PositionState.FLAT and context.history_length == _WARMUP_BARS:
            return (
                self.build_signal(
                    context=context,
                    action=SignalAction.ENTER_LONG,
                    confidence=Decimal("0.9"),
                    reason="entry",
                ),
            )
        if context.position_state is PositionState.LONG:
            return (
                self.build_signal(
                    context=context,
                    action=SignalAction.EXIT_LONG,
                    confidence=Decimal("0.9"),
                    reason="exit",
                ),
            )
        return ()


class NeedsAverage(BaseStrategy):
    """Requires a feature, so the pipeline contract is exercised."""

    METADATA: ClassVar[StrategyMetadata] = _metadata(
        "needs_average", required_history=2, required_features=("sma_2",)
    )

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        _ = context
        return ()


class Exploding(BaseStrategy):
    """Raises, so the failure contract is exercised."""

    METADATA: ClassVar[StrategyMetadata] = _metadata("exploding")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        _ = context
        msg = "strategy failed"
        raise StrategyError(msg)


def _flat_bars(count: int, price: Decimal = Decimal(50_000)) -> tuple[MarketBar, ...]:
    """Bars with no price movement, so execution effects are isolated from PnL."""
    return make_bars([price] * count)


# --- Dataset shapes -------------------------------------------------------------------------


def test_an_empty_dataset_produces_an_empty_but_valid_result() -> None:
    engine, broker, _ = make_backtest(strategy=Silent(_Params()))

    result = engine.run(())

    assert result.bars_processed == 0
    assert result.equity_curve == ()
    assert result.events == ()
    assert result.performance is not None
    assert result.performance.final_equity == result.config.initial_capital
    assert result.performance.sharpe_ratio is None
    assert result.started_at is None
    assert broker.open_orders() == ()


def test_a_single_bar_run_completes() -> None:
    engine, _, _ = make_backtest(strategy=Silent(_Params()))

    result = engine.run(_flat_bars(1))

    assert result.bars_processed == 1
    assert len(result.equity_curve) == 1
    assert len(result.snapshots) == 1


def test_multiple_bars_produce_one_snapshot_each_in_order() -> None:
    engine, _, _ = make_backtest(strategy=Silent(_Params()))

    result = engine.run(_flat_bars(5))

    assert result.bars_processed == 5
    assert len(result.snapshots) == 5
    times = [snapshot.taken_at for snapshot in result.snapshots]
    assert times == sorted(times)
    assert [point.at for point in result.equity_curve] == times


def test_a_strategy_with_no_opinion_trades_nothing() -> None:
    engine, _, portfolio = make_backtest(strategy=Silent(_Params()))

    result = engine.run(_flat_bars(4))

    assert result.signals == ()
    assert result.intents == ()
    assert result.decisions == ()
    assert result.fills == ()
    assert portfolio.positions() == ()
    assert result.performance is not None
    assert result.performance.total_return == Decimal(0)


# --- The chain ------------------------------------------------------------------------------


def test_one_buy_traverses_every_stage_and_settles() -> None:
    engine, _, portfolio = make_backtest(strategy=BuyOnce(_Params()))

    result = engine.run(_flat_bars(5))

    assert len(result.signals) == 1
    assert len(result.intents) == 1
    assert len(result.decisions) == 1
    assert len(result.approved_orders) == 1
    assert len(result.fills) == 1
    assert portfolio.positions()[0].quantity > Decimal(0)


def test_execution_is_next_bar_never_the_deciding_bar() -> None:
    # The whole point: a decision taken from bar 0's close must not fill at bar 0's open, a
    # price that printed before the strategy had seen the data it decided on.
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()))

    result = engine.run(_flat_bars(5))

    decided = _WARMUP_BARS - 1
    assert result.bars[decided].signals != ()
    assert result.bars[decided].fills == ()
    assert result.bars[decided + 1].fills != ()
    fill = result.fills[0]
    assert fill.executed_at == result.bars[decided + 1].bar.close_time


def test_buy_then_sell_closes_the_position_and_records_a_trade() -> None:
    engine, _, portfolio = make_backtest(strategy=BuyThenSell(_Params()))

    result = engine.run(_flat_bars(8))

    sides = [fill.side for fill in result.fills]
    assert OrderSide.BUY in sides
    assert OrderSide.SELL in sides
    assert portfolio.positions()[0].quantity == Decimal(0)
    assert result.performance is not None
    assert result.performance.trades.count >= 1


def test_the_portfolio_is_the_only_thing_that_books_a_fill() -> None:
    engine, _, portfolio = make_backtest(strategy=BuyOnce(_Params()))

    result = engine.run(_flat_bars(5))

    for fill in result.fills:
        assert portfolio.has_applied(fill.fill_id)
    assert result.calls.portfolio_fills_applied == len(result.fills)


# --- Invocation counts -----------------------------------------------------------------------


def test_every_stage_is_invoked_the_expected_number_of_times() -> None:
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()))

    result = engine.run(_flat_bars(5))

    assert result.calls.feature_computations == 5
    assert result.calls.strategy_invocations == 5
    assert result.calls.broker_bars_processed == 5
    assert result.calls.risk_evaluations == len(result.intents)
    assert result.calls.broker_submissions == len(result.approved_orders)


def test_the_strategy_is_not_called_before_it_has_the_history_it_declared() -> None:
    engine, _, _ = make_backtest(
        strategy=NeedsAverage(_Params()), features=MovingAverageFeatures([2])
    )

    result = engine.run(_flat_bars(4))

    # Four bars, but the first cannot satisfy a two-bar requirement.
    assert result.calls.feature_computations == 4
    assert result.calls.strategy_invocations == 3


def test_features_are_computed_for_every_bar() -> None:
    engine, _, _ = make_backtest(
        strategy=NeedsAverage(_Params()), features=MovingAverageFeatures([2])
    )
    result = engine.run(_flat_bars(6))
    assert result.calls.feature_computations == 6


# --- Risk and broker outcomes -----------------------------------------------------------------


def test_a_rejected_intent_does_not_stop_the_run() -> None:
    # A per-order notional ceiling below the venue minimum makes every entry unfundable.
    engine, _, _ = make_backtest(
        strategy=BuyOnce(_Params()),
        symbols={SYMBOL: make_symbol_rules(min_notional=Decimal(10_000_000))},
    )

    result = engine.run(_flat_bars(5))

    assert result.bars_processed == 5
    assert len(result.rejected_decisions) == 1
    assert result.approved_orders == ()
    assert result.fills == ()


def test_a_resized_order_is_executed_at_its_reduced_quantity() -> None:
    engine, _, portfolio = make_backtest(
        strategy=BuyOnce(_Params()), cash=Decimal(10_000), max_order_notional=Decimal(1_000)
    )

    result = engine.run(_flat_bars(5))

    assert len(result.approved_orders) == 1
    approved = result.approved_orders[0]
    assert result.fills[0].quantity == approved.quantity
    assert portfolio.positions()[0].quantity == approved.quantity


def test_partial_fills_settle_across_several_bars() -> None:
    engine, _, portfolio = make_backtest(
        strategy=BuyOnce(_Params()), fill_ratio=Decimal("0.5"), cash=Decimal(10_000)
    )

    result = engine.run(_flat_bars(12))

    assert len(result.fills) > 1
    assert result.calls.portfolio_fills_applied == len(result.fills)
    assert portfolio.positions()[0].quantity == sum(
        (fill.quantity for fill in result.fills), start=Decimal(0)
    )


# --- Costs ------------------------------------------------------------------------------------


def test_commission_is_accumulated_from_the_fills_themselves() -> None:
    policy = make_execution_policy(
        fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(10)
    )
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()), policy=policy)

    result = engine.run(_flat_bars(5))

    assert result.performance is not None
    assert result.performance.commission_paid == sum(
        (fill.fee for fill in result.fills), start=Decimal(0)
    )
    assert result.performance.commission_paid > Decimal(0)


def test_slippage_is_measured_against_the_bar_open() -> None:
    policy = make_execution_policy(slippage_bps=Decimal(20))
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()), policy=policy)

    result = engine.run(_flat_bars(5))

    fill = result.fills[0]
    expected = (fill.price - result.bars[_WARMUP_BARS].bar.open) * fill.quantity
    assert result.performance is not None
    assert result.performance.slippage_paid == expected
    assert expected > Decimal(0)


def test_a_run_without_costs_reports_none() -> None:
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()), policy=make_execution_policy())

    result = engine.run(_flat_bars(5))

    assert result.performance is not None
    assert result.performance.commission_paid == Decimal(0)
    assert result.performance.slippage_paid == Decimal(0)


def test_drawdown_is_recorded_when_equity_falls() -> None:
    engine, _, _ = make_backtest(
        strategy=BuyOnce(_Params()),
        policy=make_execution_policy(
            fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(50)
        ),
    )

    result = engine.run(
        make_bars(
            [Decimal(50_000), Decimal(50_000), Decimal(50_000), Decimal(50_000), Decimal(40_000)]
        )
    )

    assert result.performance is not None
    assert result.performance.max_drawdown > Decimal(0)
    assert result.performance.total_return is not None
    assert result.performance.total_return < Decimal(0)


# --- Events and ordering -------------------------------------------------------------------------


def test_events_are_stored_in_the_order_they_were_produced() -> None:
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()))

    result = engine.run(_flat_bars(5))

    assert result.events != ()
    times = [event.occurred_at for event in result.events]
    assert times == sorted(times)
    # The chain's own order: the decision precedes the order it authorised, which precedes
    # the fill that order produced.
    kinds = [type(event) for event in result.events]
    assert kinds.index(RiskDecisionMade) < kinds.index(FillReceived)
    assert OrderStatusChanged in kinds


def test_bar_outcomes_carry_their_own_events_and_snapshot() -> None:
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()))

    result = engine.run(_flat_bars(5))

    for outcome in result.bars:
        assert outcome.snapshot is not None
        assert outcome.snapshot.taken_at == outcome.bar.close_time
    assert sum(len(outcome.events) for outcome in result.bars) == len(result.events)


# --- Multiple symbols -------------------------------------------------------------------------


def test_two_symbols_are_orchestrated_independently() -> None:
    eth = make_symbol_rules(symbol="ETH/USDT", base_asset="ETH")
    symbols = {SYMBOL: make_symbol_rules(), "ETH/USDT": eth}
    # Each entry asks for a third of equity so both symbols fit inside the exposure budget;
    # at the default fraction the first entry would consume it and the second would be
    # refused, which is correct risk behaviour but tests nothing about orchestration.
    engine, _, portfolio = make_backtest(
        strategy=BuyOnce(_Params()),
        symbols=symbols,
        cash=Decimal(200_000),
        max_open_positions=2,
        config=make_backtest_config(cash=Decimal(200_000), entry_fraction=Decimal("0.33")),
    )

    btc = make_bars([Decimal(50_000)] * 5)
    eth_bars = tuple(
        make_bar(index=index, close=Decimal(2_000), symbol="ETH/USDT") for index in range(5)
    )
    interleaved = tuple(bar for pair in zip(btc, eth_bars, strict=True) for bar in pair)

    result = engine.run(interleaved)

    assert result.bars_processed == 10
    traded = {position.symbol for position in portfolio.positions() if position.is_open}
    assert traded == {SYMBOL, "ETH/USDT"}


# --- Determinism -------------------------------------------------------------------------------


def _run_twice() -> tuple[object, object]:
    bars = make_bars(
        [Decimal(50_000), Decimal(50_500), Decimal(50_200), Decimal(50_400), Decimal(50_300)]
    )
    outputs = []
    for _ in range(2):
        engine, _, _ = make_backtest(
            strategy=BuyThenSell(_Params()),
            policy=make_execution_policy(
                slippage_bps=Decimal(5),
                fee_model=CommissionModel.BASIS_POINTS,
                fee_basis_points=Decimal(10),
            ),
        )
        outputs.append(engine.run(bars))
    return outputs[0], outputs[1]


def test_two_runs_over_identical_input_produce_identical_output() -> None:
    first, second = _run_twice()

    assert first.fills == second.fills  # type: ignore[attr-defined]
    assert first.orders == second.orders  # type: ignore[attr-defined]
    assert first.snapshots == second.snapshots  # type: ignore[attr-defined]
    assert first.equity_curve == second.equity_curve  # type: ignore[attr-defined]
    assert first.performance == second.performance  # type: ignore[attr-defined]


def test_identifiers_and_the_event_sequence_replay_exactly() -> None:
    first, second = _run_twice()

    assert [event.event_id for event in first.events] == [  # type: ignore[attr-defined]
        event.event_id
        for event in second.events  # type: ignore[attr-defined]
    ]
    assert [order.client_order_id for order in first.approved_orders] == [  # type: ignore[attr-defined]
        order.client_order_id
        for order in second.approved_orders  # type: ignore[attr-defined]
    ]
    assert [fill.fill_id for fill in first.fills] == [  # type: ignore[attr-defined]
        fill.fill_id
        for fill in second.fills  # type: ignore[attr-defined]
    ]


def test_two_runs_on_the_same_engine_instance_do_not_contaminate_each_other() -> None:
    engine, _, _ = make_backtest(strategy=Silent(_Params()))
    bars = _flat_bars(3)

    first = engine.run(bars)
    second = engine.run(bars)

    assert first.bars_processed == second.bars_processed
    assert first.calls == second.calls


def test_the_engine_reads_no_wall_clock() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "quantplatform" / "backtesting"
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in ("datetime.now(", "utcnow(", "time.time(", "random.", "SystemClock"):
            assert token not in source, f"{path.name} references {token}"


# --- Failure behaviour ---------------------------------------------------------------------------


def test_a_strategy_exception_stops_the_run() -> None:
    engine, _, _ = make_backtest(strategy=Exploding(_Params()))

    with pytest.raises(StrategyError):
        engine.run(_flat_bars(3))


def test_an_open_bar_is_refused_before_anything_is_processed() -> None:
    engine, _, _ = make_backtest(strategy=Silent(_Params()))
    bars = (make_bar(index=0, is_closed=False),)

    with pytest.raises(DataIntegrityError, match="closed bars"):
        engine.run(bars)


def test_out_of_order_bars_are_refused() -> None:
    engine, _, _ = make_backtest(strategy=Silent(_Params()))
    bars = make_bars([Decimal(50_000), Decimal(51_000)])

    with pytest.raises(DataIntegrityError, match="non-decreasing"):
        engine.run((bars[1], bars[0]))


def test_an_unregistered_symbol_is_refused() -> None:
    engine, _, _ = make_backtest(strategy=Silent(_Params()))
    bars = (make_bar(index=0, symbol="ETH/USDT"),)

    with pytest.raises(DataIntegrityError, match="venue rules"):
        engine.run(bars)


def test_a_pipeline_missing_a_required_feature_fails_before_the_run() -> None:
    engine, _, _ = make_backtest(strategy=NeedsAverage(_Params()), features=NullFeaturePipeline())

    with pytest.raises(StrategyContextError, match="does not produce"):
        engine.run(_flat_bars(3))


def test_strict_risk_without_a_spread_assumption_fails_before_the_run() -> None:
    engine, _, _ = make_backtest(
        strategy=Silent(_Params()),
        config=make_backtest_config(assumed_spread_basis_points=None),
    )

    with pytest.raises(ConfigurationError, match="spread"):
        engine.run(_flat_bars(2))
