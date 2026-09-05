"""Turning a session's numbers into a reading of what is going on.

A dashboard that only prints metrics leaves the interpreting to whoever is looking, which
means it is useful exactly to the person who already knows what 9/21 bars implies. This
module does the interpreting: each *brain* reports a level, a headline, and one sentence of
plain language, and each KPI carries what it means, where it came from, and why it is the
colour it is.

Three rules hold throughout, and they are the reason this is Python rather than JavaScript —
they are testable here:

* **Nothing is invented.** A KPI with no source is ``None`` with a stated reason, never a
  plausible number. The summary at the top is assembled from the brains' own findings, so it
  cannot claim anything they do not.
* **Unknown, zero and unobservable are three different answers.** ``Source.UNAVAILABLE``
  says nobody records it; a zero from a real reading is a measurement.
* **A small sample is labelled, not hidden.** Win rate over two trades is arithmetic, not
  evidence, and presenting it beside a green tick is how a dashboard talks somebody into a
  conclusion the data does not support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from quantplatform.status.events import ActivityCounts
from quantplatform.status.model import Health, SessionStatus

__all__ = [
    "HEALTHY_FILL_RATE",
    "MEANINGFUL_TRADE_SAMPLE",
    "Brain",
    "Kpi",
    "Level",
    "Source",
    "Summary",
    "build_brains",
    "summarise",
]

HEALTHY_FILL_RATE = 0.9
"""Below this share of intents filled, something is refusing orders often enough to look at."""

MEANINGFUL_TRADE_SAMPLE = 30
"""Closed trades below which performance figures are labelled low-confidence.

Not a statistical threshold anybody derived — it is a deliberately conservative round number
whose only job is to stop a win rate over three trades from being read as a result. The exact
value matters far less than that a boundary exists and is stated.
"""


class Level(StrEnum):
    """How a reader should feel about something, in four steps."""

    GOOD = "good"
    ATTENTION = "attention"
    DANGER = "danger"
    INFO = "info"


class Source(StrEnum):
    """Where a figure came from, which decides how much weight it carries."""

    STRUCTURED = "structured"
    """Persisted by the session itself — the snapshot, the lock, its daily report."""

    LOG_DERIVED = "log-derived"
    """Parsed from the session's own JSON logs. Real, but logs rotate, truncate and outlive
    the run that wrote them, so this is weaker evidence than a snapshot."""

    UNAVAILABLE = "unavailable"
    """Nothing records it. The value is ``None`` and the reason says why."""


@dataclass(frozen=True)
class Kpi:
    """One figure, with everything a reader needs to judge it."""

    key: str
    label: str
    value: str | None
    """``None`` renders as N/A. Never a placeholder number."""
    meaning: str
    """What the KPI is, in one sentence, for somebody who has not seen it before."""
    source: Source
    level: Level
    why: str
    """Why it is this colour, or why there is nothing to show."""
    low_confidence: bool = False


@dataclass(frozen=True)
class Brain:
    """One area of the system, read rather than merely measured."""

    key: str
    title: str
    level: Level
    headline: str
    """The interpreted state — ``WARMING UP — 9/21 bars``, not ``bars: 9``."""
    explanation: str
    kpis: tuple[Kpi, ...]


@dataclass(frozen=True)
class Summary:
    """The one paragraph somebody reads before deciding whether to keep reading."""

    level: Level
    headline: str
    sentences: tuple[str, ...]
    intervention_required: bool
    blockers: tuple[str, ...]


# --- helpers -------------------------------------------------------------------------------


def _money(value: Decimal | None, unit: str) -> str | None:
    """Render an amount with its unit, or nothing when there is nothing to render."""
    return None if value is None else f"{value:,.2f} {unit}"


def _pct(fraction: Decimal | float | None, digits: int = 2) -> str | None:
    """Render a 0..1 fraction as a percentage."""
    return None if fraction is None else f"{float(fraction) * 100:.{digits}f}%"


def _count(value: int | None) -> str | None:
    """Render a count, keeping zero distinct from absent."""
    return None if value is None else str(value)


def _unavailable(key: str, label: str, meaning: str, why: str) -> Kpi:
    """Build a KPI that has no value, saying plainly why not."""
    return Kpi(
        key=key,
        label=label,
        value=None,
        meaning=meaning,
        source=Source.UNAVAILABLE,
        level=Level.INFO,
        why=why,
    )


def _worst(levels: list[Level]) -> Level:
    """Return the level a reader should act on: the most alarming one present."""
    for level in (Level.DANGER, Level.ATTENTION, Level.GOOD):
        if level in levels:
            return level
    return Level.INFO


def _elapsed_hours(status: SessionStatus) -> float | None:
    """Return how long the session has been alive, in hours."""
    elapsed = status.elapsed
    return None if elapsed is None else elapsed.total_seconds() / 3600


def _bars_expected(status: SessionStatus) -> int | None:
    """Return how many hourly candles should have closed since the session started.

    Counts hour boundaries crossed, not elapsed hours divided: a session that starts at
    20:35 sees its first close at 21:00, twenty-five minutes in, and dividing would call
    that zero and report a healthy feed as behind.
    """
    if status.started_at is None:
        return None
    start = status.started_at
    now = datetime.now(UTC)
    first_close = (start + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    if now < first_close:
        return 0
    return int((now - first_close).total_seconds() // 3600) + 1


# --- market --------------------------------------------------------------------------------


def _market_brain(status: SessionStatus, feed_state: str | None) -> Brain:
    """Where the data is coming from, and whether it is arriving intact."""
    report = status.report
    kpis: list[Kpi] = []
    levels: list[Level] = []

    connected = feed_state in {"connected", "streaming"}
    feed_level = Level.GOOD if (connected and status.running) else Level.ATTENTION
    kpis.append(
        Kpi(
            key="feed_state",
            label="Feed",
            value=(feed_state or "unknown").upper(),
            meaning="Whether the platform is receiving live candles from the exchange.",
            source=Source.LOG_DERIVED,
            level=feed_level,
            why=(
                "The feed last reported itself connected and the session process is alive."
                if feed_level is Level.GOOD
                else "The feed's last reported state is not a connected one, or no session "
                "is running to hold the connection. Connection state is never persisted, so "
                "this is the last thing the feed said rather than a live check of the socket."
            ),
        )
    )
    levels.append(feed_level)

    kpis.append(
        Kpi(
            key="last_close",
            label="Last price",
            value=_money(status.last_bar.close, status.quote_asset) if status.last_bar else None,
            meaning="Closing price of the most recent completed candle.",
            source=Source.STRUCTURED if status.last_bar else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Read from the last candle the session finished processing."
                if status.last_bar
                else "No candle has been processed yet."
            ),
        )
    )
    kpis.append(
        Kpi(
            key="last_close_time",
            label="Last candle",
            value=(
                status.last_bar.close_time.strftime("%Y-%m-%d %H:%M UTC")
                if status.last_bar
                else None
            ),
            meaning="When that candle closed. On H1 a new one should arrive every hour.",
            source=Source.STRUCTURED if status.last_bar else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Taken from the processed candle itself."
                if status.last_bar
                else "No candle has been processed yet."
            ),
        )
    )

    expected = _bars_expected(status)
    received = status.bars_processed
    if expected is None or received is None:
        kpis.append(
            _unavailable(
                "bar_coverage",
                "Candles received",
                "How many candles arrived against how many should have.",
                "The session has not recorded a start time or a candle count yet.",
            )
        )
    else:
        behind = expected - received
        coverage_level = Level.GOOD if behind <= 1 else Level.ATTENTION
        kpis.append(
            Kpi(
                key="bar_coverage",
                label="Candles received",
                value=f"{received} / {expected} expected",
                meaning=(
                    "Candles the session processed against how many hourly closes have "
                    "happened since it started. A shortfall means data was missed."
                ),
                source=Source.STRUCTURED,
                level=coverage_level,
                why=(
                    "Every candle that should have closed has been processed."
                    if coverage_level is Level.GOOD
                    else f"{behind} expected candles have not been processed. One candle of "
                    "lag is normal around the hour boundary; more suggests the feed missed data."
                ),
            )
        )
        levels.append(coverage_level)

    for key, label, value, meaning in (
        (
            "reconnects",
            "Reconnects",
            report.statistics.daily_reconnects if report else None,
            "Times the feed dropped and had to reconnect today.",
        ),
        (
            "gaps",
            "Data gaps",
            report.statistics.daily_gaps if report else None,
            "Missing candles the feed detected today. The platform never fills these in.",
        ),
    ):
        if value is None:
            kpis.append(
                _unavailable(
                    key,
                    label,
                    meaning,
                    "These are counted per day in the daily report, which is written when a "
                    "day rolls over. No report exists yet.",
                )
            )
            continue
        level = Level.GOOD if value == 0 else Level.ATTENTION
        kpis.append(
            Kpi(
                key=key,
                label=label,
                value=str(value),
                meaning=meaning,
                source=Source.STRUCTURED,
                level=level,
                why=(
                    "None recorded today."
                    if level is Level.GOOD
                    else f"{value} recorded today. Worth checking whether the run has a "
                    "connectivity problem."
                ),
            )
        )
        levels.append(level)

    kpis.append(
        _unavailable(
            "regime",
            "Volatility regime",
            "A label for what kind of market this is — trending, ranging, volatile.",
            "The platform does not classify market regime during a live session. Regime "
            "labelling exists only in the research tools, applied to historical data.",
        )
    )

    level = _worst(levels)
    if not status.running:
        headline, explanation = (
            "NO SESSION RUNNING",
            "Nothing is consuming market data, because no trading session holds the lock.",
        )
        level = Level.ATTENTION
    elif connected:
        instrument = status.symbols[0] if status.symbols else "—"
        state_word = "STREAMING" if feed_state == "streaming" else "CONNECTED"
        headline = f"{state_word} · {instrument}"
        explanation = (
            f"Live {status.timeframe.upper()} candles are arriving and being processed."
            if level is Level.GOOD
            else "The feed is connected, but the data arriving is not entirely clean — see below."
        )
    else:
        headline = "FEED NOT CONFIRMED"
        explanation = "The feed has not reported a connected state."
    return Brain(
        key="market",
        title="Market",
        level=level,
        headline=headline,
        explanation=explanation,
        kpis=tuple(kpis),
    )


# --- strategy ------------------------------------------------------------------------------


def _strategy_brain(status: SessionStatus, activity: ActivityCounts) -> Brain:
    """What the strategy is allowed to do, and what it has done."""
    report = status.report
    trades = report.statistics.trade_count if report else None
    low = trades is not None and 0 < trades < MEANINGFUL_TRADE_SAMPLE
    kpis: list[Kpi] = []

    required = status.required_history
    seen = status.bars_processed
    warm = status.warmup_complete
    kpis.append(
        Kpi(
            key="warmup",
            label="Warm-up",
            value=None if (required is None or seen is None) else f"{seen} / {required} bars",
            meaning=(
                "How much history the strategy has seen. It needs a full window before it "
                "may form an opinion at all."
            ),
            source=Source.STRUCTURED
            if required is not None and seen is not None
            else Source.UNAVAILABLE,
            level=Level.GOOD if warm else Level.INFO,
            why=(
                "The strategy has the history it needs and may signal."
                if warm
                else "Still filling its lookback window. Until it is full the strategy is "
                "structurally unable to signal, so no signals is the correct outcome, not a fault."
            )
            if required is not None and seen is not None
            else "The configured strategy is not registered here, so its requirement is unknown.",
        )
    )

    kpis.append(
        Kpi(
            key="signals",
            label="Signals",
            value=_count(activity.signals),
            meaning="Times the strategy said it wanted to enter or exit.",
            source=Source.LOG_DERIVED if activity.signals is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Counted from the per-candle records the session logs. A signal is a request, "
                "not a trade — risk decides whether it becomes one."
                if activity.signals is not None
                else "No candle has been logged yet."
            ),
        )
    )

    if trades is None:
        for key, label, meaning in (
            ("trades", "Closed trades", "Round trips completed: entered and exited."),
            ("win_rate", "Win rate", "Share of closed trades that made money."),
            ("expectancy", "Expectancy", "Average profit or loss per trade."),
            (
                "profit_factor",
                "Profit factor",
                "Gross profit over gross loss. Above 1 means winners outweigh losers.",
            ),
            ("avg_win", "Average win", "Mean size of a winning trade."),
            ("avg_loss", "Average loss", "Mean size of a losing trade."),
        ):
            kpis.append(
                _unavailable(
                    key,
                    label,
                    meaning,
                    "Performance figures are computed in the daily report, which is written "
                    "when a day rolls over. No report exists yet.",
                )
            )
    elif report is not None:
        stats = report.statistics
        sample_why = (
            f"Only {trades} closed trade{'s' if trades != 1 else ''} so far. "
            f"Below about {MEANINGFUL_TRADE_SAMPLE} this is arithmetic, not evidence — do "
            "not read it as performance."
            if low
            else f"Computed over {trades} closed trades."
        )
        kpis.append(
            Kpi(
                key="trades",
                label="Closed trades",
                value=str(trades),
                meaning="Round trips completed: entered and exited.",
                source=Source.STRUCTURED,
                level=Level.INFO,
                why=(
                    "No trade has closed yet."
                    if trades == 0
                    else f"{trades} recorded in today's report."
                ),
            )
        )
        for key, label, value, meaning in (
            (
                "win_rate",
                "Win rate",
                _pct(stats.win_rate),
                "Share of closed trades that made money.",
            ),
            (
                "expectancy",
                "Expectancy",
                _money(stats.expectancy, status.quote_asset),
                "Average profit or loss per trade. Negative means the average trade loses.",
            ),
            (
                "profit_factor",
                "Profit factor",
                None if stats.profit_factor is None else f"{stats.profit_factor:.2f}",
                "Gross profit over gross loss. Above 1 means winners outweigh losers.",
            ),
            (
                "avg_win",
                "Average win",
                _money(stats.average_win, status.quote_asset),
                "Mean size of a winning trade.",
            ),
            (
                "avg_loss",
                "Average loss",
                _money(stats.average_loss, status.quote_asset),
                "Mean size of a losing trade.",
            ),
        ):
            kpis.append(
                Kpi(
                    key=key,
                    label=label,
                    value=value,
                    meaning=meaning,
                    source=Source.STRUCTURED if value is not None else Source.UNAVAILABLE,
                    level=Level.INFO,
                    why=sample_why if value is not None else "No closed trade to compute it from.",
                    low_confidence=low and value is not None,
                )
            )

    kpis.append(
        Kpi(
            key="time_in_market",
            label="Time in market",
            value=_pct(report.statistics.exposure_utilization)
            if report and report.statistics.exposure_utilization is not None
            else None,
            meaning="Share of the run spent holding a position rather than flat.",
            source=(
                Source.STRUCTURED
                if report and report.statistics.exposure_utilization is not None
                else Source.UNAVAILABLE
            ),
            level=Level.INFO,
            why=(
                "Taken from today's report."
                if report and report.statistics.exposure_utilization is not None
                else "Computed in the daily report, which does not exist yet."
            ),
        )
    )

    kpis.append(
        Kpi(
            key="realized_pnl",
            label="Realised P&L",
            value=_money(status.realized_pnl, status.quote_asset),
            meaning="Money actually banked by closed trades, fees included.",
            source=Source.STRUCTURED if status.realized_pnl is not None else Source.UNAVAILABLE,
            level=_pnl_level(status.realized_pnl),
            why=(
                "Persisted by the session itself."
                if status.realized_pnl is not None
                else "No snapshot has been written yet."
            ),
        )
    )

    if not status.running:
        headline, level, explanation = (
            "NOT RUNNING",
            Level.ATTENTION,
            "No session is running, so the strategy is doing nothing.",
        )
    elif warm is False:
        headline = f"WARMING UP — {seen}/{required} bars"
        level = Level.INFO
        explanation = "Strategy cannot generate valid signals yet."
    elif status.open_positions:
        headline, level = "IN POSITION", Level.INFO
        explanation = "The strategy is holding a position; risk is managing its exit."
    elif warm:
        headline, level = "READY", Level.GOOD
        explanation = "Warm-up complete. The strategy is watching for a setup and is flat."
    else:
        headline, level = "UNKNOWN", Level.INFO
        explanation = "Not enough information to say what the strategy is doing."

    return Brain(
        key="strategy",
        title="Strategy",
        level=level,
        headline=headline,
        explanation=explanation,
        kpis=tuple(kpis),
    )


def _pnl_level(value: Decimal | None) -> Level:
    """Colour a profit figure by its sign, treating unknown as neutral."""
    if value is None or value == 0:
        return Level.INFO
    return Level.GOOD if value > 0 else Level.ATTENTION


# --- risk ----------------------------------------------------------------------------------


def _risk_brain(status: SessionStatus) -> Brain:
    """The limits in force, and whether any of them has fired."""
    report = status.report
    kpis: list[Kpi] = []
    levels: list[Level] = []

    kpis.append(
        Kpi(
            key="risk_per_trade",
            label="Risk per trade",
            value=_pct(status.risk_per_trade_pct),
            meaning="Share of the account deliberately put at risk on a single trade.",
            source=Source.STRUCTURED
            if status.risk_per_trade_pct is not None
            else Source.UNAVAILABLE,
            level=Level.GOOD if status.risk_v2_active else Level.ATTENTION,
            why=(
                "Risk V2 is configured, so every entry is sized from the capital it risks."
                if status.risk_v2_active
                else "No risk budget is configured, so positions are not sized from risk."
            ),
        )
    )

    exposure = _exposure(status)
    kpis.append(
        Kpi(
            key="exposure",
            label="Current exposure",
            value=_pct(exposure) if exposure is not None else None,
            meaning="Share of equity currently held in an open position rather than cash.",
            source=Source.STRUCTURED if exposure is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Position value at the last close, over equity."
                if exposure is not None
                else "Needs a snapshot and a marked price; one of them is missing."
            ),
        )
    )

    covered = _stop_coverage(status)
    if covered is None:
        kpis.append(
            Kpi(
                key="stop_coverage",
                label="Stop coverage",
                value="No open position",
                meaning="Whether every open position has a protective stop recorded.",
                source=Source.STRUCTURED,
                level=Level.GOOD,
                why="Nothing is open, so there is nothing to protect.",
            )
        )
    else:
        level = Level.GOOD if covered else Level.DANGER
        kpis.append(
            Kpi(
                key="stop_coverage",
                label="Stop coverage",
                value="Fully covered" if covered else "UNPROTECTED",
                meaning="Whether every open position has a protective stop recorded.",
                source=Source.STRUCTURED,
                level=level,
                why=(
                    "Every open position carries a recorded stop."
                    if covered
                    else "A position is open with no stop written down. The session is "
                    "holding risk it has not recorded."
                ),
            )
        )
        levels.append(level)

    for key, label, value, meaning in (
        (
            "approvals",
            "Orders approved",
            report.statistics.approved_orders if report else None,
            "Order requests the risk engine allowed through.",
        ),
        (
            "risk_rejections",
            "Rejected by risk",
            report.statistics.risk_rejections if report else None,
            "Order requests the risk engine refused, for example for breaching a limit.",
        ),
        (
            "broker_rejections",
            "Rejected by broker",
            report.statistics.broker_rejections if report else None,
            "Orders the venue itself refused, for example below minimum size.",
        ),
    ):
        kpis.append(
            Kpi(
                key=key,
                label=label,
                value=_count(value),
                meaning=meaning,
                source=Source.STRUCTURED if value is not None else Source.UNAVAILABLE,
                level=Level.INFO,
                why=(
                    "Counted in today's report."
                    if value is not None
                    else "Counted in the daily report, which does not exist yet."
                ),
            )
        )

    kpis.append(
        _unavailable(
            "rejection_reasons",
            "Rejection reasons",
            "Which rule refused each order.",
            "The daily report counts rejections but does not keep a per-decision reason, and "
            "the session snapshot keeps none at all. Only the running process knows.",
        )
    )

    kpis.append(
        Kpi(
            key="max_drawdown",
            label="Max drawdown",
            value=_pct(report.statistics.max_drawdown) if report else None,
            meaning="Largest fall from a peak in account value so far.",
            source=Source.STRUCTURED if report else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Measured in today's report."
                if report
                else "Measured in the daily report, which does not exist yet."
            ),
        )
    )
    kpis.append(
        Kpi(
            key="daily_loss",
            label="Daily P&L",
            value=_money(report.statistics.daily_pnl, status.quote_asset) if report else None,
            meaning="Profit or loss for the current day, which the daily-loss breaker watches.",
            source=Source.STRUCTURED if report else Source.UNAVAILABLE,
            level=_pnl_level(report.statistics.daily_pnl if report else None),
            why=(
                "From today's report."
                if report
                else "Computed in the daily report, which does not exist yet."
            ),
        )
    )

    streak = max(
        (breaker.consecutive_losses for breaker in status.breakers),
        default=0 if status.state_present else None,
    )
    kpis.append(
        Kpi(
            key="consecutive_losses",
            label="Consecutive losses",
            value=_count(streak),
            meaning="How many losing trades in a row, which the loss-streak breaker watches.",
            source=Source.STRUCTURED if streak is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Read from the session's recorded breaker state."
                if streak is not None
                else "No snapshot has been written yet."
            ),
        )
    )

    for key, label in (
        ("trailing", "Trailing stop moves"),
        ("break_even", "Break-even moves"),
    ):
        kpis.append(
            _unavailable(
                key,
                label,
                "How often the stop was moved up to lock in profit.",
                "Not observable. Trailing and break-even logic only move the trigger price "
                "fed into a single stop check, which records one reason for all of them, so "
                "a stop-out at a trailed level is indistinguishable from one at the original.",
            )
        )

    kpis.append(
        _unavailable(
            "forced_exits",
            "Forced exits",
            "Exits the risk engine imposed rather than the strategy asking.",
            "Recorded per backtest run but not persisted in a live session's snapshot or "
            "daily report.",
        )
    )

    tripped = [b for b in status.breakers if b.reason is not None]
    if tripped:
        levels.append(Level.DANGER)
    breaker_names = {
        "daily_loss_limit": "Daily Loss",
        "excessive_drawdown": "Total Drawdown",
        "consecutive_losses": "Loss Streak",
    }
    fired = {b.reason.value for b in tripped if b.reason is not None}
    for key, label in breaker_names.items():
        hit = key in fired
        kpis.append(
            Kpi(
                key=f"breaker_{key}",
                label=f"Breaker · {label}",
                value="TRIGGERED" if hit else "OK",
                meaning=f"Halts trading when the {label.lower()} limit is breached.",
                source=Source.STRUCTURED,
                level=Level.DANGER if hit else Level.GOOD,
                why=(
                    "This breaker has fired. It does not clear itself — trading stays halted "
                    "until a person intervenes."
                    if hit
                    else "Not triggered."
                ),
            )
        )

    level = _worst(levels)
    if tripped:
        names = ", ".join(sorted(fired))
        headline, explanation = (
            "TRADING HALTED",
            (
                f"A circuit breaker has fired ({names}). It will not clear itself; the session "
                "cannot trade until somebody intervenes."
            ),
        )
    elif not status.risk_v2_active:
        headline, level = "RISK V1", Level.ATTENTION
        explanation = (
            "No risk budget is configured, so entries are not sized from risk and stops "
            "are not required."
        )
    elif level is Level.DANGER:
        headline, explanation = "ATTENTION REQUIRED", "An open position has no recorded stop."
    else:
        headline, level = "ARMED", Level.GOOD
        explanation = (
            "Risk V2 is fully armed. No breaker triggered. "
            f"Current exposure: {_pct(exposure, 0) if exposure is not None else 'unknown'}."
        )
    return Brain(
        key="risk",
        title="Risk",
        level=level,
        headline=headline,
        explanation=explanation,
        kpis=tuple(kpis),
    )


def _exposure(status: SessionStatus) -> Decimal | None:
    """Return the share of equity held in positions, or nothing when unknowable."""
    if not status.state_present:
        return None
    if not status.open_positions:
        return Decimal(0)
    mark = status.marked_at
    equity = status.equity
    if mark is None or equity is None or equity == 0:
        return None
    held = sum((position.market_value(mark) for position in status.open_positions), Decimal(0))
    return held / equity


def _stop_coverage(status: SessionStatus) -> bool | None:
    """Return whether open positions are protected, or ``None`` when none are open."""
    if not status.open_positions:
        return None
    protected = {risk.symbol for risk in status.position_risk}
    return all(position.symbol in protected for position in status.open_positions)


# --- execution -----------------------------------------------------------------------------


def _execution_brain(status: SessionStatus, activity: ActivityCounts) -> Brain:
    """What actually reached the venue."""
    report = status.report
    kpis: list[Kpi] = []

    kpis.append(
        Kpi(
            key="intents",
            label="Order intents",
            value=_count(activity.intents),
            meaning="Orders the system wanted to place, before risk had its say.",
            source=Source.LOG_DERIVED if activity.intents is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Counted from the per-candle records the session logs."
                if activity.intents is not None
                else "No candle has been logged yet."
            ),
        )
    )
    kpis.append(
        Kpi(
            key="fills",
            label="Fills",
            value=_count(activity.fills),
            meaning="Orders that actually executed.",
            source=Source.LOG_DERIVED if activity.fills is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Counted from the per-candle records the session logs."
                if activity.fills is not None
                else "No candle has been logged yet."
            ),
        )
    )

    if activity.intents is None or activity.fills is None:
        kpis.append(
            _unavailable(
                "fill_rate",
                "Fill rate",
                "Share of intended orders that executed.",
                "Needs both intents and fills, and no candle has been logged yet.",
            )
        )
    elif activity.intents == 0:
        kpis.append(
            Kpi(
                key="fill_rate",
                label="Fill rate",
                value="No orders yet",
                meaning="Share of intended orders that executed.",
                source=Source.LOG_DERIVED,
                level=Level.INFO,
                why="Nothing has been ordered, so there is no rate to compute.",
            )
        )
    else:
        rate = activity.fills / activity.intents
        kpis.append(
            Kpi(
                key="fill_rate",
                label="Fill rate",
                value=_pct(rate, 0),
                meaning="Share of intended orders that executed.",
                source=Source.LOG_DERIVED,
                level=Level.GOOD if rate >= HEALTHY_FILL_RATE else Level.ATTENTION,
                why=(
                    f"{activity.fills} of {activity.intents} intents filled."
                    if rate >= HEALTHY_FILL_RATE
                    else f"Only {activity.fills} of {activity.intents} intents filled; the "
                    "rest were refused or resized away."
                ),
            )
        )

    kpis.append(
        Kpi(
            key="fees",
            label="Fees paid",
            value=_money(status.total_fees, status.quote_asset),
            meaning="Total commission paid, which comes straight out of profit.",
            source=Source.STRUCTURED if status.total_fees is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Persisted by the session itself."
                if status.total_fees is not None
                else "No snapshot has been written yet."
            ),
        )
    )
    kpis.append(
        Kpi(
            key="slippage",
            label="Slippage",
            value=_money(report.statistics.slippage_paid, status.quote_asset) if report else None,
            meaning="Cost of filling away from the expected price.",
            source=Source.STRUCTURED if report else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Measured in today's report."
                if report
                else "Measured in the daily report, which does not exist yet."
            ),
        )
    )
    kpis.append(
        Kpi(
            key="rejected_orders",
            label="Rejected orders",
            value=_count(report.statistics.rejected_orders if report else None),
            meaning="Orders that never became a fill, for any reason.",
            source=Source.STRUCTURED if report else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Counted in today's report."
                if report
                else "Counted in the daily report, which does not exist yet."
            ),
        )
    )
    kpis.append(
        _unavailable(
            "latency",
            "Order latency",
            "How long the venue took to answer.",
            "Not measured. Paper execution is simulated locally, so there is no venue "
            "round trip to time.",
        )
    )

    filled = activity.fills or 0
    if filled == 0:
        headline, level = "NO ORDERS YET", Level.INFO
        explanation = "Nothing has been sent to the venue, so there is nothing to execute."
    else:
        headline, level = f"{filled} FILL{'S' if filled != 1 else ''}", Level.GOOD
        explanation = "Orders are reaching the venue and executing."
    return Brain(
        key="execution",
        title="Execution",
        level=level,
        headline=headline,
        explanation=explanation,
        kpis=tuple(kpis),
    )


# --- portfolio -----------------------------------------------------------------------------


def _portfolio_brain(status: SessionStatus) -> Brain:
    """The money, and what is currently held."""
    unit = status.quote_asset
    total = None
    if status.realized_pnl is not None and status.unrealized_pnl is not None:
        total = status.realized_pnl + status.unrealized_pnl

    kpis = [
        Kpi(
            key="starting_capital",
            label="Starting capital",
            value=_money(status.starting_capital, unit),
            meaning="What the account began with.",
            source=Source.STRUCTURED,
            level=Level.INFO,
            why="Declared in configuration and used to seed the account.",
        ),
        Kpi(
            key="equity",
            label="Equity",
            value=_money(status.equity, unit),
            meaning="Everything the account is worth: cash plus what open positions are worth.",
            source=Source.STRUCTURED if status.equity is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Cash plus positions valued at the last closed candle, not a live tick."
                if status.equity is not None
                else "Needs a snapshot, and none has been written yet."
            ),
        ),
        Kpi(
            key="cash",
            label="Cash",
            value=_money(status.cash, unit),
            meaning="Spendable balance not tied up in a position.",
            source=Source.STRUCTURED if status.cash is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Persisted by the session."
                if status.cash is not None
                else "No snapshot has been written yet."
            ),
        ),
        Kpi(
            key="realized_pnl",
            label="Realised P&L",
            value=_money(status.realized_pnl, unit),
            meaning="Money banked by closed trades. This is real and cannot change.",
            source=Source.STRUCTURED if status.realized_pnl is not None else Source.UNAVAILABLE,
            level=_pnl_level(status.realized_pnl),
            why=(
                "Persisted by the session."
                if status.realized_pnl is not None
                else "No snapshot has been written yet."
            ),
        ),
        Kpi(
            key="unrealized_pnl",
            label="Unrealised P&L",
            value=_money(status.unrealized_pnl, unit),
            meaning="Paper profit on an open position. It moves with price and is not banked.",
            source=Source.STRUCTURED if status.unrealized_pnl is not None else Source.UNAVAILABLE,
            level=_pnl_level(status.unrealized_pnl),
            why=(
                "Valued at the last closed candle."
                if status.unrealized_pnl is not None
                else "Needs a snapshot and a marked price."
            ),
        ),
        Kpi(
            key="total_pnl",
            label="Total P&L",
            value=_money(total, unit),
            meaning="Realised plus unrealised: the whole result so far.",
            source=Source.STRUCTURED if total is not None else Source.UNAVAILABLE,
            level=_pnl_level(total),
            why=(
                "Realised and unrealised added together."
                if total is not None
                else "One of its two halves is unknown."
            ),
        ),
    ]

    if status.open_positions:
        held = status.open_positions[0]
        risk = next((r for r in status.position_risk if r.symbol == held.symbol), None)
        stop = risk.stop.trigger_price if risk else None
        mark = status.marked_at
        distance = None
        if stop is not None and mark is not None and mark != 0:
            distance = (mark - stop) / mark
        kpis.extend(
            [
                Kpi(
                    key="entry",
                    label="Entry price",
                    value=_money(held.avg_entry_price, unit),
                    meaning="Average price the position was opened at.",
                    source=Source.STRUCTURED,
                    level=Level.INFO,
                    why="Recorded when the position opened.",
                ),
                Kpi(
                    key="current",
                    label="Current price",
                    value=_money(mark, unit),
                    meaning="Last closed candle's price, used to value the position.",
                    source=Source.STRUCTURED if mark is not None else Source.UNAVAILABLE,
                    level=Level.INFO,
                    why=(
                        "From the last processed candle."
                        if mark is not None
                        else "No candle has been processed."
                    ),
                ),
                Kpi(
                    key="stop",
                    label="Stop price",
                    value=_money(stop, unit),
                    meaning="The price at which risk will close this position to cap the loss.",
                    source=Source.STRUCTURED if stop is not None else Source.UNAVAILABLE,
                    level=Level.GOOD if stop is not None else Level.DANGER,
                    why=(
                        "Recorded with the position when it opened."
                        if stop is not None
                        else "No stop is recorded for an open position."
                    ),
                ),
                Kpi(
                    key="distance_to_stop",
                    label="Distance to stop",
                    value=_pct(distance),
                    meaning="How far price can fall before the stop closes the position.",
                    source=Source.STRUCTURED if distance is not None else Source.UNAVAILABLE,
                    level=Level.INFO,
                    why=(
                        "Current price against the recorded stop."
                        if distance is not None
                        else "Needs both a stop and a current price."
                    ),
                ),
            ]
        )
        headline = f"LONG {held.symbol}"
        explanation = "A position is open. Risk is managing its stop."
        level = Level.DANGER if stop is None else Level.INFO
    else:
        headline, level = "FLAT", Level.INFO
        explanation = (
            "No position is open. The whole account is in cash."
            if status.state_present
            else "No position recorded. Nothing has been persisted yet."
        )

    return Brain(
        key="portfolio",
        title="Portfolio",
        level=level,
        headline=headline,
        explanation=explanation,
        kpis=tuple(kpis),
    )


# --- infrastructure ------------------------------------------------------------------------


def _infra_brain(status: SessionStatus) -> Brain:
    """Whether the machinery under all of this is holding up."""
    report = status.report
    lock = status.lock
    kpis: list[Kpi] = []
    levels: list[Level] = []

    running_level = Level.GOOD if status.running else Level.ATTENTION
    kpis.append(
        Kpi(
            key="service",
            label="Session process",
            value="RUNNING" if status.running else "NOT RUNNING",
            meaning="Whether a trading session is alive and holding the lock.",
            source=Source.STRUCTURED,
            level=running_level,
            why=(
                f"A live process (pid {lock.pid}) holds the session lock."
                if status.running and lock
                else "No live process holds the session lock."
            ),
        )
    )
    levels.append(running_level)

    elapsed = _elapsed_hours(status)
    kpis.append(
        Kpi(
            key="uptime",
            label="Uptime",
            value=None if elapsed is None else f"{int(elapsed)}h {int((elapsed % 1) * 60):02d}m",
            meaning="How long this session has been running without stopping.",
            source=Source.STRUCTURED if elapsed is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Measured from the session's own recorded start time."
                if elapsed is not None
                else "No start time is recorded."
            ),
        )
    )

    restarts = status.restarts
    kpis.append(
        Kpi(
            key="restarts",
            label="Restarts",
            value=_count(restarts),
            meaning="How many times this session was resumed after stopping.",
            source=Source.STRUCTURED if restarts is not None else Source.UNAVAILABLE,
            level=Level.GOOD if restarts == 0 else Level.ATTENTION,
            why=(
                "The session has run continuously since it started."
                if restarts == 0
                else f"The session has been resumed {restarts} time(s)."
                if restarts is not None
                else "No snapshot has been written yet."
            ),
        )
    )

    persistence_level = Level.GOOD if status.state_present else Level.INFO
    kpis.append(
        Kpi(
            key="persistence",
            label="Persistence",
            value="OK" if status.state_present else "No snapshot yet",
            meaning="Whether the session is successfully writing its state to disk.",
            source=Source.STRUCTURED,
            level=persistence_level,
            why=(
                "A snapshot exists and parses."
                if status.state_present
                else "A session writes its first snapshot after it finishes a candle, so "
                "this is expected before the first close."
            ),
        )
    )

    kpis.append(
        Kpi(
            key="latest_snapshot",
            label="Latest snapshot",
            value=status.saved_at.strftime("%Y-%m-%d %H:%M UTC") if status.saved_at else None,
            meaning="When the session last wrote its state down.",
            source=Source.STRUCTURED if status.saved_at else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Stamped by the session when it saved."
                if status.saved_at
                else "Nothing has been saved yet."
            ),
        )
    )

    errors = report.statistics.runtime_exceptions if report else None
    error_level = Level.INFO if errors is None else (Level.GOOD if errors == 0 else Level.DANGER)
    kpis.append(
        Kpi(
            key="errors",
            label="Runtime errors",
            value=_count(errors),
            meaning="Unexpected failures the session hit while running.",
            source=Source.STRUCTURED if errors is not None else Source.UNAVAILABLE,
            level=error_level,
            why=(
                "None recorded today."
                if errors == 0
                else f"{errors} recorded today — worth investigating."
                if errors is not None
                else "Counted in the daily report, which does not exist yet."
            ),
        )
    )
    if errors is not None:
        levels.append(error_level)

    kpis.extend(_warm_start_kpis(status))

    kpis.append(
        _unavailable(
            "database",
            "Database",
            "Whether the platform's database is reachable.",
            "This dashboard never connects to the database — it reads files only — so it "
            "cannot report on one without becoming something more than an observer.",
        )
    )

    level = _worst(levels)
    if status.running:
        headline = "HEALTHY" if level is Level.GOOD else "ATTENTION REQUIRED"
        explanation = (
            "The session process is alive, writing snapshots, and has not logged errors."
            if level is Level.GOOD
            else "The session is running but something below needs looking at."
        )
    else:
        headline, level = "SESSION DOWN", Level.ATTENTION
        explanation = "No process holds the session lock. The session is not running."
    return Brain(
        key="infra",
        title="Infrastructure",
        level=level,
        headline=headline,
        explanation=explanation,
        kpis=tuple(kpis),
    )


# --- smoke ---------------------------------------------------------------------------------


def _warm_start_kpis(status: SessionStatus) -> list[Kpi]:
    """Report how the session obtained its market context.

    Three outcomes, kept apart on purpose. ``NOT USED`` is an ordinary cold start and says
    so; ``FAILED`` means a history existed and was refused, which an operator needs to
    distinguish from having none at all. Collapsing them would report a rejected history and
    a first-ever deployment as the same thing.

    ``Financial state restored`` is a constant ``NO``. It is not a measurement that could
    come out otherwise — it is the contract, shown rather than left to be taken on trust.
    """
    record = status.warm_start
    if record is None:
        return [
            Kpi(
                key="warm_start",
                label="Warm start",
                value="NOT USED",
                meaning=(
                    "Whether the session began with market history already loaded, instead "
                    "of waiting out its warm-up blind."
                ),
                source=Source.STRUCTURED,
                level=Level.INFO,
                why=(
                    "No market context was restored. Either none was kept, or it was "
                    "refused — the session's own log names which, and a refusal is never "
                    "silent."
                ),
            ),
            Kpi(
                key="warm_start_financial",
                label="Financial state restored",
                value="NO",
                meaning="Whether warm-start brought back any money, position or order.",
                source=Source.STRUCTURED,
                level=Level.GOOD,
                why=(
                    "Never, by construction. Warm-start carries candles; a candle has no "
                    "field capable of expressing a balance, a position or a fill."
                ),
            ),
        ]

    continuity_proven = record.first_live_bar_close_time is not None
    return [
        Kpi(
            key="warm_start",
            label="Warm start",
            value="ACTIVE",
            meaning=(
                "The session began with market history already loaded, so the strategy did "
                "not have to wait out its warm-up blind."
            ),
            source=Source.STRUCTURED,
            level=Level.GOOD,
            why=(
                f"{record.bars_loaded} candles restored against a requirement of "
                f"{record.required_history}."
            ),
        ),
        Kpi(
            key="warm_start_source",
            label="Source session",
            value=record.source_session_id,
            meaning="Which session's candles were reused.",
            source=Source.STRUCTURED,
            level=Level.INFO,
            why=(
                "That session was checked and found to carry no financial state; one that "
                "did would have been refused."
            ),
        ),
        Kpi(
            key="warm_start_bars",
            label="Bars loaded",
            value=f"{record.bars_loaded} / {record.required_history} required",
            meaning="Candles restored against what the strategy needs before it may signal.",
            source=Source.STRUCTURED,
            level=Level.GOOD,
            why="A history shorter than the requirement is refused, never applied in part.",
        ),
        Kpi(
            key="warm_start_first",
            label="First warm bar",
            value=record.first_bar_close_time.strftime("%Y-%m-%d %H:%M UTC"),
            meaning="Where the restored window begins.",
            source=Source.STRUCTURED,
            level=Level.INFO,
            why="Read from the restored history itself.",
        ),
        Kpi(
            key="warm_start_last",
            label="Last warm bar",
            value=record.last_bar_close_time.strftime("%Y-%m-%d %H:%M UTC"),
            meaning="The last restored candle, which the first live one must follow.",
            source=Source.STRUCTURED,
            level=Level.INFO,
            why="Read from the restored history itself.",
        ),
        Kpi(
            key="warm_start_first_live",
            label="First live bar",
            value=(
                record.first_live_bar_close_time.strftime("%Y-%m-%d %H:%M UTC")
                if record.first_live_bar_close_time is not None
                else None
            ),
            meaning="The first candle to arrive from the feed after the restored window.",
            source=Source.STRUCTURED if continuity_proven else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "Recorded when it was accepted."
                if continuity_proven
                else "No live candle has arrived yet, so the seam has not been exercised."
            ),
        ),
        Kpi(
            key="warm_start_continuity",
            label="Continuity",
            value="PASS" if continuity_proven else "PENDING",
            meaning=(
                "Whether the first live candle followed the restored window without a gap "
                "or an overlap."
            ),
            source=Source.STRUCTURED,
            level=Level.GOOD if continuity_proven else Level.INFO,
            why=(
                "A live candle was accepted after the restored window. A gap or an overlap "
                "would have aborted the session rather than being recorded here."
                if continuity_proven
                else "Waiting for the first live candle. Until one arrives there is no seam "
                "to have proven."
            ),
        ),
        Kpi(
            key="warm_start_digest",
            label="Integrity",
            value="PASS",
            meaning="Whether the restored candles matched the digest recorded with them.",
            source=Source.STRUCTURED,
            level=Level.GOOD,
            why=(
                f"Digest {record.digest[:12]}… verified on load. A mismatch refuses the "
                "history rather than warm-starting from candles that were altered."
            ),
        ),
        Kpi(
            key="warm_start_financial",
            label="Financial state restored",
            value="NO",
            meaning="Whether warm-start brought back any money, position or order.",
            source=Source.STRUCTURED,
            level=Level.GOOD,
            why=(
                "Never, by construction. Warm-start carries candles; a candle has no field "
                "capable of expressing a balance, a position or a fill."
            ),
        ),
    ]


def _smoke_brain(status: SessionStatus, smoke_hours: float | None) -> Brain:
    """Progress through the validation run, when one was declared."""
    elapsed = status.elapsed
    kpis: list[Kpi] = []

    kpis.append(
        Kpi(
            key="elapsed",
            label="Elapsed",
            value=None if elapsed is None else _duration(elapsed),
            meaning="How long the run has been going.",
            source=Source.STRUCTURED if elapsed is not None else Source.UNAVAILABLE,
            level=Level.INFO,
            why=(
                "From the session's recorded start time."
                if elapsed is not None
                else "No start time is recorded."
            ),
        )
    )

    if smoke_hours is None or elapsed is None:
        kpis.append(
            _unavailable(
                "remaining",
                "Remaining",
                "How much of the planned run is left.",
                "No target length was declared for this run, so there is nothing to count "
                "down to. A progress bar against an undeclared target would be invented.",
            )
        )
        progress = None
    else:
        target = timedelta(hours=smoke_hours)
        remaining = target - elapsed
        progress = min(elapsed / target, 1.0)
        kpis.append(
            Kpi(
                key="remaining",
                label="Remaining",
                value=_duration(remaining) if remaining.total_seconds() > 0 else "complete",
                meaning="How much of the planned run is left.",
                source=Source.STRUCTURED,
                level=Level.INFO,
                why=f"Target length is {smoke_hours:g} hours.",
            )
        )

    expected = _bars_expected(status)
    received = status.bars_processed
    if expected is None or received is None:
        kpis.append(
            _unavailable(
                "bars",
                "Candles expected vs received",
                "Whether every hourly candle that should have arrived did.",
                "Needs a start time and a candle count.",
            )
        )
    else:
        behind = expected - received
        level = Level.GOOD if behind <= 1 else Level.ATTENTION
        kpis.append(
            Kpi(
                key="bars",
                label="Candles expected vs received",
                value=f"{received} received / {expected} expected",
                meaning="Whether every hourly candle that should have arrived did.",
                source=Source.STRUCTURED,
                level=level,
                why=(
                    "Every expected candle has been processed."
                    if level is Level.GOOD
                    else f"{behind} candles short. That gap is what a smoke test exists to catch."
                ),
            )
        )

    if progress is None:
        headline, level = "RUNNING", Level.INFO
        explanation = "The session is running, with no declared end."
    else:
        headline = f"{progress * 100:.0f}% COMPLETE"
        level = Level.GOOD if status.running else Level.ATTENTION
        explanation = (
            f"{_duration(elapsed)} into a {smoke_hours:g} hour run."
            if status.running and elapsed is not None
            else "The run stopped before reaching its target length."
        )
    return Brain(
        key="smoke",
        title="Smoke test",
        level=level,
        headline=headline,
        explanation=explanation,
        kpis=tuple(kpis),
    )


def _duration(span: timedelta) -> str:
    """Render a duration in whole hours and minutes."""
    total = max(int(span.total_seconds()), 0)
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


# --- assembly ------------------------------------------------------------------------------


def build_brains(
    status: SessionStatus,
    activity: ActivityCounts,
    *,
    feed_state: str | None = None,
    smoke_hours: float | None = None,
) -> tuple[Brain, ...]:
    """Read a gathered status as seven areas, in the order a person checks them."""
    return (
        _market_brain(status, feed_state),
        _strategy_brain(status, activity),
        _risk_brain(status),
        _execution_brain(status, activity),
        _portfolio_brain(status),
        _infra_brain(status),
        _smoke_brain(status, smoke_hours),
    )


def summarise(status: SessionStatus, brains: tuple[Brain, ...]) -> Summary:
    """State the whole system in a few sentences, drawn only from the brains themselves.

    Assembled rather than composed freely: every sentence restates a finding one of the
    brains already made, so the summary cannot assert something the panels below contradict.
    """
    by_key = {brain.key: brain for brain in brains}
    sentences = _summary_sentences(status, by_key)
    blockers = _summary_blockers(status, by_key, brains)

    level = _worst([brain.level for brain in brains])
    if status.health == Health.FAILED:
        level = Level.DANGER
    intervention = level is Level.DANGER or not status.running
    sentences.append("Intervention required." if intervention else "No intervention required.")

    headline = {
        Level.GOOD: "HEALTHY",
        Level.INFO: "HEALTHY",
        Level.ATTENTION: "DEGRADED",
        Level.DANGER: "ATTENTION REQUIRED",
    }[level]
    return Summary(
        level=level,
        headline=headline,
        sentences=tuple(sentences),
        intervention_required=intervention,
        blockers=tuple(blockers),
    )


def _summary_sentences(status: SessionStatus, by_key: dict[str, Brain]) -> list[str]:
    """Restate each brain's own finding in one short sentence."""
    sentences: list[str] = []

    infra = by_key["infra"]
    if infra.level is Level.GOOD:
        sentences.append("System healthy.")
    elif status.running:
        sentences.append("System running with something to look at.")
    else:
        sentences.append("No trading session is running.")

    sentences.append(
        "Market feed stable."
        if by_key["market"].level is Level.GOOD
        else "Market feed needs checking."
    )

    strategy = by_key["strategy"]
    if strategy.headline.startswith("WARMING UP"):
        sentences.append(f"Strategy still warming up ({strategy.headline.split('— ')[-1]}).")
    elif strategy.headline == "READY":
        sentences.append("Strategy ready and watching for a setup.")
    elif strategy.headline == "IN POSITION":
        sentences.append("Strategy is holding a position.")

    portfolio = by_key["portfolio"]
    sentences.append(
        "No positions open." if portfolio.headline == "FLAT" else f"{portfolio.headline} open."
    )

    risk = by_key["risk"]
    sentences.append("Risk engine fully armed." if risk.level is Level.GOOD else risk.explanation)
    return sentences


def _summary_blockers(
    status: SessionStatus, by_key: dict[str, Brain], brains: tuple[Brain, ...]
) -> list[str]:
    """Collect everything standing between the run and a clean bill of health."""
    blockers: list[str] = []
    if not status.running:
        blockers.append("The session process is not running.")
    if by_key["market"].level is not Level.GOOD:
        blockers.append(by_key["market"].explanation)
    if by_key["risk"].level is Level.DANGER:
        blockers.append(by_key["risk"].explanation)
    for brain in brains:
        for kpi in brain.kpis:
            if kpi.level is Level.DANGER and kpi.why not in blockers:
                blockers.append(kpi.why)
    return blockers
