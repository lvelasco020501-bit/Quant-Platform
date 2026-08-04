"""Phase 5 unit tests: features, intent building, metrics and configuration."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.backtesting.intents import build_intent
from quantplatform.backtesting.metrics import (
    EquityPoint,
    TradeStatistics,
    compute_performance,
)
from quantplatform.core.enums import (
    ExecutionMode,
    MarketType,
    OrderSide,
    SignalAction,
    Timeframe,
)
from quantplatform.core.errors import ConfigurationError, UnsupportedRiskInputError
from quantplatform.core.models.signals import Signal
from quantplatform.features import (
    CompositeFeaturePipeline,
    MovingAverageFeatures,
    NullFeaturePipeline,
)
from tests.factories import ANCHOR, SYMBOL, make_bars, make_position, make_snapshot

# --- Feature pipelines ---------------------------------------------------------------------


def test_the_null_pipeline_produces_nothing() -> None:
    pipeline = NullFeaturePipeline()
    assert pipeline.feature_names == ()
    assert pipeline.compute(make_bars([Decimal(1), Decimal(2)])) == {}


def test_moving_averages_are_exact_decimals() -> None:
    pipeline = MovingAverageFeatures([2, 3])
    bars = make_bars([Decimal(10), Decimal(20), Decimal(30)])

    features = pipeline.compute(bars)

    assert features["close"] == Decimal(30)
    assert features["sma_2"] == Decimal(25)
    assert features["sma_3"] == Decimal(20)
    assert all(isinstance(value, Decimal) for value in features.values())


def test_a_feature_the_window_cannot_support_is_omitted_not_guessed() -> None:
    pipeline = MovingAverageFeatures([2, 5])
    features = pipeline.compute(make_bars([Decimal(10), Decimal(20)]))

    assert "sma_2" in features
    assert "sma_5" not in features


def test_moving_average_periods_are_deduplicated_and_sorted() -> None:
    assert MovingAverageFeatures([5, 2, 5]).periods == (2, 5)


@pytest.mark.parametrize("periods", [[], [0], [-1]])
def test_invalid_moving_average_periods_are_refused(periods: list[int]) -> None:
    with pytest.raises(ConfigurationError):
        MovingAverageFeatures(periods)


def test_a_composite_pipeline_merges_its_members() -> None:
    composite = CompositeFeaturePipeline([MovingAverageFeatures([2]), NullFeaturePipeline()])
    features = composite.compute(make_bars([Decimal(10), Decimal(20)]))

    assert features["sma_2"] == Decimal(15)
    assert composite.required_history == 2


def test_a_composite_pipeline_refuses_colliding_feature_names() -> None:
    with pytest.raises(ConfigurationError, match="same feature name"):
        CompositeFeaturePipeline([MovingAverageFeatures([2]), MovingAverageFeatures([2])])


def test_feature_computation_is_pure() -> None:
    pipeline = MovingAverageFeatures([3])
    bars = make_bars([Decimal(10), Decimal(20), Decimal(30)])
    assert pipeline.compute(bars) == pipeline.compute(bars)


# --- Intent building -------------------------------------------------------------------------


def _signal(action: SignalAction, symbol: str = SYMBOL) -> Signal:
    return Signal(
        signal_id=UUID(int=1),
        strategy_id="dummy_trend",
        strategy_version="1.0.0",
        symbol=symbol,
        market_type=MarketType.SPOT,
        timeframe=Timeframe.H1,
        bar_close_time=ANCHOR,
        generated_at=ANCHOR,
        action=action,
        confidence=Decimal("0.5"),
        reason="test",
    )


def test_a_hold_signal_produces_no_intent() -> None:
    intent = build_intent(
        _signal(SignalAction.HOLD),
        snapshot=make_snapshot(cash=Decimal(10_000)),
        entry_fraction=Decimal("0.9"),
        execution_mode=ExecutionMode.BACKTEST,
    )
    assert intent is None


def test_an_entry_asks_for_a_share_of_equity_as_notional() -> None:
    intent = build_intent(
        _signal(SignalAction.ENTER_LONG),
        snapshot=make_snapshot(cash=Decimal(10_000)),
        entry_fraction=Decimal("0.9"),
        execution_mode=ExecutionMode.BACKTEST,
    )
    assert intent is not None
    assert intent.side is OrderSide.BUY
    assert intent.requested_notional == Decimal(9_000)
    assert intent.requested_quantity is None


def test_an_exit_asks_to_close_exactly_what_is_held() -> None:
    snapshot = make_snapshot(
        cash=Decimal(1_000), positions=(make_position(quantity=Decimal("0.4")),)
    )
    intent = build_intent(
        _signal(SignalAction.EXIT_LONG),
        snapshot=snapshot,
        entry_fraction=Decimal("0.9"),
        execution_mode=ExecutionMode.BACKTEST,
    )
    assert intent is not None
    assert intent.side is OrderSide.SELL
    assert intent.requested_quantity == Decimal("0.4")


def test_an_exit_with_nothing_held_produces_no_intent() -> None:
    intent = build_intent(
        _signal(SignalAction.EXIT_LONG),
        snapshot=make_snapshot(cash=Decimal(10_000)),
        entry_fraction=Decimal("0.9"),
        execution_mode=ExecutionMode.BACKTEST,
    )
    assert intent is None


def test_a_short_signal_is_refused_rather_than_dropped() -> None:
    with pytest.raises(UnsupportedRiskInputError):
        build_intent(
            _signal(SignalAction.ENTER_SHORT),
            snapshot=make_snapshot(cash=Decimal(10_000)),
            entry_fraction=Decimal("0.9"),
            execution_mode=ExecutionMode.BACKTEST,
        )


def test_intent_identifiers_are_derived_not_random() -> None:
    signal = _signal(SignalAction.ENTER_LONG)
    snapshot = make_snapshot(cash=Decimal(10_000))
    first = build_intent(
        signal,
        snapshot=snapshot,
        entry_fraction=Decimal("0.9"),
        execution_mode=ExecutionMode.BACKTEST,
    )
    second = build_intent(
        signal,
        snapshot=snapshot,
        entry_fraction=Decimal("0.9"),
        execution_mode=ExecutionMode.BACKTEST,
    )
    assert first is not None
    assert second is not None
    assert first.intent_id == second.intent_id
    assert first.idempotency_key == second.idempotency_key


# --- Metrics -----------------------------------------------------------------------------------


def _curve(values: list[Decimal]) -> tuple[EquityPoint, ...]:
    if not values:
        return ()
    points = []
    peak = values[0]
    for index, value in enumerate(values):
        peak = max(peak, value)
        drawdown = (peak - value) / peak if peak > 0 else Decimal(0)
        points.append(
            EquityPoint(at=ANCHOR + timedelta(hours=index + 1), equity=value, drawdown=drawdown)
        )
    return tuple(points)


def _summary(values: list[Decimal], **overrides: object) -> object:
    defaults: dict[str, object] = {
        "curve": _curve(values),
        "initial_equity": Decimal(10_000),
        "realized_pnl": Decimal(0),
        "unrealized_pnl": Decimal(0),
        "commission_paid": Decimal(0),
        "slippage_paid": Decimal(0),
        "trades": TradeStatistics(
            count=0, wins=0, losses=0, gross_profit=Decimal(0), gross_loss=Decimal(0)
        ),
        "periods_per_year": Decimal(8_760),
        "risk_free_rate": Decimal(0),
        "minimum_periods_for_ratios": 2,
    }
    return compute_performance(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_total_return_is_measured_against_initial_equity() -> None:
    summary = _summary([Decimal(11_000)])
    assert summary.total_return == Decimal("0.1")  # type: ignore[attr-defined]


def test_max_drawdown_is_the_deepest_decline_from_the_peak() -> None:
    summary = _summary([Decimal(10_000), Decimal(12_000), Decimal(9_000), Decimal(11_000)])
    assert summary.max_drawdown == Decimal("0.25")  # type: ignore[attr-defined]


def test_an_empty_run_reports_nothing_computable_rather_than_zero() -> None:
    summary = _summary([])
    assert summary.final_equity == Decimal(10_000)  # type: ignore[attr-defined]
    assert summary.sharpe_ratio is None  # type: ignore[attr-defined]
    assert summary.sortino_ratio is None  # type: ignore[attr-defined]
    assert summary.cagr is None  # type: ignore[attr-defined]
    assert summary.max_drawdown == Decimal(0)  # type: ignore[attr-defined]


def test_ratios_are_not_computed_from_a_single_observation() -> None:
    summary = _summary([Decimal(10_500)])
    assert summary.sharpe_ratio is None  # type: ignore[attr-defined]
    assert summary.sortino_ratio is None  # type: ignore[attr-defined]


def test_a_flat_equity_curve_has_no_sharpe_rather_than_an_infinite_one() -> None:
    summary = _summary([Decimal(10_000), Decimal(10_000), Decimal(10_000)])
    assert summary.sharpe_ratio is None  # type: ignore[attr-defined]


def test_sortino_is_not_computed_when_nothing_ever_lost() -> None:
    summary = _summary([Decimal(10_100), Decimal(10_200), Decimal(10_300)])
    assert summary.sortino_ratio is None  # type: ignore[attr-defined]


def test_sharpe_is_computable_from_a_varying_curve() -> None:
    summary = _summary([Decimal(10_100), Decimal(10_050), Decimal(10_300)])
    assert summary.sharpe_ratio is not None  # type: ignore[attr-defined]
    assert isinstance(summary.sharpe_ratio, Decimal)  # type: ignore[attr-defined]


def test_cagr_is_not_computed_for_a_wiped_out_account() -> None:
    summary = _summary([Decimal(5_000), Decimal(0)])
    assert summary.cagr is None  # type: ignore[attr-defined]


def test_trade_statistics_leave_ratios_undefined_without_trades() -> None:
    stats = TradeStatistics(
        count=0, wins=0, losses=0, gross_profit=Decimal(0), gross_loss=Decimal(0)
    )
    assert stats.win_rate is None
    assert stats.profit_factor is None
    assert stats.expectancy is None


def test_zero_initial_equity_does_not_divide_by_zero() -> None:
    summary = _summary([Decimal(0)], initial_equity=Decimal(0))
    assert summary.total_return is None  # type: ignore[attr-defined]


@given(
    values=st.lists(
        st.decimals(min_value=Decimal(1), max_value=Decimal(100_000), places=2),
        min_size=1,
        max_size=20,
    )
)
def test_property_drawdown_is_always_between_zero_and_one(values: list[Decimal]) -> None:
    summary = _summary(values)
    assert Decimal(0) <= summary.max_drawdown <= Decimal(1)  # type: ignore[attr-defined]


@given(
    values=st.lists(
        st.decimals(min_value=Decimal(1), max_value=Decimal(100_000), places=2),
        min_size=1,
        max_size=12,
    )
)
def test_property_metrics_never_raise_on_any_curve(values: list[Decimal]) -> None:
    summary = _summary(values)
    assert summary.bars_processed == len(values)  # type: ignore[attr-defined]


# --- Configuration -------------------------------------------------------------------------------


def test_periods_per_year_follows_the_timeframe() -> None:
    assert BacktestConfig(timeframe=Timeframe.H1).periods_per_year == Decimal(8_760)
    assert BacktestConfig(timeframe=Timeframe.D1).periods_per_year == Decimal(365)


def test_a_run_without_capital_is_refused() -> None:
    with pytest.raises(ValueError, match="initial_capital"):
        BacktestConfig(initial_capital=Decimal(0))


def test_configuration_rejects_float_money() -> None:
    with pytest.raises(ValueError, match="binary floating point"):
        BacktestConfig(initial_capital=10_000.0)  # type: ignore[arg-type]
