"""Checking that the bars a run consumes are the dataset it claims to describe.

A definition names a symbol, a market, a timeframe and a range. Nothing before this module
checked that the bars handed to the engine actually were that — a loader bug, a mixed-up
join, or a caller passing the wrong sequence would run silently, and the digest would
fingerprint whatever arrived rather than what was declared. This closes that gap before the
digest is taken, not after: the whole point is that the fingerprint names the bars that were
actually validated, never bars nobody checked.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.errors import DatasetMismatchError
from quantplatform.research.dataset import validate_dataset
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.digest import bars_digest
from tests.factories import ANCHOR, make_bar, make_bars, make_experiment_definition


def _definition() -> ExperimentDefinition:
    return make_experiment_definition(
        symbol="BTC/USDT",
        market_type=MarketType.SPOT,
        timeframe="1h",
        start=ANCHOR,
        end=ANCHOR + timedelta(hours=8),
    )


def test_bars_that_match_the_dataset_come_back_unchanged_as_a_tuple() -> None:
    bars = make_bars([Decimal(50_000)] * 4)

    validated = validate_dataset(_definition(), bars)

    assert validated == tuple(bars)


def test_an_empty_dataset_is_rejected() -> None:
    with pytest.raises(DatasetMismatchError, match="no bars"):
        validate_dataset(_definition(), ())


def test_a_bar_from_another_symbol_is_rejected_and_names_its_index() -> None:
    bars = (
        *make_bars([Decimal(50_000)]),
        make_bar(index=1, close=Decimal(50_000)).model_copy(update={"symbol": "ETH/USDT"}),
    )

    with pytest.raises(DatasetMismatchError, match="symbol") as excinfo:
        validate_dataset(_definition(), bars)

    assert excinfo.value.details["index"] == 1


def test_a_bar_from_another_market_type_is_rejected_even_though_the_digest_cannot_see_it() -> None:
    # canonical_bar_line has nine fields and market_type is not one of them: two datasets
    # differing only in market would fingerprint identically. This check is the only place
    # in the pipeline that can tell them apart, which is why it exists here rather than
    # being left to the digest.
    spot_bars = make_bars([Decimal(50_000)])
    futures_bars = (spot_bars[0].model_copy(update={"market_type": MarketType.FUTURES}),)

    assert bars_digest(spot_bars) == bars_digest(futures_bars)
    with pytest.raises(DatasetMismatchError, match="market_type"):
        validate_dataset(_definition(), futures_bars)


def test_a_bar_of_the_wrong_timeframe_is_rejected() -> None:
    bar = make_bar(close=Decimal(50_000), timeframe=Timeframe.M15)

    with pytest.raises(DatasetMismatchError, match="timeframe"):
        validate_dataset(_definition(), (bar,))


def test_a_dataset_naming_something_that_is_not_a_timeframe_does_not_construct() -> None:
    with pytest.raises(ValueError, match=r"[Tt]imeframe"):
        make_experiment_definition(timeframe="17-minutes")


def test_a_bar_opening_before_the_dataset_starts_is_rejected() -> None:
    bar = make_bar(index=-1, close=Decimal(50_000))

    with pytest.raises(DatasetMismatchError, match="range"):
        validate_dataset(_definition(), (bar,))


def test_a_bar_opening_exactly_at_end_is_rejected_because_the_range_is_half_open() -> None:
    bar = make_bar(index=8, close=Decimal(50_000))  # opens exactly at ANCHOR + 8h == dataset.end

    with pytest.raises(DatasetMismatchError, match="range"):
        validate_dataset(_definition(), (bar,))


def test_a_bar_opening_exactly_at_start_is_accepted() -> None:
    bar = make_bar(index=0, close=Decimal(50_000))  # opens exactly at ANCHOR == dataset.start

    validated = validate_dataset(_definition(), (bar,))

    assert validated == (bar,)


def test_an_open_bar_cannot_be_used_as_evidence() -> None:
    bar = make_bar(close=Decimal(50_000), is_closed=False)

    with pytest.raises(DatasetMismatchError, match="closed"):
        validate_dataset(_definition(), (bar,))


def test_a_repeated_or_out_of_order_open_time_is_rejected() -> None:
    first = make_bar(index=1, close=Decimal(50_000))
    duplicate = make_bar(index=1, close=Decimal(51_000))

    with pytest.raises(DatasetMismatchError, match="order"):
        validate_dataset(_definition(), (first, duplicate))


def test_a_gap_between_bars_is_accepted() -> None:
    # Missing candles are a data-quality question the ingestion layer already owns
    # (DataGapError); rejecting them here would make almost any real history unrunnable.
    # Strict ordering (tested above) still catches duplicates and reversed bars.
    first = make_bar(index=0, close=Decimal(50_000))
    after_a_gap = make_bar(index=3, close=Decimal(50_500))

    validated = validate_dataset(_definition(), (first, after_a_gap))

    assert validated == (first, after_a_gap)
