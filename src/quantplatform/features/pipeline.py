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

__all__ = ["CompositeFeaturePipeline", "MovingAverageFeatures", "NullFeaturePipeline"]


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
