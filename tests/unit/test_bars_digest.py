"""Naming the data an experiment actually consumed.

A definition names a dataset — symbol, timeframe, a range, a label — and a label can be
wrong. Re-ingesting a venue's history, correcting a bad candle, or reading a different
vintage of the same days all produce a run over "the same dataset" by that description and a
different one in fact. Without a fingerprint over the bars themselves, a run that disagrees
with its predecessor is indistinguishable from a run over different numbers, and the
reproducibility question cannot be asked at all.
"""

from __future__ import annotations

from decimal import Decimal

from quantplatform.research.digest import bars_digest, canonical_bar_line
from tests.factories import make_bar, make_bars


def test_the_same_bars_always_produce_the_same_digest() -> None:
    assert bars_digest(make_bars([Decimal(50_000)] * 5)) == bars_digest(
        make_bars([Decimal(50_000)] * 5)
    )


def test_one_changed_candle_changes_the_digest() -> None:
    original = make_bars([Decimal(50_000)] * 5)
    corrected = (*original[:3], make_bar(index=3, close=Decimal("50000.01")), *original[4:])

    assert bars_digest(original) != bars_digest(corrected)


def test_the_order_of_the_bars_is_part_of_the_dataset() -> None:
    # The engine consumes a sequence, not a set: the same candles in another order are a
    # different run, and would produce different fills. A digest blind to order would call
    # two genuinely different datasets the same one.
    bars = make_bars([Decimal(50_000), Decimal(51_000), Decimal(52_000)])

    assert bars_digest(bars) != bars_digest(tuple(reversed(bars)))


def test_a_price_written_with_different_scale_is_the_same_price() -> None:
    # 50000, 50000.00 and 5E+4 are one number. Hashing their default string forms would make
    # a re-ingest at a different decimal scale look like corrected data, and every historical
    # comparison would break for a reason nobody could see in the numbers.
    plain = canonical_bar_line(make_bar(index=0, close=Decimal(50_000)))
    padded = canonical_bar_line(make_bar(index=0, close=Decimal("50000.00")))
    exponent = canonical_bar_line(make_bar(index=0, close=Decimal("5E+4")))

    assert plain == padded == exponent


def test_a_different_symbol_over_identical_prices_is_a_different_dataset() -> None:
    btc = make_bars([Decimal(50_000)] * 3)
    eth = tuple(
        make_bar(index=index, close=Decimal(50_000), symbol="ETH/USDT") for index in range(3)
    )

    assert bars_digest(btc) != bars_digest(eth)


def test_an_empty_dataset_has_a_stable_digest_of_its_own() -> None:
    # A run over no bars is a real outcome the engine already supports. It needs a name like
    # any other, and two of them must agree.
    assert bars_digest(()) == bars_digest(())
    assert bars_digest(()) != bars_digest(make_bars([Decimal(50_000)]))


def test_the_canonical_line_contains_no_field_separator_inside_a_field() -> None:
    # The encoding is unambiguous only while that holds — the same guarantee the platform's
    # idempotency keys already rely on.
    line = canonical_bar_line(make_bar(index=0, close=Decimal(50_000)))

    assert line.count("|") == 8
