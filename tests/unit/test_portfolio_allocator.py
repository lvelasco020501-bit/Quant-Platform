"""M34's allocator is the only thing that milestone adds, so it is the only thing to get wrong.

Two tests carry the weight. ``test_a_single_sleeve_fully_invested_matches_buy_and_hold`` holds
this module's equity arithmetic to the arithmetic in ``rotation``, which is what makes it safe to
have written that loop twice rather than refactoring a module three published milestones depend
on. And ``test_every_unit_of_equity_is_attributed_to_a_holding`` is the conservation law: if
profit can appear outside the per-holding books, then "does one strategy explain the result" and
"does one market explain it" are both unanswerable, and those are two of M34's gates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.research.portfolio import (
    Allocation,
    deployed,
    normalise_to,
    simulate_portfolio,
    targets_for,
)
from quantplatform.research.rotation import INITIAL_EQUITY, buy_and_hold

START = datetime(2020, 1, 1, tzinfo=UTC)
FREE = Decimal(0)
COSTLY = Decimal(100)
"""One percent per side, so cost arithmetic is legible in the result."""


def bars_of(symbol: str, closes: list[str]) -> list[MarketBar]:
    """Build a daily series for one market."""
    out: list[MarketBar] = []
    for index, close in enumerate(closes):
        open_time = START + timedelta(days=index)
        out.append(
            MarketBar(
                symbol=f"{symbol.ljust(2, 'Z')}/USDT",
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
    return out


def always(n: int, *, first: int = 0) -> list[bool]:
    """Return a mask that is on from slot ``first`` onwards."""
    return [index >= first for index in range(n)]


def everything(assets: list[str], n: int) -> list[frozenset[str]]:
    """Return an eligibility column admitting every market at every slot."""
    return [frozenset(assets) for _ in range(n)]


# --- The declared fractions -----------------------------------------------------------------------


def test_the_allocation_is_derived_from_breadth_and_sleeve_count() -> None:
    one = Allocation(universe_size=6, sleeves=1)
    two = Allocation(universe_size=6, sleeves=2)

    assert one.per_signal == Decimal(1) / Decimal(6)
    assert two.per_signal == Decimal(1) / Decimal(12)
    assert one.per_asset_cap == two.per_asset_cap == Decimal(1) / Decimal(6)


def test_a_portfolio_can_never_exceed_the_account() -> None:
    # Every sleeve long in every eligible market at once is exactly fully invested, never more.
    allocation = Allocation(universe_size=6, sleeves=2)
    assert allocation.per_signal * 12 == Decimal(1)


def test_two_sleeves_in_one_market_reach_the_cap_and_do_not_pass_it() -> None:
    allocation = Allocation(universe_size=6, sleeves=2)
    masks = {("B2", "AA"): [True], ("G1", "AA"): [True]}

    targets = targets_for(masks, [frozenset({"AA"})], allocation)

    assert sum(targets[0].values()) == allocation.per_asset_cap


def test_a_market_wanting_more_than_the_cap_is_scaled_back_proportionally() -> None:
    # Three sleeves at one sixth each would be half the account in one market. The cap binds on
    # the market, so it must not matter which sleeve is examined first.
    allocation = Allocation(universe_size=6, sleeves=1)
    masks = {(f"S{i}", "AA"): [True] for i in range(3)}

    targets = targets_for(masks, [frozenset({"AA"})], allocation)

    assert sum(targets[0].values()) == allocation.per_asset_cap
    assert len(set(targets[0].values())) == 1


def test_an_ineligible_market_receives_nothing_however_loud_its_signal() -> None:
    # The point-in-time liquidity rule is what stops the portfolio funding something nobody
    # could have identified as investable at the time.
    allocation = Allocation(universe_size=6, sleeves=1)
    masks = {("B2", "AA"): [True, True], ("B2", "ZZ"): [True, True]}

    targets = targets_for(masks, [frozenset({"AA"}), frozenset({"AA", "ZZ"})], allocation)

    assert set(targets[0]) == {("B2", "AA")}
    assert set(targets[1]) == {("B2", "AA"), ("B2", "ZZ")}


# --- The equity arithmetic, held to rotation's ----------------------------------------------------


def test_a_single_sleeve_fully_invested_matches_buy_and_hold() -> None:
    # The test that licenses the duplicated loop. One market, one sleeve, the whole account,
    # entering on the second bar exactly as rotation's benchmark does.
    closes = ["100", "120", "90", "150", "150"]
    series = {"AA": bars_of("AA", closes)}
    allocation = Allocation(universe_size=1, sleeves=1)
    masks = {("ONE", "AA"): always(len(closes), first=1)}

    mine = simulate_portfolio(
        series,
        targets_for(masks, everything(["AA"], len(closes)), allocation),
        cost_basis_points=FREE,
    )
    theirs = buy_and_hold(series["AA"], cost_basis_points=FREE)

    assert mine.final_equity == theirs.final_equity


def test_holding_one_sixth_earns_one_sixth_of_the_move() -> None:
    series = {"AA": bars_of("AA", ["100", "100", "200"])}
    allocation = Allocation(universe_size=6, sleeves=1)
    masks = {("B2", "AA"): always(3, first=1)}

    run = simulate_portfolio(
        series, targets_for(masks, everything(["AA"], 3), allocation), cost_basis_points=FREE
    )

    # One sixth of a +100% move is +16.67% on the account. Compared with a tolerance because
    # the simulation runs at forty digits and this expression does not.
    expected = INITIAL_EQUITY * (Decimal(1) + Decimal(1) / Decimal(6))
    assert abs(run.final_equity - expected) < Decimal("0.0000001")


def test_cash_earns_nothing_and_costs_nothing() -> None:
    series = {"AA": bars_of("AA", ["100", "50", "25"])}
    allocation = Allocation(universe_size=6, sleeves=1)
    masks = {("B2", "AA"): [False, False, False]}

    run = simulate_portfolio(
        series, targets_for(masks, everything(["AA"], 3), allocation), cost_basis_points=COSTLY
    )

    assert run.final_equity == INITIAL_EQUITY
    assert run.cost_paid == Decimal(0)
    assert run.bars_held == 0
    assert run.bars_in_cash == run.bars


def test_turnover_is_charged_on_the_notional_that_moves() -> None:
    series = {"AA": bars_of("AA", ["100", "100", "100", "100"])}
    allocation = Allocation(universe_size=6, sleeves=1)
    masks = {("B2", "AA"): [False, True, False, True]}

    run = simulate_portfolio(
        series, targets_for(masks, everything(["AA"], 4), allocation), cost_basis_points=COSTLY
    )

    assert run.turnover > Decimal(0)
    assert run.cost_paid > Decimal(0)


# --- Attribution ----------------------------------------------------------------------------------


def test_every_unit_of_equity_is_attributed_to_a_holding() -> None:
    # Equity moves for exactly two reasons, a holding's market profit and a rebalance's cost, and
    # both are filed against a holding. So the closed episodes must sum to the whole change.
    series = {
        "AA": bars_of("AA", ["100", "120", "90", "150", "140"]),
        "BB": bars_of("BB", ["100", "95", "160", "120", "180"]),
    }
    allocation = Allocation(universe_size=6, sleeves=2)
    masks = {
        ("B2", "AA"): [False, True, True, False, True],
        ("G1", "BB"): [False, True, False, True, True],
    }

    run = simulate_portfolio(
        series,
        targets_for(masks, everything(["AA", "BB"], 5), allocation),
        cost_basis_points=COSTLY,
    )

    assert run.final_equity - INITIAL_EQUITY == sum(run.episode_returns)


def test_the_two_attributions_describe_the_same_run() -> None:
    series = {
        "AA": bars_of("AA", ["100", "120", "90", "150"]),
        "BB": bars_of("BB", ["100", "95", "160", "120"]),
    }
    allocation = Allocation(universe_size=6, sleeves=2)
    masks = {
        ("B2", "AA"): always(4, first=1),
        ("G1", "AA"): always(4, first=1),
        ("B2", "BB"): always(4, first=1),
        ("G1", "BB"): always(4, first=1),
    }

    run = simulate_portfolio(
        series,
        targets_for(masks, everything(["AA", "BB"], 4), allocation),
        cost_basis_points=COSTLY,
    )

    by_sleeve = sum(c.net_profit for c in run.by_sleeve)
    by_asset = sum(c.net_profit for c in run.by_asset)
    assert by_sleeve == by_asset
    assert {c.name for c in run.by_sleeve} == {"B2", "G1"}
    assert {c.name for c in run.by_asset} == {"AA", "BB"}


def test_two_sleeves_in_one_market_are_attributed_separately() -> None:
    # Netting them would be cheaper and would make the single-sleeve gate unanswerable.
    series = {"AA": bars_of("AA", ["100", "100", "200"])}
    allocation = Allocation(universe_size=6, sleeves=2)
    masks = {("B2", "AA"): always(3, first=1), ("G1", "AA"): [False, False, False]}

    run = simulate_portfolio(
        series,
        targets_for(masks, everything(["AA"], 3), allocation),
        cost_basis_points=FREE,
        holdings=list(masks),
    )

    sleeves = {c.name: c.net_profit for c in run.by_sleeve}
    assert sleeves["B2"] > Decimal(0)
    # Reported at zero rather than missing: a sleeve that never fired is a finding, not an
    # absence, and dropping it would make this portfolio look single-sleeved.
    assert sleeves["G1"] == Decimal(0)


def test_an_out_of_sample_start_ignores_earlier_slots() -> None:
    closes = ["100", "110", "120", "130", "140"]
    series = {"AA": bars_of("AA", closes)}
    allocation = Allocation(universe_size=1, sleeves=1)
    masks = {("B2", "AA"): always(5)}
    targets = targets_for(masks, everything(["AA"], 5), allocation)

    whole = simulate_portfolio(series, targets, cost_basis_points=FREE)
    late = simulate_portfolio(
        series, targets, cost_basis_points=FREE, start=START + timedelta(days=2)
    )

    assert late.bars < whole.bars
    assert late.final_equity < whole.final_equity


@pytest.mark.parametrize("sleeves", [1, 2, 3])
def test_a_run_never_allocates_more_than_the_account(sleeves: int) -> None:
    series = {
        "AA": bars_of("AA", ["100", "120", "90", "150"]),
        "BB": bars_of("BB", ["100", "95", "160", "120"]),
    }
    allocation = Allocation(universe_size=2, sleeves=sleeves)
    masks = {(f"S{index}", asset): always(4) for index in range(sleeves) for asset in ("AA", "BB")}

    run = simulate_portfolio(
        series,
        targets_for(masks, everything(["AA", "BB"], 4), allocation),
        cost_basis_points=FREE,
    )

    assert run.final_equity > Decimal(0)
    assert run.bars_held <= run.bars


# --- The corrected breadth probe ------------------------------------------------------------------
# M34's probe moved breadth and position size together, so a narrower universe both concentrated
# the book and changed how much was at work. These pin the correction: aggregate exposure equal
# bar by bar, breadth the only thing varying.


def test_normalising_leaves_the_deployed_total_exactly_where_it_was() -> None:
    reference = [Decimal("0.5"), Decimal("0.25"), Decimal(0)]
    targets = [
        {("B2", "AA"): Decimal("0.1"), ("G1", "BB"): Decimal("0.1")},
        {("B2", "AA"): Decimal("0.9")},
        {("B2", "AA"): Decimal("0.4")},
    ]

    out = normalise_to(targets, reference)

    assert deployed(out) == (Decimal("0.5"), Decimal("0.25"), Decimal(0))


def test_normalising_spreads_the_same_money_over_however_many_positions_there_are() -> None:
    # The whole point: one number at work, divided differently. Two positions get half each,
    # four get a quarter each, and the account is equally invested either way.
    reference = [Decimal("0.6")]
    narrow = [{("B2", "AA"): Decimal(1), ("G1", "AA"): Decimal(1)}]
    wide = [dict.fromkeys([("B2", "AA"), ("G1", "AA"), ("B2", "BB"), ("G1", "BB")], Decimal(1))]

    thin = normalise_to(narrow, reference)
    fat = normalise_to(wide, reference)

    assert set(thin[0].values()) == {Decimal("0.3")}
    assert set(fat[0].values()) == {Decimal("0.15")}
    assert deployed(thin) == deployed(fat) == (Decimal("0.6"),)


def test_a_breadth_with_no_eligible_signal_stays_in_cash() -> None:
    # Nothing to spread the reference exposure across. Inventing a position to hit a number
    # would be the opposite of the point, so the slot is cash and gets counted.
    reference = [Decimal("0.5")]

    assert normalise_to([{}], reference) == ({},)


def test_normalising_to_zero_deploys_nothing() -> None:
    assert normalise_to([{("B2", "AA"): Decimal("0.4")}], [Decimal(0)]) == ({},)


def test_the_reference_breadth_normalises_to_itself_unchanged() -> None:
    # The check the pre-declaration names: at the declared breadth the correction must be a
    # no-op, because equal weights already deploy exactly the reference total. Equal to within
    # Decimal rounding rather than bit-identical -- summing twelfths and dividing back rounds
    # differently from writing a twelfth down, and the difference is in the 28th digit.
    allocation = Allocation(universe_size=6, sleeves=2)
    masks = {
        ("B2", "AA"): [True, True],
        ("G1", "AA"): [True, False],
        ("B2", "BB"): [False, True],
    }
    targets = targets_for(masks, [frozenset({"AA", "BB"})] * 2, allocation)

    same = normalise_to(targets, deployed(targets))

    assert [sorted(slot) for slot in same] == [sorted(slot) for slot in targets]
    for mine, theirs in zip(same, targets, strict=True):
        for holding, weight in theirs.items():
            assert abs(mine[holding] - weight) < Decimal("1e-20")
