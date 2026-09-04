"""The read-only HTTP surface Mission Control is built on.

**Every route is a GET.** There is no ``post``, ``put``, ``patch`` or ``delete`` anywhere in
this module, and a test asserts that by inspecting the built application's route table rather
than by trusting this sentence. The service is an observer: it has no code path that could
start a session, place an order, move a stop or clear a breaker, because the ``web`` domain
cannot import ``execution``, ``risk``, ``portfolio``, ``paper`` or ``backtesting``.

One endpoint carries the whole picture. The page is a single screen, so a dozen routes would
mean a dozen round trips to render it and a dozen chances for the parts to disagree about
which instant they describe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from quantplatform.config.settings import load_settings
from quantplatform.core.models.risk import CircuitBreakerState
from quantplatform.status import gather_status
from quantplatform.status.events import ActivityCounts, read_activity, read_timeline
from quantplatform.status.model import SessionStatus
from quantplatform.strategies.registry import build_default_registry
from quantplatform.web.config import SCHEMA_VERSION, WebSettings
from quantplatform.web.static import STATIC_ROOT

__all__ = ["create_app"]


def create_app(settings: WebSettings | None = None) -> FastAPI:
    """Build the Mission Control application.

    Args:
        settings: Where to read from and what to bind. Environment-driven when omitted.

    Returns:
        An application exposing two GET endpoints and the static page.
    """
    resolved = settings or WebSettings()
    app = FastAPI(
        title="Quant Platform — Mission Control",
        description="Read-only observer of a paper trading session. No mutation endpoints.",
        version=SCHEMA_VERSION,
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        """Return everything the page renders, as one coherent reading."""
        return _payload(resolved)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Return liveness of this dashboard, which says nothing about the session."""
        return {"status": "ok", "schema_version": SCHEMA_VERSION}

    @app.get("/")
    def index() -> FileResponse:
        """Serve the single page."""
        return FileResponse(STATIC_ROOT / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
    return app


def _payload(settings: WebSettings) -> dict[str, Any]:
    """Gather one reading and shape it for the browser.

    Serialisation only. Every judgement — what is healthy, what is unknown, what a warm-up
    needs — was already made by the status domain, and making any of it again here would be
    a second opinion that could drift from the command-line one.
    """
    platform = load_settings(
        paper={
            "state_directory": str(settings.state_directory),
            "reports_directory": str(settings.reports_directory),
            "log_directory": str(settings.log_directory),
            **({"session_id": settings.session_id} if settings.session_id else {}),
        }
    )
    gathered = gather_status(
        platform, registry=build_default_registry(), session_id=settings.session_id
    )
    activity = read_activity(settings.log_directory, session_id=gathered.session_id)
    timeline = read_timeline(settings.log_directory, session_id=gathered.session_id)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "refresh_seconds": settings.refresh_seconds,
        "system": _system(gathered),
        "smoke": _smoke(gathered, settings.smoke_hours),
        "portfolio": _portfolio(gathered),
        "position": _position(gathered),
        "market": _market(gathered),
        "risk": _risk(gathered),
        "activity": _activity(activity),
        "infrastructure": _infrastructure(gathered),
        "timeline": [
            {
                "at": event.at.isoformat(),
                "kind": event.kind,
                "title": event.title,
                "detail": event.detail,
                "severity": event.severity,
                "log_derived": event.log_derived,
            }
            for event in timeline
        ],
        "details": _details(gathered),
        "notes": list(gathered.notes),
    }


def _num(value: Decimal | None) -> str | None:
    """Render a decimal as a string, preserving every digit it carries.

    Strings rather than floats: a balance that survives a round trip through binary floating
    point is a balance that can print as 9999.999999999998, and a money figure that is only
    nearly right is worse than one that is obviously text.
    """
    return None if value is None else str(value)


def _system(status: SessionStatus) -> dict[str, Any]:
    """The banner: one word, plus the badges that qualify it."""
    return {
        "health": status.health,
        "running": status.running,
        "mode": (status.execution_mode or "unknown").upper(),
        "strategy_id": status.strategy_id,
        "strategy_label": _strategy_label(status),
        "risk_label": "RISK V2" if status.risk_v2_active else "RISK V1",
        "symbols": list(status.symbols),
        "timeframe": status.timeframe,
    }


def _strategy_label(status: SessionStatus) -> str:
    """Name the strategy the way a person says it: breakout 20/10."""
    if status.strategy_id is None:
        return "NOT CONFIGURED"
    if not status.strategy_parameters:
        return status.strategy_id.upper()
    values = "/".join(str(value) for _, value in sorted(status.strategy_parameters.items()))
    return f"{status.strategy_id.upper()} {values}"


def _smoke(status: SessionStatus, smoke_hours: float | None) -> dict[str, Any]:
    """Progress against a declared run length, or an honest absence of one."""
    elapsed = status.elapsed
    payload: dict[str, Any] = {
        "session_id": status.session_id,
        "started_at": status.started_at.isoformat() if status.started_at else None,
        "elapsed_seconds": int(elapsed.total_seconds()) if elapsed else None,
        "target_hours": smoke_hours,
        "remaining_seconds": None,
        "target_end": None,
        "progress": None,
    }
    if smoke_hours is None or elapsed is None or status.started_at is None:
        return payload
    target_seconds = smoke_hours * 3600
    payload["remaining_seconds"] = max(int(target_seconds - elapsed.total_seconds()), 0)
    payload["target_end"] = (status.started_at + timedelta(seconds=target_seconds)).isoformat()
    payload["progress"] = min(elapsed.total_seconds() / target_seconds, 1.0)
    return payload


def _portfolio(status: SessionStatus) -> dict[str, Any]:
    """Money, with the mark price it depends on stated alongside it."""
    starting = status.starting_capital
    equity = status.equity
    change = None
    if starting is not None and equity is not None and starting != 0:
        change = float((equity - starting) / starting)
    return {
        "quote_asset": status.quote_asset,
        "starting_capital": _num(starting),
        "cash": _num(status.cash),
        "equity": _num(equity),
        "equity_change": change,
        "realized_pnl": _num(status.realized_pnl),
        "unrealized_pnl": _num(status.unrealized_pnl),
        "fees": _num(status.total_fees),
        "marked_at": _num(status.marked_at),
    }


def _position(status: SessionStatus) -> dict[str, Any]:
    """The open position and, above all, whether it is protected."""
    if not status.open_positions:
        return {"open": False, "message": "No open position"}
    held = status.open_positions[0]
    risk = next((r for r in status.position_risk if r.symbol == held.symbol), None)
    mark = status.marked_at
    stop = risk.stop.trigger_price if risk is not None else None
    distance = None
    if stop is not None and mark is not None and mark != 0:
        distance = float((mark - stop) / mark)
    return {
        "open": True,
        "side": "LONG",
        "symbol": held.symbol,
        "quantity": _num(held.quantity),
        "entry": _num(held.avg_entry_price),
        "current": _num(mark),
        "stop": _num(stop),
        "stop_kind": risk.stop.kind.value if risk is not None else None,
        "distance_to_stop": distance,
        "risked_at_entry": _num(risk.initial_risk_amount) if risk is not None else None,
        "unrealized_pnl": _num(status.unrealized_pnl),
        "unprotected": risk is None,
    }


def _market(status: SessionStatus) -> dict[str, Any]:
    """The instrument, the last candle, and how far the warm-up has got."""
    required = status.required_history
    seen = status.bars_processed
    return {
        "symbol": status.symbols[0] if status.symbols else None,
        "timeframe": status.timeframe,
        "last_close_time": status.last_bar.close_time.isoformat() if status.last_bar else None,
        "last_close": _num(status.last_bar.close) if status.last_bar else None,
        "bars_processed": seen,
        "warmup_required": required,
        "warmup_complete": status.warmup_complete,
        "warmup_progress": _warmup_progress(seen, required),
    }


def _warmup_progress(seen: int | None, required: int | None) -> float | None:
    """Return how far warm-up has got, or nothing when either half is unknown."""
    if seen is None or required is None or required == 0:
        return None
    return min(seen / required, 1.0)


def _risk(status: SessionStatus) -> dict[str, Any]:
    """The limits in force, and each breaker's own answer.

    All three breakers are listed whether or not they have fired. A page that only shows
    what tripped cannot be used to confirm that nothing has.
    """
    tripped = {
        breaker.reason.value: breaker for breaker in status.breakers if breaker.reason is not None
    }
    named = (
        ("daily_loss_limit", "Daily Loss"),
        ("excessive_drawdown", "Total Drawdown"),
        ("consecutive_losses", "Loss Streak"),
    )
    return {
        "v2_active": status.risk_v2_active,
        "risk_per_trade": _num(status.risk_per_trade_pct),
        "stop_required": status.stop_required,
        "breakers": [
            {
                "key": key,
                "label": label,
                "tripped": key in tripped,
                "at": _tripped_at(tripped.get(key)),
            }
            for key, label in named
        ],
        "other_tripped": [
            {"key": key, "at": _tripped_at(breaker)}
            for key, breaker in tripped.items()
            if key not in {key for key, _ in named}
        ],
    }


def _tripped_at(breaker: CircuitBreakerState | None) -> str | None:
    """Return when a breaker fired, or nothing when it has not."""
    if breaker is None or breaker.tripped_at is None:
        return None
    return breaker.tripped_at.isoformat()


def _activity(activity: ActivityCounts) -> dict[str, Any]:
    """What has been decided, flagged as coming from logs rather than from state."""
    return {
        "signals": activity.signals,
        "approved": activity.decisions,
        "rejected": None
        if activity.intents is None
        else max(activity.intents - (activity.decisions or 0), 0),
        "fills": activity.fills,
        "bars_seen": activity.bars_seen,
        "log_derived": True,
    }


def _infrastructure(status: SessionStatus) -> dict[str, Any]:
    """The plumbing: what is running, and whether it has written itself down."""
    report = status.report
    lock = status.lock
    return {
        "service_running": status.running,
        "pid": lock.pid if lock else None,
        "pid_alive": lock.is_alive if lock else None,
        "restarts": status.restarts,
        "persistence_ok": status.state_present,
        "latest_snapshot": status.saved_at.isoformat() if status.saved_at else None,
        "reconnects": report.statistics.daily_reconnects if report else None,
        "data_gaps": report.statistics.daily_gaps if report else None,
        "runtime_errors": report.statistics.runtime_exceptions if report else None,
        "daily_report_day": report.day.isoformat() if report else None,
    }


def _details(status: SessionStatus) -> dict[str, Any]:
    """The debug drawer: identifiers, and where each number came from."""
    return {
        "session_id": status.session_id,
        "strategy_id": status.strategy_id,
        "strategy_parameters": {k: str(v) for k, v in status.strategy_parameters.items()},
        "execution_mode": status.execution_mode,
        "started_at": status.started_at.isoformat() if status.started_at else None,
        "snapshot_saved_at": status.saved_at.isoformat() if status.saved_at else None,
        "quote_asset": status.quote_asset,
        "sources": {
            "portfolio": "structured — persisted session snapshot",
            "position": "structured — persisted session snapshot",
            "risk_breakers": "structured — persisted session snapshot",
            "market": "structured — persisted session snapshot",
            "infrastructure": "structured — session lock, snapshot and daily report",
            "activity": "log-derived — paper.log, one record per processed bar",
            "timeline": "log-derived — orchestration.log, marketdata.log, paper.log",
        },
    }
