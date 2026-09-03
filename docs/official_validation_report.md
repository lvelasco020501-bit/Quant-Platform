# QuantPlatform Official Validation Report

**Document status:** FINAL — the 7-day observation window closed on 2026-08-28T13:35:59Z.
Section 10 is complete; this document is no longer live.

**Evidence discipline:** every entry is a direct read of the persisted state file, a log
file, a daily report, or a live query against the running process. Log counters were
filtered by `timestamp >= 2026-08-21T12:41:11Z` so that rotated files from the retired
`paper-7c-week3` and `paper-7c-week4` sessions — still present in `var/logs/` — could not
contribute. That filter is not cosmetic: an unfiltered first count reported 1 CRITICAL and
4 ERRORs that on inspection all belonged to those earlier sessions. Timestamps are UTC.

---

## 1. Executive Summary

| | |
|---|---|
| Status | **COMPLETE** — 7 days served, stopped cleanly |
| Session ID | `paper-7c-week5` |
| Started | 2026-08-21T12:41:11.958106Z |
| Stopped | 2026-08-28T13:35:59Z (SIGTERM, cooperative shutdown) |
| Duration | 7d 00h 54m |
| Commit | `2f9bff2e364d7a227e7747f0905b4e191984e0b6` |
| PID | 20661 |
| Bars processed | 169 |
| Restarts | 0 |
| Overall verdict | Infrastructure PASS · Trading lifecycle PASS · Reconnect resilience PASS |

**Two prior attempts did not survive**, and the difference is the point of this run.
`paper-7c-week3` ended at hour 33 on a 16-candle gap after the host slept; `paper-7c-week4`
ended after 50 hours when a transport hiccup exhausted a 5-attempt reconnect budget in 31
seconds. Both produced fixes (`557c7e4`, `2f9bff2`). This run absorbed 4 reconnects without
noticing them.

---

## 2. Operational Timeline

| Timestamp (UTC) | Event | Source |
|---|---|---|
| 2026-08-21T12:41:09.597Z | Session lock acquired, pid 20661 | `orchestration.log` |
| 2026-08-21T12:41:11.958Z | Session started, BTC/USDT 1h, 10 000 USDT | state `started_at` |
| 2026-08-21T13:00:00.010Z | First closed bar emitted and processed | `marketdata.log`, `paper.log` |
| 2026-08-22T11:31:54Z | Reconnect 1 — recovered in 2 attempts (~13.6s) | `marketdata.log` |
| 2026-08-23T14:00:00.004Z | Warm-up complete (bar 50/50) | `paper.log` |
| 2026-08-24T11:56:06Z | Reconnect 2 — `ConnectionClosedOK` (1001 going away), 1 attempt (~1.7s) | `marketdata.log` |
| 2026-08-24T12:00:00Z | **First signal** — 1 signal, 1 intent, 1 decision, 0 fills | `paper.log` |
| 2026-08-24T13:00:00Z | **First fill** — long 0.12050 BTC @ 78 561.354696825 | state `positions` |
| 2026-08-26T15:00:00Z | Exit signal — 1 signal, 1 intent, 1 decision | `paper.log` |
| 2026-08-26T16:00:00Z | Exit fill — position closed, realised −106.94634335099250 | state |
| 2026-08-27T06:00:00Z | Re-entry signal | `paper.log` |
| 2026-08-27T07:00:00Z | Re-entry fill — long 0.11850 BTC @ 79 030.94826627 | state |
| 2026-08-28T13:00:00.112Z | Final bar processed (169) | state `saved_at` |
| 2026-08-28T13:35:53.709Z | `stop requested`, reason SIGTERM | `orchestration.log` |
| 2026-08-28T13:35:59.034Z | `paper session stopped` — 169 bars, 7 reports, 0 report failures | `orchestration.log` |
| 2026-08-28T13:35:59.043Z | Session lock released | `orchestration.log` |

Incidents: **none**. Restarts: **none**. Daily reports: **7 written, 0 failures**.

---

## 3. System Stability

| Metric | Value |
|---|---|
| Uptime | 7d 00h 54m, single process, 0 restarts |
| Reconnects | 4 — all recovered; none exhausted the retry budget |
| Heartbeat timeouts | 0 |
| Watchdog CRITICAL | 0 |
| Gaps detected | 0 |
| Malformed frames | 0 |
| ERROR lines | 0 |
| WARNING lines | 13 (all reconnect-cycle lines: `receive failed` / `reconnecting`) |
| Report failures | 0 of 7 |
| Memory (RSS) | 23–103 MB observed, no sustained growth |
| CPU | 0.0–0.6% |
| Log growth | ≈12 MB/day |

**Reconnect resilience is the headline result.** Four transport interruptions, one of them
(`ConnectionClosedOK`, code 1001) an ordinary server-initiated close. Under the pre-`2f9bff2`
budget, any of them taking six attempts would have ended the run. None did, and none
produced a data gap.

---

## 4. Feed Integrity

| Metric | Value |
|---|---|
| Candles accepted (session) | 169 |
| Gaps | **0** |
| Duplicates suppressed | 0 |
| Out-of-order candles | 0 |
| Malformed frames | 0 |
| Continuity | Unbroken across all 4 reconnects |

Every reconnect resumed exactly where the series left off. `DataGapError` — the only
automatic termination cause left for market data after `2f9bff2` — never fired.

---

## 5. Strategy Behaviour

| Metric | Value |
|---|---|
| Warm-up requirement | 50 bars |
| Warm-up completed | 2026-08-23T14:00:00Z (bar 50) |
| Signals | 3 |
| Signals discarded | 0 |
| Signals accepted | 3 |

Three signals over 119 post-warm-up bars: one entry, one exit, one re-entry. That cadence
is consistent with a 20/50 EMA crossover filter on 1h BTC/USDT and is not evidence of a
fault.

---

## 6. Risk Engine

| Metric | Value |
|---|---|
| Intents | 3 |
| Risk decisions | 3 |
| Rejections | 0 |
| Orders submitted | 3 |
| Fills | 3 |
| Open positions at close | 1 |

Every intent was approved. No rejection path was exercised in production — the risk engine's
refusal logic remains covered only by its unit tests, not by this run.

---

## 7. Portfolio

| Metric | Value |
|---|---|
| Starting capital | 10 000.00 USDT |
| Realised PnL | **−106.94634335099250 USDT** |
| Unrealised PnL (final mark) | **+46.2270554470050 USDT** |
| Fees paid | **28.1820635764875 USDT** |
| Final equity | **9 939.2807120960125 USDT** (−0.607%) |
| Final cash | 527.8862870960125 USDT free, ~0 locked |
| Final position | 0.11850 BTC @ 79 030.94826627, marked at 79 421.05 |

**Round trip 1** (closed): entered 78 561.354696825, exited 2026-08-26T16:00Z, realised
−106.95 USDT including fees. **Round trip 2** (open at close): entered 79 030.94826627,
unrealised +46.23 at the final mark, preserved in the snapshot rather than liquidated.

**Fees are 26% of the gross loss.** Three fills cost 28.18 USDT against a −106.95 realised
result; on a strategy trading this rarely, cost per round trip is a material term, not a
rounding error.

---

## 8. Control Center Consistency

Verified at session start, at the first bar, and at every checkpoint through the run.
`session_id`, `bars_processed`, `last_bar_close_time` and `last_state_update` matched the
persisted state exactly on every comparison. `session_process_status` correctly reported
`running` throughout, backed by `os.kill(pid, 0)` against the session lock.

Two known reporting artifacts persisted and are **not** defects in the trading system:

- **Feed acceptance ≈0.1%** — compares accepted closed candles against every websocket
  frame received (~276 000 ticks vs 169 candles). A scale mismatch in the metric, not a
  dropped-data problem. The dashboard already computes and displays a corrected operational
  reading beside it.
- **Rollover ±1 bar** — the candle closing at 00:00 UTC is counted as received on the day
  that ends and processed on the day that begins, so `daily_session_bars_received` and
  `..._processed` differ by one on rollover days. Deterministic, no candle lost.

---

## 9. Incidents

**None.** Zero SEV-1, zero SEV-2, zero SEV-3 raised during the window.

---

## 10. Final Certification

**The infrastructure is certified. The strategy is not, and was never the subject of this
test.**

| Dimension | Verdict | Basis |
|---|---|---|
| Infrastructure | **PASS** | 7 days, single process, 0 restarts, 0 errors, clean shutdown |
| Reconnect resilience | **PASS** | 4 reconnects absorbed, 0 gaps, 0 terminations |
| Trading lifecycle E2E | **PASS** | Signal → intent → risk → order → fill → portfolio → state → dashboard, exercised 3× including a complete round trip |
| Data integrity | **PASS** | 169/169 candles, 0 gaps, 0 duplicates, 0 malformed |
| Persistence & reporting | **PASS** | 7 daily reports, 0 failures, final state intact after shutdown |
| Open infrastructure blockers | **0** | |
| Economic result | **Observed, not certified** | −0.607% equity over 7 days on 3 fills |

### What this run does not establish

It does not establish that `ema_trend` has an edge. Three trades over seven days is not a
sample from which any conclusion about profitability follows, in either direction. The
−0.607% is reported because it happened, not because it means something.

### Limitations of the EMA 20/50 benchmark, as demonstrated

1. **No hard stop.** The only exit is `EMA20 < EMA50`. A position can run arbitrarily far
   against the account before the averages cross, and nothing intervenes.
2. **No take-profit, no trailing stop, no time stop.** Confirmed absent by exhaustive search
   of `src/`; unrealised gains are given back if the crossover has not occurred.
3. **~95% of equity per entry.** `entry_fraction = 0.95` (`backtesting/config.py:40`) sizes
   every entry as a fixed fraction of equity, with no reference to risk taken or to the
   distance to any stop — because no stop exists.
4. **Survival depends entirely on the strategy's own exit.** The risk engine caps exposure
   and drawdown but has no mechanism to close an open position; it can only refuse new
   intents.
5. **Lagging by construction.** A crossover filter concedes the beginning of every move and
   the end of every move; the −106.95 realised loss on round trip 1 is that concession
   working exactly as designed.

`ema_trend` is hereby **frozen as the benchmark**. It will not be patched into a production
strategy. Every future strategy is measured against it.

**Certified 2026-08-28. Evidence preserved read-only at `var/audit/week5-final/`.**

---

**Last updated:** 2026-08-28T13:40:00Z
