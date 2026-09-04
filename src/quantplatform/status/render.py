"""Turning a gathered status into something a person can read at a glance.

Plain text and ANSI, no rendering library. The layout is fixed-width columns and a single
box, which every terminal draws the same way and which stays readable when colour is off —
piped to a file, read by someone colour-blind, or captured in an incident report.

**Colour is emphasis, never information.** Every state this prints is spelled out in words
as well, so nothing is lost when the escape codes are stripped. Green agrees with the word
HEALTHY; it does not replace it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from decimal import Decimal

from quantplatform.status.model import Health, SessionStatus

__all__ = ["render_status", "supports_colour"]

_WIDTH = 48
_LABEL = 21

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

_HEALTH_COLOUR = {
    Health.HEALTHY: _GREEN,
    Health.DEGRADED: _YELLOW,
    Health.FAILED: _RED,
    Health.STOPPED: _YELLOW,
}


def supports_colour(stream: object | None = None) -> bool:
    """Return whether escape codes should be emitted.

    Decided from the destination alone: a status piped into a log file or an incident report
    should not arrive full of escape sequences.

    The ``NO_COLOR`` convention is deliberately *not* read here. Environment access is
    confined to the configuration layer by an architectural invariant, and reading one
    variable directly from a rendering module is how that confinement stops being true.
    ``--no-color`` is the supported switch, and it works everywhere this does.
    """
    target = stream if stream is not None else sys.stdout
    isatty = getattr(target, "isatty", None)
    return bool(isatty and isatty())


class _Paint:
    """Applies colour, or does not, depending on where the output is going."""

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    def __call__(self, text: str, colour: str) -> str:
        """Return ``text`` wrapped in ``colour`` when colour is enabled."""
        return f"{colour}{text}{_RESET}" if self._enabled else text

    def width(self, text: str) -> int:
        """Return the printable width of already-painted text."""
        return len(_strip(text)) if self._enabled else len(text)


def _strip(text: str) -> str:
    """Remove ANSI escape sequences, for width arithmetic."""
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\033":
            while index < len(text) and text[index] != "m":
                index += 1
            index += 1
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def render_status(
    status: SessionStatus, *, colour: bool | None = None, smoke_hours: float | None = None
) -> str:
    """Render a full status display.

    Args:
        status: What was gathered.
        colour: Force colour on or off; decided from the terminal when ``None``.
        smoke_hours: Length of the run being tracked, when one was declared. Without it the
            elapsed time is still shown and the remaining time is ``N/A`` — a progress bar
            against a target nobody stated would be an invented number.

    Returns:
        The rendered text, without a trailing newline.
    """
    paint = _Paint(enabled=supports_colour() if colour is None else colour)
    lines: list[str] = []
    lines.extend(_banner(status, paint))
    lines.append("")
    lines.extend(_market(status, paint))
    lines.append("")
    lines.extend(_portfolio(status))
    lines.append("")
    lines.extend(_position(status, paint))
    lines.append("")
    lines.extend(_risk(status, paint))
    lines.append("")
    lines.extend(_activity(status))
    lines.append("")
    lines.extend(_infrastructure(status, paint))
    lines.append("")
    lines.extend(_run(status, smoke_hours))
    if status.notes:
        lines.append("")
        lines.extend(_notes(status, paint))
    return "\n".join(lines)


def _banner(status: SessionStatus, paint: _Paint) -> list[str]:
    """The four facts an operator checks first, boxed."""
    title = " QUANT PLATFORM "
    fill = _WIDTH - len(title)
    left = fill // 2
    rows = [
        ("SYSTEM", paint(status.health, _HEALTH_COLOUR.get(status.health, _YELLOW))),
        ("MODE", (status.execution_mode or "unknown").upper()),
        ("STRATEGY", _strategy_label(status)),
        (
            "RISK",
            paint("V2 ACTIVE", _GREEN)
            if status.risk_v2_active
            else paint("V1 (no stops)", _YELLOW),
        ),
    ]
    out = ["╭" + "─" * left + title + "─" * (fill - left) + "╮"]
    for label, value in rows:
        pad = _WIDTH - _LABEL - 1 - paint.width(value)
        out.append("│ " + label.ljust(_LABEL - 1) + value + " " * max(pad, 0) + " │")
    out.append("╰" + "─" * _WIDTH + "╯")
    return out


def _strategy_label(status: SessionStatus) -> str:
    """Name the strategy the way a person would say it out loud."""
    if status.strategy_id is None:
        return "not configured"
    if not status.strategy_parameters:
        return status.strategy_id
    values = "/".join(str(value) for _, value in sorted(status.strategy_parameters.items()))
    return f"{status.strategy_id} {values}"


def _market(status: SessionStatus, paint: _Paint) -> list[str]:
    """Where the data is coming from and how much of it has arrived."""
    out = [_heading("MARKET")]
    out.append(f"{' · '.join(status.symbols)} · {status.timeframe}")
    out.append(_row("Last closed bar", _bar_time(status)))
    out.append(_row("Bars processed", _int(status.bars_processed)))
    out.append(_row("Warm-up", _warmup(status, paint)))
    return out


def _bar_time(status: SessionStatus) -> str:
    """Say when the last bar closed, always in UTC and always saying so."""
    if status.last_bar is None:
        return "N/A (none processed yet)"
    return _stamp(status.last_bar.close_time)


def _warmup(status: SessionStatus, paint: _Paint) -> str:
    """Report progress towards the strategy being allowed to have an opinion."""
    if status.required_history is None:
        return "N/A"
    seen = status.bars_processed
    if seen is None:
        return f"0 / {status.required_history}"
    if seen >= status.required_history:
        return paint(f"COMPLETE ({seen} / {status.required_history})", _GREEN)
    return paint(f"{seen} / {status.required_history}", _YELLOW)


def _portfolio(status: SessionStatus) -> list[str]:
    """The money, separated into what is banked and what is only on paper."""
    unit = status.quote_asset
    return [
        _heading("PORTFOLIO"),
        _row("Starting capital", _money(status.starting_capital, unit)),
        _row("Cash", _money(status.cash, unit)),
        _row("Equity", _money(status.equity, unit)),
        _row("Realised P&L", _money(status.realized_pnl, unit)),
        _row("Unrealised P&L", _money(status.unrealized_pnl, unit)),
        _row("Fees paid", _money(status.total_fees, unit)),
    ]


def _position(status: SessionStatus, paint: _Paint) -> list[str]:
    """What is actually held, and what is protecting it."""
    out = [_heading("POSITION")]
    if not status.open_positions:
        out.append("No open position")
        return out
    risk_by_symbol = {risk.symbol: risk for risk in status.position_risk}
    for held in status.open_positions:
        out.append(_row("Symbol", held.symbol))
        out.append(_row("Quantity", _number(held.quantity)))
        out.append(_row("Entry price", _money(held.avg_entry_price, status.quote_asset)))
        out.append(_row("Marked at", _money(status.marked_at, status.quote_asset)))
        risk = risk_by_symbol.get(held.symbol)
        if risk is None:
            out.append(_row("Stop", paint("NONE RECORDED", _RED)))
        else:
            out.append(_row("Stop", _money(risk.stop.trigger_price, status.quote_asset)))
            out.append(
                _row("Risked at entry", _money(risk.initial_risk_amount, status.quote_asset))
            )
    return out


def _risk(status: SessionStatus, paint: _Paint) -> list[str]:
    """The limits in force, and whether any of them has fired."""
    out = [_heading("RISK")]
    out.append(_row("Risk per trade", _percent(status.risk_per_trade_pct)))
    out.append(_row("Stop required", "YES" if status.stop_required else "NO"))
    if not status.breakers:
        out.append(_row("Circuit breakers", paint("none tripped", _GREEN)))
        return out
    for breaker in status.breakers:
        reason = breaker.reason.value if breaker.reason is not None else "unknown"
        when = _stamp(breaker.tripped_at) if breaker.tripped_at else "unknown time"
        out.append(_row("TRIPPED", paint(f"{reason} at {when}", _RED)))
    return out


def _activity(status: SessionStatus) -> list[str]:
    """What the session decided today, as its own daily report recorded it."""
    out = [_heading("ACTIVITY (today's report)")]
    report = status.report
    if report is None:
        out.append("N/A — no daily report written yet")
        out.append(_DIM_NOTE)
        return out
    stats = report.statistics
    out.append(_row("Report for", report.day.isoformat()))
    out.append(_row("Orders approved", _int(stats.approved_orders)))
    out.append(_row("Orders resized", _int(stats.resized_orders)))
    out.append(_row("Rejected by risk", _int(stats.risk_rejections)))
    out.append(_row("Rejected by broker", _int(stats.broker_rejections)))
    out.append(_row("Round trips closed", _int(stats.trade_count)))
    out.append(_DIM_NOTE)
    return out


_DIM_NOTE = "  (strategy signal counts are not persisted anywhere — see docs)"


def _infrastructure(status: SessionStatus, paint: _Paint) -> list[str]:
    """Whether the plumbing is holding up."""
    out = [_heading("INFRASTRUCTURE")]
    out.append(_row("Session process", _process(status, paint)))
    out.append(_row("State persistence", _persistence(status, paint)))
    out.append(_row("Restarts", _int(status.restarts)))
    report = status.report
    if report is None:
        out.append(_row("Reconnects (today)", "N/A"))
        out.append(_row("Data gaps (today)", "N/A"))
        out.append(_row("Runtime errors (today)", "N/A"))
        return out
    # Per-day figures, and labelled as such. The feed's own counters are cumulative for the
    # life of the session; the report subtracts the day's opening reading precisely so that
    # yesterday's reconnects do not haunt today, and flattening that back out here would
    # undo the distinction.
    stats = report.statistics
    out.append(_row("Reconnects (today)", _int(stats.daily_reconnects)))
    out.append(_row("Data gaps (today)", _flagged(stats.daily_gaps, paint)))
    out.append(_row("Runtime errors (today)", _flagged(stats.runtime_exceptions, paint)))
    return out


def _process(status: SessionStatus, paint: _Paint) -> str:
    """Say whether something is actually running, and as what."""
    if status.lock is None:
        return paint("not running (no lock held)", _YELLOW)
    if not status.lock.is_alive:
        return paint(f"DEAD — lock held by absent pid {status.lock.pid}", _RED)
    return paint(f"running (pid {status.lock.pid})", _GREEN)


def _persistence(status: SessionStatus, paint: _Paint) -> str:
    """Say whether the session has managed to write itself down."""
    if not status.state_present:
        return paint("no snapshot yet", _YELLOW)
    return paint(f"OK (saved {_stamp(status.saved_at)})", _GREEN)


def _flagged(count: int, paint: _Paint) -> str:
    """Render a counter that should be zero, in amber when it is not."""
    return str(count) if count == 0 else paint(str(count), _YELLOW)


def _run(status: SessionStatus, smoke_hours: float | None) -> list[str]:
    """How long this has been going, and how much is left when a target was declared."""
    out = [_heading("RUN")]
    out.append(_row("Session", status.session_id or "N/A"))
    out.append(_row("Started", _stamp(status.started_at)))
    elapsed = status.elapsed
    out.append(_row("Elapsed", _duration(elapsed)))
    if smoke_hours is None or elapsed is None:
        out.append(_row("Remaining", "N/A (no target declared)"))
        out.append(_row("Progress", "N/A"))
        return out
    target = timedelta(hours=smoke_hours)
    remaining = target - elapsed
    fraction = min(elapsed / target, 1.0)
    out.append(_row("Target", _duration(target)))
    out.append(_row("Remaining", _duration(remaining) if remaining.total_seconds() > 0 else "0m"))
    out.append(_row("Progress", f"{fraction * 100:.0f}%  {_bar(fraction)}"))
    return out


def _bar(fraction: float) -> str:
    """A twenty-cell progress bar, drawn with characters every terminal has."""
    filled = round(fraction * 20)
    return "[" + "#" * filled + "-" * (20 - filled) + "]"


def _notes(status: SessionStatus, paint: _Paint) -> list[str]:
    """Everything the gatherer could not fold into a number."""
    out = [_heading("NOTES")]
    out.extend(paint(f"• {note}", _YELLOW) for note in status.notes)
    return out


# --- formatting primitives ---------------------------------------------------------------


def _heading(text: str) -> str:
    """Return a section heading."""
    return text


def _row(label: str, value: str) -> str:
    """Return one aligned label/value line."""
    return f"{label:<{_LABEL}}{value}"


def _money(amount: Decimal | None, unit: str) -> str:
    """Render an amount with its currency, or say it is unknown."""
    if amount is None:
        return "N/A"
    return f"{amount:,.2f} {unit}"


def _number(amount: Decimal | None) -> str:
    """Render a quantity at full precision, trailing zeros trimmed."""
    if amount is None:
        return "N/A"
    return format(amount.normalize(), "f")


def _percent(fraction: Decimal | None) -> str:
    """Render a fraction as a percentage."""
    if fraction is None:
        return "N/A"
    return f"{fraction * 100:.2f}%"


def _int(value: int | None) -> str:
    """Render a count, distinguishing zero from unknown."""
    return "N/A" if value is None else str(value)


def _stamp(moment: datetime | None) -> str:
    """Render an instant, always in UTC and always saying so."""
    if moment is None:
        return "N/A"
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _duration(span: timedelta | None) -> str:
    """Render a duration in whole hours and minutes."""
    if span is None:
        return "N/A"
    total = int(span.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours}h {minutes:02d}m"
