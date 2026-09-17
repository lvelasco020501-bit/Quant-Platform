"""M16's protocol: the six assets, one strategy, one timeframe, one risk policy.

Every rule here was fixed before a single cross-asset result was looked at. The point of the
milestone is to find out whether regime_trend works anywhere but BTC, so the tests that matter
most are the ones holding every asset to *identical* strategy parameters and *identical* risk:
a per-asset tweak would answer a different question.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.orchestration.research import load_definition
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m16 import (
    ASSETS,
    BENCHMARKS,
    COMMON_START,
    DATA_END,
    DEPLOYED_POLICY,
    POLICY,
    STRATEGY,
    TIMEFRAME,
    discard_reason,
    pool,
    study_definition,
    symbol_rules_for,
    walk_forward_folds_for,
    years_for,
)
from quantplatform.research.sprint import CANDIDATES

DEPLOYED = Path(__file__).resolve().parents[2] / "tests/fixtures/deployed_risk_v2_definition.json"


@pytest.fixture
def base() -> ExperimentDefinition:
    return load_definition(DEPLOYED)


# --- The assets ----------------------------------------------------------------------------


def test_the_six_assets_are_the_ones_asked_for_in_that_order() -> None:
    assert [a.symbol for a in ASSETS] == [
        "BTC/USDT",
        "ETH/USDT",
        "BNB/USDT",
        "SOL/USDT",
        "XRP/USDT",
        "ADA/USDT",
    ]


def test_each_asset_starts_at_its_first_complete_month_of_exchange_history() -> None:
    # Binance Vision's first monthly archive is a partial month (the listing month), so the
    # dataset starts at the month after it: the first month the venue traded end to end.
    assert {a.raw: a.start for a in ASSETS} == {
        "BTCUSDT": datetime(2017, 9, 1, tzinfo=UTC),
        "ETHUSDT": datetime(2017, 9, 1, tzinfo=UTC),
        "BNBUSDT": datetime(2017, 12, 1, tzinfo=UTC),
        "SOLUSDT": datetime(2020, 9, 1, tzinfo=UTC),
        "XRPUSDT": datetime(2018, 6, 1, tzinfo=UTC),
        "ADAUSDT": datetime(2018, 5, 1, tzinfo=UTC),
    }


def test_every_asset_ends_on_the_same_day_and_shares_one_common_window() -> None:
    assert datetime(2026, 9, 15, tzinfo=UTC) == DATA_END
    assert max(a.start for a in ASSETS) == COMMON_START
    assert datetime(2020, 9, 1, tzinfo=UTC) == COMMON_START


def test_each_asset_pins_its_own_venue_rules_not_another_assets() -> None:
    for asset in ASSETS:
        rules = symbol_rules_for(asset)
        assert rules.symbol == asset.symbol
        assert rules.source == f"binance_spot:{asset.raw}"


# --- One strategy, one timeframe, one policy -----------------------------------------------


def test_the_study_is_regime_trend_at_four_hours_under_policy_c() -> None:
    canonical = next(c for c in CANDIDATES if c.strategy_id == "regime_trend")
    assert STRATEGY.strategy_id == "regime_trend"
    assert STRATEGY.params == canonical.params
    assert STRATEGY.neighbours == canonical.neighbours
    assert TIMEFRAME is Timeframe.H4
    assert (POLICY.key, POLICY.streak_limit, POLICY.drawdown_latch_pct) == (
        "C",
        None,
        Decimal("0.10"),
    )
    assert DEPLOYED_POLICY.key == "A"


def test_the_benchmarks_are_the_two_research_copies() -> None:
    assert [c.strategy_id for c in BENCHMARKS] == ["ema_trend_mtf", "breakout_mtf"]


def test_every_asset_gets_identical_parameters_risk_and_costs(base: ExperimentDefinition) -> None:
    built = [
        study_definition(asset, STRATEGY, base=base, policy=POLICY, window=None) for asset in ASSETS
    ]
    assert len({d.strategy.model_dump_json() for d in built}) == 1, "parameters differ by asset"
    assert len({d.risk.model_dump_json() for d in built}) == 1, "risk differs by asset"
    assert len({d.risk.execution_policy.model_dump_json() for d in built}) == 1
    # ... and the only thing that does differ is which market it is.
    assert [d.dataset.symbol for d in built] == [a.symbol for a in ASSETS]
    assert len({d.dataset.symbol_rules.price_tick for d in built}) > 1


def test_the_risk_is_m15s_four_hour_conversion_with_policy_cs_breakers(
    base: ExperimentDefinition,
) -> None:
    risk = study_definition(ASSETS[0], STRATEGY, base=base, policy=POLICY, window=None).risk
    converted = risk_for_timeframe(base.risk, Timeframe.H4)
    assert risk.initial_stop_distance_bps == converted.initial_stop_distance_bps == 600
    assert risk.max_holding_bars == converted.max_holding_bars == 42
    assert risk.execution_policy == base.risk.execution_policy, "fees and slippage are untouched"
    assert risk.max_consecutive_losses is None, "policy C has no streak breaker"
    assert risk.max_total_drawdown_pct == Decimal("0.10")
    assert risk.latch_total_drawdown is True


def test_the_deployed_policy_run_keeps_production_risk_v2s_breakers(
    base: ExperimentDefinition,
) -> None:
    risk = study_definition(
        ASSETS[0], STRATEGY, base=base, policy=DEPLOYED_POLICY, window=None
    ).risk
    assert risk.max_consecutive_losses == 5
    assert risk.max_total_drawdown_pct == Decimal("0.20")


# --- Windows -------------------------------------------------------------------------------


def test_years_tile_each_assets_own_history_without_gaps() -> None:
    for asset in ASSETS:
        years = years_for(asset)
        assert years[0].start == asset.start
        assert years[-1].end == DATA_END
        for earlier, later in pairwise(years):
            assert earlier.end == later.start


def test_walk_forward_trains_on_one_year_and_tests_the_next() -> None:
    for asset in ASSETS:
        folds = walk_forward_folds_for(asset)
        years = years_for(asset)
        assert len(folds) == len(years) - 1
        for fold, (train, test) in zip(folds, pairwise(years), strict=True):
            assert (fold.train, fold.test) == (train, test)


def test_solana_has_the_shortest_history_and_bitcoin_the_longest() -> None:
    assert len(years_for(ASSETS[3])) < len(years_for(ASSETS[0]))


# --- Pooling the sample --------------------------------------------------------------------


def test_pooling_adds_trades_and_recomputes_the_ratios_from_the_totals() -> None:
    pooled = pool(
        [
            {"trades": 60, "gross_profit": Decimal("900"), "gross_loss": Decimal("500")},
            {"trades": 40, "gross_profit": Decimal("300"), "gross_loss": Decimal("700")},
        ]
    )
    assert pooled.trades == 100
    assert pooled.profit_factor == Decimal("1200") / Decimal("1200")
    assert pooled.expectancy == Decimal("0")


def test_pooling_reports_no_profit_factor_when_nothing_was_lost() -> None:
    pooled = pool([{"trades": 3, "gross_profit": Decimal("10"), "gross_loss": Decimal("0")}])
    assert pooled.profit_factor is None
    assert pooled.trades == 3


def test_pooling_nothing_is_an_empty_sample_not_a_division_by_zero() -> None:
    pooled = pool([])
    assert pooled.trades == 0
    assert pooled.expectancy is None


# --- What gets discarded -------------------------------------------------------------------


def test_an_asset_is_discarded_when_it_loses_money_or_never_trades() -> None:
    assert discard_reason(trades=0, net=Decimal("0"), profit_factor=None, worst_stress=None)
    assert discard_reason(
        trades=50, net=Decimal("-0.05"), profit_factor=Decimal("0.9"), worst_stress=None
    )
    assert discard_reason(
        trades=50, net=Decimal("0.05"), profit_factor=Decimal("0.98"), worst_stress=Decimal("0.01")
    )


def test_an_asset_is_discarded_when_costs_alone_turn_it_negative() -> None:
    reason = discard_reason(
        trades=50,
        net=Decimal("0.04"),
        profit_factor=Decimal("1.3"),
        worst_stress=Decimal("-0.01"),
    )
    assert reason is not None
    assert "stress" in reason


def test_an_asset_that_makes_money_and_survives_costs_is_kept() -> None:
    assert (
        discard_reason(
            trades=50,
            net=Decimal("0.06"),
            profit_factor=Decimal("1.4"),
            worst_stress=Decimal("0.02"),
        )
        is None
    )
