"""Running an experiment from the command line.

If an experiment cannot be run from here it will be run from a notebook, and a notebook
leaves no record — which is the one thing the ledger exists to prevent. So this is the
narrowest possible door: one definition, one result, one appended line.

The layering is the point. The command reads a definition and the data, orchestration builds
the engine, and the research runner measures. Nothing here decides anything about trading.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.clock import SystemClock
from quantplatform.core.enums import Timeframe
from quantplatform.core.errors import QuantPlatformError
from quantplatform.core.models.market import MarketBar
from quantplatform.marketdata.symbol_rules import BinanceSpotSymbolRulesProvider
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import (
    ExperimentEngineFactory,
    code_revision,
    load_definition,
)
from quantplatform.research.canonical import canonical_json
from quantplatform.research.compare import COMPARISON_METRICS, compare
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.result import ExperimentResult, ExperimentStatus
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.store import ResultStore
from quantplatform.storage.repository import SqlAlchemyMarketBarRepository
from quantplatform.storage.session import create_engine, create_session_factory
from quantplatform.strategies.registry import build_default_registry

EXIT_FATAL = 1

app = typer.Typer(
    name="research",
    help="Run recorded experiments over stored bars.",
    no_args_is_help=True,
)

_DefinitionArgument = Annotated[Path, typer.Argument(help="Experiment definition, as JSON.")]
_LedgerOption = Annotated[Path, typer.Option("--ledger", help="Append-only ledger file.")]
_ResultsOption = Annotated[
    Path, typer.Option("--results", help="Directory holding each attempt's evidence.")
]
_JsonOption = Annotated[bool, typer.Option("--json", help="Emit machine-readable output.")]


async def _stored_bars(
    *, settings: Settings, definition: ExperimentDefinition
) -> tuple[MarketBar, ...]:
    """Return the bars the definition's dataset names, in the half-open range it declares."""
    engine = create_engine(settings.database)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            repository = SqlAlchemyMarketBarRepository(session)
            return tuple(
                await repository.get_bars(
                    symbol=definition.dataset.symbol,
                    market_type=definition.dataset.market_type,
                    timeframe=Timeframe(definition.dataset.timeframe),
                    start=definition.dataset.start,
                    end=definition.dataset.end,
                )
            )
    finally:
        await engine.dispose()


@app.command("run")
def run(
    definition_path: _DefinitionArgument,
    ledger: _LedgerOption,
    results: _ResultsOption = Path("var/research/results"),
) -> None:
    """Run one experiment and append it to the ledger.

    One definition only. Sweeps, walk-forward and comparison are separate concerns and are
    deliberately not reachable from here yet: each of them multiplies the number of looks
    taken at the data, and the counting has to be trustworthy before the multiplying starts.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when the definition cannot be read, or when the
            experiment failed. A definition that cannot be read touches nothing — there is
            no experiment to record, and inventing a ledger line for one would be recording
            a run that never happened.
    """
    try:
        definition = load_definition(definition_path)
    except (OSError, ValueError) as exc:
        typer.echo(f"cannot read the experiment definition: {exc}", err=True)
        raise typer.Exit(code=EXIT_FATAL) from exc

    settings = load_settings()
    bars = asyncio.run(_stored_bars(settings=settings, definition=definition))
    factory = ExperimentEngineFactory(
        registry=build_default_registry(),
        features_for=features_for,
        quote_asset=settings.market.quote_asset,
    )
    result = ExperimentRunner().run(
        definition,
        bars=bars,
        factory=factory,
        code_revision=code_revision(Path.cwd()),
    )
    entry = ExperimentLedger(ledger).record(result, store=ResultStore(results))
    typer.echo(json.dumps(json.loads(entry.model_dump_json()), indent=2))
    if result.status is not ExperimentStatus.SUCCEEDED:
        raise typer.Exit(code=EXIT_FATAL)


@app.command("verify")
def verify_command(ledger: _LedgerOption) -> None:
    """Report what repeating each experiment established.

    Reads the persisted record rather than re-running anything: the verdicts were reached when
    the results were recorded, and recomputing them here would let a checker disagree with the
    evidence it is meant to be reading.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when any experiment contradicted itself, so this
            can stand in a pipeline rather than only in a terminal.
    """
    entries = ExperimentLedger(ledger).entries()
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.verdict is not None:
            counts[entry.verdict.value] = counts.get(entry.verdict.value, 0) + 1
    failures = [
        entry for entry in entries if entry.status is ExperimentStatus.REPRODUCIBILITY_FAILURE
    ]
    payload = {
        "entries": len(entries),
        "verdicts": counts,
        "reproducibility_failures": [
            {"experiment_id": entry.experiment_id, "compared_with": entry.compared_with}
            for entry in failures
        ],
    }
    typer.echo(json.dumps(payload, indent=2))
    if failures:
        raise typer.Exit(code=EXIT_FATAL)


@app.command("compare")
def compare_command(
    experiment_ids: Annotated[list[str], typer.Argument(help="Experiments to place side by side.")],
    ledger: _LedgerOption,
    results: _ResultsOption = Path("var/research/results"),
    *,
    json_output: _JsonOption = False,
) -> None:
    """Put results beside each other, in the order asked for.

    No ranking, no sorting by performance, no winner. The reader brought an order and gets it
    back; forming a view is their job and doing it for them is what a comparison stops being
    useful for.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when an experiment has no recorded attempt, which
            is a question about a run that never happened.
    """
    store = ResultStore(results)
    lines = ExperimentLedger(ledger).entries()
    ordered: list[ExperimentResult] = []
    for experiment_id in experiment_ids:
        attempts = [line for line in lines if line.experiment_id == experiment_id]
        if not attempts:
            typer.echo(f"no recorded attempt for experiment {experiment_id}", err=True)
            raise typer.Exit(code=EXIT_FATAL)
        ordered.append(store.load(attempts[-1].attempt_id))

    table = compare(ordered)
    if json_output:
        typer.echo(canonical_json(table, indent=2))
        return
    for row in table.rows:
        typer.echo(f"{row.name} [{row.role.value}] {row.status.value}")
        for metric in COMPARISON_METRICS:
            value = row.metrics[metric]
            typer.echo(f"  {metric:26} {'—' if value is None else value}")


@app.command("capture-rules")
def capture_rules(
    symbol: Annotated[str, typer.Option("--symbol", help="Canonical platform symbol.")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the captured rules.")],
) -> None:
    """Capture the venue's current trading rules for pinning into a definition.

    The one place research touches a live venue, and it happens when an experiment is
    *written* rather than when it is reproduced. A backtest that asked the exchange for
    today's filters would be a different run every time it repeated, and the ledger would
    report the difference as the engine's fault.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` if the venue has no such symbol. No default is
            invented: a default tick size is a wrong tick size, and an order sized against one
            is a wrong order.
    """
    provider = BinanceSpotSymbolRulesProvider(clock=SystemClock())
    try:
        rules = dict(provider.fetch((symbol,)))
    except QuantPlatformError as exc:
        typer.echo(f"cannot capture rules for {symbol}: {exc}", err=True)
        raise typer.Exit(code=EXIT_FATAL) from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(rules[symbol], indent=2), encoding="utf-8")
    typer.echo(f"captured {symbol} rules to {out}")
