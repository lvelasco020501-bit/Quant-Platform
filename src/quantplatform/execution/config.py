"""Deterministic execution assumptions for the simulated broker.

The fee and slippage formulas themselves live in
:mod:`quantplatform.core.models.execution_policy`, not here. They are shared verbatim with
the risk engine, which must fund exactly what this broker will charge; defining them twice
is how the two silently diverge. This module adds only what is genuinely execution-local —
how much of an order a bar absorbs — and bundles it with the shared policy.

Every knob is a fixed rule, never a random draw: a backtest whose fills depend on a random
number is not reproducible, and reproducibility is the whole point of simulating execution
rather than guessing at it.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from quantplatform.core.constants import ONE
from quantplatform.core.models.execution_policy import ExecutionPolicy, FeePolicy, SlippagePolicy

__all__ = ["ExecutionConfig"]


class ExecutionConfig(BaseModel):
    """Complete deterministic execution profile of a simulated venue."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    """Fee and slippage assumptions, shared with whatever risk engine funds these orders."""

    fill_ratio: Decimal = Field(default=ONE, gt=0, le=1)
    """Fraction of an order's remaining quantity executed per matching bar.

    ``1`` (the default) fills everything matchable in one go. A smaller value models a book
    that absorbs an order gradually — ``0.25``, ``0.5`` and ``0.75`` are the intended
    settings — without any randomness: the same order against the same bars always produces
    the same sequence of partial fills. The broker rounds each slice down onto the symbol's
    quantity step and never leaves a remainder below the venue minimum, so a partially
    filled order always terminates rather than halving forever.
    """

    @property
    def commission(self) -> FeePolicy:
        """Return the shared fee policy this venue charges under."""
        return self.policy.fee

    @property
    def slippage(self) -> SlippagePolicy:
        """Return the shared slippage policy this venue executes under."""
        return self.policy.slippage
