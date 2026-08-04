"""Deterministic feature computation over closed bars.

Features are computed from closed bars only and are supplied to strategies through
:class:`~quantplatform.core.models.signals.StrategyContext`.

Every pipeline is a pure function of the bar window it is handed: no clock, no input or
output, no randomness, and no visibility past the bar being decided on. That is what lets a
backtest reproduce its signals exactly, and what keeps a feature from seeing a price that had
not yet printed.
"""

from __future__ import annotations

from quantplatform.features.pipeline import (
    CompositeFeaturePipeline,
    MovingAverageFeatures,
    NullFeaturePipeline,
)

__all__ = ["CompositeFeaturePipeline", "MovingAverageFeatures", "NullFeaturePipeline"]
