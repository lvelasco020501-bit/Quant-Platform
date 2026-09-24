"""M24's fairness guarantees, pinned.

The risk in a comparison milestone is not that a number is wrong but that the two things
compared were not asked the same question. So these tests check symmetry rather than
behaviour: both contenders frozen at parameters fixed by earlier milestones, both built by
one function, both perturbed by one rule, both measured against thresholds imported from M23
rather than restated here.

The load-bearing one is
:func:`test_the_neighbour_rule_reproduces_m23_s_own_neighbours_for_b2`. M23 computed B2's
four neighbours with its own code; if M24's generalised rule did not reproduce them exactly,
then B2 would be measured for fragility one way and regime_trend another, and the comparison
would be decided by the measuring instrument.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.research.m22 import TIMEFRAME
from quantplatform.research.m23 import (
    CANDIDATE,
    MIN_DEPLOYED_TRADES_PER_YEAR,
    MIN_OOS_RETURN,
    NEIGHBOUR_STEP,
    NEIGHBOURS,
)
from quantplatform.research.m23 import MARKETS as M23_MARKETS
from quantplatform.research.m23 import OOS_START as M23_OOS_START
from quantplatform.research.m24 import (
    CONTENDERS,
    INTEGER_PARAMS,
    MARKETS,
    OOS_START,
    contender_definition,
    contender_for,
    full_window,
    in_sample_window,
    neighbours_for,
    oos_window,
    scaled,
)
from quantplatform.research.sprint import CANDIDATES

# --- The protocol is M23's, by import -----------------------------------------------------------


def test_the_window_is_m23_s_own_object() -> None:
    assert OOS_START is M23_OOS_START
    assert MARKETS is M23_MARKETS


def test_the_two_halves_still_join_into_the_whole_history() -> None:
    for market in MARKETS:
        assert full_window(market).start == in_sample_window(market).start
        assert full_window(market).end == oos_window(market).end


def test_the_thresholds_are_not_restated_here() -> None:
    # Imported, not redeclared. A copy could drift for one contender and not the other.
    assert Decimal("0.03") == MIN_OOS_RETURN
    assert MIN_DEPLOYED_TRADES_PER_YEAR == 5


# --- Both contenders are frozen -------------------------------------------------------------------


def test_there_are_exactly_two_contenders() -> None:
    assert len(CONTENDERS) == 2
    assert {c.key for c in CONTENDERS} == {"B2", "RT"}


def test_b2_is_exactly_what_m23_validated() -> None:
    b2 = contender_for("B2")
    assert b2.params == CANDIDATE.candidate.params
    assert b2.strategy_id == CANDIDATE.candidate.strategy_id


def test_regime_trend_is_exactly_what_m13_declared() -> None:
    rt = contender_for("RT")
    m13 = next(c for c in CANDIDATES if c.strategy_id == "regime_trend")
    assert rt.params == m13.params
    assert dict(rt.params) == {"lookback": "72", "er_window": "72", "er_min": "0.30"}


def test_neither_contender_can_carry_per_market_parameters() -> None:
    for contender in CONTENDERS:
        assert not hasattr(contender, "symbol")
        assert not hasattr(contender, "market")


# --- One perturbation rule, applied to both -------------------------------------------------------


def test_each_contender_gets_exactly_four_neighbours() -> None:
    for contender in CONTENDERS:
        assert len(neighbours_for(contender)) == 4


def test_the_neighbour_rule_reproduces_m23_s_own_neighbours_for_b2() -> None:
    # If this fails, the two contenders are being measured with different instruments and no
    # comparison between them means anything.
    assert {n.params for n in neighbours_for(contender_for("B2"))} == {n.params for n in NEIGHBOURS}


def test_both_contenders_move_by_the_same_step() -> None:
    assert Decimal("0.25") == NEIGHBOUR_STEP
    horizon = next(
        n for n in neighbours_for(contender_for("RT")) if n.axis == "horizon" and n.direction > 0
    )
    assert dict(horizon.params)["lookback"] == "90"  # 72 * 1.25
    assert dict(horizon.params)["er_window"] == "90"


def test_a_threshold_axis_stays_a_decimal_rather_than_rounding_to_zero() -> None:
    # er_min 0.30 scaled down is 0.225. Through int() that is 0, which would be a rule that
    # enters on every bar and would report "robust" for the wrong reason.
    down = next(
        n for n in neighbours_for(contender_for("RT")) if n.axis == "regime" and n.direction < 0
    )
    assert Decimal("0.225") == Decimal(dict(down.params)["er_min"])


def test_the_threshold_neighbours_are_wider_than_m13_s_own() -> None:
    # M13 declared 0.25 and 0.35. A quarter either way reaches 0.225 and 0.375, so this is
    # the stricter test — and it is the same test B2 gets.
    values = sorted(
        Decimal(dict(n.params)["er_min"])
        for n in neighbours_for(contender_for("RT"))
        if n.axis == "regime"
    )
    assert values[0] < Decimal("0.25")
    assert values[1] > Decimal("0.35")


def test_every_neighbour_moves_exactly_one_axis() -> None:
    for contender in CONTENDERS:
        base = dict(contender.params)
        members = dict(contender.axes)
        for neighbour in neighbours_for(contender):
            changed = {k for k, v in dict(neighbour.params).items() if base[k] != v}
            assert changed == set(members[neighbour.axis]), neighbour.key


def test_no_neighbour_equals_its_contender() -> None:
    for contender in CONTENDERS:
        assert all(n.params != contender.params for n in neighbours_for(contender))


def test_neighbour_keys_are_unique_across_both_contenders() -> None:
    keys = [n.key for c in CONTENDERS for n in neighbours_for(c)]
    assert len(keys) == len(set(keys)) == 8


def test_an_axis_naming_a_parameter_the_rule_lacks_is_refused() -> None:
    broken = contender_for("RT").model_copy(update={"axes": (("bogus", ("no_such_param",)),)})
    with pytest.raises(ValueError, match="does not have"):
        neighbours_for(broken)


def test_the_integer_parameters_are_the_ones_counted_in_bars() -> None:
    assert {
        "entry_lookback",
        "exit_lookback",
        "trend_period",
        "lookback",
        "er_window",
    } == INTEGER_PARAMS


def test_scaling_a_bar_count_returns_a_whole_number() -> None:
    assert scaled("72", "lookback", 1) == "90"
    assert scaled("40", "entry_lookback", -1) == "30"


# --- One builder, both contenders -----------------------------------------------------------------


def test_both_contenders_are_built_by_the_same_function() -> None:
    window = full_window("BTCUSDT")
    for contender in CONTENDERS:
        definition = contender_definition("BTCUSDT", contender, window, label="full")
        assert definition.dataset.timeframe is TIMEFRAME
        assert definition.backtest.timeframe is TIMEFRAME
        assert definition.dataset.symbol == "BTC/USDT"


def test_both_contenders_get_the_same_research_variant_risk() -> None:
    window = full_window("ETHUSDT")
    risks = {
        (
            contender_definition("ETHUSDT", c, window, label="full").risk.max_consecutive_losses,
            contender_definition("ETHUSDT", c, window, label="full").risk.latch_total_drawdown,
            contender_definition("ETHUSDT", c, window, label="full").risk.max_holding_bars,
        )
        for c in CONTENDERS
    }
    assert risks == {(None, False, 42)}


def test_both_contenders_get_the_same_deployed_risk() -> None:
    window = full_window("BNBUSDT")
    risks = {
        contender_definition(
            "BNBUSDT", c, window, label="deployed", latching=True
        ).risk.max_consecutive_losses
        for c in CONTENDERS
    }
    assert risks == {5}


def test_the_two_contenders_never_share_an_experiment_id() -> None:
    window = full_window("SOLUSDT")
    ids = {
        contender_definition("SOLUSDT", c, window, label="full").experiment_id for c in CONTENDERS
    }
    assert len(ids) == 2


def test_each_window_gets_its_own_experiment_id() -> None:
    contender = contender_for("RT")
    ids = {
        contender_definition("BTCUSDT", contender, w, label=label).experiment_id
        for w, label in (
            (in_sample_window("BTCUSDT"), "is"),
            (oos_window("BTCUSDT"), "oos"),
            (full_window("BTCUSDT"), "full"),
        )
    }
    assert len(ids) == 3
