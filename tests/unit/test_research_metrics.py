"""Counting streaks and exposure, over records rather than over a run.

Both metrics are simple enough that the only way to get them wrong is at a boundary, and both
have a boundary that reads oddly until it is written down: a trade that broke exactly even is
neither a win nor a loss, and a bar that held nothing is not a bar that held zero.
"""

from __future__ import annotations

from decimal import Decimal

from quantplatform.backtesting.metrics import longest_streaks, time_in_market
from quantplatform.core.models.trades import ClosedTrade
from tests.factories import ANCHOR, SYMBOL


def _trade(pnl: str) -> ClosedTrade:
    return ClosedTrade(
        symbol=SYMBOL,
        realized_pnl=Decimal(pnl),
        initial_risk_amount=Decimal(100),
        opened_at=ANCHOR,
        closed_at=ANCHOR,
    )


def test_the_longest_run_of_losses_is_the_longest_run_and_not_the_last() -> None:
    trades = [_trade(p) for p in ("-1", "-1", "-1", "5", "-1", "-1")]

    wins, losses = longest_streaks(trades)

    assert losses == 3
    assert wins == 1


def test_a_trade_that_broke_even_breaks_both_streaks() -> None:
    # Neither a win nor a loss. Letting it continue either run would report a streak that did
    # not happen, and which of the two it extended would depend on an arbitrary choice.
    trades = [_trade(p) for p in ("5", "0", "5")]

    wins, losses = longest_streaks(trades)

    assert wins == 1
    assert losses == 0


def test_no_trades_means_no_streaks() -> None:
    assert longest_streaks([]) == (0, 0)


def test_time_in_market_is_one_when_every_bar_held_something() -> None:
    assert time_in_market(exposed_bars=10, bars_processed=10) == Decimal(1)


def test_time_in_market_is_zero_when_no_bar_held_anything() -> None:
    assert time_in_market(exposed_bars=0, bars_processed=10) == Decimal(0)


def test_time_in_market_is_the_share_of_bars_and_not_of_elapsed_time() -> None:
    # Bars, not wall time: a gap in the feed is not time spent holding a position, and on any
    # timeframe the two only agree when nothing is missing — which is exactly the case where
    # the distinction would not have mattered.
    assert time_in_market(exposed_bars=3, bars_processed=12) == Decimal("0.25")


def test_a_run_that_processed_no_bars_reports_no_exposure_rather_than_dividing() -> None:
    assert time_in_market(exposed_bars=0, bars_processed=0) is None
