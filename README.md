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

There is no live exchange ingestion: this phase reads local CSV files only. REST clients,
WebSocket streaming and real-time ingestion are explicitly out of scope until phase 7.

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
  `Position.quantity` must equal the corresponding base-asset `Balance.total`. Maintaining
  that reconciliation after every fill is the future portfolio engine's job, not something
  a single domain model can enforce on its own.
- `Position.realized_pnl` is cumulative for the position's current *lifecycle* only. It
  resets to zero only when a new lifecycle begins after a prior one closed (quantity
  returned to zero); a closed lifecycle's final snapshot keeps its realized PnL forever,
  immutable like every other record in this domain.
- Aggregate fee fields (`Order.fees_paid`, `Position.fees_paid`,
  `PortfolioSnapshot.total_fees`) are always quote-asset denominated. A `Fill` may pay its
  fee in a different asset (`Fill.fee_asset`); converting or rejecting that fee before it
  reaches a quote-denominated aggregate is a future portfolio-engine responsibility — no
  component may silently sum fees across currencies.

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

- **No live or exchange-backed ingestion.** Only local CSV files are read. REST clients,
  WebSocket streaming, CCXT and real-time ingestion are out of scope until phase 7.
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

## License

Proprietary.
