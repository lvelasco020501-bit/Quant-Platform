"""The allocation M42 proposes: same holdings as equal weight, weighted by the reciprocal of risk.

The claims worth testing are not about returns -- nothing here simulates anything -- but about
construction, because M42's whole argument rests on one property: **both arms hold the same
capital in the same bars.** Without it a lower drawdown would just mean less money at work, and
the experiment would answer a question nobody asked. So the exposure tests here are the load
bearing ones, and the cap and water-filling tests exist to show that the per-market limit is
preserved without quietly taking the shortfall out of the market.

``test_the_cap_can_never_force_a_shortfall`` is the lemma the pre-declaration asserts in prose.
``test_the_volatility_is_the_production_number`` is the other half: the volatility is not a
reimplementation that happens to agree, it is the indicator the strategies read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.features.indicators import IndicatorFeatures
from quantplatform.research.m42 import EXPOSURE_MATCH_TOLERANCE
from quantplatform.research.portfolio import (
    Allocation,
    Holding,
    deployed,
    inverse_vol_targets,
    positions,
    realised_volatility,
    simulate_portfolio,
    targets_for,
)
from quantplatform.research.rotation import ZERO, align
from tests.factories import make_bar

WINDOW = 3
"""Short enough to read by hand. The window M42 declares is M32's 72; nothing here depends on
the size, only on the rule."""


def series_of(closes: list[Decimal], *, start: int = 0, skip: int = 0) -> tuple[MarketBar, ...]:
    """Return a bar series, optionally with a calendar hole after ``skip`` bars."""
    out = []
    for index, close in enumerate(closes):
        shift = start + index + (12 if skip and index >= skip else 0)
        out.append(make_bar(index=shift, close=close, timeframe=Timeframe.D1))
    return tuple(out)


RISING = [Decimal(100), Decimal(101), Decimal(102), Decimal(104), Decimal(103), Decimal(107)]


# --- The volatility -------------------------------------------------------------------------------


def test_the_volatility_is_the_production_number() -> None:
    # Asserted against the indicator pipeline itself rather than against a hand-computed
    # standard deviation: the point is that M42 reads the project's own rvol_<n>, so a test that
    # agreed with an independent formula would prove the weaker thing.
    bars = series_of(RISING)
    grid = align({"A": bars})
    column = realised_volatility({"A": bars}, positions({"A": bars}, grid), window=WINDOW)
    expected = IndicatorFeatures([f"rvol_{WINDOW}"]).compute(bars)[f"rvol_{WINDOW}"]

    assert column["A"][-1] == expected


def test_a_window_that_is_not_yet_full_has_no_volatility() -> None:
    # rvol_n reads n + 1 closes, because n returns need n + 1 prices. The first n slots are
    # therefore silent, and that one extra bar is the whole insufficient-history case M42
    # declares: a market is eligible one bar before it is measurable.
    bars = series_of(RISING)
    grid = align({"A": bars})
    column = realised_volatility({"A": bars}, positions({"A": bars}, grid), window=WINDOW)["A"]

    assert column[:WINDOW] == (None,) * WINDOW
    assert column[WINDOW] is not None


def test_a_window_broken_by_a_listing_gap_has_no_volatility() -> None:
    # M31's lesson, applied here rather than rediscovered: FTT was halted for 311 days, and its
    # bars either side of the hole are adjacent in the file. A 3-bar return across that seam is
    # not a 3-bar return, so it is not a volatility either.
    bars = series_of(RISING, skip=3)
    grid = align({"A": bars})
    column = realised_volatility({"A": bars}, positions({"A": bars}, grid), window=WINDOW)["A"]

    assert bars[3].open_time - bars[2].open_time > timedelta(days=1)
    assert column[3] is None


def test_a_flat_window_reports_zero_rather_than_nothing() -> None:
    # Zero is the true answer and is reported as such. Refusing to fund it is the allocator's
    # decision, made where the reciprocal is actually taken.
    flat = series_of([Decimal(100)] * 6)
    grid = align({"A": flat})
    column = realised_volatility({"A": flat}, positions({"A": flat}, grid), window=WINDOW)["A"]

    assert column[-1] == ZERO


def test_the_volatility_at_a_slot_does_not_depend_on_later_bars() -> None:
    # The no-look-ahead claim, tested by changing the future and demanding the past hold still.
    bars = series_of(RISING)
    tampered = (*bars[:5], make_bar(index=5, close=Decimal(900), timeframe=Timeframe.D1))
    grid = align({"A": bars})
    before = realised_volatility({"A": bars}, positions({"A": bars}, grid), window=WINDOW)["A"]
    after = realised_volatility(
        {"A": tampered}, positions({"A": tampered}, align({"A": tampered})), window=WINDOW
    )["A"]

    assert after[:5] == before[:5]
    assert after[5] != before[5]


# --- The allocation -------------------------------------------------------------------------------

ALLOCATION = Allocation(universe_size=4, sleeves=2)
ELIGIBLE = (frozenset({"A", "B", "C", "D"}),)
"""One slot, four eligible markets, so the cap is 1/4 and one signal is worth 1/8."""


def one_slot(
    wanted: list[Holding], vols: Mapping[str, Decimal | None]
) -> tuple[dict[Holding, Decimal], Decimal, Decimal]:
    """Return the inverse-vol weights for a single slot, with the budget it had to match."""
    masks = dict.fromkeys(wanted, (True,))
    equal = targets_for(masks, ELIGIBLE, ALLOCATION)
    reference = deployed(equal)
    plan = inverse_vol_targets(
        masks,
        ELIGIBLE,
        ALLOCATION,
        volatility={asset: (vol,) for asset, vol in vols.items()},
        reference=reference,
    )
    return plan.targets[0], reference[0], plan.deficit[0]


def test_weights_are_proportional_to_the_reciprocal_of_volatility() -> None:
    weights, _, _ = one_slot(
        [("s1", "A"), ("s1", "B")], {"A": Decimal("0.01"), "B": Decimal("0.02")}
    )

    # B is twice as volatile, so it carries half the weight.
    assert weights[("s1", "A")] / weights[("s1", "B")] == Decimal(2)


def test_the_funded_set_is_the_one_equal_weight_would_have_funded() -> None:
    # Same masks, same eligibility, same holdings. M42 changes how much, never what.
    masks = {("s1", "A"): (True,), ("s1", "B"): (True,), ("s1", "Z"): (True,)}
    vols = {asset: (Decimal("0.01"),) for asset in ("A", "B", "Z")}
    equal = targets_for(masks, ELIGIBLE, ALLOCATION)
    plan = inverse_vol_targets(
        masks, ELIGIBLE, ALLOCATION, volatility=vols, reference=deployed(equal)
    )

    assert set(plan.targets[0]) == set(equal[0])
    assert ("s1", "Z") not in plan.targets[0]  # not eligible at that slot


@pytest.mark.parametrize(
    "vols",
    [
        {"A": Decimal("0.01"), "B": Decimal("0.02"), "C": Decimal("0.04")},
        {"A": Decimal("0.001"), "B": Decimal("0.2"), "C": Decimal("0.05")},
        {"A": Decimal("0.03"), "B": Decimal("0.03"), "C": Decimal("0.03")},
    ],
)
def test_the_slot_deploys_exactly_what_equal_weight_deployed(vols: dict[str, Decimal]) -> None:
    # The load-bearing property. Without it a drawdown comparison is a comparison of two
    # different amounts of money at work.
    wanted: list[Holding] = [("s1", "A"), ("s1", "B"), ("s2", "B"), ("s1", "C")]
    weights, budget, deficit = one_slot(wanted, vols)

    assert abs(sum(weights.values(), start=ZERO) - budget) <= EXPOSURE_MATCH_TOLERANCE
    assert abs(deficit) <= EXPOSURE_MATCH_TOLERANCE


def test_no_slot_ever_deploys_more_than_equal_weight() -> None:
    # No leverage, stated as the inequality rather than as an intention.
    wanted: list[Holding] = [("s1", "A"), ("s1", "B")]
    weights, budget, _ = one_slot(wanted, {"A": Decimal("0.0001"), "B": Decimal("0.5")})

    assert sum(weights.values(), start=ZERO) <= budget + EXPOSURE_MATCH_TOLERANCE


def test_no_market_may_exceed_the_cap_however_quiet_it_is() -> None:
    # A is a hundred times quieter than the others, so proportionality alone would hand it most
    # of the book. The cap equal weight already enforces is not something inverse-vol relaxes.
    wanted: list[Holding] = [("s1", "A"), ("s1", "B"), ("s1", "C"), ("s1", "D")]
    weights, budget, _ = one_slot(
        wanted,
        {"A": Decimal("0.0001"), "B": Decimal("0.01"), "C": Decimal("0.01"), "D": Decimal("0.01")},
    )

    assert weights[("s1", "A")] <= ALLOCATION.per_asset_cap
    assert abs(sum(weights.values(), start=ZERO) - budget) <= EXPOSURE_MATCH_TOLERANCE


def test_the_cap_binds_on_the_market_not_on_one_sleeve() -> None:
    # Two sleeves long the same market hold one instrument, so the limit has to be read on the
    # market. Both of A's sleeves together, not each of them, sit under the cap.
    wanted: list[Holding] = [("s1", "A"), ("s2", "A"), ("s1", "B")]
    weights, _, _ = one_slot(wanted, {"A": Decimal("0.0005"), "B": Decimal("0.05")})

    together = weights[("s1", "A")] + weights[("s2", "A")]
    assert together <= ALLOCATION.per_asset_cap
    assert weights[("s1", "A")] == weights[("s2", "A")]


def test_what_the_cap_takes_off_one_market_goes_to_the_others() -> None:
    # Water-filling. The alternative -- leaving the capped surplus in cash -- would make the arm
    # hold less than the baseline and confound every number downstream. A is quiet enough that
    # strict proportionality would hand it 99.9% of the book; pinned at the cap, the two thirds
    # it cannot have are divided between B and C, still in inverse proportion to their own
    # volatilities rather than evenly.
    wanted: list[Holding] = [("s1", "A"), ("s1", "B"), ("s1", "C")]
    capped, budget, _ = one_slot(
        wanted, {"A": Decimal("0.00001"), "B": Decimal("0.02"), "C": Decimal("0.04")}
    )
    unconstrained = budget * Decimal(100_000) / Decimal(100_000 + 50 + 25)

    assert capped[("s1", "A")] == ALLOCATION.per_asset_cap
    assert unconstrained > ALLOCATION.per_asset_cap  # the cap really did bind
    assert capped[("s1", "B")] + capped[("s1", "C")] == budget - ALLOCATION.per_asset_cap
    assert capped[("s1", "B")] / capped[("s1", "C")] == Decimal(2)  # still inverse to vol
    assert abs(sum(capped.values(), start=ZERO) - budget) <= EXPOSURE_MATCH_TOLERANCE


@pytest.mark.parametrize("unusable", [None, ZERO])
def test_a_market_whose_volatility_cannot_be_used_is_not_funded(unusable: Decimal | None) -> None:
    # Unknown risk is the last thing that should be sized as if it were known, and the
    # reciprocal of zero does not exist. Its share goes to the measurable holdings.
    wanted: list[Holding] = [("s1", "A"), ("s1", "B")]
    weights, budget, deficit = one_slot(wanted, {"A": Decimal("0.02"), "B": unusable})

    assert ("s1", "B") not in weights
    assert weights[("s1", "A")] == budget  # redistributed, not withheld
    assert abs(deficit) <= EXPOSURE_MATCH_TOLERANCE


def test_a_bar_with_nothing_measurable_stays_in_cash_and_is_counted() -> None:
    # The only way a deficit can arise at all, so it is reported rather than absorbed.
    masks = {("s1", "A"): (True,)}
    equal = targets_for(masks, ELIGIBLE, ALLOCATION)
    plan = inverse_vol_targets(
        masks, ELIGIBLE, ALLOCATION, volatility={"A": (None,)}, reference=deployed(equal)
    )

    assert plan.targets[0] == {}
    assert plan.deficit[0] == deployed(equal)[0]
    assert plan.unfunded_bars == 1


def test_a_bar_equal_weight_left_in_cash_is_not_given_a_position() -> None:
    masks = {("s1", "A"): (False,)}
    plan = inverse_vol_targets(
        masks,
        ELIGIBLE,
        ALLOCATION,
        volatility={"A": (Decimal("0.02"),)},
        reference=(ZERO,),
    )

    assert plan.targets[0] == {}
    assert plan.deficit[0] == ZERO
    assert plan.unfunded_bars == 0


def test_the_cap_can_never_force_a_shortfall() -> None:
    # The lemma M42's pre-declaration states: equal weight deploys at most
    # `wanted markets / universe size`, which is exactly the caps available, so water-filling
    # always has somewhere left to put the budget. Checked at the tightest possible case --
    # every sleeve long every eligible market, which is where the two bounds coincide.
    allocation = Allocation(universe_size=3, sleeves=2)
    eligible = (frozenset({"A", "B", "C"}),)
    wanted: list[Holding] = [(sleeve, asset) for sleeve in ("s1", "s2") for asset in "ABC"]
    masks = dict.fromkeys(wanted, (True,))
    vols = {"A": (Decimal("0.0001"),), "B": (Decimal("0.05"),), "C": (Decimal("0.5"),)}
    reference = deployed(targets_for(masks, eligible, allocation))
    plan = inverse_vol_targets(masks, eligible, allocation, volatility=vols, reference=reference)

    # Fully invested: the hardest case for the cap, and the one where the two bounds coincide.
    assert abs(reference[0] - Decimal(1)) <= EXPOSURE_MATCH_TOLERANCE
    assert abs(plan.deficit[0]) <= EXPOSURE_MATCH_TOLERANCE
    for asset in "ABC":
        mine = sum((w for holding, w in plan.targets[0].items() if holding[1] == asset), start=ZERO)
        # All three pinned, nothing left over, to the last digit the precision carries.
        assert abs(mine - allocation.per_asset_cap) <= EXPOSURE_MATCH_TOLERANCE


def test_a_longer_run_matches_exposure_at_every_single_bar() -> None:
    masks = {
        ("s1", "A"): (True, True, False, True, True),
        ("s1", "B"): (False, True, True, True, False),
        ("s2", "A"): (True, False, False, True, True),
    }
    eligible = (frozenset({"A", "B"}),) * 5
    vols = {
        "A": (Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.01"), Decimal("0.05")),
        "B": (Decimal("0.04"), Decimal("0.01"), Decimal("0.02"), Decimal("0.06"), Decimal("0.01")),
    }
    reference = deployed(targets_for(masks, eligible, ALLOCATION))
    plan = inverse_vol_targets(masks, eligible, ALLOCATION, volatility=vols, reference=reference)

    for slot, weights in enumerate(plan.targets):
        assert abs(sum(weights.values(), start=ZERO) - reference[slot]) <= EXPOSURE_MATCH_TOLERANCE
    assert plan.unfunded_bars == 0
    assert all(abs(value) <= EXPOSURE_MATCH_TOLERANCE for value in plan.deficit)


# --- Isolating one window of an already measured run ----------------------------------------------


def test_a_bounded_window_measures_only_the_bars_inside_it() -> None:
    # What makes drawdown attribution possible: the same run, restricted to the peak-to-trough
    # window, so each market's contribution is the one it made *there* rather than over the
    # whole sample. Without it, an asset that lost during the drawdown and recovered afterwards
    # would be reported as blameless.
    closes = [Decimal(100), Decimal(110), Decimal(50), Decimal(120)]
    bars = series_of(closes)
    series = {"A": bars}
    grid = align(series)
    targets: tuple[dict[Holding, Decimal], ...] = tuple({("s1", "A"): Decimal(1)} for _ in grid)

    whole = simulate_portfolio(series, targets, cost_basis_points=ZERO)
    crash = simulate_portfolio(series, targets, cost_basis_points=ZERO, start=grid[1], end=grid[2])

    assert whole.bars == len(grid) - 1
    assert crash.bars == 1
    assert whole.total_return > 0  # 100 -> 120 over the whole run
    assert crash.total_return < 0  # 110 -> 50 inside the window
