"""Platform-wide constants.

Centralising these values keeps business logic free of magic numbers and makes the
assumptions of the platform explicit and reviewable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# --- Identity ------------------------------------------------------------------------

PLATFORM_NAME: Final[str] = "quantplatform"
"""Stable machine-readable platform identifier used in logs, ids and persistence."""

IDEMPOTENCY_KEY_PREFIX: Final[str] = "qp"
"""Prefix applied to every deterministic idempotency key and client order id."""

CLIENT_ORDER_ID_DIGEST_LENGTH: Final[int] = 26
"""Hex characters of the digest retained in a client order id.

Kept short because several venues cap the client order id at 32-36 characters.
"""

MAX_CLIENT_ORDER_ID_LENGTH: Final[int] = 36
"""Conservative upper bound accepted by the venues we intend to support."""

# --- Numeric -------------------------------------------------------------------------

DECIMAL_WORKING_PRECISION: Final[int] = 34
"""Significant digits used inside local decimal contexts for division-heavy maths."""

ZERO: Final[Decimal] = Decimal(0)
ONE: Final[Decimal] = Decimal(1)
ONE_HUNDRED: Final[Decimal] = Decimal(100)
BASIS_POINTS_PER_UNIT: Final[Decimal] = Decimal(10_000)

# --- Time ----------------------------------------------------------------------------

SECONDS_PER_MINUTE: Final[int] = 60
SECONDS_PER_HOUR: Final[int] = 3_600
SECONDS_PER_DAY: Final[int] = 86_400
DAYS_PER_YEAR: Final[int] = 365

# --- Safety ---------------------------------------------------------------------------

LIVE_CONFIRMATION_PHRASE: Final[str] = "I_UNDERSTAND_LIVE_TRADING_RISK"
"""Exact confirmation value required, in addition to configuration, to arm live trading."""

REDACTED_PLACEHOLDER: Final[str] = "***REDACTED***"
"""Replacement emitted by the logging redaction filter."""

SENSITIVE_FIELD_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "api_secret",
        "apikey",
        "authorization",
        "credential",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "session_token",
        "signature",
        "token",
    }
)
"""Substrings that mark a structured log field as sensitive."""
