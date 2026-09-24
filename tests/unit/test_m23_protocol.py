"""M23's pre-declaration: the out-of-sample window and the ways B2 is allowed to fail.

M22 ended WEAK for one reason that was not a failed test — it never declared an out-of-sample
window, so the verdict's check on that field could only fail. This milestone exists to fix
that properly, which means the window has to be fixed **before** the runs and pinned here
where changing it breaks the suite.

The window is chosen by a calendar rule that cannot see a result: a fixed date, identical on
all four markets. What these tests guarantee is not that the choice is wise but that it is
closed — one date, one candidate, one mechanical neighbour rule, and failure conditions whose
thresholds are either reused from the platform or written down with their reasoning before
any number arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantplatform.research.m15 import MIN_PAPER_TRADES, MIN_TRADES_PER_TEST_WINDOW
from quantplatform.research.m22 import CANDIDATES_M22, asset_for
from quantplatform.research.m23 import (
    CANDIDATE,
    MARKETS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_DEPLOYED_TRADES_PER_YEAR,
    MIN_MARKETS_POSITIVE,
    MIN_OOS_RETURN,
    MIN_YEARS_POSITIVE_SHARE,
    NEIGHBOUR_STEP,
    NEIGHBOURS,
    OOS_START,
    Failure,
    in_sample_window,
    neighbour_params,
    oos_window,
)

# --- The window, fixed before anything ran ------------------------------------------------------


def test_the_out_of_sample_window_starts_on_the_declared_date() -> None:
    assert datetime(2024, 1, 1, tzinfo=UTC) == OOS_START


def test_the_same_window_applies_to_every_market() -> None:
    # A per-market boundary would be a choice per market, and four choices are four chances to
    # land on a flattering split.
    starts = {oos_window(m).start for m in MARKETS}
    ends = {oos_window(m).end for m in MARKETS}
    assert len(starts) == 1
    assert len(ends) == 1


def test_in_sample_ends_exactly_where_out_of_sample_begins() -> None:
    for market in MARKETS:
        assert in_sample_window(market).end == oos_window(market).start


def test_in_sample_starts_at_each_market_s_own_first_complete_month() -> None:
    for market in MARKETS:
        assert in_sample_window(market).start == asset_for(market).start


def test_every_market_keeps_years_of_in_sample_history() -> None:
    # A window that leaves a market with almost no in-sample period would make its
    # out-of-sample result the whole study rather than a check on it.
    for market in MARKETS:
        window = in_sample_window(market)
        assert (window.end - window.start).days >= 3 * 365, market


def test_the_four_markets_are_the_ones_m22_carried() -> None:
    assert MARKETS == ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")


# --- One candidate, unchanged -------------------------------------------------------------------


def test_the_candidate_is_b2_exactly_as_m22_ran_it() -> None:
    b2 = next(v for v in CANDIDATES_M22 if v.key == "B2")
    assert CANDIDATE.candidate.params == b2.candidate.params
    assert CANDIDATE.candidate.strategy_id == "breakout_trend"


def test_the_candidate_declares_no_per_market_parameters() -> None:
    assert not hasattr(CANDIDATE, "symbol")


# --- Neighbours: few, mechanical, one axis at a time ---------------------------------------------


def test_there_are_exactly_four_neighbours() -> None:
    # Two axes, two directions. Fewer would not separate the channel from the filter; more
    # would be a grid search wearing a smaller name.
    assert len(NEIGHBOURS) == 4


def test_the_step_is_a_quarter_in_each_direction() -> None:
    assert Decimal("0.25") == NEIGHBOUR_STEP


def test_each_neighbour_moves_exactly_one_axis() -> None:
    base = dict(CANDIDATE.candidate.params)
    for neighbour in NEIGHBOURS:
        changed = {k for k, v in dict(neighbour.params).items() if base[k] != v}
        assert changed in ({"entry_lookback", "exit_lookback"}, {"trend_period"}), (
            f"{neighbour.key} moves {changed}, which is not one axis"
        )


def test_the_channel_neighbours_keep_the_entry_to_exit_ratio() -> None:
    # 40/20 is one channel described by two numbers. Scaling only one of them would change
    # what the rule is, not how long it looks back.
    for neighbour in NEIGHBOURS:
        params = dict(neighbour.params)
        entry, exit_ = int(params["entry_lookback"]), int(params["exit_lookback"])
        assert entry == exit_ * 2


def test_the_neighbours_are_computed_not_chosen() -> None:
    base = CANDIDATE.candidate.params
    expected = {
        neighbour_params(base, axis, direction)
        for axis in ("channel", "trend")
        for direction in (-1, 1)
    }
    assert {n.params for n in NEIGHBOURS} == expected


def test_a_neighbour_is_never_the_candidate_itself() -> None:
    assert all(n.params != CANDIDATE.candidate.params for n in NEIGHBOURS)


def test_neighbour_keys_are_unique() -> None:
    assert len({n.key for n in NEIGHBOURS}) == len(NEIGHBOURS)


def test_an_unknown_axis_is_refused() -> None:
    with pytest.raises(ValueError, match="axis"):
        neighbour_params(CANDIDATE.candidate.params, "volume", 1)


# --- How the candidate is allowed to fail -------------------------------------------------------


def test_the_failure_reasons_are_the_ones_asked_for() -> None:
    assert {f.value for f in Failure} == {
        "works only on BTC",
        "out-of-sample return is negative or nearly nil",
        "the cost stress breaks the edge",
        "small parameter changes destroy the result",
        "the result rests on a single year",
        "Risk V2 leaves it barely operable",
    }


def test_the_nearly_nil_floor_is_three_percent_over_the_window() -> None:
    assert Decimal("0.03") == MIN_OOS_RETURN


def test_three_of_four_markets_must_be_positive_out_of_sample() -> None:
    assert MIN_MARKETS_POSITIVE == 3


def test_no_single_year_may_carry_more_than_half_the_result() -> None:
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE


def test_most_years_must_be_positive() -> None:
    assert Decimal("0.60") == MIN_YEARS_POSITIVE_SHARE


def test_the_operability_floor_reuses_the_platform_s_own_number() -> None:
    # Not a new threshold: the platform already requires five trades in every walk-forward
    # test year before it will call anything a paper candidate.
    assert MIN_DEPLOYED_TRADES_PER_YEAR == MIN_TRADES_PER_TEST_WINDOW


def test_the_sample_floor_is_the_platform_s_own_number() -> None:
    assert MIN_PAPER_TRADES == 100
