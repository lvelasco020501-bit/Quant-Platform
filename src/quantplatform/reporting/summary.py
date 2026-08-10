"""The human-readable account of a day.

Two renderings of the same facts: a short :class:`~quantplatform.reporting.models.DailySummary`
an operator reads first, and a full Markdown page they read when the summary says something
is wrong.

**Operational observations only.** Nothing here says what to trade, how much to allocate,
whether a strategy is worth running or where a market is going. Those are investment
opinions, and a reporting layer is in no position to offer one — it can see what happened to
a process, not what should happen to a portfolio. Recommendations are limited to things an
operator can act on without touching a position: check a feed, look at a threshold, read a
log. There is a test that keeps it that way.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.reporting.models import (
    DailyAlerts,
    DailyComparison,
    DailyHealth,
    DailyReport,
    DailyStatistics,
    DailySummary,
    HealthLevel,
)

__all__ = ["build_summary", "render_csv", "render_markdown"]

_MONEY_PLACES = 2
_RATIO_PLACES = 4

_HEALTH_ADVICE: dict[HealthLevel, str] = {
    HealthLevel.GREEN: "No operational action required.",
    HealthLevel.YELLOW: "Review the failing checks before the next session.",
    HealthLevel.RED: (
        "Treat this session's output as unreliable until the failing checks are resolved."
    ),
}


def build_summary(
    *,
    day: date,
    statistics: DailyStatistics,
    health: DailyHealth,
    alerts: DailyAlerts,
    comparison: DailyComparison | None = None,
) -> DailySummary:
    """Write the short account of a day.

    Args:
        day: The day being described.
        statistics: Its computed figures.
        health: Its operational verdict.
        alerts: Everything it raised.
        comparison: How it moved against the previous reported day, if there was one.

    Returns:
        The summary, in operational language only.
    """
    return DailySummary(
        headline=_headline(day, statistics, health),
        profit_line=_profit_line(statistics),
        health_line=_health_line(health, alerts),
        recommendations=_recommendations(statistics, health, alerts),
        anomalies=_anomalies(statistics, alerts, comparison),
    )


def _headline(day: date, statistics: DailyStatistics, health: DailyHealth) -> str:
    """Return the one-line description of the day."""
    if statistics.bars_processed == 0:
        return f"{day.isoformat()}: no bars were processed."
    trades = (
        "no trades closed"
        if statistics.trade_count == 0
        else f"{statistics.trade_count} trade(s) closed"
    )
    return (
        f"{day.isoformat()}: {statistics.bars_processed} bar(s) processed, {trades}, "
        f"health {health.level.value}."
    )


def _profit_line(statistics: DailyStatistics) -> str:
    """Return the day's result in plain terms."""
    pnl = statistics.daily_pnl
    if pnl == ZERO:
        return f"The account finished flat at {statistics.daily_equity}."
    verb = "gained" if pnl > ZERO else "lost"
    line = f"The account {verb} {abs(pnl)}, finishing at {statistics.daily_equity}"
    if statistics.daily_return is not None:
        line += f" ({_percent(statistics.daily_return)})"
    return f"{line}."


def _health_line(health: DailyHealth, alerts: DailyAlerts) -> str:
    """Return the operational verdict in plain terms."""
    failing = health.failing
    if not failing and alerts.count == 0:
        return "Every operational check passed and no alerts fired."
    parts: list[str] = []
    if failing:
        parts.append(
            f"{len(failing)} check(s) failed: " + ", ".join(check.name.value for check in failing)
        )
    if alerts.count:
        parts.append(f"{alerts.count} alert(s) raised")
    return "; ".join(parts) + "."


def _recommendations(
    statistics: DailyStatistics, health: DailyHealth, alerts: DailyAlerts
) -> tuple[str, ...]:
    """Return operational next steps — never trading decisions."""
    advice: list[str] = [_HEALTH_ADVICE[health.level]]
    for check in health.failing:
        advice.append(f"Investigate {check.name.value.replace('_', ' ')}: {check.message}")
    if statistics.report_failures:
        advice.append(
            f"{statistics.report_failures} report(s) failed to generate during the session; "
            "check the reporting sink."
        )
    skipped = health.skipped
    if skipped:
        advice.append(
            "Not measured this session: "
            + ", ".join(check.name.value.replace("_", " ") for check in skipped)
            + "."
        )
    if alerts.critical:
        advice.append("Critical alerts are present; read the alert table before restarting.")
    return tuple(advice)


def _anomalies(
    statistics: DailyStatistics,
    alerts: DailyAlerts,
    comparison: DailyComparison | None,
) -> tuple[str, ...]:
    """Return the things about the day that stand out."""
    noted: list[str] = [alert.message for alert in alerts.alerts]
    if statistics.bars_processed > 0 and statistics.trade_count == 0:
        noted.append("The session processed bars but closed no positions.")
    if statistics.approved_orders == 0 and statistics.risk_rejections > 0:
        noted.append("Every order intent was refused by risk; none reached the broker.")
    if comparison is not None:
        noted.extend(comparison.deteriorations)
    return tuple(noted)


def render_markdown(report: DailyReport) -> str:
    """Render a full day as a Markdown page.

    Args:
        report: The finished report.

    Returns:
        Markdown text, one heading per section.
    """
    statistics = report.statistics
    lines: list[str] = [
        f"# Daily report — {report.day.isoformat()}",
        "",
        f"- **Session**: `{report.session_id}`",
        f"- **Strategy**: `{report.strategy_id}`",
        f"- **Reporting zone**: {report.timezone}",
        f"- **Generated**: {report.generated_at.isoformat()}",
        f"- **Health**: **{report.health.level.value.upper()}**",
        "",
        "## Summary",
        "",
        report.summary.headline,
        "",
        report.summary.profit_line,
        "",
        report.summary.health_line,
        "",
    ]
    lines += _markdown_table(
        "## Account",
        (
            ("Opening equity", _money(statistics.opening_equity)),
            ("Closing equity", _money(statistics.daily_equity)),
            ("PnL", _money(statistics.daily_pnl)),
            ("Return", _optional_percent(statistics.daily_return)),
            ("Max drawdown", _percent(statistics.max_drawdown)),
            ("Sharpe", _optional(statistics.sharpe_ratio)),
            ("Sortino", _optional(statistics.sortino_ratio)),
            ("Exposure at close", _optional_percent(statistics.exposure_utilization)),
        ),
    )
    lines += _markdown_table(
        "## Trades",
        (
            ("Closed", str(statistics.trade_count)),
            ("Long / short", f"{statistics.long_trades} / {statistics.short_trades}"),
            ("Wins / losses", f"{statistics.winning_trades} / {statistics.losing_trades}"),
            ("Win rate", _optional_percent(statistics.win_rate)),
            ("Average win", _optional(statistics.average_win)),
            ("Average loss", _optional(statistics.average_loss)),
            ("Profit factor", _optional(statistics.profit_factor)),
            ("Expectancy", _optional(statistics.expectancy)),
            ("Largest winner", _optional(statistics.largest_winner)),
            ("Largest loser", _optional(statistics.largest_loser)),
            ("Average position size", _optional(statistics.average_position_size)),
            ("Average holding (s)", _optional(statistics.average_holding_seconds)),
        ),
    )
    lines += _markdown_table(
        "## Costs and order flow",
        (
            ("Commission", _money(statistics.commission_paid)),
            ("Slippage", _money(statistics.slippage_paid)),
            ("Traded notional", _money(statistics.traded_notional)),
            ("Approved orders", str(statistics.approved_orders)),
            ("Resized orders", str(statistics.resized_orders)),
            ("Rejected orders", str(statistics.rejected_orders)),
            ("Risk rejections", str(statistics.risk_rejections)),
            ("Broker rejections", str(statistics.broker_rejections)),
        ),
    )
    lines += _markdown_table(
        "## Process",
        (
            ("Bars processed (day)", str(statistics.bars_processed)),
            ("Session bars received (day)", str(statistics.daily_session_bars_received)),
            ("Session bars processed (day)", str(statistics.daily_session_bars_processed)),
            (
                "Session acceptance (daily)",
                _optional_percent(statistics.daily_session_acceptance_rate),
            ),
            ("Bars rejected (session)", str(statistics.bars_rejected)),
            ("Bar acceptance (session)", _optional_percent(statistics.acceptance_rate)),
            ("Runtime span (s)", _money(statistics.runtime_seconds)),
            ("Reconnects", str(statistics.daily_reconnects)),
            ("Gaps", str(statistics.daily_gaps)),
            ("Heartbeat failures", str(statistics.daily_heartbeat_failures)),
            ("Duplicate candles", str(statistics.daily_duplicate_candles)),
            ("Out-of-order candles", str(statistics.out_of_order_candles)),
            ("Unknown symbols", str(statistics.unknown_symbols)),
            ("Missing bars", str(statistics.missing_bars)),
            ("Runtime exceptions", str(statistics.runtime_exceptions)),
            ("Session interruptions", str(statistics.session_interruptions)),
            ("Report failures", str(statistics.report_failures)),
            ("Clock drift (s)", _optional(statistics.clock_drift_seconds)),
        ),
    )
    lines += _feed_health_section(report)
    lines += _symbol_rules_section(report)
    lines += _health_section(report)
    lines += _alert_section(report)
    lines += _comparison_section(report)
    lines += _recommendation_section(report)
    return "\n".join(lines).rstrip() + "\n"


def _symbol_rules_section(report: DailyReport) -> list[str]:
    """Render whether the venue's rulebook is still being re-read.

    Session-cumulative, and labelled as such. Unlike the feed section above, these are not
    daily deltas: the question is not "how often did refresh fail today" but "is refresh
    working right now, and how old are the rules the risk engine is about to judge". A
    difference between two days answers neither.

    An unwired refresh loop is called out rather than shown as zeros, for the same reason
    the feed section does it. A run nobody is keeping current will have every order refused
    once the rules pass the freshness budget, and printing a tidy set of zeros on the way
    there would make the last healthy-looking report the most misleading one.
    """
    statistics = report.statistics
    lines = ["## Venue rules — session", ""]
    if not statistics.symbol_rules_telemetry_available:
        lines += [
            "No symbol-rules telemetry was supplied, so nothing here is measured. If no "
            "refresh loop is running, the venue's rules will pass the risk engine's "
            "freshness budget and every order intent will be refused from that point on.",
            "",
        ]
    age = statistics.symbol_rules_age_seconds
    budget = statistics.symbol_rules_stale_after_seconds
    rows = (
        ("Refresh attempts", str(statistics.symbol_rules_refresh_attempts)),
        ("Refresh successes", str(statistics.symbol_rules_refresh_successes)),
        ("Refresh failures", str(statistics.symbol_rules_refresh_failures)),
        ("Consecutive failures", str(statistics.symbol_rules_consecutive_failures)),
        ("Last refresh", _optional_time(statistics.symbol_rules_last_refresh_at)),
        ("Rules age", _optional_hours(age)),
        ("Staleness budget", _optional_hours(Decimal(budget) if budget > 0 else None)),
        ("Venue rule changes", str(statistics.symbol_rules_changes)),
        (
            "Working orders in conflict",
            str(statistics.symbol_rules_working_order_conflicts),
        ),
        ("Last failure", statistics.symbol_rules_last_failure_reason or "—"),
    )
    lines += ["| Metric | Value |", "| --- | --- |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    lines.append("")
    return lines


def _optional_hours(seconds: Decimal | None) -> str:
    """Render an age in hours, or an em dash when nothing measured it."""
    if seconds is None:
        return "—"
    return f"{seconds / Decimal(3600):.1f}h"


def _optional_time(moment: datetime | None) -> str:
    """Render an instant, or an em dash when it never happened."""
    if moment is None:
        return "—"
    return moment.isoformat()


def _markdown_table(heading: str, rows: tuple[tuple[str, str], ...]) -> list[str]:
    """Render a two-column table under a heading."""
    lines = [heading, "", "| Metric | Value |", "| --- | --- |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    lines.append("")
    return lines


def _feed_health_section(report: DailyReport) -> list[str]:
    """Render what the data stream did *today*.

    Every counter here is a daily figure, obtained by subtracting the feed reading that
    opened the day from the one that closed it. That distinction is the whole point of the
    section heading: the feed's own counters never reset, so an operator reading a raw one
    would see Monday's reconnects again on Friday.

    Kept separate from the trading figures and from the wider health table because it
    answers its own question: was the data this day traded on complete? A day can be
    profitable and green on every process check while the feed dropped a quarter of what
    the venue sent.

    When no feed reading reached the report the section says so rather than printing
    zeros. Zeros would read as a clean stream, and the whole point of measuring is to stop
    an unmeasured day looking like a healthy one.
    """
    statistics = report.statistics
    lines = ["## Feed health — daily", ""]
    if not statistics.feed_metrics_available:
        lines += [
            "No feed metrics were supplied for this day, so stream health is unknown. "
            "The counters below are not zero — they are unmeasured.",
            "",
        ]
    rows = (
        ("Reconnects", str(statistics.daily_reconnects)),
        ("Heartbeat failures", str(statistics.daily_heartbeat_failures)),
        ("Detected gaps", str(statistics.daily_gaps)),
        ("Rejected frames", str(statistics.daily_rejected_frames)),
        ("Malformed frames", str(statistics.daily_malformed_frames)),
        ("Candles received", str(statistics.daily_candles_received)),
        ("Candles accepted", str(statistics.daily_candles_accepted)),
        ("Candles rejected", str(statistics.daily_candles_rejected)),
        ("Duplicate candles", str(statistics.daily_duplicate_candles)),
        ("Feed acceptance (daily)", _optional_percent(statistics.daily_feed_acceptance_rate)),
        ("Overall feed status", _feed_status(report)),
    )
    lines += ["| Metric | Value |", "| --- | --- |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    lines.append("")
    return lines


def _feed_status(report: DailyReport) -> str:
    """Return the one-word verdict on the data stream."""
    if not report.statistics.feed_metrics_available:
        return "unmeasured"
    return report.health.feed_level.value


def _health_section(report: DailyReport) -> list[str]:
    """Render every health check as a table."""
    lines = ["## Health checks", "", "| Check | Level | Detail |", "| --- | --- | --- |"]
    for check in report.health.checks:
        level = "skipped" if check.skipped else check.level.value
        lines.append(f"| {check.name.value} | {level} | {check.message} |")
    lines.append("")
    return lines


def _alert_section(report: DailyReport) -> list[str]:
    """Render the alert table, or say plainly that nothing fired."""
    if report.alerts.count == 0:
        return ["## Alerts", "", "None.", ""]
    lines = ["## Alerts", "", "| Alert | Severity | Detail |", "| --- | --- | --- |"]
    lines += [
        f"| {alert.code.value} | {alert.severity.value} | {alert.message} |"
        for alert in report.alerts.alerts
    ]
    lines.append("")
    return lines


def _comparison_section(report: DailyReport) -> list[str]:
    """Render the day-on-day comparison, if there was a previous day."""
    comparison = report.comparison
    if comparison is None:
        return ["## Compared with the previous day", "", "No previous report to compare.", ""]
    lines = _markdown_table(
        "## Compared with the previous day",
        (
            ("Previous day", comparison.previous_day.isoformat()),
            ("PnL delta", _money(comparison.pnl_delta)),
            ("Win-rate delta", _optional_percent(comparison.win_rate_delta)),
            ("Drawdown delta", _optional_percent(comparison.drawdown_delta)),
            ("Sharpe delta", _optional(comparison.sharpe_delta)),
            ("Trade-count delta", str(comparison.trade_count_delta)),
            ("Acceptance-rate delta", _optional_percent(comparison.acceptance_rate_delta)),
            ("Health delta", str(comparison.health_delta)),
        ),
    )
    if comparison.deteriorations:
        lines += ["### Deterioration", ""]
        lines += [f"- {item}" for item in comparison.deteriorations]
        lines.append("")
    return lines


def _recommendation_section(report: DailyReport) -> list[str]:
    """Render the operational recommendations and anomalies."""
    lines = ["## Operational notes", ""]
    lines += [f"- {item}" for item in report.summary.recommendations]
    lines.append("")
    if report.summary.anomalies:
        lines += ["### Anomalies", ""]
        lines += [f"- {item}" for item in report.summary.anomalies]
        lines.append("")
    return lines


def render_csv(report: DailyReport) -> str:
    """Render a day's statistics as a one-row CSV with a header.

    Shaped so a month of days concatenates into a single table without reshaping.

    Args:
        report: The finished report.

    Returns:
        Two lines of CSV, newline-terminated.
    """
    fields: dict[str, str] = {
        "day": report.day.isoformat(),
        "session_id": report.session_id,
        "strategy_id": report.strategy_id,
        "timezone": report.timezone,
        "health": report.health.level.value,
        "feed_status": _feed_status(report),
        "alerts": str(report.alerts.count),
    }
    for name, value in report.statistics.model_dump().items():
        fields[name] = "" if value is None else str(value)
    header = ",".join(_csv_escape(name) for name in fields)
    row = ",".join(_csv_escape(value) for value in fields.values())
    return f"{header}\n{row}\n"


def _csv_escape(value: str) -> str:
    """Quote a CSV field when it carries a delimiter, quote or newline."""
    if any(character in value for character in (",", '"', "\n", "\r")):
        escaped = value.replace('"', '""')
        return f'"{escaped}"'
    return value


def _display(value: Decimal, places: int) -> str:
    """Round a decimal for human display, without scientific notation.

    Markdown is the only rendering that rounds. JSON keeps full precision because it is
    what a later comparison reads back, and CSV keeps it because it is machine-readable —
    a report that lost digits on the way to disk would compare against a rounded yesterday.
    A Sharpe ratio printed to thirty-four places is not more truthful, only less readable.
    """
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        quantum = Decimal(1).scaleb(-places)
        return f"{value.quantize(quantum, rounding=ROUND_HALF_EVEN):f}"


def _money(value: Decimal) -> str:
    """Render a quote-asset amount to two decimal places."""
    return _display(value, _MONEY_PLACES)


def _optional(value: Decimal | None) -> str:
    """Render a metric that may not have been computable."""
    return "not computable" if value is None else _display(value, _RATIO_PLACES)


def _percent(value: Decimal) -> str:
    """Render a unit-interval ratio as a percentage."""
    return f"{value * Decimal(100):.2f}%"


def _optional_percent(value: Decimal | None) -> str:
    """Render an optional ratio as a percentage."""
    return "not computable" if value is None else _percent(value)
