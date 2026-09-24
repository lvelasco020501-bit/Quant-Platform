"""M22's pre-declaration, pinned so it cannot drift once results exist.

The point of this milestone is to look for edge, and the only thing that separates a search
from a fishing trip is that the things being judged were fixed before anyone looked. So what
these tests check is not behaviour — no strategy code changes here at all — but that the
declaration is closed: six families, two variants each, every second variant derivable from
the first by one mechanical rule, the same parameters on every asset, and a screen gate whose
thresholds are written down rather than chosen once the table is on screen.

The rule that makes the second variant checkable is the useful one. A variant chosen by hand
can always be defended after the fact; a variant that is *computed* from the first by doubling
every window and touching no threshold cannot be quietly tuned, because the test recomputes it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research.m22 import (
    CANDIDATES_M22,
    EXTENSION_ASSETS,
    MAX_FAMILIES_FORWARD,
    REFERENCES,
    SCREEN_ASSETS,
    SCREEN_MAX_DRAWDOWN,
    SCREEN_MIN_PROFIT_FACTOR,
    SCREEN_MIN_TRADES,
    TIME_STOP_BARS,
    TIMEFRAME,
    EdgeFamily,
    Horizon,
    Variant,
    deployed_definition,
    doubled,
    forwarded,
    ranking_key,
    shows_signal,
    study_definition,
    walk_forward_summary,
)
from quantplatform.research.sprint import CANDIDATES, Family


def _by(key: str) -> Variant:
    return next(v for v in CANDIDATES_M22 if v.key == key)


# --- The declaration is closed ----------------------------------------------------------------


def test_every_family_is_represented_by_exactly_two_variants() -> None:
    for family in EdgeFamily:
        variants = [v for v in CANDIDATES_M22 if v.family is family]
        assert len(variants) == 2, f"{family} must have exactly two variants, has {len(variants)}"


def test_there_are_six_families_and_twelve_variants() -> None:
    assert len(EdgeFamily) == 6
    assert len(CANDIDATES_M22) == 12
    assert len({v.key for v in CANDIDATES_M22}) == 12, "keys must be unique"


def test_each_family_pairs_one_base_horizon_with_one_doubled() -> None:
    for family in EdgeFamily:
        horizons = {v.horizon for v in CANDIDATES_M22 if v.family is family}
        assert horizons == {Horizon.BASE, Horizon.DOUBLED}


def test_the_two_variants_of_a_family_run_the_same_rule() -> None:
    # A family is a question, not a collection. Two different strategy ids inside one family
    # would make "this family shows signal" mean nothing.
    for family in EdgeFamily:
        ids = {v.candidate.strategy_id for v in CANDIDATES_M22 if v.family is family}
        assert len(ids) == 1, f"{family} mixes strategies: {ids}"


def test_the_six_families_use_six_different_strategies() -> None:
    ids = {v.candidate.strategy_id for v in CANDIDATES_M22}
    assert len(ids) == 6, "families that share a rule are not different families"


# --- The doubling rule is mechanical, not a matter of taste -----------------------------------


def test_every_doubled_variant_is_computed_from_its_base() -> None:
    for family in EdgeFamily:
        base = next(v for v in CANDIDATES_M22 if v.family is family and v.horizon is Horizon.BASE)
        long = next(
            v for v in CANDIDATES_M22 if v.family is family and v.horizon is Horizon.DOUBLED
        )
        assert long.candidate.params == doubled(base.candidate.params), (
            f"{family}'s long variant is not its base with every window doubled"
        )


def test_doubling_moves_windows_and_leaves_levels_alone() -> None:
    params = (("lookback", "72"), ("er_window", "72"), ("er_trend", "0.30"), ("entry_z", "-2"))
    assert doubled(params) == (
        ("lookback", "144"),
        ("er_window", "144"),
        ("er_trend", "0.30"),
        ("entry_z", "-2"),
    )


def test_doubling_refuses_a_parameter_it_has_no_rule_for() -> None:
    # Silence here would be the dangerous outcome: an unclassified parameter left untouched
    # looks exactly like a level, and a window left at its base length is a different rule.
    with pytest.raises(ValueError, match="no doubling rule"):
        doubled((("mystery_knob", "3"),))


def test_doubling_is_not_idempotent() -> None:
    once = doubled((("lookback", "72"),))
    assert doubled(once) == (("lookback", "288"),)


# --- Parameters came from before, not from the results ----------------------------------------


def test_every_base_variant_reuses_the_parameters_declared_in_m13() -> None:
    # The strongest available evidence that these numbers were not picked for this study is
    # that they were picked for a different one, years of commits ago.
    for variant in CANDIDATES_M22:
        if variant.horizon is not Horizon.BASE:
            continue
        m13 = next(c for c in CANDIDATES if c.strategy_id == variant.candidate.strategy_id)
        assert variant.candidate.params == m13.params, (
            f"{variant.key} changed M13's canonical parameters"
        )


def test_no_variant_declares_per_asset_parameters() -> None:
    # There is nowhere to put one, and that is the point: the model has no asset field, so
    # "same parameters on every asset" is structural rather than a promise.
    for variant in CANDIDATES_M22:
        assert not hasattr(variant, "symbol")
        assert not hasattr(variant.candidate, "symbol")


# --- References are references, not candidates ------------------------------------------------


def test_the_references_include_both_benchmarks_and_the_incumbent() -> None:
    ids = {r.strategy_id for r in REFERENCES}
    assert ids == {"ema_trend_mtf", "breakout_mtf", "regime_trend"}


def test_no_reference_is_also_a_candidate() -> None:
    candidates = {v.candidate.strategy_id for v in CANDIDATES_M22}
    assert candidates.isdisjoint({r.strategy_id for r in REFERENCES})


def test_the_incumbent_reference_keeps_its_m16_parameters() -> None:
    incumbent = next(r for r in REFERENCES if r.strategy_id == "regime_trend")
    m13 = next(c for c in CANDIDATES if c.strategy_id == "regime_trend")
    assert incumbent.params == m13.params


def test_both_benchmarks_are_declared_as_benchmarks() -> None:
    for reference in REFERENCES:
        if reference.strategy_id == "regime_trend":
            continue
        assert reference.family is Family.BENCHMARK


# --- Scope ------------------------------------------------------------------------------------


def test_the_screen_runs_btc_and_eth_at_four_hours() -> None:
    assert SCREEN_ASSETS == ("BTCUSDT", "ETHUSDT")
    assert TIMEFRAME is Timeframe.H4


def test_the_extension_markets_are_the_other_two_asked_for() -> None:
    assert EXTENSION_ASSETS == ("BNBUSDT", "SOLUSDT")
    assert set(SCREEN_ASSETS).isdisjoint(EXTENSION_ASSETS)


def test_the_excluded_markets_appear_nowhere() -> None:
    every = set(SCREEN_ASSETS) | set(EXTENSION_ASSETS)
    assert every.isdisjoint({"ADAUSDT", "XRPUSDT"})


# --- The screen gate, fixed before the first run ----------------------------------------------


def test_the_screen_thresholds_are_the_declared_ones() -> None:
    assert SCREEN_MIN_TRADES == 30
    assert Decimal("1.0") == SCREEN_MIN_PROFIT_FACTOR
    assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN


def test_a_configuration_that_clears_every_threshold_shows_signal() -> None:
    assert shows_signal(
        trades=40,
        total_return=Decimal("0.20"),
        profit_factor=Decimal("1.3"),
        max_drawdown=Decimal("0.20"),
    )


@pytest.mark.parametrize(
    ("missed", "trades", "total_return", "profit_factor", "max_drawdown"),
    [
        ("sample", 29, Decimal("0.20"), Decimal("1.3"), Decimal("0.20")),
        ("return", 40, Decimal("0"), Decimal("1.3"), Decimal("0.20")),
        ("profit factor", 40, Decimal("0.20"), Decimal("0.99"), Decimal("0.20")),
        ("drawdown", 40, Decimal("0.20"), Decimal("1.3"), Decimal("0.36")),
    ],
)
def test_missing_any_single_threshold_is_no_signal(
    missed: str,
    trades: int,
    total_return: Decimal,
    profit_factor: Decimal,
    max_drawdown: Decimal,
) -> None:
    assert not shows_signal(
        trades=trades,
        total_return=total_return,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
    ), f"a configuration failing on {missed} alone must not show signal"


def test_a_configuration_that_never_lost_a_trade_is_not_punished_for_it() -> None:
    # A profit factor is undefined without a losing trade. Reading that as failure would
    # discard the one case it cannot be computed for.
    assert shows_signal(
        trades=40, total_return=Decimal("0.20"), profit_factor=None, max_drawdown=Decimal("0.20")
    )


# --- What advances, and on what grounds -------------------------------------------------------


def test_a_family_advances_only_when_one_variant_works_on_both_screen_markets() -> None:
    passing = {("T1", "BTCUSDT"), ("T1", "ETHUSDT"), ("M1", "BTCUSDT")}
    assert forwarded(passing) == (EdgeFamily.TREND,)


def test_a_family_carried_by_a_single_market_does_not_advance() -> None:
    assert forwarded({("M1", "BTCUSDT")}) == ()


def test_two_variants_covering_one_market_each_do_not_add_up_to_a_family() -> None:
    # T1 on BTC and T2 on ETH is not "the family works on both": neither configuration did,
    # and carrying that forward would be picking the best variant per market — which is the
    # per-asset fitting this milestone excludes, arrived at by accident.
    assert forwarded({("T1", "BTCUSDT"), ("T2", "ETHUSDT")}) == ()


def test_either_variant_may_be_the_one_that_carries_its_family() -> None:
    assert forwarded({("T2", "BTCUSDT"), ("T2", "ETHUSDT")}) == (EdgeFamily.TREND,)


def test_families_come_back_in_declaration_order_not_discovery_order() -> None:
    passing = {("V1", "BTCUSDT"), ("V1", "ETHUSDT"), ("T1", "BTCUSDT"), ("T1", "ETHUSDT")}
    assert forwarded(passing) == (EdgeFamily.TREND, EdgeFamily.VOL_FILTERED)


def test_at_most_three_families_are_carried_forward() -> None:
    assert MAX_FAMILIES_FORWARD == 3


def test_the_ranking_never_reads_a_return() -> None:
    # The tie-break exists so that "which three" is decided before the numbers arrive. If it
    # could see a return, it would be selection by return wearing a different hat.
    rich = ranking_key(positive_years=3, walk_forward_share=Decimal("0.5"), trades=50)
    poor = ranking_key(positive_years=5, walk_forward_share=Decimal("0.2"), trades=31)
    assert poor > rich, "more positive years must outrank everything below it"


def test_the_ranking_breaks_ties_by_walk_forward_then_sample() -> None:
    a = ranking_key(positive_years=4, walk_forward_share=Decimal("0.75"), trades=31)
    b = ranking_key(positive_years=4, walk_forward_share=Decimal("0.50"), trades=200)
    assert a > b
    c = ranking_key(positive_years=4, walk_forward_share=Decimal("0.75"), trades=90)
    assert c > a


# --- Definitions ------------------------------------------------------------------------------


def test_a_definition_carries_the_variant_key_into_its_name() -> None:
    variant = _by("T1")
    definition = study_definition("BTCUSDT", variant)
    assert "m22" in definition.name
    assert "T1" in definition.name
    assert "BTCUSDT" in definition.name


def test_two_variants_of_one_family_get_different_experiment_ids() -> None:
    first = study_definition("BTCUSDT", _by("T1"))
    second = study_definition("BTCUSDT", _by("T2"))
    assert first.experiment_id != second.experiment_id


def test_the_same_variant_on_two_markets_gets_different_experiment_ids() -> None:
    btc = study_definition("BTCUSDT", _by("T1"))
    eth = study_definition("ETHUSDT", _by("T1"))
    assert btc.experiment_id != eth.experiment_id


def test_the_definition_runs_at_four_hours_under_the_research_variant() -> None:
    definition = study_definition("BTCUSDT", _by("M1"))
    assert definition.dataset.timeframe is Timeframe.H4
    assert definition.backtest.timeframe is Timeframe.H4
    # The research variant is exactly the deployed configuration with its latches released.
    assert definition.risk.max_consecutive_losses is None
    assert definition.risk.latch_total_drawdown is False


def test_the_deployed_definition_keeps_risk_v2_latching() -> None:
    definition = deployed_definition("BTCUSDT", _by("M1"))
    assert definition.risk.max_consecutive_losses == 5
    assert definition.risk.latch_total_drawdown is True


def test_the_time_stop_is_the_same_seven_days_on_every_definition() -> None:
    # Declared here because it dominates every result in this milestone: at 4h the deployed
    # configuration closes any position after 42 bars, so no variant can hold a trend longer
    # than a week no matter what its own exit rule says.
    assert TIME_STOP_BARS == 42
    for key in ("T1", "T2", "B1", "B2", "M1", "M2"):
        definition = study_definition("BTCUSDT", _by(key))
        assert definition.risk.max_holding_bars == TIME_STOP_BARS


# --- The walk-forward aggregate ---------------------------------------------------------------


def test_only_the_test_halves_of_a_walk_forward_count() -> None:
    # The bug this pins: matching OUT_OF_SAMPLE instead of WALK_FORWARD_TEST counted zero test
    # windows in all thirty screen runs and reported 0/0, which reads as a measured failure
    # rather than as a filter that matched nothing.
    folds = [
        {"role": "walk_forward_train", "card": {"total_return": "9.99"}},
        {"role": "walk_forward_test", "card": {"total_return": "0.10"}},
        {"role": "walk_forward_test", "card": {"total_return": "-0.20"}},
    ]
    summary = walk_forward_summary(folds)
    assert summary["tested"] == 2, "a train half is context, never a window"
    assert summary["positive"] == 1
    assert summary["positive_share"] == Decimal("0.5")


def test_an_out_of_sample_role_is_not_a_walk_forward_window() -> None:
    folds = [{"role": "out_of_sample", "card": {"total_return": "0.10"}}]
    assert walk_forward_summary(folds)["tested"] == 0


def test_a_fold_that_produced_no_result_is_not_counted_as_a_loss() -> None:
    folds: list[dict[str, Any]] = [
        {"role": "walk_forward_test", "card": None},
        {"role": "walk_forward_test", "card": {"total_return": "0.10"}},
    ]
    summary = walk_forward_summary(folds)
    assert summary["tested"] == 1
    assert summary["positive_share"] == Decimal(1)


def test_no_test_window_reports_nothing_rather_than_zero() -> None:
    # None fails the verdict's check; zero would look like a strategy that lost every window.
    summary = walk_forward_summary([])
    assert summary["positive_share"] is None
    assert summary["median_return"] is None
    assert summary["tested"] == 0
