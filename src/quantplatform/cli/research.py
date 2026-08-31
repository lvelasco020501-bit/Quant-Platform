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
from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.core.symbol_rules import SymbolRulesStore
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import (
    ExperimentEngineFactory,
    code_revision,
    load_definition,
)
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import ExperimentRunner
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
                    market_type=MarketType.SPOT,
                    timeframe=Timeframe(definition.dataset.timeframe),
                    start=definition.dataset.start,
                    end=definition.dataset.end,
                )
            )
    finally:
        await engine.dispose()


@app.command("run")
def run(definition_path: _DefinitionArgument, ledger: _LedgerOption) -> None:
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
        symbols=SymbolRulesStore({}),
        quote_asset=settings.market.quote_asset,
    )
    result = ExperimentRunner().run(
        definition,
        bars=bars,
        factory=factory,
        code_revision=code_revision(Path.cwd()),
    )
    entry = ExperimentLedger(ledger).record(result)
    typer.echo(json.dumps(json.loads(entry.model_dump_json()), indent=2))
    if result.status is not ExperimentStatus.SUCCEEDED:
        raise typer.Exit(code=EXIT_FATAL)
