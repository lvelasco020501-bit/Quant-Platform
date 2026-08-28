"""Where a position stops losing money.

In its own module for a structural reason rather than a stylistic one. An
:class:`~quantplatform.core.models.orders.OrderIntent` must be able to carry the stop it
was proposed under, and :mod:`quantplatform.core.models.risk` already imports ``orders``
for :class:`~quantplatform.core.models.orders.ApprovedOrder` — so the stop cannot live
there without making the two modules import each other. Both import this one instead.

:class:`~quantplatform.core.models.risk.StopSpecification` remains importable from
``risk`` as well, since that is where it was first published.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from quantplatform.core.enums import StopKind
from quantplatform.core.models.base import DomainModel, UtcDatetime
from quantplatform.core.numeric import NonNegativeMoney, Price

__all__ = ["StopSpecification"]


class StopSpecification(DomainModel):
    """Where a position stops losing money, in a form the strategy does not own.

    Exactly one way of naming the level is permitted per specification. Carrying both an
    absolute price and a relative distance would give the enforcement layer two sources of
    truth for one trigger, and whichever it chose the other would be a silent lie about
    where the stop actually sits.
    """

    kind: StopKind

    trigger_price: Price | None = None
    """Absolute level, when it is known at intent time."""

    distance_bps: NonNegativeMoney | None = None
    """Distance from entry in basis points, for a stop whose level follows the fill."""

    max_holding_seconds: int | None = Field(default=None, gt=0)
    """Maximum time in position, for :attr:`StopKind.TIME`."""

    activated_at: UtcDatetime | None = None
    """When a trailing or break-even stop armed; ``None`` while it is still dormant."""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the stop can actually be evaluated.

        Raises:
            ValueError: If the specification names no level, names two, or omits the
                duration a time stop is defined by.
        """
        if self.kind is StopKind.TIME:
            if self.max_holding_seconds is None:
                msg = "a time stop requires max_holding_seconds"
                raise ValueError(msg)
            return self
        if self.trigger_price is not None and self.distance_bps is not None:
            msg = "a stop names either a trigger_price or a distance_bps, not both"
            raise ValueError(msg)
        if self.trigger_price is None and self.distance_bps is None:
            msg = "a stop requires either a trigger_price or a distance_bps"
            raise ValueError(msg)
        return self
