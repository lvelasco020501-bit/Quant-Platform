"""Execution adapters for the simulated, paper, shadow and live venues.

Adapters accept only risk-approved orders and never generate signals or make risk
decisions. Phase 3B implements the deterministic simulated broker
(:class:`~quantplatform.execution.broker.SimulatedBroker`); paper and shadow follow in phase
7 and the live adapter in phase 10, with live execution disabled by default.
"""

from __future__ import annotations

from quantplatform.execution.broker import (
    CancellationResult,
    ExecutionResult,
    SimulatedBroker,
    SubmissionResult,
)
from quantplatform.execution.config import ExecutionConfig

__all__ = [
    "CancellationResult",
    "ExecutionConfig",
    "ExecutionResult",
    "SimulatedBroker",
    "SubmissionResult",
]
