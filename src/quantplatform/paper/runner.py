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

from quantplatform.core.errors import PaperSessionStateError, TelemetryNotConfiguredError
from quantplatform.core.interfaces import (
    FeedMetricsReader,
    PaperMarketDataFeed,
    StreamingMarketDataProvider,
    SymbolRulesMaintainer,
)
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
        feed_metrics: FeedMetricsReader | None = None,
        symbol_rules: SymbolRulesMaintainer | None = None,
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
            feed_metrics: Reader of the feed's own health counters, handed to the session
                before each bar so a daily report can describe the data it traded on.
                Usually the feed itself. A separate port rather than a method on
                :class:`~quantplatform.core.interfaces.PaperMarketDataFeed`, because a
                deterministic replay has no health to report and forcing it to invent
                counters would put fiction where a report expects measurement.
            symbol_rules: Whatever keeps the venue's trading rules current, driven once per
                bar. The runner does not know what a refresh interval is and does not decide
                when one is due — it performs maintenance and forwards the reading to the
                session, exactly as it does for feed metrics. Omitted for a replay, whose
                rules are fixed by the recording and cannot go stale against a live venue.

        Raises:
            TelemetryNotConfiguredError: If ``feed`` is a live streaming provider and no
                reader was supplied. A replay may run without telemetry; a real stream may
                not, because a run that cannot see its own data quality produces reports
                that look clean for the wrong reason.
        """
        self._session = session
        self._feed = feed
        self._max_bars = max_bars
        self._on_bar = on_bar
        self._feed_metrics = feed_metrics
        self._symbol_rules = symbol_rules
        self._stopping = False
        self._require_telemetry_for_live_feeds()

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
            self._refresh_feed_metrics()
            self._maintain_symbol_rules()
            self._session.submit_bar(bar)
            if self._on_bar is not None:
                self._on_bar(bar)
            if self._max_bars is not None and received >= self._max_bars:
                break

    def _require_telemetry_for_live_feeds(self) -> None:
        """Refuse to drive a live stream that cannot report its own health.

        The distinction is capability, not configuration. A feed that satisfies
        :class:`~quantplatform.core.interfaces.StreamingMarketDataProvider` can drop, stall
        and skip candles; one that does not — a replay, a recorded double — cannot. Only
        the first kind can produce a report that looks healthy because nothing was measured,
        and wiring time is the last moment where that is cheap to fix.

        Raises:
            TelemetryNotConfiguredError: If a streaming provider was supplied without a
                reader.
        """
        if self._feed_metrics is not None:
            return
        if not isinstance(self._feed, StreamingMarketDataProvider):
            return
        raise TelemetryNotConfiguredError(
            "a live market-data feed must be wired with a feed_metrics reader, so daily "
            "reports describe the data the session actually traded on",
            session_id=self._session.session_id,
            feed=type(self._feed).__name__,
        )

    def _refresh_feed_metrics(self) -> None:
        """Hand the session the feed's latest health reading, before the bar is offered.

        Order matters. A day rollover is detected *inside* ``submit_bar``, so the snapshot
        has to be in place before the first bar of a new day is submitted — refreshing
        afterwards would report every day using the health reading from the day before.
        """
        if self._feed_metrics is None:
            return
        self._session.record_feed_metrics(self._feed_metrics.read_feed_metrics())

    def _maintain_symbol_rules(self) -> None:
        """Keep the venue's trading rules current, and tell the session how that is going.

        Before the bar, for the same reason feed metrics are refreshed before the bar: a day
        rollover is detected inside ``submit_bar``, so a reading taken afterwards would
        describe the new day in the closing report of the old one.

        Ordering also matters for a second reason here. The rules this call may replace are
        the ones the bar about to be submitted will be sized against, so refreshing first is
        what makes a decision use the newest limits the venue has published rather than the
        ones that happened to be in force when the previous candle arrived.
        """
        if self._symbol_rules is None:
            return
        self._session.record_symbol_rules_telemetry(self._symbol_rules.maintain())

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
        self._refresh_feed_metrics()
        self._maintain_symbol_rules()
        self._session.submit_bar(bar)
