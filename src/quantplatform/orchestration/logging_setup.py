"""Per-domain log files for a long-running paper session.

Four streams, because when something goes wrong at 3am the first question is *which layer*.
A dropped socket, a refused order and a failed report are three different investigations,
and interleaving them into one file makes each one harder.

    marketdata.log     the feed: connections, reconnects, gaps, heartbeats
    paper.log          the session: bars accepted and refused, lifecycle
    reporting.log      daily reports: what was written, what failed
    orchestration.log  startup, shutdown, wiring, everything unrouted

Rotated at UTC midnight so a day's file matches a day's report. Every handler carries the
platform's redaction filter, so a credential cannot reach a file even if some future caller
logs one by accident — the guarantee is structural rather than a rule people remember.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Final

from quantplatform.core.constants import PLATFORM_NAME
from quantplatform.core.enums import LogFormat
from quantplatform.core.errors import StorageError
from quantplatform.core.logging_config import (
    ContextFilter,
    JsonFormatter,
    RedactingFilter,
    TextFormatter,
)

__all__ = ["LOG_STREAMS", "close_file_logging", "configure_file_logging"]

LOG_STREAMS: Final[dict[str, str]] = {
    "marketdata": "quantplatform.marketdata",
    "paper": "quantplatform.paper",
    "reporting": "quantplatform.reporting",
    "orchestration": "quantplatform.orchestration",
}
"""File name stem to the logger namespace it captures."""

_MARKER: Final[str] = "_quantplatform_file_handler"
_BACKUP_DAYS: Final[int] = 30


class _NamespaceFilter(logging.Filter):
    """Admit only records from one logger namespace.

    ``orchestration`` additionally takes everything that belongs to no other stream, so a
    record from an unrouted namespace lands somewhere rather than nowhere.
    """

    def __init__(self, namespace: str, *, catch_all: bool = False) -> None:
        super().__init__()
        self._namespace = namespace
        self._catch_all = catch_all
        self._others = tuple(value for value in LOG_STREAMS.values() if value != namespace)

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether this record belongs in this stream's file."""
        if record.name == self._namespace or record.name.startswith(f"{self._namespace}."):
            return True
        if not self._catch_all:
            return False
        return not any(
            record.name == other or record.name.startswith(f"{other}.") for other in self._others
        )


def configure_file_logging(
    *,
    directory: Path,
    level: str = "INFO",
    log_format: LogFormat = LogFormat.JSON,
    secrets: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Install one rotating file handler per domain on the root logger.

    Calling this repeatedly is safe: handlers installed by a previous call are removed
    first, so a restart inside one process does not double every line.

    Args:
        directory: Where log files are written. Created if absent.
        level: Minimum level name.
        log_format: JSON for machine consumption, text for reading over someone's shoulder.
        secrets: Literal credential values to mask wherever they appear.

    Returns:
        The log file paths, in :data:`LOG_STREAMS` order.

    Raises:
        StorageError: If the log directory cannot be created or written to.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StorageError("log directory is not usable", directory=str(directory)) from exc

    close_file_logging()
    _reenable_platform_loggers()
    root = logging.getLogger()
    paths: list[Path] = []
    for stem, namespace in LOG_STREAMS.items():
        path = directory / f"{stem}.log"
        handler = TimedRotatingFileHandler(
            path, when="midnight", utc=True, backupCount=_BACKUP_DAYS, encoding="utf-8"
        )
        setattr(handler, _MARKER, True)
        handler.setFormatter(JsonFormatter() if log_format is LogFormat.JSON else TextFormatter())
        handler.addFilter(ContextFilter())
        handler.addFilter(RedactingFilter(secrets))
        handler.addFilter(_NamespaceFilter(namespace, catch_all=stem == "orchestration"))
        root.addHandler(handler)
        paths.append(path)

    root.setLevel(level.upper())
    return tuple(paths)


def _reenable_platform_loggers() -> None:
    """Undo any third-party logging configuration that switched our loggers off.

    :func:`logging.config.fileConfig` disables every logger that already exists unless it
    is told otherwise, and libraries call it — Alembic does. A module that created its
    logger at import time is then silently muted: nothing raises, nothing is written, and
    the absence looks like "nothing happened" rather than "logging is broken".

    Configuring logging should therefore *guarantee* logging, not merely add a handler and
    hope nothing upstream turned the lights off.
    """
    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and name.startswith(PLATFORM_NAME):
            logger.disabled = False


def close_file_logging() -> None:
    """Remove and close every file handler this module installed.

    Called on shutdown so buffered lines reach disk, and at the start of
    :func:`configure_file_logging` so handlers never accumulate across restarts.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
            handler.close()
