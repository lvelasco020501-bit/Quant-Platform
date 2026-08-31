"""One serialised form, used for hashing and for writing files.

Pydantic serialises computed fields and the domain models forbid unknown input, so a raw dump
cannot be read back: ``SymbolRules`` publishes ``price_precision`` and ``quantity_precision``,
and feeding either one back in fails validation. That bit an experiment definition written to
disk and read again — the very round trip the command line depends on.

Derived from ``model_fields`` rather than from a hand-written list of exclusions, because a
list goes stale the first time a model gains a property and nobody remembers this file. Every
value left out is a function of values kept, so nothing is lost and everything is recomputed
identically on load.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

__all__ = ["canonical_json", "storable"]


def storable(value: Any) -> Any:  # noqa: ANN401, PLR0911 - one branch per JSON-able shape
    """Return a JSON-ready form of a model with its computed fields removed."""
    if isinstance(value, BaseModel):
        return {name: storable(getattr(value, name)) for name in type(value).model_fields}
    if isinstance(value, Mapping):
        return {str(key): storable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [storable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def canonical_json(value: BaseModel, *, indent: int | None = None) -> str:
    """Return the canonical text of a model: stable, sorted, and readable back.

    Keys are sorted rather than left in declaration order, so adding a field in the middle of
    a model does not rewrite the identity of everything that came before it for a reason
    unrelated to what was run.
    """
    return json.dumps(storable(value), sort_keys=True, indent=indent)
