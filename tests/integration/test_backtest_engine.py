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

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.enums import (
    CommissionModel,
    ExecutionMode,
    MarketType,
    OrderSide,
    PositionState,
    RiskCheckCode,
    RiskCheckStatus,
    SignalAction,
    Timeframe,
)
from quantplatform.core.errors import (
    ConfigurationError,
    DataIntegrityError,
    PositionRiskAmbiguityError,
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
