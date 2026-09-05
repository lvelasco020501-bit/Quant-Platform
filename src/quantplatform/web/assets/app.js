/* Mission Control — the whole client, in one file and no framework.
 *
 * This file renders and nothing else. Every judgement on the page — what is healthy, what a
 * warm-up implies, whether a sample is big enough to mean anything, why a figure is amber —
 * was made server-side in the status domain, where it is testable. Duplicating any of it
 * here would create a second opinion that could drift from the first.
 *
 * Two rules run through the rendering:
 *
 *   1. `null` means "not known" and renders as N/A, never as 0. A session that paid no fee
 *      and a session whose fees cannot be read are different facts.
 *   2. When the API cannot be reached, the page says so and dims. Showing the last good
 *      numbers as though they were current is the one failure that could genuinely mislead.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  let refreshMs = 12000;
  let timer = null;
  let tickTimer = null;
  let lastGoodAt = null;
  let kpiIndex = new Map();

  /* --- text ------------------------------------------------------------------------- */

  function escape(text) {
    return String(text).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  function stamp(iso) {
    if (!iso) return "N/A";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "N/A";
    const p = (n) => String(n).padStart(2, "0");
    return (
      `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
      `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`
    );
  }

  const SOURCE_TEXT = {
    structured: "Structured — persisted by the session itself, in its snapshot or daily report. This is the strongest kind of evidence available here.",
    "log-derived": "Log-derived — parsed from the session's own JSON logs. Real, but logs rotate and outlive the run that wrote them, so it is weaker evidence than a snapshot.",
    unavailable: "Not available — nothing in the system records this. No value is shown rather than a guess.",
  };

  const SOURCE_LABEL = {
    structured: "structured",
    "log-derived": "log-derived",
    unavailable: "N/A",
  };

  /* --- rendering ---------------------------------------------------------------------- */

  function renderHeader(data) {
    const sys = data.system;
    const summary = data.summary;

    const health = $("health");
    health.textContent = summary.headline;
    health.className = `health health--${summary.level}`;

    $("subtitle").textContent =
      `${sys.mode === "PAPER" ? "Paper Trading" : sys.mode} · ` +
      `${sys.risk_label === "RISK V2" ? "Risk V2" : "Risk V1"} · ` +
      `${sys.symbols.join(", ") || "—"}`;

    $("badges").innerHTML = [
      [sys.mode, "badge--accent"],
      [sys.risk_label, sys.risk_label === "RISK V2" ? "badge--good" : ""],
      [sys.strategy_label, ""],
      [sys.symbols.join(" ") || "—", ""],
      [sys.timeframe.toUpperCase(), ""],
    ]
      .map(([text, cls]) => `<span class="badge ${cls}">${escape(text)}</span>`)
      .join("");

    $("schema").textContent = `schema ${data.schema_version}`;
  }

  function renderSummary(data) {
    const s = data.summary;
    const node = $("summary");
    node.className = `summary summary--${s.level}`;
    $("summary-verdict").textContent = s.intervention_required
      ? "Intervention required"
      : "No intervention required";
    $("summary-verdict").className = `summary__verdict ${
      s.intervention_required ? "summary__verdict--alert" : ""
    }`;
    $("summary-text").textContent = s.text;

    const blockers = $("summary-blockers");
    if (!s.blockers.length) {
      blockers.hidden = true;
      blockers.innerHTML = "";
      return;
    }
    blockers.hidden = false;
    blockers.innerHTML = s.blockers.map((b) => `<li>${escape(b)}</li>`).join("");
  }

  function renderBrains(data) {
    kpiIndex = new Map();
    $("brains").innerHTML = data.brains
      .map((brain) => {
        const kpis = brain.kpis
          .map((kpi) => {
            const id = `${brain.key}.${kpi.key}`;
            kpiIndex.set(id, { ...kpi, brain: brain.title });
            const value =
              kpi.value === null || kpi.value === undefined
                ? `<span class="na">N/A</span>`
                : escape(kpi.value);
            const flag = kpi.low_confidence
              ? `<span class="chip__flag">low confidence</span>`
              : "";
            return (
              `<button class="chip chip--${kpi.level}" type="button" data-kpi="${escape(id)}">` +
              `<span class="chip__label">${escape(kpi.label)}</span>` +
              `<span class="chip__value">${value}</span>` +
              flag +
              `</button>`
            );
          })
          .join("");

        return (
          `<section class="brain brain--${brain.level}">` +
          `<header class="brain__head">` +
          `<h2 class="brain__title">${escape(brain.title)}</h2>` +
          `<span class="pill pill--${brain.level}">${escape(levelWord(brain.level))}</span>` +
          `</header>` +
          `<p class="brain__headline">${escape(brain.headline)}</p>` +
          `<p class="brain__explanation">${escape(brain.explanation)}</p>` +
          `<div class="chips">${kpis}</div>` +
          `</section>`
        );
      })
      .join("");
  }

  function levelWord(level) {
    return { good: "OK", attention: "ATTENTION", danger: "DANGER", info: "INFO" }[level] || "INFO";
  }

  /* --- WHY panel ------------------------------------------------------------------------ */

  function openWhy(id) {
    const kpi = kpiIndex.get(id);
    if (!kpi) return;
    $("why-title").textContent = `${kpi.brain} · ${kpi.label}`;
    $("why-value").innerHTML =
      kpi.value === null || kpi.value === undefined
        ? `<span class="na">N/A</span>`
        : escape(kpi.value);
    $("why-meaning").textContent = kpi.meaning;
    $("why-source").innerHTML =
      `<span class="src src--${kpi.source.replace("-", "")}">${escape(SOURCE_LABEL[kpi.source] || kpi.source)}</span> ` +
      escape(SOURCE_TEXT[kpi.source] || "");
    $("why-reason").textContent =
      kpi.why + (kpi.low_confidence ? " This figure is flagged low confidence." : "");
    $("why").hidden = false;
    document.body.classList.add("no-scroll");
  }

  function closeWhy() {
    $("why").hidden = true;
    document.body.classList.remove("no-scroll");
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-kpi]");
    if (trigger) {
      openWhy(trigger.getAttribute("data-kpi"));
      return;
    }
    if (event.target.closest("[data-close]")) closeWhy();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeWhy();
  });

  /* --- freshness -------------------------------------------------------------------------- */

  function markStale(message) {
    $("app").classList.add("app--stale");
    const health = $("health");
    health.textContent = "DEGRADED";
    health.className = "health health--attention";
    const node = $("summary");
    node.className = "summary summary--danger";
    $("summary-verdict").textContent = "Data temporarily unavailable";
    $("summary-verdict").className = "summary__verdict summary__verdict--alert";
    $("summary-text").textContent =
      message + " The panels below are the last successful reading and are no longer current.";
  }

  function tick() {
    if (lastGoodAt === null) return;
    const seconds = Math.round((Date.now() - lastGoodAt) / 1000);
    $("updated").textContent = `Last updated ${seconds}s ago`;
  }

  /* --- polling ------------------------------------------------------------------------------ */

  async function refresh() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`The service answered ${response.status}.`);
      const data = await response.json();

      $("app").classList.remove("app--stale");
      $("app").setAttribute("aria-busy", "false");
      renderHeader(data);
      renderSummary(data);
      renderBrains(data);

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

  // Returning to a backgrounded tab should not show a number minted before lunch.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
})();
