"""Turning an operator's Ctrl-C into an orderly stop.

``SIGINT`` and ``SIGTERM`` must not kill a paper session mid-bar. A process torn down
between a fill and the portfolio update that settles it leaves persisted state describing
an account that never existed, and a resume cannot reason about that.

So a signal sets a flag and returns. Nothing is closed inside the handler — signal handlers
run between bytecodes, and doing real work there is how a shutdown path acquires its own
race conditions. The loops read the flag at their next natural boundary and unwind
normally, which is exactly the cooperative stop the runner and feed were already built for.

**Previous handlers are restored on exit.** A process that installed a handler and never
gave it back would leave the next component's signal handling broken in a way that only
shows up when someone tries to stop it.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from types import FrameType
from typing import Final

__all__ = ["ShutdownSignal", "shutdown_on_signals"]

_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGINT, signal.SIGTERM)


class ShutdownSignal:
    """A flag set when the process is asked to stop."""

    def __init__(self, on_request: Callable[[str], None] | None = None) -> None:
        """Create a shutdown flag.

        Args:
            on_request: Called with the signal name the first time a stop is requested, so
                the caller can ask its loops to wind down. Kept deliberately small — it
                runs inside a signal handler.
        """
        self._requested = False
        self._reason: str | None = None
        self._on_request = on_request

    @property
    def requested(self) -> bool:
        """Return whether a stop has been asked for."""
        return self._requested

    @property
    def reason(self) -> str | None:
        """Return the signal that asked for the stop, if any."""
        return self._reason

    def request(self, reason: str) -> None:
        """Record a stop request and notify the caller once.

        Repeat signals are ignored rather than re-notifying: an impatient operator pressing
        Ctrl-C three times should not start three shutdowns.
        """
        if self._requested:
            return
        self._requested = True
        self._reason = reason
        if self._on_request is not None:
            self._on_request(reason)


@contextmanager
def shutdown_on_signals(
    flag: ShutdownSignal, *, signals: tuple[signal.Signals, ...] = _SIGNALS
) -> Iterator[ShutdownSignal]:
    """Install stop-requesting handlers for the duration of the block.

    Args:
        flag: The flag to set when a signal arrives.
        signals: Which signals to trap.

    Yields:
        The same flag, for convenience.
    """
    previous: dict[signal.Signals, object] = {}

    def _handle(number: int, frame: FrameType | None) -> None:
        _ = frame
        flag.request(signal.Signals(number).name)

    for number in signals:
        try:
            previous[number] = signal.getsignal(number)
            signal.signal(number, _handle)
        except (ValueError, OSError):  # pragma: no cover - non-main thread or unsupported
            previous.pop(number, None)
    try:
        yield flag
    finally:
        for number, handler in previous.items():
            with suppress(ValueError, OSError):  # pragma: no cover - non-main thread
                signal.signal(number, handler)  # type: ignore[arg-type]
