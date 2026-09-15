"""Slower bars built from faster ones: exact aggregation, nothing invented, nothing lost quietly.

Open is the first bar's open, close the last bar's close, high and low the extremes, volume
and trade count the sums — the only aggregation that describes the same market the source
bars did. A bucket is built only when every one of its source bars is present.

**What may be dropped, and what may not.** A dataset rarely starts or ends on a slower
boundary, so an incomplete bucket at either *edge* is dropped: it is the dataset's boundary,
not a hole in the data. A missing bar anywhere else is refused — a 4h bar built from three
hours would describe a market nobody observed, and silently skipping it would shift every
indicator that reads a window across it.

**The one exception is named, never inferred.** An exchange occasionally halts trading for a
few hours; those hours have no kline because nothing traded, and the exchange's own 4h and 1d
klines are built from the hours that did trade. A caller may pass exactly those hours as
``allowed_missing``; a bucket missing only named hours is built from the hours present, which is
the exchange's own bar, and anything else missing is still refused. The M15 dataset proves the
claim rather than assuming it, by comparing every resampled bar with Binance's official one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from itertools import groupby, pairwise

from quantplatform.core.enums import Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.core.timeutils import bar_close_time, floor_to_timeframe

__all__ = ["resample_bars"]


def resample_bars(
    bars: Sequence[MarketBar],
    timeframe: Timeframe,
    *,
    allowed_missing: frozenset[datetime] = frozenset(),
) -> tuple[MarketBar, ...]:
    """Aggregate ``bars`` into complete bars of a slower ``timeframe``.

    Args:
        bars: Closed bars of one symbol and one timeframe, in ascending order.
        timeframe: A slower timeframe the source timeframe divides exactly.
        allowed_missing: Source open times documented as exchange outages. A bucket missing
            only these is built from the bars it has; empty by default, so nothing is ever
            assumed to be an outage.

    Returns:
        One bar per complete bucket, in order.

    Raises:
        ValueError: If ``timeframe`` is not slower than the source, or if any bucket other
            than the first or last is missing a source bar, or a whole bucket is missing.
    """
    if not bars:
        return ()
    source = bars[0].timeframe
    if timeframe.seconds <= source.seconds or timeframe.seconds % source.seconds:
        msg = f"can only resample {source.value} bars to a slower timeframe it divides exactly"
        raise ValueError(msg)
    per_bucket = timeframe.seconds // source.seconds

    groups = [
        (start, list(members))
        for start, members in groupby(
            bars, key=lambda bar: floor_to_timeframe(bar.open_time, timeframe)
        )
    ]
    out: list[MarketBar] = []
    for index, (start, members) in enumerate(groups):
        expected = [start + source.duration * i for i in range(per_bucket)]
        times = [bar.open_time for bar in members]
        if times == expected:
            out.append(_aggregate(start, members, timeframe))
            continue
        missing_here = set(expected) - set(times)
        if missing_here <= allowed_missing and times == [t for t in expected if t in set(times)]:
            out.append(_aggregate(start, members, timeframe))
            continue
        leading = index == 0 and times == expected[-len(times) :]
        trailing = index == len(groups) - 1 and times == expected[: len(times)]
        if leading or trailing:
            continue
        missing = sorted(set(expected) - set(times))
        msg = (
            f"the {timeframe.value} bucket at {start.isoformat()} is missing "
            f"{len(missing)} of its {per_bucket} source bars"
        )
        raise ValueError(msg)

    for earlier, later in pairwise(out):
        # A bucket with no bar at all is acceptable only when every one of its source hours
        # is a documented outage: then the exchange has no bar there either.
        cursor = earlier.open_time + timeframe.duration
        while cursor < later.open_time:
            hours = [cursor + source.duration * i for i in range(per_bucket)]
            if not all(hour in allowed_missing for hour in hours):
                after = earlier.open_time.isoformat()
                msg = f"whole {timeframe.value} buckets are missing after {after}"
                raise ValueError(msg)
            cursor += timeframe.duration
    return tuple(out)


def _aggregate(start: datetime, members: Sequence[MarketBar], timeframe: Timeframe) -> MarketBar:
    first, last = members[0], members[-1]
    quote = [bar.quote_volume for bar in members]
    counts = [bar.trade_count for bar in members]
    return MarketBar(
        symbol=first.symbol,
        market_type=first.market_type,
        timeframe=timeframe,
        open_time=start,
        close_time=bar_close_time(start, timeframe),
        open=first.open,
        high=max(bar.high for bar in members),
        low=min(bar.low for bar in members),
        close=last.close,
        volume=sum((bar.volume for bar in members), start=Decimal(0)),
        quote_volume=(
            None
            if any(q is None for q in quote)
            else sum((q for q in quote if q is not None), start=Decimal(0))
        ),
        trade_count=(
            None if any(c is None for c in counts) else sum(c for c in counts if c is not None)
        ),
        source=first.source,
        is_closed=True,
    )
