"""Driving a paper session from a live feed.

The runner is the loop and nothing more: pull the next closed bar, hand it to the session,
repeat. Keeping it this thin is deliberate — every decision about *what to do* with a bar
belongs to the session and, below it, to the pipeline. A runner that started filtering or
reordering bars would become a second place where trading behaviour lives.

Synchronous by design. No threads, no async, no background workers: a paper run must be
reproducible from its inputs, and concurrency is the fastest way to lose that.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from quantplatform.core.errors import PaperSessionStateError
from quantplatform.core.interfaces import PaperMarketDataFeed
from quantplatform.core.models.market import MarketBar
from quantplatform.paper.results import SessionResult
from quantplatform.paper.session import PaperTradingSession

__all__ = ["PaperTradingRunner"]


class PaperTradingRunner:
    """Pumps bars from a feed into a session until told to stop."""

    def __init__(
        self,
        *,
        session: PaperTradingSession,
        feed: PaperMarketDataFeed,
        max_bars: int | None = None,
        on_bar: Callable[[MarketBar], None] | None = None,
    ) -> None:
        """Wire a runner.

        Args:
            session: The session to drive.
            feed: Source of closed bars.
            max_bars: Stop after this many *received* bars, or ``None`` to run until the feed
                ends. Bounds a test, and bounds an operator's smoke run.
            on_bar: Optional observer called after each received bar, for progress reporting.
                It cannot influence the session — it is handed the bar and its return value is
                discarded — so a broken reporter cannot change a trading outcome.
        """
        self._session = session
        self._feed = feed
        self._max_bars = max_bars
        self._on_bar = on_bar
        self._stopping = False

    @property
    def session(self) -> PaperTradingSession:
        """Return the session being driven."""
        return self._session

    def request_stop(self) -> None:
        """Ask the loop to finish after the bar it is currently handling.

        Cooperative rather than immediate: interrupting mid-bar could leave an order approved
        but unsettled, and the session's own consistency is worth one more candle of delay.
        """
        self._stopping = True

    def run(self, *, resume: bool = False) -> SessionResult:
        """Drive the session until the feed ends, the bar limit is hit, or a stop is asked for.

        Args:
            resume: Restore persisted state and continue an existing session rather than
                starting a fresh one.

        Returns:
            Everything the session produced.

        Raises:
            PaperSessionStateError: If the session cannot be started or resumed.
        """
        self._stopping = False
        if resume:
            self._session.resume()
        else:
            self._session.start()

        try:
            self._pump(self._feed.closed_bars())
        finally:
            # The session is stopped whatever happened, so a crash still persists a snapshot
            # and still releases the feed. A session left "running" after the process that ran
            # it has gone is the state a resume cannot reason about.
            self._session.stop()
            self._feed.close()
        return self._session.result()

    def _pump(self, bars: Iterator[MarketBar]) -> None:
        """Consume the feed, offering each bar to the session."""
        for received, bar in enumerate(bars, start=1):
            if self._stopping:
                break
            self._session.submit_bar(bar)
            if self._on_bar is not None:
                self._on_bar(bar)
            if self._max_bars is not None and received >= self._max_bars:
                break

    def run_once(self, bar: MarketBar) -> None:
        """Offer a single bar without owning the loop.

        For a caller that already has its own scheduler and wants the session's behaviour
        without the runner's loop.

        Raises:
            PaperSessionStateError: If the session is not running.
        """
        if not self._session.is_running:
            raise PaperSessionStateError(
                "session is not running", session_id=self._session.session_id
            )
        self._session.submit_bar(bar)
