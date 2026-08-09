# Quant Platform

A modular, exchange-agnostic and strategy-agnostic quantitative trading platform.

The first configured market is **BTC/USDT spot on the 1-hour timeframe**, but nothing in
the core is coupled to that instrument, that timeframe or any particular venue. Leverage
and short selling are prohibited by configuration, and live trading is architecturally
supported but disabled by default.

## Status

Phase 1 (foundation) is complete: domain models, ports, configuration, clocks, events,
structured logging, the exception hierarchy and the enforced dependency boundaries.

Phase 2 (data) is complete: a historical market-data pipeline that reads a canonical CSV
file, validates every record and the dataset as a whole, normalises survivors into
`MarketBar`, and persists them transactionally with full provenance. Portfolio accounting,
risk, backtesting, strategies, execution and the API arrive in later phases.

**The data layer only ever reports what it finds; it never repairs.** Missing bars are not
synthesised, prices are not interpolated, out-of-order input is detected before any sorting
happens, and a candle conflicting with one already stored is recorded rather than
overwritten.

The historical ingestion pipeline reads local CSV files only. Live candles arrive separately
through `marketdata` (Phase 7A), which streams them read-only from the exchange; REST backfill
and CCXT remain out of scope.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/) for dependency and environment management
- PostgreSQL 16 (Docker Compose provides one for local development)

## Getting started

```bash
uv sync
cp .env.example .env
uv run quantplatform check-config
```

`check-config` validates the effective configuration and prints a summary that never
contains secret material. It exits non-zero when the configuration is incoherent or when
live trading is requested without complete authorisation.

## Working with market data

### Local PostgreSQL

```bash
docker compose up -d postgres
```

The Compose credentials are fixed local-development placeholders and must never be reused
anywhere else; see the header of `docker-compose.yml`.

### Migrations

The schema is created by Alembic, never by `metadata.create_all`. Migrations read the same
`QP_DATABASE__DSN` the application does, so pointing that variable at a database is all it
takes to redirect them.

```bash
uv run alembic upgrade head      # create the Phase 2 tables
uv run alembic downgrade base    # drop them again
uv run alembic current           # show the applied revision
```

### Canonical CSV schema

One schema is supported. The header must be present and must contain every column below;
a missing column fails the whole file. Unknown extra columns are retained in the raw
record's metadata and can never alter a normalised value.

| Column | Meaning |
| --- | --- |
| `symbol` | Canonical `BASE/QUOTE` symbol, e.g. `BTC/USDT` |
| `market_type` | `spot`, `margin`, `futures` or `perpetual` |
| `timeframe` | `1m`, `5m`, `15m`, `1h`, `4h`, `1d`, … |
| `open_time` | ISO-8601 **with an explicit timezone offset** |
| `close_time` | ISO-8601 with an offset; must equal `open_time + timeframe` |
| `open` `high` `low` `close` | Strictly positive decimals |
| `volume` | Non-negative decimal |
| `trade_count` | Non-negative integer, or empty for unknown |

```csv
symbol,market_type,timeframe,open_time,close_time,open,high,low,close,volume,trade_count
BTC/USDT,spot,1h,2026-01-01T00:00:00+00:00,2026-01-01T01:00:00+00:00,50000.10,50200,49900,50100,12.5,100
```

Naive timestamps are rejected rather than assumed to be UTC. Monetary fields are parsed as
`Decimal`; `NaN` and `Infinity` are refused. The delimiter and encoding are configurable
(`QP_DATA__CSV_DELIMITER`, `QP_DATA__CSV_ENCODING`), defaulting to comma and UTF-8.

### Validating and ingesting

```bash
uv run quantplatform data validate --file data/btcusdt_1h.csv --symbol BTC/USDT --timeframe 1h
```

```bash
uv run quantplatform data ingest --file data/btcusdt_1h.csv --symbol BTC/USDT --timeframe 1h
```

```bash
uv run quantplatform data inspect --symbol BTC/USDT --timeframe 1h
```

`validate` persists nothing at all — neither bars nor a run record. `ingest` writes the
bars and the run record in a single transaction. `inspect` reports the stored bar count,
the covered range and a gap summary. All three exit `1` on a fatal outcome and `2` on a
configuration error, and none of them ever print the database DSN.

For a deliberately historical backfill, pass `--historical-end` (or set
`QP_DATA__HISTORICAL_BACKFILL_END`) so the import is judged against the moment the data was
captured rather than against today.

### Closed-candle policy

A candle is treated as final only when

```
reference_time >= close_time + QP_DATA__CLOSE_GRACE_PERIOD_SECONDS
```

`close_time` is exclusive, so an hourly candle covering 10:00–11:00 becomes actionable at
11:00 plus the grace period. The reference time is the injected clock, or the historical
end boundary when one is supplied. Open candles are rejected with an `open_candle` finding
and are never persisted, so every stored bar is by construction closed.

### Duplicate and revision policy

The natural key of a bar is **`(symbol, market_type, timeframe, open_time)`** — the four
fields that identify which instrument's candle covers which interval. `source` is
deliberately excluded, so re-ingesting identical values from a differently named source is
still recognised as the same candle rather than duplicated.

| Situation | Behaviour |
| --- | --- |
| Same key, identical OHLCV | Idempotent no-op, recorded as an `INFO` finding. Re-running an ingestion inserts nothing further. |
| Same key, different OHLCV | The stored bar is **preserved**. The incoming values are recorded in a `revised_bar` finding together with the stored ones, and are not written. |
| Same key twice within one file | The first occurrence is kept; a later conflicting row is rejected, because the pipeline has no basis for choosing between them. |

Overwriting a stored bar therefore requires an explicit revision policy, which this phase
deliberately does not provide.

### Finding severities

| Severity | Effect |
| --- | --- |
| `INFO` | Informational; ingestion continues and the record is kept. |
| `WARNING` | Quality concern; ingestion continues and the record is kept. |
| `ERROR` | The affected record is rejected; the rest of the dataset still ingests. |
| `FATAL` | The entire run fails and **no bar is persisted**, though the failed run and its findings still are. |

Gap severity is configurable (`QP_DATA__GAP_SEVERITY`, default `warning`), and a dataset
missing more than `QP_DATA__MAX_ALLOWED_GAP_BARS` bars in total escalates to `FATAL`
because it is too incomplete to trust.

## Development

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
uv run pytest
```

Integration tests run against SQLite by default so the suite needs no services. Set
`QP_TEST_DATABASE_DSN` to run the identical tests against PostgreSQL. SQLite is a test
convenience only — PostgreSQL is the production target, and the custom `ExactNumeric`
column type keeps decimal precision identical on both.

## Architecture

The platform is a hexagonal architecture. `core` holds the domain models, the ports and
pure utilities; every other package depends on `core`, and `core` depends on none of them.
The boundaries below are verified automatically by `tests/architecture`, so a violating
import fails the build rather than being caught in review.

| Package | Responsibility |
| --- | --- |
| `core` | Domain models, enums, errors, ports, clock, events, logging, ids, decimal maths |
| `config` | Typed configuration loaded from the environment |
| `data` | CSV loading, integrity validation, normalisation, ingestion service |
| `marketdata` | Live read-only exchange candle streams, reconnection, gap detection |
| `features` | Deterministic feature computation over closed bars |
| `strategies` | Strategy contract and registry |
| `risk` | Risk checks, sizing, limits, circuit breakers, idempotency |
| `portfolio` | Balances, positions, PnL, immutable snapshots |
| `execution` | Simulated, paper, shadow and live execution adapters |
| `backtesting` | Deterministic, look-ahead-free simulation |
| `research` | Offline parameter and robustness studies |
| `storage` | SQLAlchemy models, repositories, Alembic migrations |
| `orchestration` | Composition root and the runtime pipeline |
| `monitoring` | Health, circuit breakers, alerting, event publication |
| `api` | FastAPI surface |
| `cli` | Typer command line |

### Decision flow

Market data is validated, normalised and persisted; only once a candle is confirmed closed
are features computed and a strategy context built. Strategies emit signals, which
orchestration converts into order intents. Every intent passes through the risk engine,
which approves, resizes or rejects it and records each check it evaluated. Only approved
orders reach an execution adapter. Fills update portfolio accounting, local state is
reconciled against the venue, metrics and circuit breakers are evaluated, and every step
emits a structured event.

Two properties are enforced structurally rather than by convention:

- An `ApprovedOrder` can only exist inside a `RiskDecision`, and a decision carrying one is
  rejected at construction unless every recorded check passed. A strategy therefore cannot
  reach a venue without traversing the risk engine.
- `PortfolioSnapshot.equity` is derived from cash plus marked position value rather than
  stored, so the accounting identity cannot drift.

### Portfolio accounting model

There is exactly one ledger of truth for what the account owns: `Balance`. Everything else
is a view derived from it, never a competing record:

- `Balance.free` is available balance; `Balance.locked` is reserved balance; `total` is
  their sum.
- `PortfolioSnapshot.cash` is a **projection** of the quote asset's `Balance.total`, not an
  independently tracked value — enforced whenever that balance entry is present.
- `Position` is a **cost-basis overlay**, not a second asset ledger: for a spot symbol,
  `Position.quantity` equals the corresponding base-asset `Balance.total`. Maintaining that
  reconciliation after every fill is `SpotPortfolioEngine`'s job (see below); a single
  domain model cannot enforce it on its own.
- `Position.realized_pnl` is cumulative for the position's current *lifecycle* only. It
  resets to zero only when a new lifecycle begins after a prior one closed (quantity
  returned to zero); a closed lifecycle's final snapshot keeps its realized PnL forever,
  immutable like every other record in this domain.
- Aggregate fee fields (`Order.fees_paid`, `Position.fees_paid`,
  `PortfolioSnapshot.total_fees`) are always quote-asset denominated. A `Fill` may pay its
  fee in a different asset (`Fill.fee_asset`); folding a non-quote fee into a
  quote-denominated aggregate requires conversion, which this phase does not implement — see
  fee handling below.

#### Phase 3A: `SpotPortfolioEngine`

`quantplatform.portfolio.engine.SpotPortfolioEngine` implements the `PortfolioEngine` port
and applies fills that already exist — it never creates or matches orders. Scope is
deliberately narrow: spot markets, one base and one quote asset per symbol, long-only
exposure, average-cost accounting, fees denominated in the account's single quote asset.

- **Average-cost method.** Buying computes a new weighted average entry price from prior
  cost basis plus the executed notional; selling releases cost at the *existing* average
  entry price and leaves it unchanged on a partial reduction.
- **Buy-fee cost-basis treatment.** A quote-asset buy fee is folded into cost basis, so
  average entry price is `(prior cost basis + notional + fee) / new quantity` — a buy fee
  always makes the average entry price higher, never a separate expense line.
- **Sell-fee realized-PnL treatment.** A quote-asset sell fee reduces realized PnL directly:
  `realized = (notional − cost released) − fee`.
- **Idempotent fill application.** Applying a `fill_id` that was already applied is a no-op:
  every field of engine state is left unchanged and no events are emitted, rather than
  raising — the engine returns the unchanged current snapshot.
- **Unsupported fee assets.** A non-zero fee must be denominated in the portfolio's quote
  asset; any other asset is rejected. A *zero* fee in any asset is accepted (the `Fill`
  contract requires a `fee_asset` even when nothing is charged), since there is no amount to
  convert.
- **Account-level realized PnL and fees are cumulative across every lifecycle a position has
  ever had**, tracked as engine state rather than summed from currently open positions, so a
  closed lifecycle's contribution is never lost when a position later reopens.
- **No market-price reads.** The engine never queries a price source; the snapshot embedded
  in its `PortfolioUpdated` event marks every open position at its own average entry price
  (so its `unrealized_pnl` is always zero by construction), while `PortfolioEngine.snapshot()`
  accepts caller-supplied real mark prices for a genuinely marked view.

Deferred beyond Phase 3: fee-currency conversion, order/fill/position/portfolio persistence,
and restoring an open position when a process restarts.

### Phase 3B: `SimulatedBroker`

`quantplatform.execution.broker.SimulatedBroker` turns risk-approved orders into fills
against closed bars. It owns the order book — working orders, their lifecycle, their
reservations and the fills they produce — and nothing else. It never touches balances,
positions, PnL or snapshots: every fill it generates is handed to the portfolio engine,
which stays the sole accounting authority. It emits `OrderStatusChanged` and `FillReceived`,
never `PortfolioUpdated`.

**Supported.** Order types `MARKET` and `LIMIT`; sides `BUY` and `SELL`; time in force `GTC`
and `IOC`; spot markets only. Anything else is refused at submission with a specific error
(`UnsupportedOrderTypeError`, `UnsupportedTimeInForceError`, `UnsupportedMarketTypeError`).

**Matching rules.** Only closed bars are matched, in submission order, by fixed rules:

| Order | Matches when | Executes at |
| --- | --- | --- |
| Market buy / sell | Always | `bar.open`, moved by slippage |
| Limit buy | `bar.low <= limit_price` | `limit_price` |
| Limit sell | `bar.high >= limit_price` | `limit_price` |

An `IOC` order that does not fill completely on the bar it meets has its remainder cancelled
immediately; a `GTC` order stays working until it fills or is cancelled. There is no
probabilistic matching, no intrabar path reconstruction and no queue-position model: those
need order-book data this platform does not have, and inventing them would produce
confident-looking fills that nothing justifies.

**Reservation lifecycle.** Every accepted order holds funds in `Balance.locked`, which
changes availability but never ownership — `Balance.total`, positions and PnL are untouched,
so a reservation is invisible to accounting. A sell reserves its base quantity. A buy reserves
the largest quote debit it can incur: `price_cap × quantity` plus the most commission that
notional can attract, where the cap is `limit_price` for a limit buy and `max_execution_price`
for a market buy. That field is required on every approved market buy precisely so the debit
is boundable in advance — without it two market buys would each be accepted while silently
competing for the same money. A buy that would execute above its cap is cancelled rather than
filled. Reservations are released in full on cancellation and completion — including any
unused remainder of an over-reservation — and partially on a partial fill, with the
unconsumed remainder staying locked. A rejected order reserves nothing.

**Slippage** is deterministic: `OFF`, or `FIXED_BPS` moving the price against the taker.
It applies to market orders only — filling a limit order away from its limit would breach the
price the risk engine approved. **Commission** is likewise deterministic: `NONE`,
`BASIS_POINTS` of notional (`10` is 0.1 percent), or `FLAT` charged **once per order**. Flat
is per order, not per fill, because the number of partial fills a resting order will need is
unknowable when its funds are reserved, and a fee that cannot be bounded in advance cannot be
reserved without either under-holding or locking an arbitrary multiple. Both rates are
Decimal-only and capped at 10 000 basis points. The broker stamps the fee onto each `Fill`
and never aggregates it; summing fees is portfolio accounting.

**Cancellation** always traverses `OPEN`/`PARTIALLY_FILLED` → `PENDING_CANCEL` → `CANCELED`,
emitting a status event for each transition, including for an IOC remainder. A simulated venue
confirms instantly, but collapsing the two would erase the in-flight state the order contract
exists to model. A partially filled order keeps its filled quantity and average fill price
through both transitions.

**Settlement is atomic per order.** The reservation is released, the fill is handed to the
portfolio, and only then is broker state written. If the portfolio refuses the fill, the
release is compensated exactly — restoring the balance's original timestamp too — and no fill,
transition, reservation change or event survives. Because the portfolio engine has no
un-apply, atomicity is per order rather than per bar: orders that already settled in a bar
stay settled. Matching is therefore recorded **per order per bar**, so retrying a bar after a
mid-bar failure resumes at the order that failed and can never re-execute one that succeeded.

**No clock.** The broker starts at an explicit instant and advances only when a bar is
processed, taking that bar's `close_time`. Every fill, order and event timestamp comes from
there, and every identifier is derived, so a replay reproduces the run exactly. Resubmitting
a known `client_order_id` returns the existing order without reserving again; reprocessing a
completed bar is a no-op; cancelling an already-terminal order releases nothing.

**Deferred.** Real exchange APIs, websocket feeds, persistence, stop and stop-limit orders,
`FOK`, iceberg, OCO, trailing stops, leverage, margin, futures, options, order expiry, and
any execution algorithm beyond the rules above.

### Phase 4: `StandardRiskEngine`

`quantplatform.risk.engine.StandardRiskEngine` is the only component permitted to turn an
`OrderIntent` into an `ApprovedOrder`, which is what makes traversing it unavoidable on the
path from a strategy signal to a venue. It is a pure function of the intent and the
`RiskContext` it is handed: no clock, no connection, no balance mutation, nothing written.

**Outcomes.** `APPROVED` when the requested size survives untouched, `RESIZED` when a smaller
valid order remains, `REJECTED` when none does. Sizing only ever reduces — no path through
the engine increases what a strategy asked for. Rounding a quantity down onto the venue lot
grid counts as a resize, because the account is getting less than it asked for.

**Every check is evaluated**, not just the first failure, and all of them — passed, failed
and skipped — are recorded on the decision with a severity and a sequence number. "Why was
this rejected" is rarely answered well by one reason, and an operator tuning limits needs to
see which other constraints the intent was also close to. A decision is rejected if and only
if at least one *blocking* check failed; an advisory failure is recorded and does not veto.

**Evaluation order** is fixed: system state → configuration → execution mode → data freshness
→ closed candle → symbol-rules freshness → exchange health → reconciliation → API failures →
duplicate intent → pending orders → conflicting order → hourly/daily limits → drawdown →
spread → volatility → instrument and order permissions → reference price → price rounding →
market-buy cap → quantity sizing → venue bounds → balance → position count → exposure.

**Sizing.** A quantity-sized intent is rounded down to the lot step; a notional-sized intent
is converted at the valuation price first. The result is then reduced to the smallest of the
venue quantity ceiling, the per-order notional limit, what the free balance can fund, and the
exposure headroom. A buy is always valued at the *worst* price it may pay — its limit price,
or its market-buy cap — so every funding check is computed against the largest debit the
order can incur. Limit prices round away from the market: a buy limit down, a sell limit up,
so neither authorises execution at a worse price than was requested.

**Shared execution assumptions.** Fees and slippage are defined once, in
`core.models.execution_policy` (`FeePolicy`, `SlippagePolicy`, `ExecutionPolicy`), and the
*same object* is injected into both the risk configuration and the broker. Risk must fund
exactly what the broker will charge; two independently maintained copies of those numbers
drift, and the symptom is quiet — approvals the broker then refuses, or a price cap its own
slippage immediately breaches. Sharing one policy makes that unrepresentable rather than
merely discouraged.

**Market-buy cap.** `reference_price × (1 + total_buffer_bps / 10_000)`, rounded up to the
venue tick, where the total buffer is `market_buy_buffer_bps + additional_market_buy_safety_bps`.
The configuration refuses to construct if that total is below the shared policy's slippage
rate. **Spread is not added on top**: the reference price is a traded price from a closed bar,
so the spread is already inside it, and adding an allowance would count it twice. Spread is
policed separately by its own guard, which rejects when the book is too wide to trade.

**Fee funding.** A basis-point fee scales with size, so the affordable quantity divides by a
per-unit cost; a flat fee does not, so it is subtracted once before dividing. Treating a flat
fee as a rate is what breaks small orders — at a notional of 20, a flat 3 is 1500 basis
points.

**Pending orders count.** Position limits and exposure headroom are evaluated against what
*will* exist once working orders resolve, not only what exists now: `pending_buy_notional`
carries the value already committed per symbol, and a symbol pending a buy is treated as a
position about to exist. Without it, two intents evaluated between one another's fills each
see untouched headroom and are both approved, together breaching a limit neither breached
alone. Pending sells are excluded — a sale reduces exposure.

**Balance and exposure.** Only `Balance.free` is ever spendable — funds locked against a
working order are already spoken for. A sell additionally requires the open position to cover
it, and a position that disagrees with its base-asset balance is rejected on an
accounting-invariant check rather than guessed at. Exposure is checked against projected
values; a sell reduces exposure and is not constrained by those ceilings. Zero equity rejects
rather than dividing by zero.

**Idempotency.** Decisions are keyed by the intent's idempotency key. Re-evaluating a known
key returns that exact decision with no events and no recomputation, so a retry after a crash
cannot produce a second approved order for one logical intent. A key the *context* reports as
already decided elsewhere is rejected as a duplicate, since the engine cannot see what that
earlier decision authorised. Rejected intents consume no order-rate budget.

**Relationship to the rest of the platform.** The engine reads a `PortfolioSnapshot` but never
mutates it — `SpotPortfolioEngine` remains the sole accounting authority. It produces the
`ApprovedOrder` that `SimulatedBroker` is the sole consumer of, and it never submits, reserves
or fills anything itself.

**Risk-to-broker guarantee.** An `ApprovedOrder` is submittable by the broker under the same
shared policy and unchanged state — proven end to end across every fee model in
`tests/integration/test_risk_to_broker.py`. The unavoidable gap is time-of-check to
time-of-use: balances can change between evaluation and submission, so the broker still
enforces its reservation atomically and refuses a stale approval rather than overdrawing.

**Known limitations.** Order-rate counts (`approved_orders_last_hour` / `approved_orders_today`,
counting approvals and resizes but not rejections), pending-order state and drawdown peaks are
supplied by the caller in the context rather than derived by the engine, so their accuracy is
an orchestration responsibility. Volatility is a single scalar, not a term structure. There is
no per-strategy exposure breakdown.

### Phase 5: `BacktestEngine`

`quantplatform.backtesting.engine.BacktestEngine` is the platform's only orchestrator. It
connects the finished components in one fixed order and lets none of them reach around
another:

```
bars -> features -> strategy -> intents -> risk -> broker -> fills -> portfolio -> snapshot
```

**Execution is next-bar.** A decision taken from bar *N*'s close is submitted to the broker
and matched against bar *N+1*, at *N+1*'s open. The engine therefore settles the previous
bar's orders at the top of each iteration, before the current bar is decided on. Matching a
bar-*N* decision against bar *N* would fill it at a price that printed before the strategy saw
the data it decided on — look-ahead, and the most effective way to make a losing strategy look
profitable.

**Sizing lives in risk, not in the strategy.** A strategy returns opinions; orchestration
turns an entry into a request for a share of equity and an exit into a request to close what
is held; the risk engine reduces or refuses either. A strategy never sees the account.

**Determinism.** No clock is read, no random number is drawn, no global state is touched.
Timestamps come from bars, identifiers are derived, and two runs over the same data with the
same configuration produce byte-identical fills, snapshots, metrics and event sequence.

**Metrics are computable or absent.** Sharpe, Sortino, CAGR and the trade ratios report
`None` when the run cannot support them rather than a zero that reads like an answer — a
Sharpe of `0.0` from a one-bar run is worse than no number at all.

**Assumptions are stated, not defaulted.** Realised volatility is measured from the bars.
Spread is *not*: an OHLCV bar records no bid or ask, so a run states
`assumed_spread_basis_points` explicitly. Under the risk engine's strict-missing-metrics
default the engine refuses to start rather than silently rejecting every intent, and a
strategy cannot trade until enough bars exist to measure volatility at all.

**Failure behaviour.** A strategy exception, corrupt data or a violated accounting invariant
stops the run. A risk rejection or a broker refusal does not — both are ordinary outcomes and
are recorded.

**Deferred.** Live and paper trading, exchange adapters, persistence of runs, parameter
search and any form of concurrency.

### Phase 6: paper trading

`quantplatform.paper` runs the Phase 5 pipeline against a live market feed with virtual
money. The chain is unchanged — bars, features, strategy, risk, broker, portfolio — and that
is the whole design: a `PaperTradingSession` holds a `BacktestEngine` and feeds it one bar at
a time instead of a whole history at once. A second implementation of the trading logic for
the streaming case would be two things that must stay identical and would not.

**Only the market data is real.** Orders go to the simulated broker and settle into a virtual
portfolio. There is no code path from the paper package to a venue, and an architecture test
asserts it imports no network client.

**Closed candles only.** A bar is acted on once it is flagged closed *and* the injected clock
has passed its close plus a configurable grace period. A still-forming candle is refused, as
is one the session already processed — a refusal is an ordinary outcome and never stops a
session that has been running for a week.

**No wall clock.** Time comes through the `Clock` port, so a test drives a session through
days of market time in microseconds and gets the decisions a real run would make.

**Lifecycle.** `start()`, `stop()` and `resume()`. State survives a restart through the
`PaperStateRepository` port — the platform defines the port and ships only an in-memory
implementation, leaving the storage choice to a composition root. Resuming restores the
account from the stored snapshot rather than replaying history, because re-running past
decisions would assume the strategy, the feed and the venue all behave identically the second
time, which is the thing a paper run exists to test.

**Reporting.** `SessionSnapshot` for the account right now, `RuntimeMetrics` for how the
process itself is holding up (bars received, accepted, rejected; uptime; saves; restarts),
and `SessionResult` carrying the full pipeline record. Runtime health is kept separate from
trading performance on purpose: a session can be flat and profitable while quietly dropping
every third candle.

**Extension points, not implementations.** Strategy ensembles, regime detection, adaptive
selection and model-driven signals all plug in as further implementations of the existing
`FeaturePipeline` and `BaseStrategy` contracts. None require a change to the session, and
none are implemented in this phase.

### Phase 7A: real market data

`quantplatform.marketdata` connects the platform to a real exchange. It is the only door to
the outside world, and it is deliberately the narrowest one: it reads public Binance Spot
candle streams over a WebSocket and produces `MarketBar` objects. That is the entire contract.

**It cannot trade.** No order method, no balance lookup, no account or user-data stream, no
signed request, no credential. The endpoint is validated to be a public stream before a
socket opens — a URL carrying credentials, a query string, or any of Binance's account and
trading paths is refused at construction. Architecture tests assert the package imports no
signing primitive (`hmac`, `hashlib`, `base64`), handles no `SecretStr`, and defines no
operation named after a trade or an account. Making this package place an order is not a
matter of adding a call; nothing in its vocabulary can describe one.

**Nothing downstream knows it exists.** The feed satisfies `PaperMarketDataFeed`, the port
the Phase 6 session already consumed, so a live stream drops in exactly where a replay double
sat — with no adapter between them and no change to the session. `paper` does not import
`marketdata` and an architecture test keeps it that way; a composition root chooses which
implementation runs. An integration test drives the same candles through a socket and through
a direct hand-off and asserts the resulting accounts are identical.

**The transport is a port.** `CandleStreamTransport` moves text and reports failure, nothing
more. Everything that makes a live feed hard — reconnection, heartbeats, duplicate
suppression, gap detection — is logic tested deterministically against a scripted double,
because none of it could be if producing a dropped socket required actually dropping one.

**What the feed refuses, and how.** Conditions that are ordinary traffic on a healthy stream
are counted and discarded: a still-forming candle, and a closed candle republished verbatim
after a reconnect. Conditions that mean the data is wrong raise a domain error: an unparseable
frame, an impossible price, a negative volume, an interval that does not match the
subscription, a timestamp that moved backwards, and a venue revising a candle the pipeline has
already traded on. Nothing is ever repaired or clamped.

**A gap stops trading.** When a candle arrives later than the next one expected, the feed
records a `GapReport`, moves to `PAUSED` and raises. It does not skip the hole and it never
synthesises a candle to bridge one: an invented bar is indistinguishable downstream from a
real one, and every feature, signal and fill computed from it would be fiction wearing the
costume of history. Resuming requires an explicit `resynchronize()` — accepting a
discontinuity is a decision an operator should own.

**Reconnection is finite.** Exponential backoff with a ceiling and an attempt budget, no
jitter (one feed against one venue gains nothing from it and loses reproducibility). Exhausting
the budget raises rather than retrying forever, because a feed that stalls silently leaves a
session reporting an hour-old portfolio while every number it publishes stays plausible.
Continuity state survives a reconnect untouched, so the first candle after a stream returns is
judged against the last one delivered before it went away — a replay is suppressed, a stream
that skipped ahead is caught as a gap, one that rewound is refused.

**No wall clock.** Every instant and every duration comes through the `Clock` port, so a
dropped connection, an expired heartbeat and a full backoff schedule replay in microseconds
and behave exactly as they would over a real hour.

**Still simulated: execution.** Bars flow into the same simulated broker and virtual portfolio
as ever. This phase makes the market data real and nothing else.

### Execution modes

| Mode | Market data | Orders |
| --- | --- | --- |
| `backtest` | Historical, simulated clock | Simulated broker |
| `paper` | Real-time | Simulated fills |
| `shadow` | Real-time | Recorded only, nothing executed |
| `live` | Real-time | Submitted to the venue |

The same strategy implementation runs unchanged in all four: a strategy cannot observe the
execution mode, because `StrategyContext` does not carry it.

## Safety model

- **Money is `Decimal` everywhere.** Binary floats are rejected at the domain boundary
  rather than silently converted, because they cannot represent venue tick sizes exactly.
- **All timestamps are timezone-aware UTC.** Naive datetimes are rejected.
- **Live trading requires three independent signals** — `QP_EXECUTION_MODE=live`,
  `QP_LIVE_TRADING_ENABLED=true` and the exact phrase in `QP_LIVE_CONFIRMATION` — plus
  credentials and a non-development environment. The default is paper.
- **Secrets come only from the environment.** They are typed as `SecretStr`, never
  committed, and the logging filter masks both known secret values and any field whose name
  looks sensitive.
- **Idempotency keys are deterministic**, derived from the strategy, version, symbol,
  signal timestamp, action and execution mode, so a restart cannot duplicate an order.
- **A halted system never resumes trading on its own.**

Exchange API keys used for live trading must have withdrawal and transfer permissions
disabled, hold the minimum permissions needed to trade, be IP-restricted where the venue
supports it, and be distinct from paper or testnet credentials.

## Known limitations

- **The historical ingestion pipeline is still CSV-only.** Phase 7A adds live WebSocket
  candles through `marketdata`, but the `data` package that validates and persists history
  still reads local files; REST backfill and CCXT remain out of scope.
- **One CSV schema.** Arbitrary exchange CSV layouts are not auto-detected; a file must
  match the canonical header exactly.
- **Only fixed-duration timeframes.** Calendar-variable intervals (months, quarters, years)
  are not representable. The longest supported interval is a fixed seven-day week anchored
  to Monday.
- **A conflicting bar is never applied.** Correcting a genuinely revised candle requires an
  explicit revision policy, which does not yet exist; today the stored version always wins
  and the conflict is recorded for a human to resolve.
- **`quote_volume` is never populated from CSV.** The canonical schema carries no such
  column, so the field is always stored as null even though `MarketBar` supports it.
- **Whole-file ingestion.** A source is read fully into memory rather than streamed, which
  is appropriate for the file sizes this phase targets but not for multi-gigabyte archives.
- **Gap detection covers only the interior of a dataset.** A file that starts late or ends
  early is not faulted, because the data layer has no way to know the range the caller
  intended to cover.
- **A paper session holding an open position cannot resume.** The portfolio engine is
  flat-start by construction, so restoring an open position would bypass the invariant that
  keeps a position and its base balance in lockstep. The session refuses loudly rather than
  resuming into books that disagree with themselves; a seedable-portfolio contract is needed
  before this changes.
- **Recovery from a gap is manual.** The feed detects a hole, pauses and raises, but nothing
  backfills the missing candles. `resynchronize()` accepts the discontinuity and starts a
  fresh series; the bars either side of the hole are no longer one continuous history, and
  no automatic policy can know whether that is tolerable for the strategy running.
- **One venue, one timeframe per feed.** Only Binance Spot klines are implemented, and a feed
  subscribes at a single interval. Multi-venue and multi-timeframe streaming are not modelled.
- **The real WebSocket transport is not covered by tests.** `WebSocketCandleTransport` is a
  deliberately thin adapter — open, send, receive, close — precisely so the untested surface
  stays that small; everything above it runs against a scripted double. It has not been
  exercised against the live venue in this phase.
- **No historical backfill on connect.** A feed starts from the next candle the venue
  publishes. Warming a strategy's history from a REST endpoint before streaming begins is not
  implemented, so a session must accumulate its own warm-up bars.
- **Backtests hold no state between runs.** A `BacktestResult` is returned, never persisted,
  and the engine cannot resume an interrupted run.
- **One strategy per run.** Portfolio-level allocation across several strategies is not
  modelled.
- **Execution is simulated only.** `SimulatedBroker` (Phase 3B) matches orders against
  historical closed bars. No real venue is reachable and nothing is persisted.
- **Nothing yet drives the pipeline end to end.** The risk engine and the broker are wired by
  hand in tests; the orchestration loop that walks bars, computes features, runs strategies
  and assembles a `RiskContext` arrives with the backtesting engine in phase 5.
- **Fills are bar-resolution.** A fill is priced from a bar's open, high or low and stamped
  with the bar's close time. Anything that depends on where inside the bar a trade actually
  happened — queue position, partial-book walks, latency races — is not modelled.

## License

Proprietary.
