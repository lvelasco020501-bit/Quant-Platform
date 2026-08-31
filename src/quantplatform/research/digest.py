"""Naming the bars an experiment actually consumed.

A definition names a dataset — a symbol, a timeframe, a range, a label — and a label can be
wrong. Re-ingesting a venue's history, correcting a bad candle, or reading a different vintage
of the same days all produce a run over "the same dataset" by that description and a different
one in fact. Without a fingerprint over the bars themselves, a run that disagrees with its
predecessor cannot be told apart from a run over different numbers, and the reproducibility
question cannot honestly be asked.

Deliberately not part of the experiment's identity. A definition must be declarable before the
data exists — that is what lets a split be committed to in advance — and folding the digest
into the identifier would also destroy the one comparison this exists for: the same definition,
run against a different vintage, would stop looking like the same definition.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar

__all__ = ["bars_digest", "canonical_bar_line"]

_FIELD_SEPARATOR = "|"
_DIGEST_LENGTH = 32


def _canonical_decimal(value: Decimal) -> str:
    """Render a decimal so that numerically equal values render identically.

    ``50000``, ``50000.00`` and ``5E+4`` are one number written three ways, and their default
    string forms differ. Hashing those forms would make a re-ingest at a different decimal
    scale look like corrected data, and every historical comparison would break for a reason
    invisible in the numbers themselves.
    """
    return format(value.normalize(), "f")


def canonical_bar_line(bar: MarketBar) -> str:
    """Render one bar as an unambiguous line.

    Fields are separator-delimited and none of them may contain the separator — the same
    guarantee the platform's idempotency keys already rest on. Nothing here hashes a Python
    object: ``repr`` and pickle are not stable across versions, and a fingerprint that
    changed when the interpreter did would be worse than none.
    """
    return _FIELD_SEPARATOR.join(
        (
            bar.symbol,
            bar.timeframe.value,
            bar.open_time.isoformat(),
            bar.close_time.isoformat(),
            _canonical_decimal(bar.open),
            _canonical_decimal(bar.high),
            _canonical_decimal(bar.low),
            _canonical_decimal(bar.close),
            _canonical_decimal(bar.volume),
        )
    )


def bars_digest(bars: Sequence[MarketBar]) -> str:
    """Return the fingerprint of the exact sequence of bars a run consumed.

    Order is part of the dataset. The engine consumes a sequence rather than a set, so the
    same candles in another order would produce different fills — a digest blind to order
    would call two genuinely different datasets the same one.

    Args:
        bars: The bars handed to the engine, in the order they were handed over.

    Returns:
        A stable 32-character digest. An empty sequence has one of its own: a run over no
        bars is an outcome the engine supports, and it needs a name like any other.
    """
    payload = "\n".join(canonical_bar_line(bar) for bar in bars)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
