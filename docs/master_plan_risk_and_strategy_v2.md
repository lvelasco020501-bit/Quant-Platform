# Master Plan — Risk Engine V2 & Strategy Research V2

**Status:** DESIGN ONLY. No code accompanies this document, and none should be written
against it until it is approved.

**Premise, from `paper-7c-week5`:** the infrastructure is certified and the strategy is not.
The run proved candles arrive, orders fill, state persists and the process survives a week.
It proved nothing about edge, and it exposed one structural fact worth stating plainly:
**a single entry could commit ~95% of the account with no hard stop behind it.** That is not
a strategy defect to be patched. It is a missing layer.

The governing principle everything below serves:

> Strategy proposes. Risk decides how much capital may be lost. Execution cannot bypass Risk.

---

## 1. Architecture we reuse

The hexagonal boundaries hold and are worth keeping. What follows already exists, works, and
should not be rebuilt.

| Component | Why it survives V2 unchanged |
|---|---|
| `StandardRiskEngine` | Already the sole gate between intent and broker. Already externalises every threshold to `RiskConfiguration`. Already records a per-check audit trail (`RiskDecision`) rather than a boolean. |
| `RiskConfiguration` | Frozen Pydantic, refuses incoherent combinations at construction, no permissive fallbacks. The right shape for V2's new limits. |
| `SpotPortfolioEngine` | `Balance` is the single ledger of truth; `reserve`/`release` never touch `total`. Accounting is trustworthy. |
| `SimulatedBroker` | Reservation discipline (`_planned_reservation`, worst-case upper bound) already prevents two orders competing for the same money. |
| `BacktestEngine.advance()` | One bar in, one `BarOutcome` out. Paper and backtest share it — the property that makes any research result transferable to live. |
| `compute_performance()` | Already computes win rate, profit factor, expectancy, max drawdown, Sharpe, Sortino. |
| `Clock` port, Decimal-first, architecture tests | The reproducibility guarantees research depends on. |

---

## 2. Gaps found

Each is a fact verified against the code, not a hypothesis.

### Risk

| # | Gap | Evidence |
|---|---|---|
| G1 | **No risk-per-trade concept anywhere.** Nothing in `src/` mentions `risk_per_trade`, `risk_amount` or `stop_distance`. | exhaustive grep, 0 results |
| G2 | **Sizing is a fixed fraction of equity, blind to risk.** `entry_fraction = 0.95` (`backtesting/config.py:40`) — every entry commits the same share regardless of volatility or stop distance. | week5: 0.12050 BTC ≈ 9 468 USDT on a 10 000 account |
| G3 | **No hard stop of any kind.** No stop-loss, take-profit, trailing stop or time stop exists. | grep across `src/`, 0 results |
| G4 | **Risk cannot close a position.** `assess(intent, context)` only evaluates intents the strategy proposed; it has no path to emit its own exit. | `risk/engine.py:238` |
| G5 | **Drawdown limits exist but only gate new entries.** `max_daily_drawdown_pct`, `max_total_drawdown_pct` refuse the *next* intent; they cannot act on the position already losing money. | `risk/config.py:76-78` |
| G6 | **No consecutive-loss or circuit-breaker concept.** | grep, 0 results |
| G7 | **`max_portfolio_exposure_pct = 0.95`** is the ceiling that permitted the week5 concentration — a limit that was doing exactly what it was configured to do. | `risk/config.py:73` |

### Research

| # | Gap |
|---|---|
| G8 | No R-multiple — impossible today, since R requires a stop distance (G1/G3). |
| G9 | No reward/risk, turnover, consecutive-loss or risk-of-ruin metrics. |
| G10 | Slippage is charged by the broker but never surfaced as a research metric. |
| G11 | No walk-forward, OOS split, parameter-sensitivity or regime-analysis harness. |
| G12 | No way to run two strategies over identical data and compare — no benchmark comparison primitive. |

### Observability

| # | Gap |
|---|---|
| G13 | EMA20/EMA50 and every other feature value are never logged or persisted — the reason "how close is the exit?" was unanswerable all week. |
| G14 | Requested vs approved size, and the risk check that resized it, are not visible outside the process. |
| G15 | Feed acceptance ≈0.1% artifact (scale mismatch) and rollover ±1 artifact remain in reporting. |

---

## 3. New contracts required

Contracts only — no numbers. Every threshold below is a configurable field whose default is
chosen later, with evidence, not now.

### 3.1 `RiskBudget` (new)

The missing concept: how much of the account a single decision is allowed to lose.

```
RiskBudget
  risk_per_trade_pct        Rate      # fraction of equity at risk per position
  max_position_exposure_pct Rate      # ceiling on a single position's marked value
  min_stop_distance_bps     Money     # refuses stops so tight they are noise
  max_stop_distance_bps     Money     # refuses stops so wide sizing becomes meaningless
```

### 3.2 `StopSpecification` (new)

A stop that belongs to the **intent**, not to the strategy's internal state — so Risk can
size against it and Execution can enforce it even if the strategy never speaks again.

```
StopSpecification
  kind          StopKind          # HARD | TRAILING | BREAK_EVEN | TIME
  trigger_price Price | None      # absolute, when known at intent time
  distance_bps  Money | None      # relative alternative
  activated_at  datetime | None   # for trailing/break-even arming
```

`OrderIntent` gains `stop: StopSpecification | None`. **An intent carrying no stop is
refusable by configuration** — that single flag is what makes "no naked entries" enforceable
rather than aspirational.

### 3.3 `PositionRiskState` (new, persisted)

The gap that made week5's open position unmanageable after a restart: nothing recorded what
protection the position was supposed to have.

```
PositionRiskState
  symbol             Symbol
  stop               StopSpecification
  risk_amount        Money        # what was actually put at risk at entry
  entry_price        Price
  highest_price_seen Price | None # trailing bookkeeping
  opened_at          datetime
```

Persisted in `PaperSessionState` alongside `positions`. Note this interacts with the
fail-closed resume guard (`557c7e4`) — see §8.

### 3.4 `RiskAction` (new)

The contract that lets Risk act rather than only refuse. Deliberately narrow.

```
RiskAction
  kind      RiskActionKind   # NONE | REDUCE | CLOSE | HALT_NEW_ENTRIES
  symbol    Symbol | None
  quantity  Quantity | None
  reason    Text
  triggered_by HealthCheckName
```

New method: `RiskEngine.evaluate_open_positions(context) -> Sequence[RiskAction]`, called
once per bar **before** the strategy is consulted. This is the single largest change in V2
and the one that closes G4/G5.

### 3.5 `CircuitBreakerState` (new, persisted)

```
CircuitBreakerState
  tripped_at         datetime | None
  reason             Text | None
  consecutive_losses int
  daily_loss         Money
  resets_at          datetime | None   # explicit, never silent
```

A tripped breaker **halts new entries and requires an explicit operator reset** — the same
discipline `resynchronize()` already uses for gaps. Automatic un-tripping would make the
breaker a delay rather than a stop.

### 3.6 `PositionSizer` (new port)

```
PositionSizer.size(intent, stop, budget, snapshot) -> Quantity
```

Implementations: `FixedFractionSizer` (today's behaviour, kept as the benchmark's sizer),
`RiskBasedSizer` (risk_amount ÷ stop_distance), `VolatilityAdjustedSizer` (later). A port,
not a branch, so a strategy family can declare which sizing it was researched under.

### 3.7 Research metrics (extend `PerformanceSummary`)

Add: `r_multiples`, `average_r`, `expectancy_r`, `reward_risk_ratio`, `turnover`,
`total_slippage`, `max_consecutive_losses`, `max_consecutive_wins`, `time_in_market_pct`,
`fee_drag_pct`. Risk-of-ruin only when a sample is large enough to compute it honestly —
otherwise reported as unavailable, never estimated.

---

## 4. Modules likely to change

| Path | Change | Risk of regression |
|---|---|---|
| `core/models/risk.py` | New: `StopSpecification`, `RiskBudget`, `RiskAction`, `PositionRiskState` | Low — additive |
| `core/models/orders.py` | `OrderIntent.stop` field | **Medium** — touches a frozen model every layer reads |
| `core/interfaces.py` | New `PositionSizer` port | Low — additive |
| `risk/config.py` | New budget/breaker/stop-policy sections | **Medium** — `_validate_coherence` must grow with them |
| `risk/engine.py` | New `evaluate_open_positions()`; stop-aware sizing | **High** — the most safety-critical file in the repo |
| `risk/sizing.py` | `RiskBasedSizer` alongside existing helpers | Medium |
| `execution/broker.py` | Honour stop triggers during matching | **High** — reservation/fill accounting is subtle |
| `backtesting/engine.py` | Call `evaluate_open_positions()` before strategy each bar | **High** — changes the per-bar contract |
| `backtesting/metrics.py` | New research metrics | Low — additive |
| `core/models/paper.py` | Persist `PositionRiskState`, `CircuitBreakerState` | **Medium** — schema change; interacts with resume guard |
| `paper/session.py` | Resume guard must account for new persisted state | Medium |
| `strategies/base.py` | Strategies declare a `StopSpecification` with entries | Medium |
| `strategies/ema_trend.py` | **Frozen. Not modified.** Benchmark only. | — |
| `research/` (new package) | Walk-forward, OOS, sensitivity, regime, benchmark comparison | Low — greenfield |

---

## 5. Implementation order

Strictly sequential where safety depends on it; §9 marks what can run in parallel.

**M1 — Contracts, no behaviour.** New models and ports. Nothing consumes them yet. The
repo stays green and shippable.

**M2 — Metrics.** Extend `PerformanceSummary`. Purely additive, immediately useful for
measuring the benchmark itself, and independent of every risk change.

**M3 — Stop plumbing.** `OrderIntent.stop`, persistence of `PositionRiskState`, resume-guard
update. **No enforcement yet** — the stop is carried and recorded but nothing acts on it.
This isolates the schema change from the behaviour change.

**M4 — Risk-based sizing.** `RiskBasedSizer` behind configuration, default off. The benchmark
keeps `FixedFractionSizer`. Both must produce identical results when configured identically —
that equivalence is the test.

**M5 — Hard stop enforcement.** Broker honours stops during matching. First point at which
behaviour genuinely changes.

**M6 — `evaluate_open_positions()`.** Risk gains the ability to act. Largest blast radius;
deliberately after everything it depends on is proven.

**M7 — Circuit breakers.** Daily loss, drawdown, consecutive losses. Depends on M6.

**M8 — Position management components.** Take-profit, trailing, break-even, time stop,
scale-out, re-entry — each independently toggleable, all default **off**.

**M9 — Research harness.** Walk-forward, OOS, sensitivity, regime, stress, benchmark
comparison.

**M10 — Strategy families.** Trend, breakout, mean-reversion, volatility-aware. Only after
M9 can measure them honestly.

---

## 6. Tests required per milestone

| M | Tests |
|---|---|
| M1 | Model validation: incoherent stop refused, budget bounds enforced, frozen semantics preserved |
| M2 | Each metric against hand-computed fixtures; `None` (not zero) when the sample is too small |
| M3 | Intent round-trips its stop; state persists and reloads `PositionRiskState`; resume guard behaviour unchanged for stopless state |
| M4 | `RiskBasedSizer` sizes to exactly `risk_amount ÷ stop_distance`; **equivalence test** — both sizers identical under equivalent config; refuses when stop distance is outside bounds |
| M5 | Stop triggers intrabar; does not trigger when untouched; gap-through-stop fills at the gap not the stop; reservation released correctly on stop-out |
| M6 | Returns `CLOSE` when the stop is breached; `NONE` when healthy; actions are emitted **before** the strategy is consulted; an action can never be silently dropped |
| M7 | Breaker trips on each condition independently; halts new entries; does **not** self-reset; explicit reset works |
| M8 | Each component in isolation, plus off-by-default assertions; A/B harness produces two comparable runs from one dataset |
| M9 | Walk-forward splits are non-overlapping and chronological; OOS never leaks in-sample data; sensitivity sweep is deterministic |
| M10 | Each family satisfies the strategy contract; benchmark comparison runs both over identical bars |

**Standing requirement:** the full suite (currently 1486) stays green at every milestone. No
existing test is weakened to accommodate a new behaviour.

---

## 7. Acceptance criteria

| M | Done when |
|---|---|
| M1 | Models exist, validate, are unused. Suite green. |
| M2 | Metrics computed for the frozen week5 result and reviewed. |
| M3 | A stop survives a full persist → resume cycle intact. |
| M4 | Equivalence test passes; risk-based sizing produces a demonstrably smaller position than fixed-fraction for the same account and a realistic stop. |
| M5 | A position that breaches its stop is closed by the broker without the strategy speaking. |
| M6 | **An open position is closed by Risk alone.** This is the criterion that retires the week5 exposure. |
| M7 | A breaker trips on synthetic loss sequences and refuses new entries until reset. |
| M8 | Each component measurably changes outcomes when enabled and provably does nothing when disabled. |
| M9 | The benchmark can be walk-forward tested end to end and produces a report. |
| M10 | At least one family completes the §5 promotion protocol against the benchmark. |

---

## 8. Regression risks

1. **`OrderIntent` is frozen and universally read.** Adding a field touches risk, broker,
   portfolio, reporting and every fixture. Mitigate: `stop` optional, default `None`,
   behaviour unchanged when absent (M3 exists precisely to isolate this).
2. **`evaluate_open_positions()` changes the per-bar contract.** A new call site in
   `advance()` is the single riskiest edit in the plan — it runs before the strategy on every
   bar of every backtest ever run afterwards. Mitigate: no-op by default; equivalence test
   asserting an unconfigured V2 reproduces V1 results bar for bar.
3. **Broker stop handling interacts with reservations.** The `release`-before-`apply` ordering
   is subtle and already carries a rollback path. Mitigate: M5 in isolation, reservation
   assertions on every stop path.
4. **Persisting new state interacts with the fail-closed resume guard (`557c7e4`).** The guard
   refuses resume when financial state exists; `PositionRiskState` is financial state. Do not
   weaken the guard to make resume convenient — if anything, a position with an armed stop is
   *more* dangerous to resume without its broker context, not less.
5. **Metrics drift.** New metrics must not change existing ones. Mitigate: golden-value tests
   on current outputs before adding anything.
6. **Config coherence explosion.** `RiskConfiguration._validate_coherence` grows combinatorially.
   Mitigate: property-based tests over the parameter space.

---

## 9. Parallelisable work

Independent of the risk chain, safe to develop concurrently:

- **M2 (metrics)** — pure computation over existing results.
- **M9 (research harness)** — greenfield `research/` package, consumes existing `BacktestEngine`.
- **Observability fixes (G13/G14/G15)** — logging and dashboard only, no trading path.
- **VPS migration prep** — infrastructure, orthogonal to all of the above.

Strictly sequential: **M1 → M3 → M4 → M5 → M6 → M7 → M8**. Each depends on the last being
proven; none should be compressed.

---

## 10. Milestones and commits

One commit per milestone, each independently revertible, each leaving the suite green.

| Commit | Scope |
|---|---|
| 1 | `feat(core): contracts for risk budgets, stops and risk actions` (M1) |
| 2 | `feat(backtesting): R-multiple, turnover and survival research metrics` (M2) |
| 3 | `feat(risk): carry and persist a stop specification with every position` (M3) |
| 4 | `feat(risk): size a position from the capital it risks, not a fixed fraction` (M4) |
| 5 | `feat(execution): honour a hard stop during matching` (M5) |
| 6 | `feat(risk): let risk close a position the strategy has not exited` (M6) |
| 7 | `feat(risk): circuit breakers for daily loss, drawdown and loss streaks` (M7) |
| 8 | `feat(strategies): composable exit components, all off by default` (M8) |
| 9 | `feat(research): walk-forward, out-of-sample and benchmark comparison` (M9) |
| 10+ | One commit per strategy family (M10) |

---

## 11. Deferred operational work (registered, not scheduled)

**A. VPS Linux 24/7** — Hetzner CX22 (~$4.59/mo) recommended; systemd replaces
`caffeinate`/`nohup`, Tailscale for Mission Control, 24–72h validation before promotion.
Retires the AC-power and clamshell operating constraints entirely.

**B. Certified warm-start** — persist validated historical candles and indicator state so a
new session does not repeat ~50 hours of warm-up. Must preserve reproducibility: a warm-started
session has to produce the same decisions a cold one would, and that equivalence needs its own
test before the feature is trusted.

**C. Observability** — G13 (feature values: EMA20/50 per bar), G14 (requested vs approved size,
the check that resized it, the full Signal → Intent → RiskDecision → Order → Fill chain),
G15 (feed acceptance and rollover reporting artifacts).

---

## 12. What this plan refuses to do

- **No parameter values are proposed.** Every threshold above is a named, typed, configurable
  field. Choosing numbers before there is a research harness capable of testing them is how
  a system acquires magic constants nobody can later justify.
- **No profitability is promised.** The plan improves survivability and measurability. Whether
  any strategy has edge is a question this plan builds the apparatus to *answer*, not to assume.
- **`ema_trend` is not improved.** It is the benchmark. A benchmark that keeps changing is not
  a benchmark.
- **No ML.** Deferred until there is out-of-sample methodology, a benchmark and enough data —
  all of which this plan is a prerequisite for.
