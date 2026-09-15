"""The M15 protocol: timeframe-dependent risk, stated as explicit conversions before any run.

M14 reused 168 one-hour bars as 168 daily bars — a seven-day time stop became a 168-day one —
and a 300 bps stop that fits an hour's noise sat inside a single day's range. These tests pin
every conversion, and pin that *every* risk field is classified, so a field added later cannot
slip through unconverted and unnoticed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.orchestration.research import load_definition
from quantplatform.research.m15 import (
    DATA_END,
    DATA_START,
    MIN_PAPER_TRADES,
    MIN_TRADES_PER_TEST_WINDOW,
    POLICIES,
    RISK_CONVERSIONS,
    STUDY_STRATEGIES,
    YEARLY_WINDOWS,
    Conversion,
    cap_by_sample,
    risk_for_timeframe,
    study_definition,
    walk_forward_folds,
)
from quantplatform.research.sprint import Verdict
from quantplatform.risk.config import RiskConfiguration

DEPLOYED = Path(__file__).resolve().parents[1] / "fixtures" / "deployed_risk_v2_definition.json"


@pytest.fixture
def deployed() -> RiskConfiguration:
    return load_definition(DEPLOYED).risk


# --- Conversions -------------------------------------------------------------------------------


def test_one_hour_is_production_exactly(deployed: RiskConfiguration) -> None:
    assert risk_for_timeframe(deployed, Timeframe.H1) == deployed


def test_price_distances_scale_with_the_square_root_of_time(deployed: RiskConfiguration) -> None:
    h4 = risk_for_timeframe(deployed, Timeframe.H4)
    d1 = risk_for_timeframe(deployed, Timeframe.D1)
    bps = Decimal
    assert (h4.initial_stop_distance_bps, d1.initial_stop_distance_bps) == (bps(600), bps(1470))
    assert (h4.break_even_activation_bps, d1.break_even_activation_bps) == (bps(300), bps(735))
    assert (h4.trailing_activation_bps, d1.trailing_activation_bps) == (bps(600), bps(1470))
    assert (h4.trailing_distance_bps, d1.trailing_distance_bps) == (bps(400), bps(980))
    assert (h4.take_profit_distance_bps, d1.take_profit_distance_bps) == (bps(1200), bps(2939))
    h4_budget, d1_budget = h4.risk_budget, d1.risk_budget
    assert h4_budget is not None
    assert d1_budget is not None
    assert (h4_budget.min_stop_distance_bps, h4_budget.max_stop_distance_bps) == (
        bps(100),
        bps(2000),
    )
    assert (d1_budget.min_stop_distance_bps, d1_budget.max_stop_distance_bps) == (
        bps(245),
        bps(4899),
    )


def test_the_time_stop_keeps_its_time_not_its_bar_count(deployed: RiskConfiguration) -> None:
    # 168 hourly bars is seven days. Seven days is 42 four-hour bars and 7 daily ones — never
    # 168 daily bars, which would be more than five months.
    assert risk_for_timeframe(deployed, Timeframe.H4).max_holding_bars == 42
    assert risk_for_timeframe(deployed, Timeframe.D1).max_holding_bars == 7


def test_the_volatility_limit_scales_like_any_per_bar_deviation(
    deployed: RiskConfiguration,
) -> None:
    assert risk_for_timeframe(deployed, Timeframe.H4).max_volatility == Decimal("0.30")
    assert risk_for_timeframe(deployed, Timeframe.D1).max_volatility == Decimal("0.7348")


def test_sizing_stays_one_percent_of_equity_per_trade(deployed: RiskConfiguration) -> None:
    converted = risk_for_timeframe(deployed, Timeframe.D1).risk_budget
    production = deployed.risk_budget
    assert converted is not None
    assert production is not None
    assert converted.risk_per_trade_pct == production.risk_per_trade_pct
    assert converted.max_position_exposure_pct == production.max_position_exposure_pct


def test_every_field_not_converted_is_left_exactly_as_deployed(deployed: RiskConfiguration) -> None:
    d1 = risk_for_timeframe(deployed, Timeframe.D1)
    unchanged = {f for f, rule in RISK_CONVERSIONS.items() if rule is Conversion.UNCHANGED}
    for field in unchanged:
        assert getattr(d1, field) == getattr(deployed, field), field


def test_every_risk_field_is_classified_so_none_can_slip_through() -> None:
    assert set(RISK_CONVERSIONS) == set(RiskConfiguration.model_fields)


# --- Protocol ------------------------------------------------------------------------------------


def test_policies_are_production_drawdown_hybrid_and_the_reference() -> None:
    assert [p.key for p in POLICIES] == ["REF", "A", "C", "D"]


def test_the_study_covers_the_two_m14_survivors_and_both_benchmarks() -> None:
    assert [c.strategy_id for c in STUDY_STRATEGIES] == [
        "regime_trend",
        "rsi_reversal",
        "ema_trend_mtf",
        "breakout_mtf",
    ]


def test_yearly_windows_tile_the_whole_dataset() -> None:
    assert datetime(2020, 1, 1, tzinfo=UTC) == DATA_START
    assert datetime(2026, 9, 15, tzinfo=UTC) == DATA_END
    assert YEARLY_WINDOWS[0].start == DATA_START
    assert YEARLY_WINDOWS[-1].end == DATA_END
    assert len(YEARLY_WINDOWS) == 7
    for earlier, later in pairwise(YEARLY_WINDOWS):
        assert earlier.end == later.start


def test_walk_forward_trains_on_one_year_and_tests_the_next() -> None:
    folds = walk_forward_folds()
    assert len(folds) == 6
    for fold, (train, test) in zip(folds, pairwise(YEARLY_WINDOWS), strict=True):
        assert fold.train == train
        assert fold.test == test


def test_a_study_definition_carries_its_timeframes_converted_risk() -> None:
    base = load_definition(DEPLOYED)
    c = next(p for p in POLICIES if p.key == "C")
    definition = study_definition(
        STUDY_STRATEGIES[0], base=base, timeframe=Timeframe.D1, policy=c, window=None
    )
    assert definition.dataset.timeframe is Timeframe.D1
    assert definition.dataset.start == DATA_START
    assert definition.dataset.end == DATA_END
    assert definition.risk.initial_stop_distance_bps == 1470
    assert definition.risk.max_consecutive_losses is None
    assert definition.risk.latch_total_drawdown is True
    assert definition.risk.max_total_drawdown_pct == Decimal("0.10")


# --- Sample guard --------------------------------------------------------------------------------


def test_a_small_sample_can_never_be_paper_candidate() -> None:
    enough = [MIN_TRADES_PER_TEST_WINDOW] * 6
    assert (
        cap_by_sample(Verdict.PAPER_CANDIDATE, trades=MIN_PAPER_TRADES - 1, window_trades=enough)
        is Verdict.PROMISING
    )
    thin = [MIN_TRADES_PER_TEST_WINDOW] * 5 + [MIN_TRADES_PER_TEST_WINDOW - 1]
    assert (
        cap_by_sample(Verdict.PAPER_CANDIDATE, trades=MIN_PAPER_TRADES, window_trades=thin)
        is Verdict.PROMISING
    )
    assert (
        cap_by_sample(Verdict.PAPER_CANDIDATE, trades=MIN_PAPER_TRADES, window_trades=enough)
        is Verdict.PAPER_CANDIDATE
    )


def test_the_sample_guard_never_raises_a_verdict() -> None:
    for verdict in (Verdict.REJECT, Verdict.WEAK, Verdict.PROMISING):
        assert cap_by_sample(verdict, trades=10_000, window_trades=[1_000] * 6) is verdict
