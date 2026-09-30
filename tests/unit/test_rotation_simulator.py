"""The rotation simulator is a second backtest path, so it has to earn trust it did not inherit.

Every M30 conclusion will rest on this module, and none of the production engine's
certification carries over to it. So the tests here are not smoke tests: each one pins a
property that, if it broke, would turn a losing rule into a winning one on paper.

Four of them are the ones that matter. ``test_the_rule_cannot_see_the_bar_it_trades_into``
is the lookahead check, and a simulator that fails it produces beautiful nonsense.
``test_a_multi_asset_holding_drifts_instead_of_being_rebalanced_for_free`` catches the subtler
version of the same sin: free daily rebalancing is an edge no rule paid for.
``test_the_episodes_account_for_every_unit_of_equity_the_run_gained_or_lost`` is the
conservation law -- if profit can appear outside an episode, per-asset contribution is
fiction and the concentration gate is measuring nothing. And
``test_the_normalised_score_is_the_hurdle_vol_momentum_already_uses`` is what makes the
declared threshold of 1.0 mean the same thing here as in the strategy it was inherited from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.features.indicators import IndicatorFeatures
from quantplatform.research.rotation import (
    INITIAL_EQUITY,
    EquityPoint,
    RotationSpec,
    align,
    basket_index,
    buy_and_hold,
    equal_weight_basket,
    max_drawdown,
    pairwise_correlation,
    profit_factor,
    simulate,
    weights_for,
    yearly_returns,
)

START = datetime(2020, 1, 1, tzinfo=UTC)
FREE = Decimal(0)
"""Costless, used wherever a test is about mechanics and a cost would only obscure them."""
COSTLY = Decimal(100)
"""One percent per side. Deliberately enormous so cost arithmetic is legible in the result."""


def series_of(symbol: str, closes: list[str], *, offset: int = 0) -> list[MarketBar]:
    """Build a daily series from closes, starting ``offset`` days after the common origin."""
    # A domain symbol is BASE/QUOTE, so the short names these tests read by are padded into
    # one. The mapping keys stay short: they are what the rule ranks, and legibility matters.
    pair = f"{symbol.ljust(2, 'Z')}/USDT"
    bars: list[MarketBar] = []
    for index, close in enumerate(closes):
        open_time = START + timedelta(days=offset + index)
        bars.append(
            MarketBar(
                symbol=pair,
                market_type=MarketType.SPOT,
                timeframe=Timeframe.D1,
                open_time=open_time,
                close_time=open_time + timedelta(days=1),
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                volume=Decimal(1),
                quote_volume=None,
                trade_count=None,
                source="test",
                is_closed=True,
            )
        )
    return bars


def flat(symbol: str, count: int, level: str = "100", *, offset: int = 0) -> list[MarketBar]:
    """Build a series that never moves, so it can rank last without adding any return."""
    return series_of(symbol, [level] * count, offset=offset)


# --- Lookahead ------------------------------------------------------------------------------------


def test_the_rule_cannot_see_the_bar_it_trades_into() -> None:
    # A rises steadily and B is flat until it explodes on the final bar. A rule with foresight
    # would be holding B for that jump; a rule reading only closed bars cannot be, because at
    # every decision point B's trailing return is zero and A's is positive.
    a = series_of("A", ["100", "101", "102", "103", "104"])
    b = series_of("B", ["100", "100", "100", "100", "400"])

    run = simulate({"A": a, "B": b}, RotationSpec(lookback=1, hold=1), cost_basis_points=FREE)

    assert [episode.asset for episode in run.episodes] == ["A"]
    assert run.total_return < Decimal("0.05")


def test_an_asset_is_absent_from_every_ranking_before_it_lists() -> None:
    # SOL did not exist in 2019, and a rotation study that lets it be ranked then is measuring a
    # portfolio nobody could have held. NEW rockets hard enough to win every ranking it is
    # eligible for, so if it could be held early it would be.
    early = series_of("OLD", ["100", "110", "120", "130", "140", "150"])
    late = series_of("NEW", ["100", "500", "600", "700"], offset=2)

    run = simulate(
        {"OLD": early, "NEW": late}, RotationSpec(lookback=1, hold=1), cost_basis_points=FREE
    )

    opened = [e.opened_at for e in run.episodes if e.asset == "NEW"]
    assert opened
    assert min(opened) >= late[0].open_time


def test_the_grid_is_the_union_of_every_series_not_the_intersection() -> None:
    early = flat("OLD", 4)
    late = flat("NEW", 2, offset=2)

    assert align({"OLD": early, "NEW": late}) == tuple(bar.open_time for bar in early)


# --- Weights, drift and cost ----------------------------------------------------------------------


def test_a_multi_asset_holding_drifts_instead_of_being_rebalanced_for_free() -> None:
    # Two assets entered 50/50, one of them doubling on each of the next two bars. A rotation
    # rule asking for "top 2, equally weighted" re-equalises after the first double and reaches
    # 2.25x. A buy-and-hold basket lets the winner run and reaches 2.5x. The two numbers must
    # differ: if they match, positions are being tracked by the weight they were handed rather
    # than by what they are worth, and every multi-asset result is wrong.
    a = series_of("A", ["100", "100", "200", "400"])
    b = flat("B", 4)

    rotating = simulate({"A": a, "B": b}, RotationSpec(lookback=1, hold=2), cost_basis_points=FREE)
    drifting = equal_weight_basket({"A": a, "B": b}, cost_basis_points=FREE)

    assert rotating.final_equity == INITIAL_EQUITY * Decimal("2.25")
    assert drifting.final_equity == INITIAL_EQUITY * Decimal("2.5")


def test_turnover_is_charged_on_both_sides_of_a_swap() -> None:
    # A leads for the first decisions, then B takes over. The swap moves the whole book: one
    # unit of weight out and one in, so two units of turnover on top of the entry, and the cost
    # is charged on each of them.
    a = series_of("A", ["100", "110", "110", "110", "110"])
    b = series_of("B", ["100", "100", "100", "150", "150"])

    run = simulate({"A": a, "B": b}, RotationSpec(lookback=1, hold=1), cost_basis_points=COSTLY)

    assert run.turnover > Decimal(2)
    assert run.cost_paid > Decimal(0)
    assert sum(contribution.cost for contribution in run.contributions) == run.cost_paid


def test_a_rule_that_never_changes_its_mind_pays_only_to_get_in() -> None:
    a = series_of("A", ["100", "110", "120", "130"])
    b = flat("B", 4)

    run = simulate({"A": a, "B": b}, RotationSpec(lookback=1, hold=1), cost_basis_points=COSTLY)

    # One unit of weight in at the start, one unit out at the mark-to-market close, and nothing
    # in between: the position was never swapped.
    assert run.turnover == Decimal(1)


def test_holding_nothing_earns_nothing_and_costs_nothing() -> None:
    # Every score is negative, so a threshold of zero puts the rule in cash for the whole run.
    falling = series_of("A", ["100", "90", "80", "70"])

    run = simulate(
        {"A": falling},
        RotationSpec(lookback=1, hold=1, normalised=False, threshold=Decimal(0)),
        cost_basis_points=COSTLY,
    )

    assert run.final_equity == INITIAL_EQUITY
    assert run.cost_paid == Decimal(0)
    assert run.bars_held == 0
    assert run.bars_in_cash == run.bars


# --- The conservation law -------------------------------------------------------------------------


def test_the_episodes_account_for_every_unit_of_equity_the_run_gained_or_lost() -> None:
    # Equity moves for exactly two reasons -- a position's market profit and a rebalance's cost
    # -- and both are filed against an episode. So the episodes must sum to the account's whole
    # change. If they do not, profit is appearing outside the per-asset books and the
    # concentration gate is reading a fiction.
    a = series_of("A", ["100", "120", "90", "150", "150"])
    b = series_of("B", ["100", "95", "160", "120", "200"])
    c = flat("C", 5)

    run = simulate(
        {"A": a, "B": b, "C": c}, RotationSpec(lookback=1, hold=1), cost_basis_points=COSTLY
    )

    booked = sum(episode.net_profit for episode in run.episodes)
    assert run.final_equity - INITIAL_EQUITY == booked


def test_contributions_and_episodes_tell_the_same_story_per_asset() -> None:
    a = series_of("A", ["100", "120", "90", "150"])
    b = series_of("B", ["100", "95", "160", "120"])

    run = simulate({"A": a, "B": b}, RotationSpec(lookback=1, hold=1), cost_basis_points=COSTLY)

    for contribution in run.contributions:
        episodes = [e for e in run.episodes if e.asset == contribution.asset]
        assert contribution.net_profit == sum(e.net_profit for e in episodes)
        assert contribution.episodes == len(episodes)
        assert contribution.bars_held == sum(e.bars for e in episodes)


def test_nothing_is_left_open_when_the_run_ends() -> None:
    a = series_of("A", ["100", "120", "140"])

    run = simulate({"A": a}, RotationSpec(lookback=1, hold=1), cost_basis_points=FREE)

    assert run.episodes
    assert all(episode.closed_at <= a[-1].open_time for episode in run.episodes)


# --- Scores are the platform's, not this module's -------------------------------------------------


def test_the_rule_holds_whichever_asset_the_platform_s_own_roc_ranks_first() -> None:
    # Not a restatement of the formula: the three assets are ranked independently by the
    # production pipeline, and the simulator must have held the one that ranking picks. A score
    # computed any other way would eventually disagree.
    data = {
        "AA": series_of("AA", ["100", "104", "97", "115", "121", "118"]),
        "BB": series_of("BB", ["100", "101", "99", "102", "103", "140"]),
        "CC": series_of("CC", ["100", "99", "98", "97", "96", "95"]),
    }
    pipeline = IndicatorFeatures(["roc_3"])
    # The last decision the loop makes is at the second-to-last slot, reading closes up to it.
    ranked = sorted(
        ((pipeline.compute(bars[:-1])["roc_3"], asset) for asset, bars in data.items()),
        reverse=True,
    )

    run = simulate(data, RotationSpec(lookback=3, hold=1), cost_basis_points=FREE)

    assert run.episodes[-1].asset == ranked[0][1]


def test_the_normalised_score_is_the_hurdle_vol_momentum_already_uses() -> None:
    # vol_momentum enters when roc > threshold * rvol * sqrt(lookback). Dividing through, its
    # threshold is a floor on roc / (rvol * sqrt(lookback)) -- which is what this simulator
    # ranks on. The declared 1.0 therefore means the same thing in both places.
    closes = ["100", "103", "99", "108", "112", "110", "119"]
    bars = series_of("A", closes)
    features = IndicatorFeatures(["roc_3", "rvol_3"]).compute(bars)
    score = features["roc_3"] / (features["rvol_3"] * Decimal(3).sqrt())

    held = simulate(
        {"A": bars},
        RotationSpec(
            lookback=3, hold=1, normalised=True, threshold=score - Decimal("0.01"), vol_window=3
        ),
        cost_basis_points=FREE,
    )
    flat_out = simulate(
        {"A": bars},
        RotationSpec(
            lookback=3, hold=1, normalised=True, threshold=score + Decimal("0.01"), vol_window=3
        ),
        cost_basis_points=FREE,
    )

    assert held.bars_held > flat_out.bars_held


def test_ties_are_broken_by_name_so_a_run_is_reproducible() -> None:
    # Identical series, so the scores tie exactly. Insertion order must not decide the holding,
    # or the same evidence would rank differently between two runs of the same screen.
    level = ["100", "110", "110"]
    first = simulate(
        {"A": series_of("A", level), "B": series_of("B", level)},
        RotationSpec(lookback=1, hold=1),
        cost_basis_points=FREE,
    )
    again = simulate(
        {"B": series_of("B", level), "A": series_of("A", level)},
        RotationSpec(lookback=1, hold=1),
        cost_basis_points=FREE,
    )

    assert [e.asset for e in first.episodes] == [e.asset for e in again.episodes] == ["A"]


def test_the_same_inputs_produce_the_same_run_twice() -> None:
    data = {
        "A": series_of("A", ["100", "120", "90", "150"]),
        "B": series_of("B", ["100", "95", "160", "120"]),
    }
    spec = RotationSpec(lookback=1, hold=1)

    first = simulate(data, spec, cost_basis_points=COSTLY)
    again = simulate(data, spec, cost_basis_points=COSTLY)

    assert first == again


# --- The regime filter ----------------------------------------------------------------------------


def test_the_regime_filter_keeps_the_rule_out_while_the_basket_is_falling() -> None:
    falling = series_of("A", ["100"] * 4 + ["90", "80", "70", "60", "50", "40"])
    other = series_of("B", ["100"] * 4 + ["95", "90", "85", "80", "75", "70"])

    unfiltered = simulate(
        {"A": falling, "B": other}, RotationSpec(lookback=1, hold=1), cost_basis_points=FREE
    )
    filtered = simulate(
        {"A": falling, "B": other},
        RotationSpec(lookback=1, hold=1, regime_filter=3),
        cost_basis_points=FREE,
    )

    assert unfiltered.bars_held > filtered.bars_held
    assert filtered.final_equity > unfiltered.final_equity


def test_an_unfilled_regime_filter_stays_out_rather_than_assuming_good_weather() -> None:
    rising = series_of("A", ["100", "110", "120", "130"])

    run = simulate(
        {"A": rising},
        RotationSpec(lookback=1, hold=1, regime_filter=100),
        cost_basis_points=FREE,
    )

    assert run.bars_held == 0


def test_the_basket_index_starts_at_one_and_ignores_the_slot_before_any_move() -> None:
    a = series_of("A", ["100", "110", "121"])
    grid = align({"A": a})
    levels = basket_index({"A": a}, grid)

    assert levels[0] is None
    assert levels[1] == Decimal("1.1")


def test_an_asset_joining_the_universe_does_not_put_a_step_in_the_index() -> None:
    # A new listing arrives at a different price level. An index built from *moves* is
    # unaffected; one built from average prices would jump.
    old = series_of("OLD", ["100", "110", "121"])
    new = series_of("NEW", ["7", "7"], offset=1)
    grid = align({"OLD": old, "NEW": new})

    levels = basket_index({"OLD": old, "NEW": new}, grid)

    assert levels[1] == Decimal("1.1")
    assert levels[2] == Decimal("1.1") * (1 + Decimal("0.1") / 2)


# --- Benchmarks -----------------------------------------------------------------------------------


def test_buy_and_hold_is_the_asset_s_own_return_from_the_second_bar() -> None:
    bars = series_of("BTCUSDT", ["100", "200", "400", "800"])

    run = buy_and_hold(bars, cost_basis_points=FREE)

    # Entered at the close of bar 1 (200) and held to 800: four times the money.
    assert run.final_equity == INITIAL_EQUITY * 4
    assert run.turnover == Decimal(1)


def test_the_equal_weight_basket_pays_once_to_get_in_and_then_stops_trading() -> None:
    # Two assets diverging hard. The basket buys each once and never corrects the drift, so the
    # whole run moves exactly one account's worth of notional: half into each asset, once. The
    # closing mark-to-market is not a trade and correctly adds nothing.
    a = series_of("A", ["100", "100", "200", "400"])
    b = flat("B", 4)

    run = equal_weight_basket({"A": a, "B": b}, cost_basis_points=FREE)

    assert run.turnover == Decimal(1)
    assert run.final_equity == INITIAL_EQUITY * Decimal("2.5")


def test_the_basket_pays_to_add_an_asset_that_joins_late() -> None:
    old = flat("OLD", 4)
    new = flat("NEW", 2, offset=2)

    run = equal_weight_basket({"OLD": old, "NEW": new}, cost_basis_points=COSTLY)

    assert run.cost_paid > Decimal(0)
    assert run.final_equity < INITIAL_EQUITY


# --- Reading a run --------------------------------------------------------------------------------


def test_weights_are_equal_and_sum_to_one() -> None:
    weights = weights_for(
        [("A", Decimal(3)), ("B", Decimal(2)), ("C", Decimal(1))], hold=2, threshold=None
    )

    assert set(weights) == {"A", "B"}
    assert sum(weights.values()) == Decimal(1)


def test_a_threshold_can_empty_the_book_entirely() -> None:
    assert weights_for([("A", Decimal(-1)), ("B", Decimal(-2))], hold=2, threshold=Decimal(0)) == {}


def test_a_threshold_can_leave_one_of_two_slots_in_cash() -> None:
    weights = weights_for([("A", Decimal(1)), ("B", Decimal(-1))], hold=2, threshold=Decimal(0))

    assert weights == {"A": Decimal(1)}


def test_max_drawdown_is_the_deepest_fall_from_a_peak_not_the_last_one() -> None:
    curve = tuple(
        EquityPoint(at=START + timedelta(days=i), equity=Decimal(v))
        for i, v in enumerate([100, 200, 100, 150, 120])
    )

    assert max_drawdown(curve) == Decimal("0.5")


def test_yearly_returns_chain_so_they_multiply_back_to_the_whole_run() -> None:
    curve = (
        EquityPoint(at=datetime(2020, 6, 1, tzinfo=UTC), equity=Decimal(200)),
        EquityPoint(at=datetime(2021, 6, 1, tzinfo=UTC), equity=Decimal(400)),
    )

    years = yearly_returns(curve, Decimal(100))

    assert years == {2020: Decimal(1), 2021: Decimal(1)}


def test_profit_factor_is_undefined_when_nothing_lost() -> None:
    a = series_of("A", ["100", "110", "120"])
    run = simulate({"A": a}, RotationSpec(lookback=1, hold=1), cost_basis_points=FREE)

    assert profit_factor(run.episodes) is None


def test_correlation_of_a_series_with_itself_is_one() -> None:
    a = series_of("A", ["100", "110", "99", "130"])
    b = series_of("B", ["50", "55", "49.5", "65"])

    found = pairwise_correlation({"A": a, "B": b}, ["A", "B"])

    assert found is not None
    assert abs(found - Decimal(1)) < Decimal("0.0001")


def test_correlation_needs_two_assets_to_mean_anything() -> None:
    a = series_of("A", ["100", "110"])

    assert pairwise_correlation({"A": a}, ["A"]) is None


@pytest.mark.parametrize("hold", [1, 2, 3])
def test_a_run_never_allocates_more_than_the_whole_account(hold: int) -> None:
    data = {
        "A": series_of("A", ["100", "120", "90", "150"]),
        "B": series_of("B", ["100", "95", "160", "120"]),
        "C": series_of("C", ["100", "105", "102", "108"]),
    }

    run = simulate(data, RotationSpec(lookback=1, hold=hold), cost_basis_points=FREE)

    assert run.final_equity > Decimal(0)
    assert run.bars_held <= run.bars
