"""Mission Control shows a session without being able to touch it.

Control Center v1 could not read a schema-2 snapshot at all — its own copy of
``PaperSessionState`` predated ``schema_version``, ``position_risk`` and ``breakers``, and
``extra="forbid"`` turned every one of those into a hard refusal. So the first thing tested
here is that this one reads the shape the platform actually writes, and the rest is about the
two properties that make an unauthenticated page safe to leave running: it cannot change
anything, and it never claims to know something it does not.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantplatform.core.enums import CircuitBreakerReason, ExecutionMode, StopKind
from quantplatform.core.errors import ConfigurationError
from quantplatform.core.models.paper import CURRENT_SCHEMA_VERSION, PaperSessionState
from quantplatform.core.models.portfolio import Balance, Position
from quantplatform.core.models.risk import CircuitBreakerState, PositionRiskState
from quantplatform.core.models.stops import StopSpecification
from quantplatform.storage.paper_state import FilePaperStateRepository
from quantplatform.storage.session_lock import SessionLock
from quantplatform.web import WebSettings, create_app
from quantplatform.web import api as web_api
from tests.factories import SYMBOL, make_bar

SESSION = "web-session"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    made = {
        "state": tmp_path / "state",
        "reports": tmp_path / "reports",
        "logs": tmp_path / "logs",
    }
    for path in made.values():
        path.mkdir(parents=True, exist_ok=True)
    return made


def _settings(tree: dict[str, Path], **overrides: object) -> WebSettings:
    return WebSettings(
        state_directory=tree["state"],
        reports_directory=tree["reports"],
        log_directory=tree["logs"],
        session_id=SESSION,
        **overrides,  # type: ignore[arg-type]
    )


def _client(tree: dict[str, Path], **overrides: object) -> TestClient:
    return TestClient(create_app(_settings(tree, **overrides)))


def _state(**overrides: object) -> PaperSessionState:
    started = datetime.now(UTC) - timedelta(hours=6)
    base: dict[str, object] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "session_id": SESSION,
        "strategy_id": "breakout",
        "execution_mode": ExecutionMode.PAPER,
        "quote_asset": "USDT",
        "started_at": started,
        "saved_at": started + timedelta(hours=1),
        "balances": (
            Balance(
                asset="USDT",
                free=Decimal("10000"),
                locked=Decimal(0),
                updated_at=started + timedelta(hours=1),
            ),
        ),
        "bars_processed": 5,
        "last_bar": make_bar(close=Decimal("60000")),
        "realized_pnl": Decimal(0),
        "total_fees": Decimal(0),
    }
    base.update(overrides)
    return PaperSessionState(**base)  # type: ignore[arg-type]


def _persist(tree: dict[str, Path], state: PaperSessionState) -> None:
    FilePaperStateRepository(tree["state"]).save(state)


def _log(tree: dict[str, Path], name: str, records: list[dict[str, object]]) -> None:
    (tree["logs"] / name).write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )


# --- the failure that killed Control Center v1 -------------------------------------------------


def test_a_schema_two_snapshot_is_read_rather_than_refused(tree: dict[str, Path]) -> None:
    # Exactly what v1 could not do. position_risk and breakers are the fields its models did
    # not have, so a snapshot carrying them was rejected wholesale by extra="forbid".
    opened = datetime.now(UTC) - timedelta(hours=2)
    position = Position(
        symbol=SYMBOL,
        base_asset="BTC",
        quote_asset="USDT",
        quantity=Decimal("0.1"),
        avg_entry_price=Decimal("59000"),
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=opened,
        updated_at=opened,
    )
    risk = PositionRiskState(
        symbol=SYMBOL,
        stop=StopSpecification(kind=StopKind.TRAILING, trigger_price=Decimal("57230")),
        quantity=Decimal("0.1"),
        initial_risk_amount=Decimal("177"),
        current_risk_amount=Decimal("177"),
        entry_price=Decimal("59000"),
        opened_at=opened,
    )
    _persist(tree, _state(positions=(position,), position_risk=(risk,)))

    payload = _client(tree).get("/api/status").json()

    assert payload["position"]["open"] is True
    assert payload["position"]["stop"] == "57230"
    assert payload["position"]["stop_kind"] == "trailing"
    assert payload["details"]["session_id"] == SESSION


# --- read-only ---------------------------------------------------------------------------------


def test_the_application_exposes_no_mutating_route(tree: dict[str, Path]) -> None:
    # Asserted against the built route table, not against a promise in a docstring.
    app = create_app(_settings(tree))

    for route in app.routes:
        methods: set[str] = getattr(route, "methods", set()) or set()
        assert not (methods & {"POST", "PUT", "PATCH", "DELETE"}), getattr(route, "path", route)


def test_every_mutating_verb_is_refused_at_every_route(tree: dict[str, Path]) -> None:
    client = _client(tree)
    for path in ("/api/status", "/healthz", "/"):
        for verb in ("post", "put", "patch", "delete"):
            response = getattr(client, verb)(path)
            assert response.status_code == 405, f"{verb.upper()} {path}"


def test_serving_the_page_changes_nothing_on_disk(tree: dict[str, Path]) -> None:
    _persist(tree, _state())
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(tree["state"].rglob("*"))
        if path.is_file()
    }

    client = _client(tree)
    client.get("/api/status")
    client.get("/api/status")

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(tree["state"].rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_the_web_domain_cannot_import_anything_that_trades() -> None:
    source = Path(web_api.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "quantplatform.execution",
        "quantplatform.risk",
        "quantplatform.paper.",
        "quantplatform.portfolio",
        "quantplatform.backtesting",
    ):
        assert forbidden not in source


# --- secrets -----------------------------------------------------------------------------------


def test_no_secret_or_connection_string_reaches_the_browser(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QP_DATABASE__DSN", "postgresql+psycopg://quant:sup3rs3cret@db:5432/qp")
    monkeypatch.setenv("QP_EXCHANGE__API_KEY", "AKIAEXAMPLEKEY")
    monkeypatch.setenv("QP_EXCHANGE__API_SECRET", "s3cr3t-exchange-value")
    _persist(tree, _state())

    body = _client(tree).get("/api/status").text

    for secret in ("sup3rs3cret", "AKIAEXAMPLEKEY", "s3cr3t-exchange-value", "postgresql"):
        assert secret not in body


def test_the_page_itself_embeds_no_configuration(tree: dict[str, Path]) -> None:
    page = _client(tree).get("/").text

    for forbidden in ("QP_", "postgresql", "api_key", "secret"):
        assert forbidden not in page


# --- binding -----------------------------------------------------------------------------------


def test_a_wildcard_bind_is_refused_and_cannot_be_opted_into() -> None:
    # 0.0.0.0 binds the public interface too, and "I meant only the private one" is not
    # something that address can express — so there is deliberately no override for it.
    for wildcard in ("0.0.0.0", "::", "*"):  # noqa: S104 - naming them to refuse them
        with pytest.raises(ConfigurationError, match="wildcard"):
            WebSettings(host=wildcard, allow_public_bind=True)


def test_dashboard_variables_do_not_collide_with_the_platforms_namespace(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live 500 taught this: the platform's Settings reads every QP_ variable and forbids
    # extras, so a dashboard variable under that prefix does not get ignored — it stops the
    # whole platform configuration from loading, and the page fails with a message about a
    # field nobody asked it to validate.
    monkeypatch.setenv("MISSION_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("MISSION_CONTROL_PORT", "8800")
    monkeypatch.setenv("MISSION_CONTROL_SESSION_ID", SESSION)
    monkeypatch.setenv("MISSION_CONTROL_STATE_DIRECTORY", str(tree["state"]))
    monkeypatch.setenv("MISSION_CONTROL_REPORTS_DIRECTORY", str(tree["reports"]))
    monkeypatch.setenv("MISSION_CONTROL_LOG_DIRECTORY", str(tree["logs"]))
    monkeypatch.setenv("MISSION_CONTROL_SMOKE_HOURS", "72")
    _persist(tree, _state())

    # Both must construct with those variables present: the dashboard's from them, and the
    # platform's despite them.
    settings = WebSettings()
    assert settings.session_id == SESSION
    assert settings.smoke_hours == 72

    response = TestClient(create_app(settings)).get("/api/status")
    assert response.status_code == 200
    assert response.json()["smoke"]["progress"] is not None


def test_a_read_only_state_directory_is_still_readable(tree: dict[str, Path]) -> None:
    # Found in deployment: the dashboard's systemd unit mounts the trading tree with
    # ReadOnlyPaths, which is exactly the confinement wanted — and the state repository
    # refused to open at all, because its constructor demanded write access it would never
    # use. An observer must not need write permission on what it observes.
    _persist(tree, _state())
    tree["state"].chmod(0o555)
    try:
        payload = _client(tree).get("/api/status").json()

        assert payload["portfolio"]["cash"] == "10000"
        assert payload["market"]["bars_processed"] == 5
        assert not any("not writable" in note for note in payload["notes"])
    finally:
        tree["state"].chmod(0o755)


def test_no_setting_uses_the_platform_prefix() -> None:
    assert not WebSettings.model_config["env_prefix"].startswith("QP_")


def test_a_tailscale_address_binds_without_an_override() -> None:
    settings = WebSettings(host="100.65.149.67")

    assert settings.host == "100.65.149.67"


def test_loopback_binds_without_an_override() -> None:
    assert WebSettings(host="127.0.0.1").host == "127.0.0.1"


def test_a_routable_public_address_is_refused_without_an_explicit_opt_in() -> None:
    with pytest.raises(ConfigurationError, match="neither loopback nor a Tailscale"):
        WebSettings(host="203.0.113.10")


# --- unknown is not zero -----------------------------------------------------------------------


def test_missing_metrics_are_null_rather_than_zero(tree: dict[str, Path]) -> None:
    # No snapshot at all. Reporting cash as 0 would say the account is empty; it is unknown.
    payload = _client(tree).get("/api/status").json()

    assert payload["portfolio"]["cash"] is None
    assert payload["portfolio"]["equity"] is None
    assert payload["infrastructure"]["reconnects"] is None
    assert payload["activity"]["signals"] is None
    assert payload["market"]["bars_processed"] is None


def test_activity_is_zero_once_a_bar_has_been_logged_without_producing_anything(
    tree: dict[str, Path],
) -> None:
    # The other side of the same rule: a processed bar that signalled nothing is a real zero.
    _persist(tree, _state())
    _log(
        tree,
        "paper.log",
        [
            {
                "timestamp": "2026-09-04T21:00:00+00:00",
                "message": "bar processed",
                "extra": {
                    "session_id": SESSION,
                    "symbol": SYMBOL,
                    "close_time": "2026-09-04T21:00:00+00:00",
                    "signals": 0,
                    "intents": 0,
                    "decisions": 0,
                    "fills": 0,
                },
            }
        ],
    )

    activity = _client(tree).get("/api/status").json()["activity"]

    assert activity["signals"] == 0
    assert activity["fills"] == 0
    assert activity["bars_seen"] == 1
    assert activity["log_derived"] is True


def test_smoke_progress_is_absent_until_a_target_is_declared(tree: dict[str, Path]) -> None:
    _persist(tree, _state())

    without = _client(tree).get("/api/status").json()["smoke"]
    assert without["progress"] is None
    assert without["remaining_seconds"] is None

    with_target = _client(tree, smoke_hours=72).get("/api/status").json()["smoke"]
    assert 0 < with_target["progress"] < 1
    assert with_target["remaining_seconds"] > 0
    assert with_target["target_end"] is not None


# --- session states ----------------------------------------------------------------------------


def test_a_running_session_reports_healthy(tree: dict[str, Path]) -> None:
    lock = SessionLock(directory=tree["state"], session_id=SESSION)
    lock.acquire(now=datetime.now(UTC))
    try:
        _persist(tree, _state())

        payload = _client(tree).get("/api/status").json()

        assert payload["system"]["health"] == "HEALTHY"
        assert payload["infrastructure"]["service_running"] is True
    finally:
        lock.release()


def test_a_session_with_no_lock_reports_stopped(tree: dict[str, Path]) -> None:
    _persist(tree, _state())

    payload = _client(tree).get("/api/status").json()

    assert payload["system"]["health"] == "STOPPED"
    assert payload["infrastructure"]["service_running"] is False


def test_a_corrupt_snapshot_degrades_instead_of_returning_an_error(
    tree: dict[str, Path],
) -> None:
    FilePaperStateRepository(tree["state"]).path_for(SESSION).write_text("{ not json")

    response = _client(tree).get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["health"] == "DEGRADED"
    assert any("could not be read" in note for note in payload["notes"])


def test_warmup_is_reported_as_progress_towards_the_strategys_requirement(
    tree: dict[str, Path],
) -> None:
    _persist(tree, _state(bars_processed=5))
    incomplete = _client(tree).get("/api/status").json()["market"]
    assert incomplete["warmup_required"] == 21
    assert incomplete["warmup_complete"] is False
    assert 0 < incomplete["warmup_progress"] < 1

    _persist(tree, _state(bars_processed=21))
    complete = _client(tree).get("/api/status").json()["market"]
    assert complete["warmup_complete"] is True
    assert complete["warmup_progress"] == 1.0


def test_a_flat_session_says_so_in_words(tree: dict[str, Path]) -> None:
    _persist(tree, _state())

    position = _client(tree).get("/api/status").json()["position"]

    assert position["open"] is False
    assert position["message"] == "No open position"


def test_an_open_position_without_a_stop_is_flagged_unprotected(tree: dict[str, Path]) -> None:
    opened = datetime.now(UTC) - timedelta(hours=1)
    position = Position(
        symbol=SYMBOL,
        base_asset="BTC",
        quote_asset="USDT",
        quantity=Decimal("0.1"),
        avg_entry_price=Decimal("59000"),
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=opened,
        updated_at=opened,
    )
    _persist(tree, _state(positions=(position,)))

    payload = _client(tree).get("/api/status").json()["position"]

    assert payload["unprotected"] is True
    assert payload["stop"] is None


def test_every_breaker_is_listed_whether_or_not_it_fired(tree: dict[str, Path]) -> None:
    # A page that only shows what tripped cannot be used to confirm that nothing has.
    _persist(tree, _state())

    breakers = _client(tree).get("/api/status").json()["risk"]["breakers"]

    assert [b["label"] for b in breakers] == ["Daily Loss", "Total Drawdown", "Loss Streak"]
    assert all(b["tripped"] is False for b in breakers)


def test_a_latched_breaker_marks_the_system_failed(tree: dict[str, Path]) -> None:
    lock = SessionLock(directory=tree["state"], session_id=SESSION)
    lock.acquire(now=datetime.now(UTC))
    try:
        breaker = CircuitBreakerState(
            tripped_at=datetime.now(UTC),
            reason=CircuitBreakerReason.DAILY_LOSS_LIMIT,
            consecutive_losses=0,
            daily_loss=Decimal("310"),
        )
        _persist(tree, _state(breakers=(breaker,)))

        payload = _client(tree).get("/api/status").json()

        assert payload["system"]["health"] == "FAILED"
        tripped = [b for b in payload["risk"]["breakers"] if b["tripped"]]
        assert [b["key"] for b in tripped] == ["daily_loss_limit"]
    finally:
        lock.release()


# --- timeline ----------------------------------------------------------------------------------


def test_the_timeline_describes_events_rather_than_echoing_log_lines(
    tree: dict[str, Path],
) -> None:
    _persist(tree, _state())
    _log(
        tree,
        "orchestration.log",
        [
            {
                "timestamp": "2026-09-04T20:35:55+00:00",
                "message": "paper session starting",
                "extra": {"session_id": SESSION, "resume": False},
            }
        ],
    )
    _log(
        tree,
        "marketdata.log",
        [
            {
                "timestamp": "2026-09-04T20:35:56+00:00",
                "message": "feed state transition",
                "extra": {"from": "connecting", "to": "connected"},
            }
        ],
    )

    timeline = _client(tree).get("/api/status").json()["timeline"]

    titles = {event["title"] for event in timeline}
    assert "Session started" in titles
    assert "Market feed connected" in titles
    assert all(event["log_derived"] is True for event in timeline)
    # Newest first, so the most recent thing is the first thing read.
    stamps = [event["at"] for event in timeline]
    assert stamps == sorted(stamps, reverse=True)


def test_a_truncated_log_line_does_not_break_the_timeline(tree: dict[str, Path]) -> None:
    # A log being appended to while it is read can hand back half a line.
    (tree["logs"] / "paper.log").write_text(
        json.dumps(
            {
                "timestamp": "2026-09-04T21:00:00+00:00",
                "message": "bar processed",
                "extra": {"session_id": SESSION, "signals": 0, "fills": 0},
            }
        )
        + '\n{"timestamp": "2026-09-04T22:00:00+00:00", "mess',
        encoding="utf-8",
    )
    _persist(tree, _state())

    response = _client(tree).get("/api/status")

    assert response.status_code == 200
    assert response.json()["activity"]["bars_seen"] == 1


def test_another_sessions_logs_are_not_counted_as_this_ones(tree: dict[str, Path]) -> None:
    # Logs outlive a session. Without filtering, a fresh run would inherit the last one's
    # totals and report activity it never had.
    _persist(tree, _state())
    _log(
        tree,
        "paper.log",
        [
            {
                "timestamp": "2026-09-04T19:00:00+00:00",
                "message": "bar processed",
                "extra": {"session_id": "an-older-session", "signals": 9, "fills": 4},
            },
            {
                "timestamp": "2026-09-04T21:00:00+00:00",
                "message": "bar processed",
                "extra": {"session_id": SESSION, "signals": 0, "fills": 0},
            },
        ],
    )

    activity = _client(tree).get("/api/status").json()["activity"]

    assert activity["signals"] == 0
    assert activity["fills"] == 0
    assert activity["bars_seen"] == 1


# --- timestamps and shape ----------------------------------------------------------------------


def test_every_timestamp_is_utc(tree: dict[str, Path]) -> None:
    _persist(tree, _state())

    payload = _client(tree).get("/api/status").json()

    for value in (
        payload["generated_at"],
        payload["smoke"]["started_at"],
        payload["infrastructure"]["latest_snapshot"],
    ):
        assert value is not None
        assert value.endswith("+00:00") or value.endswith("Z")


def test_money_is_serialised_as_text_to_survive_the_round_trip(tree: dict[str, Path]) -> None:
    # A float would let 10000 come back as 9999.999999999998.
    _persist(tree, _state())

    portfolio = _client(tree).get("/api/status").json()["portfolio"]

    assert isinstance(portfolio["cash"], str)
    assert portfolio["cash"] == "10000"


def test_the_page_and_its_assets_are_served(tree: dict[str, Path]) -> None:
    client = _client(tree)

    page = client.get("/")
    assert page.status_code == 200
    assert "Quant" in page.text

    for asset in ("/static/styles.css", "/static/app.js"):
        assert client.get(asset).status_code == 200


def test_the_refresh_interval_is_served_so_the_client_need_not_guess(
    tree: dict[str, Path],
) -> None:
    payload = _client(tree, refresh_seconds=15).get("/api/status").json()

    assert payload["refresh_seconds"] == 15


def test_every_metric_group_declares_where_it_came_from(tree: dict[str, Path]) -> None:
    _persist(tree, _state())

    sources = _client(tree).get("/api/status").json()["details"]["sources"]

    assert "structured" in sources["portfolio"]
    assert "log-derived" in sources["activity"]
    assert "log-derived" in sources["timeline"]


# --- brains and the WHY contract -----------------------------------------------------


def test_the_payload_carries_all_seven_brains_in_reading_order(tree: dict[str, Path]) -> None:
    _persist(tree, _state())

    brains = _client(tree).get("/api/status").json()["brains"]

    assert [b["key"] for b in brains] == [
        "market",
        "strategy",
        "risk",
        "execution",
        "portfolio",
        "infra",
        "smoke",
    ]
    for brain in brains:
        assert brain["headline"], brain["key"]
        assert brain["explanation"], brain["key"]
        assert brain["level"] in {"good", "attention", "danger", "info"}


def test_every_kpi_ships_what_the_why_panel_needs(tree: dict[str, Path]) -> None:
    # The WHY panel renders only what the API sends. A KPI missing any of these would open
    # a panel with a blank field, which is worse than not offering the panel at all.
    _persist(tree, _state())

    for brain in _client(tree).get("/api/status").json()["brains"]:
        for kpi in brain["kpis"]:
            assert kpi["label"], f"{brain['key']}.{kpi['key']}"
            assert kpi["meaning"], f"{brain['key']}.{kpi['key']}"
            assert kpi["why"], f"{brain['key']}.{kpi['key']}"
            assert kpi["source"] in {"structured", "log-derived", "unavailable"}
            assert kpi["level"] in {"good", "attention", "danger", "info"}
            if kpi["value"] is None:
                assert kpi["source"] == "unavailable", f"{brain['key']}.{kpi['key']}"


def test_the_summary_is_present_and_declares_whether_to_intervene(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A genuinely all-green scenario: warmed up, risk budget configured, feed connected.
    # Leaving any of those out produces DEGRADED, which is the brains being honest rather
    # than a fault — so the healthy path has to be set up properly to be tested.
    monkeypatch.setenv("QP_RISK__RISK_PER_TRADE_PCT", "0.01")
    monkeypatch.setenv("QP_RISK__MAX_POSITION_EXPOSURE_PCT", "0.5")
    monkeypatch.setenv("QP_RISK__MIN_STOP_DISTANCE_BPS", "50")
    monkeypatch.setenv("QP_RISK__MAX_STOP_DISTANCE_BPS", "1000")
    monkeypatch.setenv("QP_RISK__INITIAL_STOP_DISTANCE_BPS", "300")
    lock = SessionLock(directory=tree["state"], session_id=SESSION)
    lock.acquire(now=datetime.now(UTC))
    try:
        _persist(tree, _state(bars_processed=21))
        _log(
            tree,
            "marketdata.log",
            [
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": "feed state transition",
                    "extra": {"from": "connecting", "to": "streaming"},
                }
            ],
        )

        summary = _client(tree).get("/api/status").json()["summary"]

        assert summary["headline"] == "HEALTHY"
        assert summary["intervention_required"] is False
        assert summary["text"]
        assert summary["blockers"] == []
    finally:
        lock.release()


def test_a_tripped_breaker_reaches_the_summary_as_a_blocker(tree: dict[str, Path]) -> None:
    lock = SessionLock(directory=tree["state"], session_id=SESSION)
    lock.acquire(now=datetime.now(UTC))
    try:
        breaker = CircuitBreakerState(
            tripped_at=datetime.now(UTC),
            reason=CircuitBreakerReason.DAILY_LOSS_LIMIT,
            consecutive_losses=0,
            daily_loss=Decimal("310"),
        )
        _persist(tree, _state(breakers=(breaker,)))

        summary = _client(tree).get("/api/status").json()["summary"]

        assert summary["intervention_required"] is True
        assert summary["blockers"]
    finally:
        lock.release()


def test_history_survives_the_midnight_log_rotation(tree: dict[str, Path]) -> None:
    # Logs roll over at midnight UTC: marketdata.log becomes marketdata.log.2026-09-04 and
    # a fresh file starts. Reading only the live file made the dashboard forget the feed had
    # ever connected and report the session's fifth candle as its first, one minute past
    # midnight, while the session was perfectly healthy.
    _persist(tree, _state())
    (tree["logs"] / "marketdata.log.2026-09-04").write_text(
        json.dumps(
            {
                "timestamp": "2026-09-04T20:35:56+00:00",
                "message": "feed state transition",
                "extra": {"from": "connecting", "to": "streaming"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _log(
        tree,
        "marketdata.log",
        [
            {
                "timestamp": "2026-09-05T00:00:00+00:00",
                "message": "closed bar emitted",
                "extra": {"symbol": SYMBOL, "close_time": "2026-09-05T00:00:00+00:00"},
            }
        ],
    )
    (tree["logs"] / "paper.log.2026-09-04").write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": f"2026-09-04T2{hour}:00:00+00:00",
                    "message": "bar processed",
                    "extra": {"session_id": SESSION, "signals": 0, "intents": 0, "fills": 0},
                }
            )
            for hour in (1, 2, 3)
        )
        + "\n",
        encoding="utf-8",
    )
    _log(
        tree,
        "paper.log",
        [
            {
                "timestamp": "2026-09-05T00:00:00+00:00",
                "message": "bar processed",
                "extra": {"session_id": SESSION, "signals": 0, "intents": 0, "fills": 0},
            }
        ],
    )

    payload = _client(tree).get("/api/status").json()

    # The feed's connection is remembered across the rotation.
    feed = next(
        kpi
        for brain in payload["brains"]
        if brain["key"] == "market"
        for kpi in brain["kpis"]
        if kpi["key"] == "feed_state"
    )
    assert feed["value"] == "STREAMING"
    # And all four candles are counted, not just the one after midnight.
    assert payload["activity"]["bars_seen"] == 4
    assert any(event["title"] == "Market feed streaming" for event in payload["timeline"])
