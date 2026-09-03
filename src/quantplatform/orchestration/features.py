"""Assembling the feature pipeline a strategy declared it needs.

Not a decision about *what* a strategy sees — the strategy already declared that in its
metadata — only about building something able to supply it. Shared by every composition root
so that a strategy run from paper trading and the same strategy run from the research harness
are fed by one implementation rather than two that must agree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quantplatform.features import (
    CompositeFeaturePipeline,
    DonchianChannelFeatures,
    ExponentialMovingAverageFeatures,
    NullFeaturePipeline,
)

if TYPE_CHECKING:
    from quantplatform.core.interfaces import FeaturePipeline
    from quantplatform.strategies.base import BaseStrategy

__all__ = ["features_for"]


def _periods_for(prefix: str, names: tuple[str, ...]) -> list[int]:
    """Return the sorted, deduplicated integer suffixes of names starting with ``prefix``."""
    return sorted(
        {
            int(name.removeprefix(prefix))
            for name in names
            if name.startswith(prefix) and name.removeprefix(prefix).isdigit()
        }
    )


def features_for(strategy: BaseStrategy) -> FeaturePipeline:
    """Return a pipeline producing the features this strategy requires.

    An ``ema_<n>`` name is answered with an exponential pipeline of period ``n``; a
    ``donchian_high_<n>`` or ``donchian_low_<n>`` name is answered with a Donchian-channel
    pipeline of period ``n`` — one pipeline serves both names, since a channel's high and low
    are computed together. A strategy needing both kinds gets a
    :class:`~quantplatform.features.CompositeFeaturePipeline` of the two; one declaring
    neither gets the null pipeline. Anything else is left to the engine's contract check,
    which refuses a run whose pipeline cannot produce what the strategy requires rather than
    letting it start and go quiet.
    """
    required = strategy.metadata.required_features
    ema_periods = _periods_for("ema_", required)
    # Donchian names come in two prefixes for one pipeline; a period declared under either
    # is enough to build the pipeline, since DonchianChannelFeatures always computes both.
    donchian_periods = sorted(
        set(_periods_for("donchian_high_", required)) | set(_periods_for("donchian_low_", required))
    )

    pipelines: list[object] = []
    if ema_periods:
        pipelines.append(ExponentialMovingAverageFeatures(ema_periods))
    if donchian_periods:
        pipelines.append(DonchianChannelFeatures(donchian_periods))

    if not pipelines:
        return NullFeaturePipeline()
    if len(pipelines) == 1:
        return pipelines[0]  # type: ignore[return-value]
    return CompositeFeaturePipeline(pipelines)
