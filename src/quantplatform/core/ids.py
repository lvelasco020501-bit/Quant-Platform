"""Deterministic identifier derivation.

Idempotency keys are pure functions of the decision that produced an order. Replaying the
same market data through the same strategy version must therefore yield the same key,
which is what allows duplicate submission to be detected after a crash or a restart.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Final

from quantplatform.core.constants import (
    CLIENT_ORDER_ID_DIGEST_LENGTH,
    IDEMPOTENCY_KEY_PREFIX,
    MAX_CLIENT_ORDER_ID_LENGTH,
    PLATFORM_NAME,
)
from quantplatform.core.enums import ExecutionMode, SignalAction
from quantplatform.core.errors import DomainValidationError
from quantplatform.core.timeutils import ensure_utc

__all__ = ["client_order_id_from_key", "deterministic_uuid", "idempotency_key"]

_FIELD_SEPARATOR = "|"

PLATFORM_UUID_NAMESPACE: Final[uuid.UUID] = uuid.uuid5(uuid.NAMESPACE_DNS, PLATFORM_NAME)
"""Fixed namespace so that derived identifiers are stable across processes and releases."""


def deterministic_uuid(kind: str, *parts: str) -> uuid.UUID:
    """Derive a reproducible UUID from a record kind and its identifying fields.

    Backtests must be reproducible down to the persisted identifiers, so entity ids are
    derived rather than drawn at random.

    Args:
        kind: Record family, for example ``signal`` or ``order_intent``.
        *parts: Field values that uniquely identify the record within its family.

    Returns:
        A version 5 UUID derived from the platform namespace.

    Raises:
        DomainValidationError: If ``kind`` is empty or no parts are supplied.
    """
    if not kind:
        raise DomainValidationError("uuid kind must not be empty")
    if not parts:
        raise DomainValidationError("at least one identifying part is required", kind=kind)
    canonical = _FIELD_SEPARATOR.join((kind, *parts))
    return uuid.uuid5(PLATFORM_UUID_NAMESPACE, canonical)


def idempotency_key(
    *,
    strategy_id: str,
    strategy_version: str,
    symbol: str,
    signal_time: datetime,
    action: SignalAction,
    execution_mode: ExecutionMode,
) -> str:
    """Derive the deterministic idempotency key of an order-producing decision.

    The key is a SHA-256 digest over the canonical, separator-delimited encoding of the
    fields that uniquely identify a logical trading decision. Field values may not contain
    the separator, which guarantees the encoding is unambiguous.

    Args:
        strategy_id: Registry identifier of the strategy.
        strategy_version: Semantic version of the strategy implementation.
        symbol: Canonical platform symbol, for example ``BTC/USDT``.
        signal_time: Closing timestamp of the bar that produced the signal.
        action: Directional action expressed by the signal.
        execution_mode: Mode the decision was produced under, so that paper and live
            decisions never collide.

    Returns:
        A stable key of the form ``qp-<64 hex characters>``.

    Raises:
        DomainValidationError: If any field is empty or contains the reserved separator.
    """
    fields = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": symbol,
    }
    for name, value in fields.items():
        if not value:
            raise DomainValidationError("idempotency field must not be empty", field=name)
        if _FIELD_SEPARATOR in value:
            raise DomainValidationError(
                "idempotency field must not contain the reserved separator",
                field=name,
                separator=_FIELD_SEPARATOR,
            )

    canonical = _FIELD_SEPARATOR.join(
        (
            strategy_id,
            strategy_version,
            symbol,
            ensure_utc(signal_time).isoformat(),
            action.value,
            execution_mode.value,
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{IDEMPOTENCY_KEY_PREFIX}-{digest}"


def client_order_id_from_key(key: str) -> str:
    """Derive a venue-safe client order id from an idempotency key.

    Venues commonly cap client order ids at 32-36 characters, so the full key is truncated
    to a fixed-length prefix of its digest. The mapping is deterministic, which preserves
    the duplicate-detection guarantee.

    Args:
        key: Idempotency key produced by :func:`idempotency_key`.

    Returns:
        A client order id of the form ``qp-<26 hex characters>``.

    Raises:
        DomainValidationError: If ``key`` does not carry the expected prefix.
    """
    expected_prefix = f"{IDEMPOTENCY_KEY_PREFIX}-"
    if not key.startswith(expected_prefix):
        raise DomainValidationError(
            "idempotency key has an unexpected prefix",
            expected_prefix=expected_prefix,
        )
    digest = key.removeprefix(expected_prefix)
    client_order_id = f"{expected_prefix}{digest[:CLIENT_ORDER_ID_DIGEST_LENGTH]}"
    if len(client_order_id) > MAX_CLIENT_ORDER_ID_LENGTH:  # pragma: no cover - guarded by consts
        raise DomainValidationError(
            "derived client order id exceeds the venue limit",
            length=len(client_order_id),
            limit=MAX_CLIENT_ORDER_ID_LENGTH,
        )
    return client_order_id
