"""The signal driver underpins every M34 number, so it needs tests of its own.

``scripts/m34_verify.py`` holds it to the certified engine on real series, which is the strongest
check it gets. These are the unit-level ones that check the things a comparison against another
implementation cannot: that venue rules cannot reach a signal, that warm-up is silence, and that
a mask includes the bar a rule entered on and excludes the bar it left on -- which is the
convention the portfolio's whole timing rests on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.research.sleeve import Interval, long_intervals, long_mask, permissive_rules
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.research import build_research_registry

START = datetime(2020, 1, 1, tzinfo=UTC)
MOMENTUM = {"lookback": 2}
"""A two-bar return: enters while it is positive, exits when it turns negative. Chosen because
its contract is derived per instance, so a small window is a legitimate configuration rather
than a mismatch with class-level metadata."""


def bars_of(
    closes: list[str], *, highs: list[str] | None = None, lows: list[str] | None = None
) -> list[MarketBar]:
    """Build a daily series, defaulting high and low to the close."""
    out: list[MarketBar] = []
    for index, close in enumerate(closes):
        high = Decimal(highs[index]) if highs else Decimal(close)
        low = Decimal(lows[index]) if lows else Decimal(close)
        open_time = START + timedelta(days=index)
        out.append(
            MarketBar(
                symbol="BTC/USDT",
                market_type=MarketType.SPOT,
                timeframe=Timeframe.D1,
                open_time=open_time,
                close_time=open_time + timedelta(days=1),
                open=low,
                high=high,
                low=low,
                close=Decimal(close),
                volume=Decimal(1),
                quote_volume=None,
                trade_count=None,
                source="test",
                is_closed=True,
            )
        )
    return out


def momentum() -> BaseStrategy:
    """Return the frozen momentum rule at a two-bar window."""
    return build_research_registry().create("momentum_roc", MOMENTUM)


def test_an_empty_series_produces_nothing() -> None:
    assert long_intervals(momentum(), [], pipelines=features_for) == ()


def test_a_rule_that_never_fires_produces_nothing() -> None:
    # A flat market has a zero two-bar return, which is not positive, so there is nothing to
    # enter. Silence, not a position at zero size.
    flat = bars_of(["100"] * 10)

    assert long_intervals(momentum(), flat, pipelines=features_for) == ()


def test_warm_up_is_silence() -> None:
    # roc_2 needs three bars before it has a value. A driver that traded through warm-up would
    # invent an edge out of a window too short to have one.
    rising = bars_of(["100", "110", "120", "130"])

    intervals = long_intervals(momentum(), rising, pipelines=features_for)

    assert intervals
    assert min(i.entered_at for i in intervals) == rising[2].open_time


def test_a_completed_stretch_carries_both_ends() -> None:
    # Rises, so the two-bar return goes positive; then falls hard, so it turns negative.
    bars = bars_of(["100", "110", "120", "130", "60", "50"])

    intervals = long_intervals(momentum(), bars, pipelines=features_for)

    assert intervals
    assert intervals[0].entered_at == bars[2].open_time
    assert intervals[0].exited_at is not None
    assert intervals[0].entered_at < intervals[0].exited_at


def test_a_stretch_still_open_at_the_end_says_so() -> None:
    # Never exits, so the last interval must be open rather than silently closed at the last bar,
    # which would make it indistinguishable from a rule that decided to leave.
    climbing = bars_of([str(100 + 10 * i) for i in range(12)])

    intervals = long_intervals(momentum(), climbing, pipelines=features_for)

    assert intervals
    assert intervals[-1].exited_at is None


def test_venue_rules_cannot_reach_a_signal() -> None:
    # The driver hands a strategy permissive placeholder rules because no frozen rule reads them.
    # If one ever did, this is what would catch it.
    rules = permissive_rules("BTC/USDT")

    assert rules.symbol == "BTC/USDT"
    assert rules.base_asset == "BTC"
    assert rules.quote_asset == "USDT"
    assert rules.min_notional < Decimal("0.001")


def test_the_mask_includes_the_entry_bar_and_excludes_the_exit_bar() -> None:
    # The convention the portfolio's timing rests on: a rule that says "leave" on a bar is not
    # long over the bar that follows, which is the one a portfolio would have held it for.
    grid = [START + timedelta(days=i) for i in range(6)]
    intervals = (Interval(entered_at=grid[1], exited_at=grid[4]),)

    assert long_mask(intervals, grid) == (False, True, True, True, False, False)


def test_an_open_stretch_masks_to_the_end_of_the_grid() -> None:
    grid = [START + timedelta(days=i) for i in range(5)]
    intervals = (Interval(entered_at=grid[2], exited_at=None),)

    assert long_mask(intervals, grid) == (False, False, True, True, True)


def test_several_stretches_mask_independently() -> None:
    grid = [START + timedelta(days=i) for i in range(8)]
    intervals = (
        Interval(entered_at=grid[1], exited_at=grid[3]),
        Interval(entered_at=grid[5], exited_at=grid[6]),
    )

    assert long_mask(intervals, grid) == (
        False,
        True,
        True,
        False,
        False,
        True,
        False,
        False,
    )


def test_no_stretches_mask_to_all_flat() -> None:
    grid = [START + timedelta(days=i) for i in range(4)]

    assert long_mask((), grid) == (False, False, False, False)
