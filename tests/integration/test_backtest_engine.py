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

from quantplatform.backtesting.engine import BacktestEngine, RunState
from quantplatform.core.enums import (
    CircuitBreakerReason,
    CommissionModel,
    ExecutionMode,
    MarketType,
    OrderSide,
    PositionState,
    RiskCheckCode,
    RiskCheckStatus,
    SignalAction,
    StopKind,
    Timeframe,
)
from quantplatform.core.errors import (
    ConfigurationError,
    DataIntegrityError,
    PositionRiskAmbiguityError,
    PositionRiskUnavailableError,
    StrategyContextError,
    StrategyError,
)
from quantplatform.core.events import FillReceived, OrderStatusChanged, RiskDecisionMade
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.risk import PositionRiskState, RiskBudget
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.features import (
    ExponentialMovingAverageFeatures,
    MovingAverageFeatures,
    NullFeaturePipeline,
)
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.registry import build_default_registry
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


# --- The production strategy through the real pipeline -------------------------------------------


def _trend_bars() -> tuple[MarketBar, ...]:
    """Flat warm-up, a sustained rise, then a sustained fall — one crossing each way."""
    closes = (
        [Decimal(50_000)] * 60
        + [Decimal(50_000) + Decimal(300) * index for index in range(1, 61)]
        + [Decimal(68_000) - Decimal(400) * index for index in range(1, 81)]
    )
    return make_bars(closes)


def _production_engine() -> BacktestEngine:
    engine, _, _ = make_backtest(
        strategy=build_default_registry().create("ema_trend", {}),
        features=ExponentialMovingAverageFeatures([20, 50]),
    )
    return engine


def test_the_builtin_strategy_runs_the_whole_chain_deterministically() -> None:
    # The production strategy against the production engine. Asserts the run happens and
    # repeats exactly; it asserts nothing about whether the rule makes money.
    bars = _trend_bars()

    first = _production_engine().run(bars)
    second = _production_engine().run(bars)

    assert first.bars_processed == len(bars)
    assert first.signals != ()
    assert [signal.action for signal in first.signals] == [
        signal.action for signal in second.signals
    ]
    assert [signal.signal_id for signal in first.signals] == [
        signal.signal_id for signal in second.signals
    ]


def test_the_builtin_strategy_signals_an_entry_once_the_fast_average_leads() -> None:
    result = _production_engine().run(_trend_bars())

    assert SignalAction.ENTER_LONG in [signal.action for signal in result.signals]


def test_the_builtin_strategy_stays_silent_through_a_flat_market() -> None:
    result = _production_engine().run(make_bars([Decimal(50_000)] * 120))

    assert result.signals == ()
    assert result.fills == ()


def test_symbol_rules_older_than_a_day_stop_every_order() -> None:
    # The risk engine refuses an intent once the venue's rules are more than
    # `stale_symbol_rules_seconds` old, and this fixture's rules are stamped at the first bar
    # and never refreshed. A run spanning more than a day therefore trades nothing.
    #
    # Kept deliberately, as the control case for the refresh mechanism rather than as an open
    # defect. Orchestration now re-fetches the rules on a schedule
    # (`tests/integration/test_symbol_rules_refresh.py` runs seven simulated days without a
    # single staleness refusal); this asserts the check it relies on is still doing its job
    # when nothing refreshes. Weakening the check would make both tests meaningless — this
    # one by removing the refusal, that one by proving nothing.
    result = _production_engine().run(_trend_bars())

    assert result.signals != ()
    assert result.fills == ()
    stale = [
        check
        for decision in result.rejected_decisions
        for check in decision.checks
        if check.code is RiskCheckCode.SYMBOL_RULES_FRESHNESS
        and check.status is RiskCheckStatus.FAILED
    ]
    assert stale, "expected the staleness check to be the refusal reason"


# --- The account is the only source of opening equity --------------------------------------
#
# `BacktestConfig.initial_capital` used to anchor the drawdown while the portfolio held
# whatever it had actually been seeded with. When a composition root declared ten thousand
# and seeded nothing, `_record_equity` kept `max(peak, equity)` at ten thousand against a
# real equity of zero, and day one of a paper run published a 100% drawdown and a
# ten-thousand loss that never happened. The run now reads the account.


def _engine_with(cash: Decimal) -> tuple[BacktestEngine, SpotPortfolioEngine]:
    engine, _, portfolio = make_backtest(strategy=Silent(_Params()), cash=cash)
    return engine, portfolio


def test_the_run_opens_from_the_account_not_from_configuration() -> None:
    engine, portfolio = _engine_with(Decimal(25_000))

    state = engine.begin()

    assert state.initial_equity == Decimal(25_000)
    assert state.peak_equity == Decimal(25_000)
    assert state.day_start_equity == Decimal(25_000)
    assert portfolio.balances()[0].total == Decimal(25_000)


def test_an_untouched_run_reports_no_drawdown_and_no_loss() -> None:
    engine, _ = _engine_with(Decimal(10_000))

    result = engine.run(_flat_bars(5))

    assert result.performance is not None
    assert result.performance.initial_equity == Decimal(10_000)
    assert result.performance.final_equity == Decimal(10_000)
    assert result.performance.total_return == Decimal(0)
    assert result.performance.max_drawdown == Decimal(0)
    assert all(point.drawdown == Decimal(0) for point in result.equity_curve)


def test_the_first_bar_can_never_show_a_drawdown() -> None:
    # The peak is anchored to what the account actually held, so the opening bar compares
    # equity against itself. A peak taken from configuration could exceed it from the start.
    engine, _ = _engine_with(Decimal(10_000))

    result = engine.run(_flat_bars(1))

    assert result.equity_curve[0].drawdown == Decimal(0)


def test_a_run_against_an_unfunded_account_is_refused_rather_than_going_quiet() -> None:
    # Configuration cannot express this — `initial_capital` is validated strictly positive
    # in both `BacktestConfig` and settings. The failure was a declared capital that never
    # reached the account, so the account is what has to be starved to reproduce it.
    #
    # Silence was the real damage: no intent, no decision, no rejection reason, and a report
    # with nothing to show but green checks.
    rules = {SYMBOL: make_symbol_rules()}
    reference, broker, _ = make_backtest(strategy=Silent(_Params()))
    starved = SpotPortfolioEngine(
        quote_asset=_USDT,
        symbols=rules,
        execution_mode=ExecutionMode.BACKTEST,
        initial_balances=(),
    )
    engine = BacktestEngine(
        config=make_backtest_config(),
        strategy=Silent(_Params()),
        features=NullFeaturePipeline(),
        risk_engine=reference._risk,
        broker=broker,
        portfolio=starved,
        symbols=rules,
    )

    with pytest.raises(ConfigurationError, match="holds no equity"):
        engine.begin()


def test_moving_the_configured_capital_moves_the_reported_opening_equity() -> None:
    # The two are one number. If they could drift, this would pass with either value.
    for cash in (Decimal(5_000), Decimal(50_000)):
        engine, _ = _engine_with(cash)

        result = engine.run(_flat_bars(3))

        assert result.performance is not None
        assert result.performance.initial_equity == cash
        assert result.config.initial_capital == cash


# --- M5b: position risk state reconstructed from real fills -------------------------------------
#
# The stop travelled as far as the approved order in M5a and stopped there. It now reaches the
# position it protects, carrying what that position genuinely risks — computed from the fill
# that actually happened and the size that actually remains, never from what was requested.
#
# Nothing here enforces anything. A stop recorded against a position is still metadata; the
# broker matches exactly as it did. What changes is that the platform can now answer "what is
# this position protected by, and how much is at stake?" — a question week 5 could not answer
# about the position it held for four days.


def _v2_backtest(
    **overrides: object,
) -> tuple[BacktestEngine, SimulatedBroker, SpotPortfolioEngine]:
    """Wire a backtest whose risk engine derives stops and sizes from a risk budget."""
    defaults: dict[str, object] = {
        "risk_budget": RiskBudget(
            risk_per_trade_pct=Decimal("0.01"),
            max_position_exposure_pct=Decimal("1"),
            min_stop_distance_bps=Decimal(1),
            max_stop_distance_bps=Decimal(10_000),
        ),
        "initial_stop_distance_bps": Decimal(200),
    }
    return make_backtest(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_a_v1_run_records_no_position_risk() -> None:
    # No stop is derived, so nothing is protected and nothing claims to be. An empty mapping
    # rather than entries reporting nothing: a record that cannot say what it protects is the
    # failure this whole layer exists to prevent.
    engine, _, _ = make_backtest(strategy=BuyOnce(_Params()))
    state = engine.begin()

    for bar in _flat_bars(_WARMUP_BARS + 2):
        engine.advance(bar, state)

    assert state.position_risk == {}


def test_a_protected_entry_records_what_it_risks() -> None:
    engine, _, portfolio = _v2_backtest(strategy=BuyOnce(_Params()))
    state = engine.begin()

    for bar in _flat_bars(_WARMUP_BARS + 2):
        engine.advance(bar, state)

    risk = state.position_risk[SYMBOL]
    position = portfolio.positions()[0]
    assert risk.symbol == SYMBOL
    assert risk.risk_amount > Decimal(0)
    assert risk.entry_price == position.avg_entry_price


def test_the_recorded_risk_uses_the_real_fill_not_the_requested_size() -> None:
    # The distinction the milestone turns on. Sizing produced a number before execution;
    # what is persisted must describe the position that exists, after every cap, rounding
    # and fill price the run actually produced.
    engine, _, portfolio = _v2_backtest(strategy=BuyOnce(_Params()))
    state = engine.begin()

    for bar in _flat_bars(_WARMUP_BARS + 2):
        engine.advance(bar, state)

    risk = state.position_risk[SYMBOL]
    position = portfolio.positions()[0]
    assert risk.quantity == position.quantity
    assert risk.entry_price == position.avg_entry_price


def test_the_recorded_stop_is_the_one_the_order_was_approved_under() -> None:
    engine, _, _ = _v2_backtest(strategy=BuyOnce(_Params()))
    state = engine.begin()

    for bar in _flat_bars(_WARMUP_BARS + 2):
        engine.advance(bar, state)

    approved = next(order for order in state.approved if order.protective_stop is not None)
    assert state.position_risk[SYMBOL].stop == approved.protective_stop


def test_closing_a_position_removes_its_risk_state() -> None:
    # A flat position is protected by nothing, and must not go on claiming otherwise.
    engine, _, _ = _v2_backtest(strategy=BuyThenSell(_Params()))
    state = engine.begin()

    for bar in _flat_bars(_WARMUP_BARS + 4):
        engine.advance(bar, state)

    assert SYMBOL not in state.position_risk


def test_risk_state_survives_a_snapshot_round_trip() -> None:
    engine, _, _ = _v2_backtest(strategy=BuyOnce(_Params()))
    state = engine.begin()

    for bar in _flat_bars(_WARMUP_BARS + 2):
        engine.advance(bar, state)

    risk = state.position_risk[SYMBOL]
    restored = PositionRiskState.model_validate_json(risk.model_dump_json())
    assert restored == risk


class BuyTwice(BaseStrategy):
    """Enters twice regardless of position state, which no shipped strategy does.

    Exists solely to reach the scale-in path: every strategy the platform ships gates its
    entry on being flat, so the ambiguity this test pins cannot occur in production today —
    and that is exactly why it needs a deliberate way to be reached before one can.
    """

    METADATA: ClassVar[StrategyMetadata] = StrategyMetadata(
        strategy_id="buy_twice",
        version="1.0.0",
        name="Buy twice",
        description="Enters on two bars, ignoring position state.",
        required_history=_WARMUP_BARS,
        required_features=(),
        supported_timeframes=(Timeframe.H1,),
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=_Params,
        operates_intrabar=False,
        allows_short=False,
    )

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Return an entry on the warm-up bar and on the one after it."""
        if context.history_length in (_WARMUP_BARS, _WARMUP_BARS + 1):
            return (
                self.build_signal(
                    context=context,
                    action=SignalAction.ENTER_LONG,
                    confidence=Decimal("0.6"),
                    reason="scale in",
                ),
            )
        return ()


def test_a_scale_in_with_a_different_stop_fails_loudly() -> None:
    # No combination policy is invented, and none is guessed at. Two entries protected at
    # different levels leave the platform unable to say what the combined position is
    # protected by — and a position that exists while the system has lost track of its
    # protection is worse than a run that stops. Prevention before the fill would be better
    # still, but belongs with whatever introduces a scale-in strategy; today no shipped
    # strategy can reach this, so the reconciliation check is what stands guard.
    engine, _, _ = _v2_backtest(
        strategy=BuyTwice(_Params()), max_open_positions=2, max_open_orders=2
    )
    state = engine.begin()
    # Prices move between the two entries so risk derives a different stop for each, but
    # by less than the first order's market-buy cap, so that order still fills rather than
    # being cancelled for breaching it.
    prices = [Decimal(50_000)] * _WARMUP_BARS + [Decimal(51_000)] * 3
    bars = make_bars(prices)

    def _drive() -> None:
        for bar in bars:
            engine.advance(bar, state)

    with pytest.raises(PositionRiskAmbiguityError, match="differ"):
        _drive()


# --- M6: the stop stops being metadata ----------------------------------------------------------
#
# Every milestone so far recorded protection without ever applying it. The engine now reads each
# closed bar against what its open positions are protected by, and turns a breach into an ordinary
# exit intent — assessed by the same risk engine, submitted to the same broker, filled on the next
# bar at that bar's open. Nothing fills at the stop price, because a stop is a trigger and not a
# guarantee of where the market will be when the order reaches it.


_STOP_DISTANCE_BPS = Decimal(200)
"""2% below entry: the stop a ``_v2_backtest`` derives, so 50,000 is protected at 49,000."""


def _crash_bars() -> tuple[MarketBar, ...]:
    """Warm up, enter, fill, then fall clean through the stop and stay there.

    The last bar exists so the exit the crash authorises has somewhere to settle: the fill
    lands at *its* open, one bar after the trigger.
    """
    return make_bars([Decimal(50_000)] * 4 + [Decimal(48_000)] * 2)


def _drive(engine: BacktestEngine, bars: Sequence[MarketBar]) -> RunState:
    state = engine.begin()
    for bar in bars:
        engine.advance(bar, state)
    return state


def test_a_position_that_falls_through_its_stop_is_closed() -> None:
    engine, _, portfolio = _v2_backtest(strategy=BuyOnce(_Params()))

    _drive(engine, _crash_bars())

    assert [p for p in portfolio.positions() if p.is_open] == []


def test_the_forced_exit_fills_at_the_next_bar_open_not_at_the_stop() -> None:
    # The honest half of the model. A gap through the stop costs what the next open costs;
    # filling at the trigger price would invent liquidity that was never there and would
    # report a loss smaller than the one the account actually took.
    engine, _, _ = _v2_backtest(strategy=BuyOnce(_Params()))

    state = _drive(engine, _crash_bars())

    exit_fill = next(fill for fill in state.fills if fill.side is OrderSide.SELL)
    assert exit_fill.price < Decimal(49_000)


def test_the_forced_exit_goes_through_the_ordinary_broker() -> None:
    # There is no second execution path inside risk. The exit is an order like any other,
    # which is what keeps fees, slippage and rejection modelled once rather than twice.
    engine, broker, _ = _v2_backtest(strategy=BuyOnce(_Params()))

    state = _drive(engine, _crash_bars())

    assert any(order.side is OrderSide.SELL for order in state.approved)
    assert broker.open_orders() == ()


def test_the_closed_position_stops_claiming_protection() -> None:
    engine, _, _ = _v2_backtest(strategy=BuyOnce(_Params()))

    state = _drive(engine, _crash_bars())

    assert SYMBOL not in state.position_risk


def test_a_position_that_holds_above_its_stop_is_left_alone() -> None:
    # The control. Without it every one of these tests would also pass for an engine that
    # closed positions indiscriminately.
    engine, _, portfolio = _v2_backtest(strategy=BuyOnce(_Params()))

    _drive(engine, make_bars([Decimal(50_000)] * 4 + [Decimal(49_500)] * 2))

    assert [p.symbol for p in portfolio.positions() if p.is_open] == [SYMBOL]


def test_a_v1_run_is_unaffected_by_the_same_crash() -> None:
    # No stop was ever derived, so there is nothing to breach. This is the behaviour every
    # completed run of this platform has had, and the crash must not change it.
    engine, _, portfolio = make_backtest(strategy=BuyOnce(_Params()))

    state = _drive(engine, _crash_bars())

    assert [p.symbol for p in portfolio.positions() if p.is_open] == [SYMBOL]
    assert state.position_risk == {}


# --- M7a: under Risk V2, protection is not optional ---------------------------------------------
#
# M6 built the enforcement and left it switched off: `require_protection` was hard-wired to
# False, so the fail-loudly path existed and nothing reached it. The switch is now the
# configuration itself — a run with a risk budget is a run where every open position must be
# accounted for — and V1 is untouched, which is what keeps the week-5 benchmark comparable.


def test_a_v2_run_with_every_position_protected_proceeds_normally() -> None:
    engine, _, portfolio = _v2_backtest(strategy=BuyOnce(_Params()))

    state = _drive(engine, _flat_bars(_WARMUP_BARS + 3))

    assert [p.symbol for p in portfolio.positions() if p.is_open] == [SYMBOL]
    assert SYMBOL in state.position_risk


def test_a_v2_run_that_loses_a_position_s_protection_stops() -> None:
    # Staged by removing the record rather than by finding a path that produces the loss,
    # because no such path is known — which is the point. The engine must detect the state
    # it cannot explain, not only the states it knows how to reach. A run that continued
    # here would hold an unprotected position while reporting a protected one.
    engine, _, _ = _v2_backtest(strategy=BuyOnce(_Params()))
    state = engine.begin()
    bars = _flat_bars(_WARMUP_BARS + 4)
    for bar in bars[:4]:
        engine.advance(bar, state)
    assert SYMBOL in state.position_risk
    del state.position_risk[SYMBOL]

    def _continue() -> None:
        for bar in bars[4:]:
            engine.advance(bar, state)

    with pytest.raises(PositionRiskUnavailableError, match="no recorded risk state"):
        _continue()


def test_a_v1_run_holds_an_unprotected_position_without_complaint() -> None:
    # The golden. Every completed run of this platform held exactly this: an open position,
    # no derived stop, no risk record, and no reason for any of that to be an error.
    engine, _, portfolio = make_backtest(strategy=BuyOnce(_Params()))

    state = _drive(engine, _flat_bars(_WARMUP_BARS + 3))

    assert [p.symbol for p in portfolio.positions() if p.is_open] == [SYMBOL]
    assert state.position_risk == {}


# --- M7b: the bar loop keeps its arithmetic when its order changes -------------------------------
#
# Circuit breakers must see a loss on the bar that realised it, not one bar later, which means
# closed trades and the equity anchors have to be updated before authorisation rather than after.
# That moves code which every performance number is derived from. These two goldens are pinned to
# literal values captured before the move: they exist to fail loudly if reordering the loop
# changes a single figure, and they are expected to pass both before and after — a golden that
# only passes afterwards would be a golden written to match the change.

_GOLDEN_POLICY_SLIPPAGE_BPS = Decimal(10)
_GOLDEN_POLICY_FEE_BPS = Decimal(20)


def _golden_backtest(strategy: BaseStrategy) -> BacktestEngine:
    """A run with real fees and real slippage, so the goldens have something to protect."""
    engine, _, _ = make_backtest(
        strategy=strategy,
        policy=make_execution_policy(
            slippage_bps=_GOLDEN_POLICY_SLIPPAGE_BPS,
            fee_model=CommissionModel.BASIS_POINTS,
            fee_basis_points=_GOLDEN_POLICY_FEE_BPS,
        ),
    )
    return engine


def test_golden_a_completed_round_trip_costs_exactly_what_it_costs() -> None:
    engine = _golden_backtest(BuyThenSell(_Params()))
    prices = [Decimal(50_000)] * _WARMUP_BARS + [
        Decimal(51_000),
        Decimal(52_000),
        Decimal(49_000),
        Decimal(50_500),
    ]

    result = engine.run(make_bars(prices))

    assert result.bars_processed == 7
    assert len(result.fills) == 2
    assert len(result.orders) == 2
    assert len(result.signals) == 2
    performance = result.performance
    assert performance is not None
    assert performance.final_equity == Decimal("101250.38193904")
    assert performance.total_return == Decimal("0.0125038193904")
    assert performance.max_drawdown == Decimal("0.0027704113104")
    assert performance.commission_paid == Decimal("372.75750096")
    assert performance.slippage_paid == Decimal("186.38056")
    assert (performance.trades.count, performance.trades.wins, performance.trades.losses) == (
        1,
        1,
        0,
    )
    assert performance.trades.gross_profit == Decimal("1250.38193904")
    assert [(fill.side, str(fill.price), str(fill.quantity)) for fill in result.fills] == [
        (OrderSide.BUY, "51051", "1.80952"),
        (OrderSide.SELL, "51948", "1.80952"),
    ]
    assert [str(point.equity) for point in result.equity_curve] == [
        "100000",
        "100000",
        "100000.0000000",
        "99722.95886896",
        "101250.38193904",
        "101250.38193904",
        "101250.38193904",
    ]


def test_golden_an_open_position_draws_the_account_down_by_exactly_this_much() -> None:
    # The complement: nothing closes, so every figure comes from marking an open position.
    # Between them the two goldens cover realised and unrealised, closed and open.
    engine = _golden_backtest(BuyOnce(_Params()))
    prices = [Decimal(50_000)] * _WARMUP_BARS + [
        Decimal(50_100),
        Decimal(50_200),
        Decimal(48_000),
        Decimal(47_000),
    ]

    result = engine.run(make_bars(prices))

    assert len(result.fills) == 1
    performance = result.performance
    assert performance is not None
    assert performance.final_equity == Decimal("94118.335830096")
    assert performance.total_return == Decimal("-0.05881664169904")
    assert performance.max_drawdown == Decimal("0.05881664169904")
    assert performance.commission_paid == Decimal("181.495217904")
    assert performance.trades.count == 0
    assert [str(point.drawdown) for point in result.equity_curve] == [
        "0",
        "0",
        "0E-7",
        "0.00272152169904",
        "0.00091200169904",
        "0.04072144169904",
        "0.05881664169904",
    ]


# --- M7b: the latches, driven by the run's own arithmetic ---------------------------------------
#
# The engine owns the state and risk only reads it, so the trigger is computed where the equity
# and the closed trades already live. What each breaker measures is deliberately different: the
# daily limit counts money booked, the drawdown measures marked equity against its own high, and
# the streak counts outcomes. An account can fail any one without failing the others.


def _breaker_backtest(**overrides: object) -> tuple[BacktestEngine, SpotPortfolioEngine]:
    engine, _, portfolio = make_backtest(
        policy=make_execution_policy(
            slippage_bps=Decimal(10),
            fee_model=CommissionModel.BASIS_POINTS,
            fee_basis_points=Decimal(20),
        ),
        **overrides,  # type: ignore[arg-type]
    )
    return engine, portfolio


class LoseRepeatedly(BaseStrategy):
    """Enters whenever flat and exits whenever long, so a falling tape loses over and over.

    No shipped strategy produces a streak on demand, and the consecutive-losses breaker
    cannot be tested without one.
    """

    METADATA: ClassVar[StrategyMetadata] = _metadata("lose_repeatedly")

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        if context.history_length < _WARMUP_BARS:
            return ()
        action = (
            SignalAction.EXIT_LONG
            if context.position_state is PositionState.LONG
            else SignalAction.ENTER_LONG
        )
        return (
            self.build_signal(
                context=context, action=action, confidence=Decimal("0.9"), reason="cycle"
            ),
        )


def _losing_streak_bars() -> tuple[MarketBar, ...]:
    """A tape that falls gently enough to fill every order it triggers."""
    prices = [Decimal(50_000)] * _WARMUP_BARS + [
        Decimal(50_000) - Decimal(200) * index for index in range(12)
    ]
    return make_bars(prices)


def _day_one_loss_bars() -> tuple[MarketBar, ...]:
    return make_bars(
        [Decimal(50_000)] * _WARMUP_BARS
        + [Decimal(50_100), Decimal(48_000), Decimal(48_000), Decimal(48_000)]
    )


def _next_day_bars(price: Decimal = Decimal(48_000)) -> tuple[MarketBar, ...]:
    """Bars on the following calendar day, which is what a rollover is measured by."""
    return tuple(make_bar(index=index, close=price) for index in range(26, 29))


def _reasons(state: RunState) -> set[CircuitBreakerReason]:
    return {breaker.reason for breaker in state.breakers if breaker.reason is not None}


def test_a_run_with_no_breaker_configured_latches_nothing() -> None:
    # The golden. Every completed run of this platform is this run.
    engine, _ = _breaker_backtest(strategy=BuyThenSell(_Params()))

    state = _drive(engine, make_bars([Decimal(50_000)] * _WARMUP_BARS + [Decimal(45_000)] * 4))

    assert state.breakers == []


def test_a_realised_loss_past_the_daily_limit_latches() -> None:
    engine, _ = _breaker_backtest(
        strategy=BuyThenSell(_Params()), max_daily_loss_pct=Decimal("0.001")
    )

    state = _drive(
        engine,
        make_bars(
            [Decimal(50_000)] * _WARMUP_BARS
            + [Decimal(50_100), Decimal(48_000)]
            + [Decimal(48_000)] * 2
        ),
    )

    assert CircuitBreakerReason.DAILY_LOSS_LIMIT in _reasons(state)


def test_a_realised_loss_inside_the_daily_limit_does_not_latch() -> None:
    engine, _ = _breaker_backtest(
        strategy=BuyThenSell(_Params()), max_daily_loss_pct=Decimal("0.90")
    )

    state = _drive(
        engine,
        make_bars(
            [Decimal(50_000)] * _WARMUP_BARS
            + [Decimal(50_100), Decimal(48_000)]
            + [Decimal(48_000)] * 2
        ),
    )

    assert CircuitBreakerReason.DAILY_LOSS_LIMIT not in _reasons(state)


def test_an_open_loss_does_not_latch_the_realised_daily_limit() -> None:
    # The distinction the two daily limits draw. Nothing has been booked, so a limit that
    # counts booked money must stay silent; the marked decline is the drawdown's business.
    engine, _ = _breaker_backtest(strategy=BuyOnce(_Params()), max_daily_loss_pct=Decimal("0.001"))

    state = _drive(
        engine,
        make_bars([Decimal(50_000)] * _WARMUP_BARS + [Decimal(50_100)] + [Decimal(40_000)] * 3),
    )

    assert CircuitBreakerReason.DAILY_LOSS_LIMIT not in _reasons(state)


def test_a_marked_decline_past_the_drawdown_limit_latches() -> None:
    engine, _ = _breaker_backtest(
        strategy=BuyOnce(_Params()),
        latch_total_drawdown=True,
        max_total_drawdown_pct=Decimal("0.05"),
    )

    state = _drive(
        engine,
        make_bars([Decimal(50_000)] * _WARMUP_BARS + [Decimal(50_100)] + [Decimal(40_000)] * 3),
    )

    assert CircuitBreakerReason.EXCESSIVE_DRAWDOWN in _reasons(state)


def test_a_streak_of_losses_latches_on_the_configured_count() -> None:
    engine, _ = _breaker_backtest(strategy=LoseRepeatedly(_Params()), max_consecutive_losses=2)

    state = _drive(engine, _losing_streak_bars())

    assert CircuitBreakerReason.CONSECUTIVE_LOSSES in _reasons(state)


def test_a_streak_one_short_of_the_limit_does_not_latch() -> None:
    engine, _ = _breaker_backtest(strategy=LoseRepeatedly(_Params()), max_consecutive_losses=9)

    state = _drive(engine, _losing_streak_bars())

    assert CircuitBreakerReason.CONSECUTIVE_LOSSES not in _reasons(state)


# --- Reset: only the daily limit is daily --------------------------------------------------------


def test_the_daily_loss_latch_clears_at_the_day_rollover() -> None:
    # A limit that never resets is not a daily limit; it is a total one wearing the wrong
    # name. This is the single exception to a breaker not clearing itself, and it is the
    # metric's own definition rather than the process deciding things have improved.
    engine, _ = _breaker_backtest(
        strategy=BuyThenSell(_Params()), max_daily_loss_pct=Decimal("0.001")
    )
    state = _drive(engine, _day_one_loss_bars())
    assert CircuitBreakerReason.DAILY_LOSS_LIMIT in _reasons(state)

    for bar in _next_day_bars():
        engine.advance(bar, state)

    assert CircuitBreakerReason.DAILY_LOSS_LIMIT not in _reasons(state)


def test_the_drawdown_latch_survives_the_day_rollover() -> None:
    engine, _ = _breaker_backtest(
        strategy=BuyOnce(_Params()),
        latch_total_drawdown=True,
        max_total_drawdown_pct=Decimal("0.05"),
    )
    state = _drive(
        engine,
        make_bars([Decimal(50_000)] * _WARMUP_BARS + [Decimal(50_100)] + [Decimal(40_000)] * 3),
    )
    assert CircuitBreakerReason.EXCESSIVE_DRAWDOWN in _reasons(state)

    for bar in _next_day_bars(price=Decimal(40_000)):
        engine.advance(bar, state)

    assert CircuitBreakerReason.EXCESSIVE_DRAWDOWN in _reasons(state)


def test_the_streak_latch_survives_the_day_rollover() -> None:
    engine, _ = _breaker_backtest(strategy=LoseRepeatedly(_Params()), max_consecutive_losses=2)
    state = _drive(engine, _losing_streak_bars())
    assert CircuitBreakerReason.CONSECUTIVE_LOSSES in _reasons(state)

    for bar in _next_day_bars():
        engine.advance(bar, state)

    assert CircuitBreakerReason.CONSECUTIVE_LOSSES in _reasons(state)


def test_a_loss_realised_on_a_bar_gates_that_bar_rather_than_the_next() -> None:
    # Why the loop is reordered. Closed trades and the equity anchors used to be updated
    # after authorisation, so a breaker fed from them arrived one bar late and the entry
    # placed on the very bar that broke the limit went through. One bar is one more position
    # opened by an account that had already been told to stop.
    engine, _ = _breaker_backtest(
        strategy=LoseRepeatedly(_Params()), max_daily_loss_pct=Decimal("0.001")
    )
    bars = _losing_streak_bars()
    state = engine.begin()

    latched_at: int | None = None
    entered_after_latch = 0
    for index, bar in enumerate(bars):
        outcome = engine.advance(bar, state)
        if latched_at is None and state.breakers:
            latched_at = index
            continue
        if latched_at is not None:
            entered_after_latch += sum(
                1 for order in outcome.submitted if order.side is OrderSide.BUY
            )

    assert latched_at is not None
    assert entered_after_latch == 0


# --- M8a: the protective level moves, and never in time to judge its own bar --------------------
#
# A trailing level computed from a bar's own high was not in the market during the part of that
# bar which preceded the high. These tests hold the ordering that makes that true: the trigger is
# evaluated against the level as the bar opened, and whatever the bar produces governs from the
# next one onward. Structurally it is guaranteed rather than checked — `evaluate_open_positions`
# reads the state and `_advance_position_risk` writes it, strictly in that order within a bar —
# but a structure nobody pinned is a structure that gets reordered.


def _m8a_backtest(**overrides: object) -> tuple[BacktestEngine, SpotPortfolioEngine]:
    engine, _, portfolio = make_backtest(
        strategy=BuyOnce(_Params()),
        risk_budget=RiskBudget(
            risk_per_trade_pct=Decimal("0.01"),
            max_position_exposure_pct=Decimal("1"),
            min_stop_distance_bps=Decimal(1),
            max_stop_distance_bps=Decimal(10_000),
        ),
        initial_stop_distance_bps=Decimal(200),
        **overrides,  # type: ignore[arg-type]
    )
    return engine, portfolio


def test_a_trailing_level_raised_by_a_bar_does_not_fire_on_that_same_bar() -> None:
    # The bar rallies to 52 000 and closes back at 50 400. A trailing stop 200 bps under the
    # new high sits at 50 960, above that low — so applying it retroactively would close the
    # position on the very bar that created the level, at a price the account never had.
    engine, portfolio = _m8a_backtest(
        trailing_activation_bps=Decimal(100), trailing_distance_bps=Decimal(200)
    )
    state = engine.begin()
    for bar in _flat_bars(_WARMUP_BARS + 1):
        engine.advance(bar, state)
    rally = make_bar(
        index=_WARMUP_BARS + 1,
        open_price=Decimal(50_000),
        high=Decimal(52_000),
        low=Decimal(49_900),
        close=Decimal(50_400),
    )

    engine.advance(rally, state)

    assert [p.symbol for p in portfolio.positions() if p.is_open] == [SYMBOL]
    assert state.position_risk[SYMBOL].stop.trigger_price == Decimal(52_000) * Decimal("0.98")


def test_the_level_raised_by_one_bar_does_fire_on_the_next() -> None:
    # The other half. Without it the previous test would also pass for an engine that simply
    # never enforced a trailing stop at all.
    engine, portfolio = _m8a_backtest(
        trailing_activation_bps=Decimal(100), trailing_distance_bps=Decimal(200)
    )
    state = engine.begin()
    for bar in _flat_bars(_WARMUP_BARS + 1):
        engine.advance(bar, state)
    rally = make_bar(
        index=_WARMUP_BARS + 1,
        open_price=Decimal(50_000),
        high=Decimal(52_000),
        low=Decimal(49_900),
        close=Decimal(50_400),
    )
    engine.advance(rally, state)

    for index in (_WARMUP_BARS + 2, _WARMUP_BARS + 3):
        engine.advance(make_bar(index=index, close=Decimal(50_000)), state)

    assert [p for p in portfolio.positions() if p.is_open] == []


def test_the_anchor_survives_a_fill() -> None:
    # D1. The risk state is rebuilt from the position after every fill, and the rebuild used
    # to drop the anchor. Nothing noticed because nothing wrote it; a trailing stop would
    # have restarted from entry on every fill and trailed only the bars since the last one.
    engine, _ = _m8a_backtest(
        trailing_activation_bps=Decimal(100),
        trailing_distance_bps=Decimal(200),
        max_open_positions=2,
        max_open_orders=2,
    )
    state = engine.begin()
    for bar in _flat_bars(_WARMUP_BARS + 1):
        engine.advance(bar, state)
    engine.advance(
        make_bar(
            index=_WARMUP_BARS + 1,
            open_price=Decimal(50_000),
            high=Decimal(52_000),
            low=Decimal(49_900),
            close=Decimal(50_400),
        ),
        state,
    )
    anchored = state.position_risk[SYMBOL].highest_price_seen
    assert anchored == Decimal(52_000)

    engine.advance(make_bar(index=_WARMUP_BARS + 2, close=Decimal(50_500)), state)

    assert state.position_risk[SYMBOL].highest_price_seen == anchored


def test_moving_the_stop_preserves_when_the_position_opened() -> None:
    engine, _ = _m8a_backtest(
        trailing_activation_bps=Decimal(100), trailing_distance_bps=Decimal(200)
    )
    state = engine.begin()
    for bar in _flat_bars(_WARMUP_BARS + 1):
        engine.advance(bar, state)
    opened_at = state.position_risk[SYMBOL].opened_at

    engine.advance(
        make_bar(
            index=_WARMUP_BARS + 1,
            open_price=Decimal(50_000),
            high=Decimal(52_000),
            low=Decimal(49_900),
            close=Decimal(50_400),
        ),
        state,
    )

    assert state.position_risk[SYMBOL].stop.kind is StopKind.TRAILING
    assert state.position_risk[SYMBOL].opened_at == opened_at


def test_a_latched_breaker_does_not_stop_a_trailing_exit() -> None:
    engine, portfolio = _m8a_backtest(
        trailing_activation_bps=Decimal(100),
        trailing_distance_bps=Decimal(200),
        latch_total_drawdown=True,
        max_total_drawdown_pct=Decimal("0.001"),
        max_daily_drawdown_pct=Decimal("0.001"),
    )
    state = engine.begin()
    for bar in _flat_bars(_WARMUP_BARS + 1):
        engine.advance(bar, state)
    engine.advance(
        make_bar(
            index=_WARMUP_BARS + 1,
            open_price=Decimal(50_000),
            high=Decimal(52_000),
            low=Decimal(49_900),
            close=Decimal(50_400),
        ),
        state,
    )

    for index in (_WARMUP_BARS + 2, _WARMUP_BARS + 3):
        engine.advance(make_bar(index=index, close=Decimal(50_000)), state)

    assert state.breakers != []
    assert [p for p in portfolio.positions() if p.is_open] == []


def test_a_v1_run_moves_no_stop_because_it_has_none() -> None:
    engine, _, portfolio = make_backtest(
        strategy=BuyOnce(_Params()),
        trailing_activation_bps=Decimal(100),
        trailing_distance_bps=Decimal(200),
    )
    state = engine.begin()

    for index in range(_WARMUP_BARS + 4):
        engine.advance(make_bar(index=index, close=Decimal(50_000) + Decimal(100) * index), state)

    assert state.position_risk == {}
    assert [p.symbol for p in portfolio.positions() if p.is_open] == [SYMBOL]
