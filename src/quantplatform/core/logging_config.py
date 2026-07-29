"""Structured logging with contextual binding and secret redaction.

Logs are the primary operational record of a trading system, so they are emitted as
machine-parseable JSON by default and enriched with a correlation context that follows a
decision from bar to portfolio update. Credentials never reach a log sink: the redaction
filter masks both known secret values and any field whose name looks sensitive.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Final, TextIO
from uuid import UUID

from quantplatform.core.constants import (
    PLATFORM_NAME,
    REDACTED_PLACEHOLDER,
    SENSITIVE_FIELD_TOKENS,
)
from quantplatform.core.enums import LogFormat

__all__ = [
    "ContextFilter",
    "JsonFormatter",
    "RedactingFilter",
    "configure_logging",
    "get_logger",
    "log_context",
]

_LOG_CONTEXT: ContextVar[Mapping[str, str]] = ContextVar(
    "quantplatform_log_context",
    default=MappingProxyType({}),
)

_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_CONTEXT_ATTR: Final[str] = "log_context"
_MIN_REDACTABLE_SECRET_LENGTH: Final[int] = 4


@contextmanager
def log_context(**fields: str) -> Iterator[None]:
    """Bind structured fields to every log record emitted inside the block.

    Bindings nest: an inner block sees the outer fields and may override them, and the
    previous state is restored on exit. The context is stored in a
    :class:`~contextvars.ContextVar`, so concurrent asyncio tasks do not observe each
    other's bindings.

    Args:
        **fields: Field names and values to attach, for example ``correlation_id``.

    Yields:
        ``None``; the bindings are active for the duration of the block.
    """
    merged = dict(_LOG_CONTEXT.get())
    merged.update(fields)
    token = _LOG_CONTEXT.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def _is_sensitive_key(key: str) -> bool:
    """Return whether a field name suggests the value is a credential."""
    lowered = key.lower()
    return any(token in lowered for token in SENSITIVE_FIELD_TOKENS)


def _json_default(value: object) -> str:
    """Render values the JSON encoder cannot handle natively.

    Args:
        value: Object encountered by :func:`json.dumps`.

    Returns:
        A string representation. Decimals are rendered exactly, never as floats.
    """
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    return repr(value)


class ContextFilter(logging.Filter):
    """Attach the active :func:`log_context` bindings to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach context to the record and always allow it through.

        Args:
            record: Record being emitted.

        Returns:
            ``True``, since this filter enriches rather than suppresses.
        """
        setattr(record, _CONTEXT_ATTR, dict(_LOG_CONTEXT.get()))
        return True


class RedactingFilter(logging.Filter):
    """Mask credentials in log records.

    Three independent defences are applied: any extra field whose name matches a sensitive
    token is masked regardless of its value's length; any registered secret value is
    replaced wherever it appears as a *substring* of the rendered message or a field value,
    but only for secrets of at least four characters, because substring replacement of a
    shorter string would corrupt unrelated text (for example, masking a two-character
    secret would also mangle any ordinary word that happens to contain it); and, with no
    length floor, any string field whose *entire* value exactly equals a registered secret
    is masked outright, since an exact-match check carries none of the substring
    collision risk regardless of how short the secret is. Together these close the
    practical gap for a short credential logged as a whole value, while still protecting
    ordinary log text from corruption by short-string substring replacement.

    Args:
        secrets: Literal secret values to mask. Every value is checked for an exact match
            no matter its length; only values of at least four characters are also scanned
            for as a substring of longer text.
    """

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._exact_secrets = frozenset(secret for secret in secrets if secret)
        self._secrets = tuple(
            secret for secret in secrets if len(secret) >= _MIN_REDACTABLE_SECRET_LENGTH
        )

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place and always allow it through.

        Args:
            record: Record being emitted.

        Returns:
            ``True``, since this filter masks rather than suppresses.
        """
        record.msg = self._mask_text(record.getMessage())
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_ATTRS:
                continue
            if _is_sensitive_key(key):
                record.__dict__[key] = REDACTED_PLACEHOLDER
            elif isinstance(value, str):
                record.__dict__[key] = self._mask_text(value)
            elif isinstance(value, dict):
                record.__dict__[key] = self._mask_mapping(value)
        return True

    def _mask_text(self, text: str) -> str:
        """Replace a registered secret found in ``text``, exactly or as a substring.

        An exact match against the whole string is checked first and applies no matter how
        short the secret is. Substring replacement then covers longer secrets embedded in
        larger text; it is not applied below the length floor, since doing so would corrupt
        unrelated text that happens to contain the same short sequence of characters.
        """
        if text in self._exact_secrets:
            return REDACTED_PLACEHOLDER
        for secret in self._secrets:
            text = text.replace(secret, REDACTED_PLACEHOLDER)
        return text

    def _mask_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``mapping`` with sensitive keys and secret values masked."""
        masked: dict[str, Any] = {}
        for key, value in mapping.items():
            if _is_sensitive_key(str(key)):
                masked[key] = REDACTED_PLACEHOLDER
            elif isinstance(value, str):
                masked[key] = self._mask_text(value)
            elif isinstance(value, dict):
                masked[key] = self._mask_mapping(value)
            else:
                masked[key] = value
        return masked


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise a record.

        Args:
            record: Record to render.

        Returns:
            A single-line JSON document.
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, _CONTEXT_ATTR, None)
        if context:
            payload["context"] = context
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS and key != _CONTEXT_ATTR
        }
        if extras:
            payload["extra"] = extras
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info is not None:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=_json_default, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Render records as readable single lines for local development."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise a record with its context appended.

        Args:
            record: Record to render.

        Returns:
            A human-readable line.
        """
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        line = f"{timestamp} {record.levelname:<8} {record.name} :: {record.getMessage()}"
        context = getattr(record, _CONTEXT_ATTR, None)
        if context:
            rendered = " ".join(f"{key}={value}" for key, value in sorted(context.items()))
            line = f"{line} | {rendered}"
        if record.exc_info is not None:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(
    *,
    level: str = "INFO",
    log_format: LogFormat = LogFormat.JSON,
    secrets: tuple[str, ...] = (),
    stream: TextIO | None = None,
) -> logging.Logger:
    """Install the platform's logging handler on the root logger.

    Calling this repeatedly is safe: previously installed platform handlers are replaced
    rather than accumulated, which keeps tests and process restarts free of duplicates.

    Args:
        level: Minimum level name, for example ``"INFO"``.
        log_format: JSON for machine consumption, text for local development.
        secrets: Literal credential values to mask wherever they appear.
        stream: Destination stream. Defaults to ``sys.stderr``.

    Returns:
        The configured root logger.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_quantplatform_handler", False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler._quantplatform_handler = True  # type: ignore[attr-defined]  # noqa: SLF001
    handler.setFormatter(JsonFormatter() if log_format is LogFormat.JSON else TextFormatter())
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactingFilter(secrets))

    root.addHandler(handler)
    root.setLevel(level.upper())
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced platform logger.

    Args:
        name: Dotted module name, conventionally ``__name__``.

    Returns:
        A logger under the platform's root namespace.
    """
    if name.startswith(PLATFORM_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{PLATFORM_NAME}.{name}")
