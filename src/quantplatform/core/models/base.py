"""Base class and shared constrained types for domain models.

Domain models are frozen pydantic models that forbid unknown fields. Immutability makes
the audit trail trustworthy: once a signal, decision, order or snapshot has been produced
it cannot be edited in place, only superseded by a new record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from quantplatform.core.timeutils import ensure_utc

__all__ = [
    "AssetCode",
    "DomainModel",
    "RegimeLabel",
    "SemanticVersion",
    "StrategyId",
    "Symbol",
    "Text",
    "UtcDatetime",
    "VenueId",
]

_SEMVER_PATTERN: Final[str] = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
"""Timezone-aware instant normalised to UTC. Naive datetimes are rejected."""

Symbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z0-9]{2,20}/[A-Z0-9]{2,20}$"),
]
"""Canonical, venue-independent symbol in ``BASE/QUOTE`` form, for example ``BTC/USDT``."""

AssetCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z0-9]{2,20}$"),
]
"""Single asset ticker such as ``BTC`` or ``USDT``."""

StrategyId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{2,63}$"),
]
"""Stable snake_case registry identifier of a strategy."""

SemanticVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=_SEMVER_PATTERN),
]
"""Semantic version string such as ``1.4.0``."""

VenueId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
"""Identifier assigned by an external venue or data source."""

Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Short non-empty human-readable text, such as the reason for a decision."""

RegimeLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{1,31}$"),
]
"""A regime name, shaped like :data:`StrategyId` rather than drawn from a closed set.

No concrete label lives in the platform. What is fixed is only the *shape* a label may
take — the categories themselves (``trend_up``, ``range``, ``high_vol``, or anything else) are
invented by whoever writes a :class:`~quantplatform.research.regime.RegimeLabeller`, later,
with evidence. Fixing the vocabulary here would be choosing a financial threshold before any
research justifies one.
"""


class DomainModel(BaseModel):
    """Immutable, strictly validated base class for every domain model."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        str_strip_whitespace=True,
        arbitrary_types_allowed=False,
        use_enum_values=False,
    )
