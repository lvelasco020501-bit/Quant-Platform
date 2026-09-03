"""Checking that the bars a run consumes are the dataset it claims to describe.

A definition names a symbol, a market, a timeframe and a range. Nothing upstream of this
module checked that the bars actually handed to the engine were that — a loader bug, a
mixed-up join, or a caller passing the wrong sequence would run silently, and the fingerprint
taken afterwards would name whatever arrived rather than what was declared.

So this runs *before* the digest, not after, and it returns the bars it validated rather than
leaving the caller to trust that whatever was checked is whatever gets used next. The digest
that follows is over these bars, never over the sequence that was handed in.

Gaps between bars are deliberately not checked here. A missing candle is a data-quality
question the ingestion layer already owns (:class:`~quantplatform.core.errors.DataGapError`),
and rejecting every gap would make most real market history unrunnable. Strict ordering still
catches duplicates and reversed bars, which is the failure mode actually reachable from a
loader bug.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from quantplatform.core.errors import DatasetMismatchError

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar
    from quantplatform.research.definition import ExperimentDefinition

__all__ = ["validate_dataset"]


def validate_dataset(
    definition: ExperimentDefinition, bars: Sequence[MarketBar]
) -> tuple[MarketBar, ...]:
    """Check that ``bars`` is the dataset ``definition`` names, and return it as a tuple.

    Every bar is checked against the declared symbol, market type, timeframe and half-open
    range, must already be closed, and must open strictly after the one before it. The first
    bar that fails any of these ends the check immediately, naming its index in the sequence
    so the caller does not have to re-scan to find it.

    Args:
        definition: What the bars are supposed to be.
        bars: What was actually loaded.

    Returns:
        ``bars`` as a tuple, unchanged, once every element has passed.

    Raises:
        DatasetMismatchError: If the sequence is empty, or any bar disagrees with the
            dataset's symbol, market type, timeframe or declared range, is not closed, or does
            not open strictly after the bar before it.
    """
    dataset = definition.dataset
    if not bars:
        msg = "a dataset with no bars cannot be validated"
        raise DatasetMismatchError(msg, symbol=dataset.symbol)

    previous_open_time = None
    for index, bar in enumerate(bars):
        if bar.symbol != dataset.symbol:
            msg = "a bar's symbol does not match the dataset it was loaded for"
            raise DatasetMismatchError(msg, index=index, expected=dataset.symbol, found=bar.symbol)
        if bar.market_type is not dataset.market_type:
            msg = "a bar's market_type does not match the dataset it was loaded for"
            raise DatasetMismatchError(
                msg,
                index=index,
                expected=dataset.market_type.value,
                found=bar.market_type.value,
            )
        if bar.timeframe is not dataset.timeframe:
            msg = "a bar's timeframe does not match the dataset it was loaded for"
            raise DatasetMismatchError(
                msg,
                index=index,
                expected=dataset.timeframe.value,
                found=bar.timeframe.value,
            )
        if not bar.is_closed:
            msg = "a bar that is not yet closed cannot be used as evidence"
            raise DatasetMismatchError(msg, index=index, open_time=bar.open_time.isoformat())
        if not (dataset.start <= bar.open_time < dataset.end):
            msg = "a bar's open_time falls outside the dataset's declared range"
            raise DatasetMismatchError(msg, index=index, open_time=bar.open_time.isoformat())
        if previous_open_time is not None and bar.open_time <= previous_open_time:
            msg = "bars must be strictly ordered by open_time, with no repeats"
            raise DatasetMismatchError(msg, index=index, open_time=bar.open_time.isoformat())
        previous_open_time = bar.open_time

    return tuple(bars)
