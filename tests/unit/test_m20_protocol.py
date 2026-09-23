"""M20's protocol: caps set where they can bind, and an allowance counted over three years.

M19's budgets never acted — the cap sat above the worst drawdown and the allowance counted a
year while the chains ran for three and four. These tests pin the corrections, including the
one that is easy to get wrong twice: J pairs the allowance with the *looser* cap, because
under the tighter one the cap binds first and the allowance goes untested again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantplatform.research.m17 import POLICIES
from quantplatform.research.m19 import GLOBAL_CAP
from quantplatform.research.m20 import (
    CAPS,
    FIRST_MARKETS,
    POLICIES_M20,
    RESET_ALLOWANCE,
    RESET_WINDOW,
    THEN_MARKETS,
)
from quantplatform.research.recovery import Recovery, resets_within


def test_the_four_policies_are_g_and_three_budgets() -> None:
    assert [p.key for p in POLICIES_M20] == ["G", "H12", "H15", "J"]


def test_every_policy_shares_gs_local_limit_and_cooldown() -> None:
    g = next(p for p in POLICIES if p.key == "G")
    for policy in POLICIES_M20:
        assert policy.drawdown_pct == g.drawdown_pct
        assert policy.cooldown == g.cooldown
        assert policy.recovery is Recovery.COOLDOWN_AND_RESTART


def test_both_caps_sit_below_the_damage_m18_actually_reached() -> None:
    # M18's ratchets reached 19.16% and 19.09%; M19's 20% cap could not bind on either.
    worst_observed = Decimal("0.1909")
    for cap in CAPS:
        assert cap < worst_observed
    assert worst_observed < GLOBAL_CAP, "the cap M19 used, kept here only as the contrast"


def test_the_caps_straddle_rather_than_guess_a_single_number() -> None:
    assert (Decimal("0.12"), Decimal("0.15")) == CAPS
    assert CAPS[0] < CAPS[1]


def test_the_hybrid_pairs_the_allowance_with_the_looser_cap() -> None:
    # Under the tighter cap the cap binds first and the allowance is never exercised, which
    # is precisely how M19 ended up with three budgets that did nothing.
    hybrid = next(p for p in POLICIES_M20 if p.key == "J")
    assert hybrid.global_drawdown_cap == max(CAPS)
    assert hybrid.max_resets_per_year == RESET_ALLOWANCE


def test_the_allowance_is_counted_over_three_years_not_one() -> None:
    assert timedelta(days=3 * 365) == RESET_WINDOW
    hybrid = next(p for p in POLICIES_M20 if p.key == "J")
    assert hybrid.reset_window == RESET_WINDOW


def test_a_three_year_window_sees_the_chains_a_one_year_window_missed() -> None:
    # XRP's chain in M18: restarts across 2020-04-30, 2022-02 and 2023-11.
    restarts = [
        datetime(2020, 4, 30, tzinfo=UTC),
        datetime(2022, 2, 1, tzinfo=UTC),
        datetime(2023, 11, 23, tzinfo=UTC),
    ]
    moment = datetime(2023, 11, 23, tzinfo=UTC)
    assert resets_within(restarts, moment, timedelta(days=365)) == 1
    assert resets_within(restarts, moment, RESET_WINDOW) >= RESET_ALLOWANCE


def test_the_markets_that_matter_are_run_first() -> None:
    assert FIRST_MARKETS == ("XRPUSDT", "ADAUSDT")
    assert set(FIRST_MARKETS) & set(THEN_MARKETS) == set()
    assert len(FIRST_MARKETS) + len(THEN_MARKETS) == 6


def test_a_reset_window_without_an_allowance_is_refused() -> None:
    with pytest.raises(ValueError, match="allowance"):
        POLICIES_M20[1].model_copy(update={"reset_window": RESET_WINDOW}).model_validate(
            {
                **POLICIES_M20[1].model_dump(),
                "reset_window": RESET_WINDOW,
                "max_resets_per_year": None,
            }
        )
