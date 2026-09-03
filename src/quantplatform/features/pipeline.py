"""Deterministic feature computation over closed bars.

Features are pure functions of a bar window. Nothing here reads a clock, draws a random
number or looks beyond the window it is handed, which is what lets a backtest reproduce its
own signals exactly: identical bars in, identical features out, identical decisions after.

A window too short to support a feature **omits** it rather than substituting a partial or
padded value. A strategy that declared the feature as required then fails its context check
loudly, instead of trading on a number that was quietly invented.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.errors import ConfigurationError
from quantplatform.core.models.market import MarketBar

__all__ = [
    "CompositeFeaturePipeline",
    "DonchianChannelFeatures",
    "ExponentialMovingAverageFeatures",
    "MovingAverageFeatures",
    "NullFeaturePipeline",
]

_MINIMUM_BARS_FOR_A_PRIOR_WINDOW = 2
"""One bar to decide on, plus at least one strictly before it to build a level from."""


class NullFeaturePipeline:
    """Produces no features at all.

    The honest default for a strategy that trades on price alone. Preferable to a pipeline
    that computes indicators nobody reads, which costs time and invites the assumption that
    a signal depended on them.
    """

    @property
    def feature_names(self) -> Sequence[str]:
        """Return the empty name tuple: this pipeline computes nothing."""
        return ()

    @property
    def required_history(self) -> int:
        """Return one: a strategy context needs at least the bar it is deciding on."""
        return 1

    def compute(self, bars: Sequence[MarketBar]) -> Mapping[str, Decimal]:
        """Return an empty mapping regardless of input."""
        _ = bars
        return {}


class MovingAverageFeatures:
    """Simple moving averages of the closing price, one per configured period.

    Emits ``sma_<period>`` for each period, plus ``close`` for the bar being decided on.
    Periods longer than the available window are omitted from the result.
    """

    def __init__(self, periods: Sequence[int]) -> None:
        """Configure the periods to compute.

        Args:
            periods: Strictly positive lookbacks, in bars.

        Raises:
            ConfigurationError: If a period is not strictly positive, or none were given.
        """
        if not periods:
            raise ConfigurationError("a moving-average pipeline needs at least one period")
        if any(period <= 0 for period in periods):
            raise ConfigurationError(
                "moving-average periods must be strictly positive",
                periods=list(periods),
            )
        self._periods = tuple(sorted(set(periods)))

    @property
    def periods(self) -> tuple[int, ...]:
        """Return the configured periods, ascending and deduplicated."""
        return self._periods

    @property
    def feature_names(self) -> Sequence[str]:
        """Return ``close`` followed by one ``sma_<period>`` name per period."""
        return ("close", *(f"sma_{period}" for period in self._periods))

    @property
    def required_history(self) -> int:
        """Return the longest configured period: the shortest window computing every feature."""
        return self._periods[-1]

    def compute(self, bars: Sequence[MarketBar]) -> Mapping[str, Decimal]:
        """Return the closing price and every computable moving average.

        Args:
            bars: Closed bars in ascending open-time order.

        Returns:
            ``close`` plus one entry per period the window is long enough to support.
        """
        if not bars:
            return {}
        closes = [bar.close for bar in bars]
        features: dict[str, Decimal] = {"close": closes[-1]}
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            for period in self._periods:
                if len(closes) < period:
                    continue
                window = closes[-period:]
                features[f"sma_{period}"] = sum(window, start=ZERO) / Decimal(period)
        return features


class ExponentialMovingAverageFeatures:
    """Exponential moving averages of the closing price, one per configured period.

    Emits ``ema_<period>`` for each period, plus ``close`` for the bar being decided on.

    **Seeded from a simple average, not from the first close.** The recursive form has to
    start somewhere, and starting from a single price makes the early values depend heavily
    on one bar. Seeding with the simple mean of the first ``period`` closes and recursing
    from there is the conventional choice and, more importantly here, a deterministic one:
    the same window always produces the same number, with no warm-up state carried between
    calls. A period longer than the window is omitted rather than approximated.
    """

    def __init__(self, periods: Sequence[int]) -> None:
        """Configure the periods to compute.

        Args:
            periods: Strictly positive lookbacks, in bars.

        Raises:
            ConfigurationError: If a period is not strictly positive, or none were given.
        """
        if not periods:
            raise ConfigurationError("an exponential-average pipeline needs at least one period")
        if any(period <= 0 for period in periods):
            raise ConfigurationError(
                "exponential-average periods must be strictly positive", periods=list(periods)
            )
        self._periods = tuple(sorted(set(periods)))

    @property
    def periods(self) -> tuple[int, ...]:
        """Return the configured periods, ascending and deduplicated."""
        return self._periods

    @property
    def feature_names(self) -> Sequence[str]:
        """Return ``close`` followed by one ``ema_<period>`` name per period."""
        return ("close", *(f"ema_{period}" for period in self._periods))

    @property
    def required_history(self) -> int:
        """Return the longest configured period: the shortest window computing every feature."""
        return self._periods[-1]

    def compute(self, bars: Sequence[MarketBar]) -> Mapping[str, Decimal]:
        """Return the closing price and every computable exponential average.

        Args:
            bars: Closed bars in ascending open-time order.

        Returns:
            ``close`` plus one entry per period the window is long enough to support.
        """
        if not bars:
            return {}
        closes = [bar.close for bar in bars]
        features: dict[str, Decimal] = {"close": closes[-1]}
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            for period in self._periods:
                if len(closes) < period:
                    continue
                features[f"ema_{period}"] = _exponential_average(closes, period)
        return features


def _exponential_average(closes: Sequence[Decimal], period: int) -> Decimal:
    """Return the exponential moving average of a close series.

    Seeded with the simple mean of the first ``period`` closes, then recursed forward with
    a smoothing factor of ``2 / (period + 1)``. Caller is responsible for the local decimal
    context; every operation here is exact decimal arithmetic.
    """
    smoothing = Decimal(2) / Decimal(period + 1)
    average = sum(closes[:period], start=ZERO) / Decimal(period)
    for close in closes[period:]:
        average = (close - average) * smoothing + average
    return average


class DonchianChannelFeatures:
    """Rolling highest-high and lowest-low over a window ending *before* the decided-on bar.

    Emits ``donchian_high_<period>`` and ``donchian_low_<period>`` for each configured period.
    Unlike :class:`MovingAverageFeatures` and :class:`ExponentialMovingAverageFeatures`, whose
    windows legitimately include the bar being decided on — an average including today's own
    close is not look-ahead, it is the strategy asking "what happened including today" — a
    breakout level exists to be tested *against* today, and would stop meaning that the moment
    today's own high or low could move it. So the window this pipeline reads is ``bars[:-1]``,
    never ``bars``: the last bar is excluded before anything is computed, unconditionally.

    That exclusion is also why :attr:`required_history` is ``period + 1`` rather than
    ``period`` — one bar more than :class:`MovingAverageFeatures` needs for the same period,
    because the bar being decided on does not count toward the window here.
    """

    def __init__(self, periods: Sequence[int]) -> None:
        """Configure the periods to compute.

        Args:
            periods: Strictly positive lookbacks, in bars, each counted over the bars
                strictly preceding the one being decided on.

        Raises:
            ConfigurationError: If a period is not strictly positive, or none were given.
        """
        if not periods:
            raise ConfigurationError("a Donchian-channel pipeline needs at least one period")
        if any(period <= 0 for period in periods):
            raise ConfigurationError(
                "Donchian-channel periods must be strictly positive", periods=list(periods)
            )
        self._periods = tuple(sorted(set(periods)))

    @property
    def periods(self) -> tuple[int, ...]:
        """Return the configured periods, ascending and deduplicated."""
        return self._periods

    @property
    def feature_names(self) -> Sequence[str]:
        """Return one ``donchian_high_<period>`` and one ``donchian_low_<period>`` per period."""
        return tuple(
            name
            for period in self._periods
            for name in (f"donchian_high_{period}", f"donchian_low_{period}")
        )

    @property
    def required_history(self) -> int:
        """Return the longest period plus one: that many prior bars, plus the current one."""
        return self._periods[-1] + 1

    def compute(self, bars: Sequence[MarketBar]) -> Mapping[str, Decimal]:
        """Return the highest high and lowest low of the bars strictly before the last one.

        Args:
            bars: Closed bars in ascending open-time order; the last is the bar being decided
                and is excluded from every window computed here.

        Returns:
            ``donchian_high_<period>``/``donchian_low_<period>`` for each period the *prior*
            window is long enough to support. A period the prior window cannot fill is
            omitted, never approximated from a shorter window.
        """
        if len(bars) < _MINIMUM_BARS_FOR_A_PRIOR_WINDOW:
            return {}
        prior = bars[:-1]
        highs = [bar.high for bar in prior]
        lows = [bar.low for bar in prior]
        features: dict[str, Decimal] = {}
        for period in self._periods:
            if len(prior) < period:
                continue
            features[f"donchian_high_{period}"] = max(highs[-period:])
            features[f"donchian_low_{period}"] = min(lows[-period:])
        return features


class CompositeFeaturePipeline:
    """Runs several pipelines and merges their output.

    Merging is order-dependent by construction, so a name produced by two pipelines resolves
    to the later one. That is a configuration mistake rather than a runtime choice, so it is
    rejected at construction instead of silently resolved.
    """

    def __init__(self, pipelines: Sequence[object]) -> None:
        """Compose pipelines, refusing overlapping feature names.

        Args:
            pipelines: Pipelines to run, each satisfying
                :class:`~quantplatform.core.interfaces.FeaturePipeline`.

        Raises:
            ConfigurationError: If two pipelines produce the same feature name.
        """
        seen: set[str] = set()
        for pipeline in pipelines:
            names = set(pipeline.feature_names)  # type: ignore[attr-defined]
            clash = names & seen
            if clash:
                raise ConfigurationError(
                    "two feature pipelines produce the same feature name",
                    names=sorted(clash),
                )
            seen |= names
        self._pipelines = tuple(pipelines)

    @property
    def feature_names(self) -> Sequence[str]:
        """Return every name across the composed pipelines."""
        return tuple(
            name
            for pipeline in self._pipelines
            for name in pipeline.feature_names  # type: ignore[attr-defined]
        )

    @property
    def required_history(self) -> int:
        """Return the deepest history any composed pipeline needs."""
        if not self._pipelines:
            return 1
        return max(
            int(pipeline.required_history)  # type: ignore[attr-defined]
            for pipeline in self._pipelines
        )

    def compute(self, bars: Sequence[MarketBar]) -> Mapping[str, Decimal]:
        """Return the merged feature mapping of every composed pipeline."""
        merged: dict[str, Decimal] = {}
        for pipeline in self._pipelines:
            merged.update(pipeline.compute(bars))  # type: ignore[attr-defined]
        return merged
