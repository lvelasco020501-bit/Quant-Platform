"""Portfolio accounting: balances, positions, realised and unrealised PnL, snapshots.

Responsibilities: apply fills exactly once, maintain positions and balances, and produce
immutable snapshots. Phase 3A (:class:`~quantplatform.portfolio.engine.SpotPortfolioEngine`)
implements deterministic spot accounting from fills that already exist. Order matching,
simulated execution and persistence remain later phases.
"""

from __future__ import annotations

from quantplatform.portfolio.engine import FillApplicationResult, SpotPortfolioEngine

__all__ = ["FillApplicationResult", "SpotPortfolioEngine"]
