# Quant Platform

A modular, exchange-agnostic and strategy-agnostic quantitative trading platform.

The first configured market is **BTC/USDT spot on the 1-hour timeframe**, but nothing in
the core is coupled to that instrument, that timeframe or any particular venue. Leverage
and short selling are prohibited by configuration, and live trading is architecturally
supported but disabled by default.

## Status

Phase 1 (foundation) is complete: domain models, ports, configuration, clocks, events,
structured logging, the exception hierarchy and the enforced dependency boundaries. Data
ingestion, portfolio accounting, risk, backtesting, strategies, execution, the API and the
CLI beyond `check-config` arrive in later phases.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/) for dependency and environment management
- PostgreSQL 16 (from phase 2 onwards; Docker Compose provides one)

## Getting started

```bash
uv sync
cp .env.example .env
uv run quantplatform check-config
```

`check-config` validates the effective configuration and prints a summary that never
contains secret material. It exits non-zero when the configuration is incoherent or when
live trading is requested without complete authorisation.

## Development

```bash
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest
```

## Architecture

The platform is a hexagonal architecture. `core` holds the domain models, the ports and
pure utilities; every other package depends on `core`, and `core` depends on none of them.
The boundaries below are verified automatically by `tests/architecture`, so a violating
import fails the build rather than being caught in review.

| Package | Responsibility |
| --- | --- |
| `core` | Domain models, enums, errors, ports, clock, events, logging, ids, decimal maths |
| `config` | Typed configuration loaded from the environment |
| `data` | Market data retrieval, integrity validation, normalisation, repositories |
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

## License

Proprietary.
