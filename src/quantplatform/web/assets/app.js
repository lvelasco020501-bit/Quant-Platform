/* Mission Control — the whole client, in one file and no framework.
 *
 * The page asks one endpoint for one coherent reading and paints it. It never decides
 * anything: the API has already judged what is healthy, what is unknown and what a warm-up
 * needs, so this file's only job is to put those answers on screen in the right colour.
 *
 * Two rules run through everything below:
 *
 *   1. `null` means "not known" and renders as N/A. It never renders as 0. A session that
 *      has recorded no fee and a session whose fees cannot be read are different facts.
 *   2. When the API cannot be reached, the page says so and dims. Showing the last good
 *      numbers as though they were current is the one failure that could actually mislead
 *      somebody into thinking a dead session is alive.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  let refreshMs = 12000;
  let timer = null;
  let lastGoodAt = null;
  let tickTimer = null;

  /* --- formatting ------------------------------------------------------------------ */

  const NA = '<span class="na">N/A</span>';

  /** Format a decimal string as money, keeping two places and thousands separators. */
  function money(value, unit) {
    if (value === null || value === undefined) return NA;
    const n = Number(value);
    if (!Number.isFinite(n)) return escape(String(value));
    const text = n.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return unit ? `${text} ${escape(unit)}` : text;
  }

  /** Format a decimal string at full precision, trailing zeros trimmed. */
  function quantity(value) {
    if (value === null || value === undefined) return NA;
    const n = Number(value);
    if (!Number.isFinite(n)) return escape(String(value));
    return n.toLocaleString("en-US", { maximumFractionDigits: 8 });
  }

  /** Format a 0..1 fraction as a percentage. */
  function percent(fraction, digits = 2) {
    if (fraction === null || fraction === undefined) return NA;
    return `${(Number(fraction) * 100).toFixed(digits)}%`;
  }

  /** Format an integer, distinguishing zero from unknown. */
  function count(value) {
    return value === null || value === undefined ? NA : String(value);
  }

  /** Format an ISO instant as an explicit UTC stamp. */
  function stamp(iso) {
    if (!iso) return NA;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return NA;
    const p = (n) => String(n).padStart(2, "0");
    return (
      `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
      `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`
    );
  }

  /** Format a second count as hours and minutes. */
  function duration(seconds) {
    if (seconds === null || seconds === undefined) return NA;
    const total = Math.max(Math.floor(seconds), 0);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    return `${h}h ${String(m).padStart(2, "0")}m`;
  }

  /** Escape text destined for innerHTML. Every value below passes through here. */
  function escape(text) {
    return String(text).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  /** Render a definition list from [label, valueHtml] pairs. */
  function pairs(node, rows) {
    node.innerHTML = rows
      .map(([label, value]) => `<dt>${escape(label)}</dt><dd>${value}</dd>`)
      .join("");
  }

  /* --- sections --------------------------------------------------------------------- */

  function renderHeader(data) {
    const sys = data.system;
    const health = $("health");
    health.textContent = sys.health;
    health.className = `health health--${sys.health.toLowerCase()}`;

    $("subtitle").textContent =
      `${sys.mode === "PAPER" ? "Paper Trading" : sys.mode} · ` +
      `${sys.risk_label === "RISK V2" ? "Risk V2" : "Risk V1"} · ` +
      `${sys.symbols.join(", ") || "—"}`;

    const badges = [
      [sys.mode, "badge--accent"],
      [sys.risk_label, sys.risk_label === "RISK V2" ? "badge--good" : ""],
      [sys.strategy_label, ""],
      [sys.symbols.join(" ") || "—", ""],
      [sys.timeframe.toUpperCase(), ""],
    ];
    $("badges").innerHTML = badges
      .map(([text, cls]) => `<span class="badge ${cls}">${escape(text)}</span>`)
      .join("");
    $("schema").textContent = `schema ${data.schema_version}`;
  }

  function renderBanner(data) {
    const banner = $("banner");
    const risk = data.risk;
    const tripped = risk.breakers.filter((b) => b.tripped);
    const unprotected = data.position.open && data.position.unprotected;

    if (tripped.length > 0) {
      banner.className = "banner banner--bad";
      banner.innerHTML =
        `<strong>Trading halted.</strong> ` +
        tripped.map((b) => escape(b.label)).join(", ") +
        ` tripped. A circuit breaker does not clear itself — this needs a person.`;
      return;
    }
    if (unprotected) {
      banner.className = "banner banner--bad";
      banner.innerHTML =
        "<strong>Open position with no recorded stop.</strong> " +
        "The session is holding risk it has not written down.";
      return;
    }
    if (data.notes && data.notes.length > 0) {
      banner.className = "banner banner--warn";
      banner.innerHTML = data.notes.map((n) => escape(n)).join("<br />");
      return;
    }
    banner.className = "banner banner--hidden";
  }

  function renderSmoke(data) {
    const s = data.smoke;
    $("card-smoke").hidden = false;
    const pct = s.progress === null || s.progress === undefined ? null : s.progress;
    $("smoke-fill").style.width = pct === null ? "0%" : `${(pct * 100).toFixed(1)}%`;
    $("smoke-pct").innerHTML =
      pct === null
        ? `<span class="na">No target declared — elapsed time only</span>`
        : `${(pct * 100).toFixed(0)}% complete`;

    pairs($("smoke-pairs"), [
      ["Started", stamp(s.started_at)],
      ["Elapsed", duration(s.elapsed_seconds)],
      ["Remaining", duration(s.remaining_seconds)],
      ["Target end", stamp(s.target_end)],
      ["Session", `<span style="font-size:0.76rem">${escape(s.session_id || "—")}</span>`],
    ]);
  }

  function renderPortfolio(data) {
    const p = data.portfolio;
    $("card-portfolio").hidden = false;
    $("equity").innerHTML = money(p.equity, p.quote_asset);

    const delta = $("equity-delta");
    if (p.equity_change === null || p.equity_change === undefined) {
      delta.textContent = "";
    } else {
      const up = p.equity_change > 0;
      const flat = p.equity_change === 0;
      delta.className = `figure__delta ${flat ? "flat" : up ? "up" : "down"}`;
      delta.textContent = `${up ? "+" : ""}${percent(p.equity_change)} vs starting capital`;
    }

    pairs($("portfolio-pairs"), [
      ["Starting capital", money(p.starting_capital, p.quote_asset)],
      ["Cash", money(p.cash, p.quote_asset)],
      ["Realised P&L", signed(p.realized_pnl, p.quote_asset)],
      ["Unrealised P&L", signed(p.unrealized_pnl, p.quote_asset)],
      ["Fees paid", money(p.fees, p.quote_asset)],
    ]);
  }

  /** Money that is coloured by sign, because a loss should not read like a balance. */
  function signed(value, unit) {
    if (value === null || value === undefined) return NA;
    const n = Number(value);
    const cls = n > 0 ? "up" : n < 0 ? "down" : "flat";
    return `<span class="${cls}">${money(value, unit)}</span>`;
  }

  function renderPosition(data) {
    const p = data.position;
    $("card-position").hidden = false;
    const body = $("position-body");

    if (!p.open) {
      body.innerHTML =
        `<div class="flat-state">` +
        `<span class="flat-state__title">FLAT</span>` +
        `<span class="hint">${escape(p.message || "No open position")}</span>` +
        `</div>`;
      return;
    }

    const stopClass = p.unprotected ? "stop-row stop-row--danger" : "stop-row";
    const stopValue = p.unprotected
      ? `<span class="down">NONE RECORDED</span>`
      : money(p.stop, data.portfolio.quote_asset);

    body.innerHTML =
      `<span class="side side--long">${escape(p.side)} ${escape(p.symbol)}</span>` +
      `<dl class="pairs" id="position-pairs"></dl>` +
      `<div class="${stopClass}">` +
      `<span class="stop-row__label">Stop${p.stop_kind ? ` · ${escape(p.stop_kind)}` : ""}</span>` +
      `<span class="stop-row__value">${stopValue}</span>` +
      `</div>`;

    pairs($("position-pairs"), [
      ["Entry", money(p.entry, data.portfolio.quote_asset)],
      ["Current", money(p.current, data.portfolio.quote_asset)],
      ["Quantity", quantity(p.quantity)],
      ["Unrealised P&L", signed(p.unrealized_pnl, data.portfolio.quote_asset)],
      ["Distance to stop", p.distance_to_stop === null ? NA : percent(p.distance_to_stop)],
      ["Risked at entry", money(p.risked_at_entry, data.portfolio.quote_asset)],
    ]);
  }

  function renderMarket(data) {
    const m = data.market;
    $("card-market").hidden = false;
    $("market-symbol").textContent = `${m.symbol || "—"} · last close`;
    $("last-close").innerHTML = money(m.last_close, data.portfolio.quote_asset);

    const done = m.warmup_complete === true;
    const progress = m.warmup_progress;
    $("warmup-label").textContent = "Warm-up";
    $("warmup-count").textContent =
      m.warmup_required === null || m.bars_processed === null
        ? "N/A"
        : `${m.bars_processed} / ${m.warmup_required}`;
    $("warmup-fill").style.width =
      progress === null || progress === undefined ? "0%" : `${(progress * 100).toFixed(1)}%`;
    $("warmup-hint").textContent = done
      ? "Strategy active — it has the history it needs."
      : "Strategy not ready yet — it will not signal until warm-up completes.";

    pairs($("market-pairs"), [
      ["Timeframe", escape(m.timeframe.toUpperCase())],
      ["Last closed candle", stamp(m.last_close_time)],
      ["Bars processed", count(m.bars_processed)],
    ]);
  }

  function renderRisk(data) {
    const r = data.risk;
    const card = $("card-risk");
    card.hidden = false;
    const anyTripped = r.breakers.some((b) => b.tripped);
    card.className = anyTripped ? "card card--alert" : "card";

    pairs($("risk-pairs"), [
      ["Risk per trade", r.risk_per_trade === null ? NA : percent(r.risk_per_trade)],
      ["Stop required", r.stop_required ? "YES" : "NO"],
    ]);

    $("breakers").innerHTML = r.breakers
      .map((b) => {
        const pill = b.tripped
          ? `<span class="state-pill state-pill--bad">TRIPPED</span>`
          : `<span class="state-pill state-pill--ok">OK</span>`;
        const when = b.tripped && b.at ? `<div class="timeline__meta">${stamp(b.at)}</div>` : "";
        return `<li class="${b.tripped ? "tripped" : ""}"><span>${escape(b.label)}${when}</span>${pill}</li>`;
      })
      .join("");
  }

  function renderActivity(data) {
    const a = data.activity;
    $("card-activity").hidden = false;
    const stats = [
      ["Signals", a.signals],
      ["Approved", a.approved],
      ["Rejected", a.rejected],
      ["Fills", a.fills],
    ];
    $("activity-stats").innerHTML = stats
      .map(
        ([label, value]) =>
          `<div class="stat"><div class="stat__value">${count(value)}</div>` +
          `<div class="stat__label">${escape(label)}</div></div>`,
      )
      .join("");

    $("activity-hint").textContent =
      a.bars_seen === 0
        ? "No candle has been processed yet — nothing to report."
        : a.fills === 0
          ? `No fills yet, across ${a.bars_seen} processed candle${a.bars_seen === 1 ? "" : "s"}.`
          : `Across ${a.bars_seen} processed candles.`;
  }

  function renderInfrastructure(data) {
    const i = data.infrastructure;
    $("card-infra").hidden = false;
    const service = i.service_running
      ? `<span class="up">RUNNING</span>`
      : `<span class="down">NOT RUNNING</span>`;
    pairs($("infra-pairs"), [
      ["Service", service],
      ["PID", count(i.pid)],
      ["Restarts", count(i.restarts)],
      ["Persistence", i.persistence_ok ? `<span class="up">OK</span>` : `<span class="na">no snapshot yet</span>`],
      ["Latest snapshot", stamp(i.latest_snapshot)],
      ["Reconnects", count(i.reconnects)],
      ["Data gaps", count(i.data_gaps)],
      ["Runtime errors", count(i.runtime_errors)],
      [
        "Daily report",
        i.daily_report_day
          ? escape(i.daily_report_day)
          : `<span class="na">Daily report pending</span>`,
      ],
    ]);
  }

  function renderTimeline(data) {
    $("card-timeline").hidden = false;
    const node = $("timeline");
    if (!data.timeline.length) {
      node.innerHTML = `<li class="empty">Nothing has happened yet.</li>`;
      return;
    }
    node.innerHTML = data.timeline
      .map(
        (e) =>
          `<li class="${escape(e.severity)}">` +
          `<div class="timeline__title">${escape(e.title)}</div>` +
          `<div class="timeline__meta">${stamp(e.at)}${e.detail ? ` · ${escape(e.detail)}` : ""}</div>` +
          `</li>`,
      )
      .join("");
  }

  function renderDetails(data) {
    const d = data.details;
    $("details-block").hidden = false;
    pairs($("details-pairs"), [
      ["Session id", escape(d.session_id || "—")],
      ["Strategy", escape(d.strategy_id || "—")],
      [
        "Parameters",
        escape(
          Object.entries(d.strategy_parameters || {})
            .map(([k, v]) => `${k}=${v}`)
            .join("  ") || "—",
        ),
      ],
      ["Execution mode", escape(d.execution_mode || "—")],
      ["Session started", stamp(d.started_at)],
      ["Snapshot saved", stamp(d.snapshot_saved_at)],
      ["Quote asset", escape(d.quote_asset || "—")],
      ["Reading generated", stamp(data.generated_at)],
    ]);
    pairs(
      $("sources-pairs"),
      Object.entries(d.sources || {}).map(([k, v]) => [k, escape(v)]),
    );
  }

  /* --- freshness ---------------------------------------------------------------------- */

  function markStale(message) {
    const app = $("app");
    app.classList.add("app--stale");
    const health = $("health");
    health.textContent = "DEGRADED";
    health.className = "health health--degraded";
    const banner = $("banner");
    banner.className = "banner banner--bad";
    banner.innerHTML = `<strong>Data temporarily unavailable.</strong> ${escape(message)} The figures below are the last successful reading and are no longer current.`;
  }

  /** Keep the "updated Ns ago" line honest between polls. */
  function tick() {
    if (lastGoodAt === null) return;
    const seconds = Math.round((Date.now() - lastGoodAt) / 1000);
    $("updated").textContent = `Last updated ${seconds}s ago`;
  }

  /* --- polling -------------------------------------------------------------------------- */

  async function refresh() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`the service answered ${response.status}.`);
      const data = await response.json();

      $("app").classList.remove("app--stale");
      $("app").setAttribute("aria-busy", "false");
      renderHeader(data);
      renderBanner(data);
      renderSmoke(data);
      renderPortfolio(data);
      renderPosition(data);
      renderMarket(data);
      renderRisk(data);
      renderActivity(data);
      renderInfrastructure(data);
      renderTimeline(data);
      renderDetails(data);

      lastGoodAt = Date.now();
      tick();

      if (data.refresh_seconds && data.refresh_seconds * 1000 !== refreshMs) {
        refreshMs = data.refresh_seconds * 1000;
        schedule();
      }
    } catch (error) {
      markStale(error && error.message ? error.message : "The service did not answer.");
    }
  }

  function schedule() {
    if (timer !== null) clearInterval(timer);
    timer = setInterval(refresh, refreshMs);
  }

  refresh();
  schedule();
  tickTimer = setInterval(tick, 1000);

  // Coming back to a backgrounded tab should not show a number minted before lunch.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
})();
