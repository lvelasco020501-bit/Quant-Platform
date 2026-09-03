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
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.clock import SystemClock
from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.errors import QuantPlatformError
from quantplatform.core.models.market import MarketBar
from quantplatform.marketdata.symbol_rules import symbol_rules_provider_for
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import (
    ExperimentEngineFactory,
    code_revision,
    load_definition,
)
from quantplatform.research.canonical import canonical_json
from quantplatform.research.compare import COMPARISON_METRICS, compare
from quantplatform.research.folds import WalkForwardPlan, WindowSpec
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.result import ExperimentResult, ExperimentStatus
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.store import ResultStore
from quantplatform.storage.repository import SqlAlchemyMarketBarRepository
from quantplatform.storage.session import create_engine, create_session_factory
from quantplatform.strategies.registry import build_default_registry

EXIT_FATAL = 1
"""A mistake nobody's fold suffered from: nothing readable, nothing that matches, nothing run."""

EXIT_FOLD_FAILURES = 2
"""A walk-forward plan completed, but at least one fold failed on its own account."""

EXIT_PLAN_ABORTED = 3
"""A walk-forward plan stopped early because the platform lost track of its own state."""

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
    *,
    settings: Settings,
    symbol: str,
    market_type: MarketType,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
) -> tuple[MarketBar, ...]:
    """Return the closed bars stored for one symbol, in the half-open range given."""
    engine = create_engine(settings.database)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            repository = SqlAlchemyMarketBarRepository(session)
            return tuple(
                await repository.get_bars(
                    symbol=symbol,
                    market_type=market_type,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                )
            )
    finally:
        await engine.dispose()


def _engine_factory(settings: Settings) -> ExperimentEngineFactory:
    """Build the one real composition root every command below runs through."""
    return ExperimentEngineFactory(
        registry=build_default_registry(),
        features_for=features_for,
        quote_asset=settings.market.quote_asset,
    )


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
    bars = asyncio.run(
        _stored_bars(
            settings=settings,
            symbol=definition.dataset.symbol,
            market_type=definition.dataset.market_type,
            timeframe=definition.dataset.timeframe,
            start=definition.dataset.start,
            end=definition.dataset.end,
        )
    )
    result = ExperimentRunner().run(
        definition,
        bars=bars,
        factory=_engine_factory(settings),
        code_revision=code_revision(Path.cwd()),
    )
    entry = ExperimentLedger(ledger).record(result, store=ResultStore(results))
    typer.echo(json.dumps(json.loads(entry.model_dump_json()), indent=2))
    if result.status is not ExperimentStatus.SUCCEEDED:
        raise typer.Exit(code=EXIT_FATAL)


def _verify_payload(ledger: ExperimentLedger) -> dict[str, object]:
    """Return exactly what a checker reads: counts, and every contradiction found."""
    entries = ledger.entries()
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.verdict is not None:
            counts[entry.verdict.value] = counts.get(entry.verdict.value, 0) + 1
    failures = [
        entry for entry in entries if entry.status is ExperimentStatus.REPRODUCIBILITY_FAILURE
    ]
    return {
        "entries": len(entries),
        "verdicts": counts,
        "reproducibility_failures": [
            {"experiment_id": entry.experiment_id, "compared_with": entry.compared_with}
            for entry in failures
        ],
    }


def _render_verify_report(payload: dict[str, object]) -> str:
    """Render the same payload for a person reading a terminal rather than a script."""
    verdicts = payload["verdicts"]
    failures = payload["reproducibility_failures"]
    assert isinstance(verdicts, dict)  # noqa: S101 - narrows a dict[str, object] payload field
    assert isinstance(failures, list)  # noqa: S101
    lines = [f"{payload['entries']} entries"]
    lines.extend(f"  {verdict:<24} {count}" for verdict, count in verdicts.items())
    noun = "failure" if len(failures) == 1 else "failures"
    lines.append(f"{len(failures)} reproducibility {noun}")
    lines.extend(
        f"  {failure['experiment_id']}  contradicts  {failure['compared_with']}"
        for failure in failures
    )
    return "\n".join(lines)


@app.command("verify")
def verify_command(
    ledger: _LedgerOption,
    *,
    json_output: _JsonOption = False,
) -> None:
    """Report what repeating each experiment established.

    Reads the persisted record rather than re-running anything: the verdicts were reached when
    the results were recorded, and recomputing them here would let a checker disagree with the
    evidence it is meant to be reading.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when any experiment contradicted itself, so this
            can stand in a pipeline rather than only in a terminal.
    """
    payload = _verify_payload(ExperimentLedger(ledger))
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(_render_verify_report(payload))
    if payload["reproducibility_failures"]:
        raise typer.Exit(code=EXIT_FATAL)


def _resolve_attempt(selector: str, *, ledger: ExperimentLedger) -> str:
    """Return the attempt a comparison selector names.

    Three forms, checked in order: ``attempt:<id>`` names one run exactly; ``experiment:<id>``
    and a bare id — kept for what already invoked this command before selectors existed — both
    name the most recent real attempt at that experiment. ``experiment_id`` and ``attempt_id``
    are both 32-character hex digests and indistinguishable by shape, which is why the form
    has to be explicit rather than guessed.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when the selector names a run that was never
            recorded.
    """
    try:
        if selector.startswith("attempt:"):
            return ledger.entry_for_attempt(selector.removeprefix("attempt:")).attempt_id
        if selector.startswith("experiment:"):
            return ledger.latest_attempt(selector.removeprefix("experiment:")).attempt_id
        return ledger.latest_attempt(selector).attempt_id
    except LookupError as exc:
        typer.echo(f"no recorded attempt for {selector!r}: {exc}", err=True)
        raise typer.Exit(code=EXIT_FATAL) from exc


@app.command("compare")
def compare_command(
    selectors: Annotated[
        list[str],
        typer.Argument(
            help=(
                "Experiments or attempts to place side by side, as attempt:<id>, "
                "experiment:<id>, or a bare experiment id (equivalent to experiment:<id>)."
            )
        ),
    ],
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
        typer.Exit: With :data:`EXIT_FATAL` when a selector names a run that was never
            recorded, which is a question about a run that never happened.
    """
    store = ResultStore(results)
    experiment_ledger = ExperimentLedger(ledger)
    ordered: list[ExperimentResult] = [
        store.load(_resolve_attempt(selector, ledger=experiment_ledger)) for selector in selectors
    ]

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
    market_type: Annotated[
        MarketType, typer.Option("--market-type", help="Market to capture rules for.")
    ],
    out: Annotated[Path, typer.Option("--out", help="Where to write the captured rules.")],
) -> None:
    """Capture a venue's current trading rules for pinning into a definition.

    The one place research touches a live venue, and it happens when an experiment is
    *written* rather than when it is reproduced. A backtest that asked the exchange for
    today's filters would be a different run every time it repeated, and the ledger would
    report the difference as the engine's fault.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` if no provider is wired for ``market_type``, or
            if the venue has no such symbol. Neither is defaulted: a default provider would
            size a future perpetual or margin backtest against spot filters without saying
            so, and a default tick size is a wrong tick size.
    """
    try:
        provider = symbol_rules_provider_for(market_type, clock=SystemClock())
        rules = dict(provider.fetch((symbol,)))
    except QuantPlatformError as exc:
        typer.echo(f"cannot capture rules for {symbol}: {exc}", err=True)
        raise typer.Exit(code=EXIT_FATAL) from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(rules[symbol], indent=2), encoding="utf-8")
    typer.echo(f"captured {symbol} rules to {out}")


def _plan_span(plan: WalkForwardPlan) -> tuple[datetime, datetime]:
    """Return the one range covering every window in the plan."""
    return (
        min(fold.train.start for fold in plan.folds),
        max(fold.test.end for fold in plan.folds),
    )


class _InMemoryBarLoader:
    """Slices one pre-fetched span of bars per fold window.

    The plan's folds share a symbol, market and timeframe — only the window changes between
    them — so the CLI fetches the union of every window once, rather than opening a database
    connection per fold.
    """

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        self._bars = bars

    def __call__(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        window: WindowSpec,
    ) -> tuple[MarketBar, ...]:
        del symbol, market_type, timeframe
        return tuple(bar for bar in self._bars if window.contains(bar.open_time))


@app.command("walk-forward")
def walk_forward(
    plan_path: Annotated[Path, typer.Argument(help="Walk-forward plan, as JSON.")],
    definition_path: Annotated[
        Path, typer.Option("--definition", help="Base experiment definition, as JSON.")
    ],
    ledger: _LedgerOption,
    results: _ResultsOption = Path("var/research/results"),
) -> None:
    """Run every fold of a walk-forward plan through the real walk-forward runner.

    Training and test windows of every fold run the *same* configuration; nothing is fitted
    and nothing passes from one window to the next. A healthy plan demonstrates stability
    across windows, not clean out-of-sample edge — the configuration was chosen by a person
    who had already seen every window, and no partition of data undoes that.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when the plan or definition cannot be read, or
            when the plan's ``base_experiment_id`` does not name the definition it was given
            — nothing runs in either case. With :data:`EXIT_PLAN_ABORTED` when a data-integrity
            failure ends the plan before every fold ran. With :data:`EXIT_FOLD_FAILURES` when
            the plan finished but at least one fold failed on its own account.
    """
    try:
        definition = load_definition(definition_path)
        plan = WalkForwardPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"cannot read the plan or its definition: {exc}", err=True)
        raise typer.Exit(code=EXIT_FATAL) from exc

    if plan.base_experiment_id != definition.experiment_id:
        typer.echo(
            "the plan's base_experiment_id does not name the definition it was given: "
            f"{plan.base_experiment_id} != {definition.experiment_id}",
            err=True,
        )
        raise typer.Exit(code=EXIT_FATAL)

    settings = load_settings()
    span_start, span_end = _plan_span(plan)
    bars = asyncio.run(
        _stored_bars(
            settings=settings,
            symbol=definition.dataset.symbol,
            market_type=definition.dataset.market_type,
            timeframe=definition.dataset.timeframe,
            start=span_start,
            end=span_end,
        )
    )
    outcome = WalkForwardRunner().run(
        definition,
        plan,
        loader=_InMemoryBarLoader(bars),
        factory=_engine_factory(settings),
        store=ResultStore(results),
        ledger=ExperimentLedger(ledger),
        code_revision=code_revision(Path.cwd()),
    )

    if outcome.aborted:
        typer.echo(
            json.dumps(
                {
                    "plan_id": outcome.plan_id,
                    "folds_run": len(outcome.folds),
                    "aborted": True,
                    "abort_reason": outcome.abort_reason,
                    "aborted_at_fold": outcome.folds[-1].entry.fold_index
                    if outcome.folds
                    else None,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=EXIT_PLAN_ABORTED)

    summary = outcome.summarise()
    failed = [run for run in outcome.folds if run.result.status is ExperimentStatus.FAILED]
    typer.echo(
        json.dumps(
            {
                "plan_id": outcome.plan_id,
                "folds_run": len(outcome.folds),
                "aborted": False,
                "summary": json.loads(summary.model_dump_json()),
            },
            indent=2,
        )
    )
    if failed:
        raise typer.Exit(code=EXIT_FOLD_FAILURES)
