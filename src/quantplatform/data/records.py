"""Raw CSV row representation, prior to any validation or type coercion.

A :class:`RawBarRecord` preserves exactly what a CSV row said, as plain strings. Nothing in
this module parses a number, checks a timestamp, or constructs a
:class:`~quantplatform.core.models.market.MarketBar`: that happens in
:mod:`quantplatform.data.validation`, after which a raw record either survives, fully
parsed, or is rejected with a finding that can still quote its original text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = ["CANONICAL_COLUMNS", "RawBarRecord"]

CANONICAL_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "market_type",
    "timeframe",
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
)
"""The one canonical CSV schema this phase supports, in the order documented in the README."""


@dataclass(frozen=True, slots=True)
class RawBarRecord:
    """A single CSV data row, exactly as read, before validation.

    Every field is the original string from the file, including ``trade_count`` (which may
    be an empty string). ``extra_fields`` retains any columns beyond the canonical set,
    keyed by header name, so they are traceable in provenance without being able to affect
    a normalised value.
    """

    source: str
    source_row: int
    symbol: str
    market_type: str
    timeframe: str
    open_time: str
    close_time: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    trade_count: str
    extra_fields: dict[str, str] = field(default_factory=dict)
