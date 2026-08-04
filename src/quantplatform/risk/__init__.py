"""Risk engine: the final authority over every order intent.

Responsibilities: evaluate the documented risk checks, size positions, enforce trading
limits and guarantee idempotency. :class:`~quantplatform.risk.engine.StandardRiskEngine` is
the only component in the platform permitted to produce an
:class:`~quantplatform.core.models.orders.ApprovedOrder`, which is what makes traversing it
unavoidable on the path from a strategy signal to a venue.
"""

from __future__ import annotations

from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.engine import RiskEvaluationResult, StandardRiskEngine

__all__ = ["RiskConfiguration", "RiskEvaluationResult", "StandardRiskEngine"]
