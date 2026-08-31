"""Assembling the feature pipeline a strategy declared it needs.

Not a decision about *what* a strategy sees — the strategy already declared that in its
metadata — only about building something able to supply it. Shared by every composition root
so that a strategy run from paper trading and the same strategy run from the research harness
are fed by one implementation rather than two that must agree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quantplatform.features import ExponentialMovingAverageFeatures, NullFeaturePipeline

if TYPE_CHECKING:
    from quantplatform.core.interfaces import FeaturePipeline
    from quantplatform.strategies.base import BaseStrategy

__all__ = ["features_for"]


def features_for(strategy: BaseStrategy) -> FeaturePipeline:
    """Return a pipeline producing the features this strategy requires.

    An ``ema_<n>`` name is answered with an exponential pipeline of period ``n``; a strategy
    declaring no features gets the null pipeline. Anything else is left to the engine's
    contract check, which refuses a run whose pipeline cannot produce what the strategy
    requires rather than letting it start and go quiet.
    """
    periods = [
        int(name.removeprefix("ema_"))
        for name in strategy.metadata.required_features
        if name.startswith("ema_") and name.removeprefix("ema_").isdigit()
    ]
    if not periods:
        return NullFeaturePipeline()
    return ExponentialMovingAverageFeatures(periods)
