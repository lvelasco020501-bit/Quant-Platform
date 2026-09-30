"""M33's pre-declaration is a promise, and these tests are what make it one.

Two of them carry the weight. ``test_every_parameter_is_one_the_project_already_declared`` is
what stops a new family from being a parameter search wearing a new name: every window and the
threshold's origin are checked against the strategies they were taken from.
``test_the_cross_asset_rule_is_declared_rather_than_decided_afterwards`` closes the gap M29 had
to admit in its own report — that milestone applied its gate per market and counted passes
without ever having said it would.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import (
    MAX_SINGLE_YEAR_SHARE,
    MIN_ASSETS_POSITIVE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
)
from quantplatform.research.m30 import ASSETS_M30
from quantplatform.research.m33 import (
    ASSETS_M33,
    COMPRESSION_THRESHOLDS,
    COST_STRESS_MULTIPLIERS,
    ENTRY_LOOKBACK,
    EXIT_LOOKBACK,
    INHERITED_MAX_RATIO,
    LONG_WINDOW,
    NEIGHBOUR_SHORT_WINDOWS,
    SHORT_WINDOW,
    TIMEFRAME,
    VARIANTS_M33,
    Compressed,
    CompressionRobustness,
    Rule,
    advances,
    definition_for,
    neighbour_of,
    survives,
)
from quantplatform.research.sprint import CANDIDATES

PASSING: dict[str, object] = {
    "annual": Decimal("0.14"),
    "max_drawdown": Decimal("0.22"),
    "calmar_ratio": Decimal("0.64"),
    "single_year_share": Decimal("0.41"),
    "neighbour_min_profit_factor": Decimal("1.20"),
    "annual_at_double_cost": Decimal("0.09"),
    "annual_at_triple_cost": Decimal("0.05"),
    "out_of_sample_return": Decimal("0.18"),
}
"""One market's result clearing every declared condition, the baseline each test breaks once."""


def _survives(**overrides: object) -> tuple[bool, tuple[CompressionRobustness, ...]]:
    return survives(Compressed(**{**PASSING, **overrides}))  # type: ignore[arg-type]


# --- What is searched -----------------------------------------------------------------------------


def test_the_universe_is_the_same_six_m30_and_m31_used() -> None:
    assert ASSETS_M33 == ASSETS_M30
    assert len(ASSETS_M33) == 6


def test_phase_one_is_daily_only() -> None:
    assert TIMEFRAME is Timeframe.D1


def test_the_three_rules_are_the_three_that_were_asked_for() -> None:
    assert [rule.value for rule in Rule] == [
        "bb_squeeze",
        "vol_compression",
        "range_compression",
    ]


def test_every_rule_gets_exactly_two_variants_and_no_more() -> None:
    for rule in Rule:
        assert len([v for v in VARIANTS_M33 if v.rule is rule]) == 2, rule
    assert len(VARIANTS_M33) == 6
    keys = [v.key for v in VARIANTS_M33]
    assert len(set(keys)) == len(keys)


def test_within_a_rule_only_the_threshold_moves() -> None:
    for rule in Rule:
        first, second = (v for v in VARIANTS_M33 if v.rule is rule)
        differing = {
            name for (name, a), (_, b) in zip(first.params, second.params, strict=True) if a != b
        }
        assert differing == {"max_compression"}, rule


def test_between_rules_nothing_moves_except_what_is_measured() -> None:
    # Same windows, same breakout leg, same exit, same thresholds. That is what makes this a
    # comparison of three definitions of stillness rather than six unrelated strategies.
    for threshold in COMPRESSION_THRESHOLDS:
        same = [v for v in VARIANTS_M33 if v.threshold == threshold]
        assert len(same) == 3
        for variant in same:
            values = dict(variant.params)
            assert values["entry_lookback"] == str(ENTRY_LOOKBACK)
            assert values["exit_lookback"] == str(EXIT_LOOKBACK)
            assert values["max_compression"] == str(threshold)
            window = values.get("short_window") or values["short_vol"]
            longer = values.get("long_window") or values["long_vol"]
            assert (window, longer) == (str(SHORT_WINDOW), str(LONG_WINDOW))


# --- Nothing here is a new number -----------------------------------------------------------------


def test_every_parameter_is_one_the_project_already_declared() -> None:
    declared = {c.strategy_id: dict(c.params) for c in CANDIDATES}

    assert int(declared["vol_filtered_momentum"]["short_vol"]) == SHORT_WINDOW
    assert int(declared["vol_filtered_momentum"]["long_vol"]) == LONG_WINDOW
    assert int(declared["breakout"]["entry_lookback"]) == ENTRY_LOOKBACK
    assert int(declared["breakout"]["exit_lookback"]) == EXIT_LOOKBACK
    assert Decimal(declared["vol_filtered_momentum"]["max_ratio"]) == INHERITED_MAX_RATIO


def test_the_thresholds_are_the_null_and_the_inverse_of_the_inherited_ceiling() -> None:
    # 1.0 is "exactly as still as drift-free noise". The second is derived from the inherited
    # ceiling rather than typed, so it cannot drift from the number it came from.
    assert COMPRESSION_THRESHOLDS[0] == Decimal(1)
    assert COMPRESSION_THRESHOLDS[1] == (Decimal(1) / INHERITED_MAX_RATIO).quantize(
        Decimal("0.0001")
    )
    assert COMPRESSION_THRESHOLDS[1] < COMPRESSION_THRESHOLDS[0]


def test_no_threshold_would_switch_the_filter_off() -> None:
    assert all(Decimal(0) < t <= Decimal(1) for t in COMPRESSION_THRESHOLDS)


def test_the_sensitivity_probes_are_half_and_double_the_measurement_window() -> None:
    assert NEIGHBOUR_SHORT_WINDOWS == (SHORT_WINDOW // 2, SHORT_WINDOW * 2)
    assert SHORT_WINDOW not in NEIGHBOUR_SHORT_WINDOWS


def test_a_neighbour_moves_the_window_and_nothing_else() -> None:
    base = VARIANTS_M33[0]
    probe = neighbour_of(base, NEIGHBOUR_SHORT_WINDOWS[0])

    assert probe.rule is base.rule
    assert probe.threshold == base.threshold
    assert dict(probe.params)["short_window"] == str(NEIGHBOUR_SHORT_WINDOWS[0])
    assert dict(probe.params)["max_compression"] == dict(base.params)["max_compression"]


def test_costs_are_stressed_upward_and_never_downward() -> None:
    assert COST_STRESS_MULTIPLIERS == (2, 3)


# --- The gate -------------------------------------------------------------------------------------


def test_a_result_clearing_every_condition_survives() -> None:
    assert _survives() == (True, ())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("annual", Decimal("-0.01"), CompressionRobustness.NEGATIVE_AFTER_COSTS),
        ("annual", None, CompressionRobustness.NEGATIVE_AFTER_COSTS),
        ("max_drawdown", Decimal("0.36"), CompressionRobustness.DRAWDOWN),
        ("calmar_ratio", Decimal("0.49"), CompressionRobustness.LOW_CALMAR),
        ("calmar_ratio", None, CompressionRobustness.LOW_CALMAR),
        ("single_year_share", Decimal("0.51"), CompressionRobustness.SINGLE_YEAR),
        ("neighbour_min_profit_factor", Decimal("0.99"), CompressionRobustness.NARROW_PEAK),
        ("neighbour_min_profit_factor", None, CompressionRobustness.NARROW_PEAK),
        ("annual_at_double_cost", Decimal(0), CompressionRobustness.COST_FRAGILE),
        ("annual_at_triple_cost", Decimal("-0.01"), CompressionRobustness.COST_FRAGILE),
        ("out_of_sample_return", Decimal("-0.01"), CompressionRobustness.OUT_OF_SAMPLE_NEGATIVE),
    ],
)
def test_each_declared_condition_can_fail_on_its_own(
    field: str, value: object, expected: CompressionRobustness
) -> None:
    passed, reasons = _survives(**{field: value})

    assert passed is False
    assert expected in reasons


def test_surviving_double_costs_is_not_enough_on_its_own() -> None:
    passed, reasons = _survives(annual_at_triple_cost=Decimal("-0.02"))

    assert passed is False
    assert CompressionRobustness.COST_FRAGILE in reasons


def test_the_gate_names_every_reason_not_just_the_first() -> None:
    passed, reasons = _survives(
        annual=Decimal("-0.05"), max_drawdown=Decimal("0.80"), single_year_share=Decimal("1.90")
    )

    assert passed is False
    assert {
        CompressionRobustness.NEGATIVE_AFTER_COSTS,
        CompressionRobustness.DRAWDOWN,
        CompressionRobustness.SINGLE_YEAR,
    } <= set(reasons)


def test_sample_size_is_measured_but_does_not_gate() -> None:
    # A compression rule is expected to trade rarely. The user declared no trade floor, and
    # rejecting on a floor nobody agreed would be the same breach as relaxing one that was.
    assert "trades" not in Compressed.model_fields


# --- The cross-asset rule, declared in advance this time ------------------------------------------


def test_the_cross_asset_rule_is_declared_rather_than_decided_afterwards() -> None:
    # M29 applied its gate per market and counted passes without ever saying it would, and had
    # to record that in its own limitations. This is the same reading, written down first.
    assert advances(MIN_ASSETS_POSITIVE) is True
    assert advances(MIN_ASSETS_POSITIVE - 1) is False
    assert advances(len(ASSETS_M33)) is True
    assert advances(0) is False


def test_passing_on_one_market_is_never_enough() -> None:
    assert advances(1) is False
    assert advances(2) is False


# --- Every threshold is the one its own milestone set ---------------------------------------------


def test_every_carried_threshold_is_inherited_unchanged() -> None:
    assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN
    assert Decimal("0.50") == MIN_CALMAR
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
    assert Decimal("1.00") == MIN_NEIGHBOUR_PROFIT_FACTOR
    assert MIN_ASSETS_POSITIVE == 3


# --- The production engine is the one that runs this ----------------------------------------------


def test_a_definition_runs_on_daily_bars_with_the_platform_s_own_costs() -> None:
    definition = definition_for("BTCUSDT", VARIANTS_M33[0], latching=False)

    assert definition.backtest.timeframe is Timeframe.D1
    assert definition.risk.execution_policy.fee.basis_points == Decimal(10)
    assert definition.risk.execution_policy.slippage.basis_points == Decimal(5)
    assert definition.strategy.strategy_id == "bb_squeeze"
    assert definition.strategy.params == VARIANTS_M33[0].params


def test_the_deployed_variant_differs_from_the_research_one_only_in_its_breakers() -> None:
    research = definition_for("BTCUSDT", VARIANTS_M33[0], latching=False)
    deployed = definition_for("BTCUSDT", VARIANTS_M33[0], latching=True)

    assert research.strategy == deployed.strategy
    assert research.dataset == deployed.dataset
    assert research.risk != deployed.risk


def test_every_declared_market_can_be_built() -> None:
    for raw in ASSETS_M33:
        assert definition_for(raw, VARIANTS_M33[0], latching=False).dataset.symbol.endswith("/USDT")
