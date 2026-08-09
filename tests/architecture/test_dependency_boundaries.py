"""Structural enforcement of the architecture's dependency rules.

These tests parse every source file and check its imports against the allowed dependency
table. A violating import fails the build instead of relying on review discipline, which is
what keeps the hexagon intact as the platform grows.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

PACKAGE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "src" / "quantplatform"
ROOT_PACKAGE: Final[str] = "quantplatform"

DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "api",
        "backtesting",
        "cli",
        "config",
        "core",
        "data",
        "execution",
        "features",
        "marketdata",
        "monitoring",
        "orchestration",
        "paper",
        "portfolio",
        "reporting",
        "research",
        "risk",
        "storage",
        "strategies",
    }
)

ALLOWED_DEPENDENCIES: Final[dict[str, frozenset[str]]] = {
    # The domain core depends on nothing inside the platform.
    "core": frozenset(),
    "config": frozenset({"core"}),
    # Strategies see the domain and nothing else: no configuration, no credentials, no
    # data access, no execution, no persistence.
    "strategies": frozenset({"core"}),
    "features": frozenset({"core", "config"}),
    # The data layer reaches persistence only through the repository and unit-of-work
    # ports declared in core, so it needs no dependency on storage at all. Composition
    # roots inject the SQLAlchemy implementations.
    "data": frozenset({"core", "config"}),
    # Live market data is the platform's only door to the outside world, and it is
    # deliberately the narrowest one: it sees the domain and its own configuration, and
    # nothing that could turn a read into a write.
    "marketdata": frozenset({"core", "config"}),
    "risk": frozenset({"core", "config"}),
    "portfolio": frozenset({"core", "config"}),
    # Paper trading is orchestration over the finished chain: it reuses the backtest engine
    # rather than reimplementing the pipeline, so it sees what that engine sees and nothing
    # more. Notably not storage — a paper session persists through a port, never a database.
    "paper": frozenset(
        {
            "core",
            "config",
            "backtesting",
            "execution",
            "features",
            "portfolio",
            "risk",
            "strategies",
        }
    ),
    # Reporting observes a finished session and writes files. It reads the paper session's
    # own record of itself and the backtesting metrics that record is expressed in, and
    # reaches nothing else — notably not the engines whose behaviour it describes, so there
    # is no path from a report back into a trading decision.
    "reporting": frozenset({"core", "config", "paper", "backtesting"}),
    "execution": frozenset({"core", "config"}),
    "storage": frozenset({"core", "config"}),
    "monitoring": frozenset({"core", "config"}),
    "backtesting": frozenset(
        {"core", "config", "data", "execution", "features", "portfolio", "risk", "strategies"}
    ),
    "research": frozenset(
        {"core", "config", "backtesting", "data", "features", "storage", "strategies"}
    ),
    # Composition roots may wire everything together.
    "orchestration": DOMAINS,
    "api": DOMAINS,
    "cli": DOMAINS,
}

_FORBIDDEN_ENVIRONMENT_ACCESS: Final[frozenset[str]] = frozenset({"os.environ", "os.getenv"})
_ENVIRONMENT_ACCESS_ALLOWLIST: Final[frozenset[str]] = frozenset({"config"})


def _source_files() -> tuple[Path, ...]:
    """Return every platform source file."""
    return tuple(sorted(PACKAGE_ROOT.rglob("*.py")))


def _domain_of(path: Path) -> str | None:
    """Return the top-level domain package a file belongs to, if any."""
    relative = path.relative_to(PACKAGE_ROOT)
    if len(relative.parts) == 1:
        return None
    return relative.parts[0]


def _imported_domains(path: Path) -> set[str]:
    """Return the platform domains a file imports from."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    domains: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                domains.update(_domain_from_dotted(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            domains.update(_domain_from_dotted(node.module))
    return domains


def _domain_from_dotted(module: str) -> set[str]:
    """Extract the platform domain referenced by a dotted module path."""
    parts = module.split(".")
    if parts[0] != ROOT_PACKAGE or len(parts) < 2:
        return set()
    return {parts[1]} if parts[1] in DOMAINS else set()


def test_every_declared_domain_package_exists() -> None:
    for domain in DOMAINS:
        assert (PACKAGE_ROOT / domain / "__init__.py").is_file(), domain


def test_dependency_table_covers_every_domain() -> None:
    assert set(ALLOWED_DEPENDENCIES) == set(DOMAINS)


@pytest.mark.parametrize("path", _source_files(), ids=lambda path: str(path.name))
def test_module_respects_its_dependency_budget(path: Path) -> None:
    domain = _domain_of(path)
    if domain is None:
        return
    allowed = ALLOWED_DEPENDENCIES[domain] | {domain}
    violations = sorted(_imported_domains(path) - allowed)
    assert not violations, f"{path.relative_to(PACKAGE_ROOT)} may not import {violations}"


def test_core_is_independent_of_every_other_domain() -> None:
    offenders: list[str] = []
    for path in _source_files():
        if _domain_of(path) != "core":
            continue
        if _imported_domains(path) - {"core"}:
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))
    assert not offenders


def test_strategies_cannot_reach_infrastructure() -> None:
    forbidden = DOMAINS - {"core", "strategies"}
    for path in _source_files():
        if _domain_of(path) != "strategies":
            continue
        assert not _imported_domains(path) & forbidden, path


def test_strategies_cannot_read_configuration_or_credentials() -> None:
    for path in _source_files():
        if _domain_of(path) != "strategies":
            continue
        source = path.read_text(encoding="utf-8")
        for token in ("Settings", "api_key", "api_secret", "os.environ", "getenv"):
            assert token not in source, f"{path.name} references {token}"


def test_environment_is_read_only_by_the_configuration_layer() -> None:
    offenders: list[str] = []
    for path in _source_files():
        domain = _domain_of(path)
        if domain in _ENVIRONMENT_ACCESS_ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8")
        if any(token in source for token in _FORBIDDEN_ENVIRONMENT_ACCESS):
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))
    assert not offenders


def test_no_module_uses_relative_imports() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                offenders.append(str(path.relative_to(PACKAGE_ROOT)))
                break
    assert not offenders


def test_execution_and_risk_never_import_strategies() -> None:
    for domain in ("execution", "risk", "portfolio"):
        for path in _source_files():
            if _domain_of(path) != domain:
                continue
            assert "strategies" not in _imported_domains(path), path


def test_the_platform_package_is_typed() -> None:
    assert (PACKAGE_ROOT / "py.typed").is_file()


# --- Persistence boundary -------------------------------------------------------------------

_ORM_MODULE: Final[str] = "quantplatform.storage.orm"
_ORM_ENTITY_NAMES: Final[frozenset[str]] = frozenset(
    {"MarketBarRow", "IngestionRunRow", "DataQualityFindingRow"}
)
"""Mapped classes that must never be named outside the storage package.

Matching is done over the import graph rather than raw text: a substring search would
collide with unrelated names such as ``BaseModel`` and report false violations.
"""


def test_orm_entities_never_escape_the_storage_package() -> None:
    offenders: list[str] = []
    for path in _source_files():
        if _domain_of(path) == "storage":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            imported = {alias.name for alias in node.names}
            if node.module == _ORM_MODULE or imported & _ORM_ENTITY_NAMES:
                offenders.append(str(path.relative_to(PACKAGE_ROOT)))
                break
    assert not offenders, f"ORM entities leaked outside storage: {offenders}"


def test_only_storage_imports_sqlalchemy() -> None:
    allowed = {"storage"}
    offenders: list[str] = []
    for path in _source_files():
        if _domain_of(path) in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imported_root_modules(node)
            if "sqlalchemy" in imported:
                offenders.append(str(path.relative_to(PACKAGE_ROOT)))
                break
    assert not offenders, f"sqlalchemy imported outside storage: {offenders}"


def test_the_data_layer_does_not_depend_on_storage() -> None:
    for path in _source_files():
        if _domain_of(path) != "data":
            continue
        assert "storage" not in _imported_domains(path), path


def _imported_root_modules(node: ast.AST) -> set[str]:
    """Return the top-level third-party modules an import node references."""
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
        return {node.module.split(".")[0]}
    return set()


# --- Risk / execution isolation ---------------------------------------------------------------

_SHARED_EXECUTION_POLICY_MODULE: Final[str] = "quantplatform.core.models.execution_policy"
"""Where the fee and slippage formulas shared by risk and execution must live.

Both packages need identical execution assumptions — the risk engine funds exactly what the
broker charges — but neither may import the other. The only way to share the definition is a
neutral layer both already depend on, which is ``core``.
"""


def test_risk_and_execution_never_import_each_other() -> None:
    # Enforced generically by the dependency budget, but stated explicitly because the reason
    # matters: the two agree on execution costs through a shared core contract, never by one
    # reaching into the other.
    for domain, forbidden in (("risk", "execution"), ("execution", "risk")):
        for path in _source_files():
            if _domain_of(path) != domain:
                continue
            assert forbidden not in _imported_domains(path), (
                f"{path.relative_to(PACKAGE_ROOT)} may not import {forbidden}"
            )


def test_shared_execution_assumptions_live_in_core() -> None:
    module_path = PACKAGE_ROOT / "core" / "models" / "execution_policy.py"
    assert module_path.is_file(), "the shared execution policy must live in core"


def test_risk_and_execution_both_consume_the_shared_policy() -> None:
    # A shared contract nothing imports is not shared. Both sides must actually reach for it,
    # which is what makes fee and slippage drift between them unrepresentable.
    importers: set[str] = set()
    for path in _source_files():
        domain = _domain_of(path)
        if domain not in {"risk", "execution"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == _SHARED_EXECUTION_POLICY_MODULE:
                importers.add(domain)
    assert importers == {"risk", "execution"}, (
        f"both risk and execution must consume the shared execution policy; got {importers}"
    )


# --- Backtesting boundaries ----------------------------------------------------------------

_ORCHESTRATION_FORBIDDEN: Final[frozenset[str]] = frozenset({"storage", "api", "cli"})
"""Packages the backtest engine must never reach for.

Orchestration composes the domain; it does not persist, serve or parse. Reaching into
storage in particular would make a backtest depend on a database being present, which is the
opposite of the reproducible-from-inputs property the engine exists to provide.
"""


def test_backtesting_never_imports_infrastructure() -> None:
    for path in _source_files():
        if _domain_of(path) != "backtesting":
            continue
        imported = _imported_domains(path)
        forbidden = sorted(imported & _ORCHESTRATION_FORBIDDEN)
        assert not forbidden, f"{path.relative_to(PACKAGE_ROOT)} may not import {forbidden}"


def test_backtesting_never_imports_sqlalchemy_or_alembic() -> None:
    for path in _source_files():
        if _domain_of(path) != "backtesting":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            roots = _imported_root_modules(node)
            assert not roots & {"sqlalchemy", "alembic"}, path


def test_backtesting_is_the_only_package_that_composes_the_whole_chain() -> None:
    # Every other domain sees a strict subset. If a second package started importing strategy,
    # risk and execution together, there would be two orchestrators and no single answer to
    # "what order do things happen in".
    composers = set()
    for path in _source_files():
        domain = _domain_of(path)
        if domain in {"orchestration", "api", "cli"} or domain is None:
            continue
        imported = _imported_domains(path)
        if {"strategies", "risk", "execution"} <= imported:
            composers.add(domain)
    assert composers <= {"backtesting"}, f"more than one orchestrator: {sorted(composers)}"


def test_strategies_cannot_reach_the_orchestrator() -> None:
    # The strategy contract's whole point: it cannot see the account, the venue or the engine.
    for path in _source_files():
        if _domain_of(path) != "strategies":
            continue
        imported = _imported_domains(path)
        assert "backtesting" not in imported, path
        assert "portfolio" not in imported, path
        assert "execution" not in imported, path
        assert "risk" not in imported, path


def test_features_depend_only_on_the_domain_and_configuration() -> None:
    for path in _source_files():
        if _domain_of(path) != "features":
            continue
        assert _imported_domains(path) <= {"core", "config", "features"}, path


# --- Paper trading boundaries ----------------------------------------------------------------

_PAPER_FORBIDDEN: Final[frozenset[str]] = frozenset({"storage", "api", "cli"})
"""Packages a paper session must never reach for.

Persistence goes through :class:`~quantplatform.core.interfaces.PaperStateRepository`, so the
session never learns what a database is. A session that imported storage directly would make
a paper run depend on one being present — and a paper run's whole value is that it is the
production chain with only the market data made real.
"""


def test_paper_never_imports_infrastructure() -> None:
    for path in _source_files():
        if _domain_of(path) != "paper":
            continue
        forbidden = sorted(_imported_domains(path) & _PAPER_FORBIDDEN)
        assert not forbidden, f"{path.relative_to(PACKAGE_ROOT)} may not import {forbidden}"


def test_paper_never_imports_a_network_client() -> None:
    # The feed is a port. A paper session that opened its own socket would be an exchange
    # adapter wearing a session's name, and the "no real orders" guarantee would rest on
    # nobody having added a submit call yet.
    for path in _source_files():
        if _domain_of(path) != "paper":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            roots = _imported_root_modules(node)
            assert not roots & {"requests", "httpx", "aiohttp", "websockets", "ccxt", "urllib"}, (
                path
            )


def test_paper_reuses_the_pipeline_rather_than_reimplementing_it() -> None:
    # The session must go through the backtest engine. If it stopped importing it, the two
    # modes would have separate trading logic and paper would stop proving anything about
    # what a backtest predicted.
    session = PACKAGE_ROOT / "paper" / "session.py"
    assert "backtesting" in _imported_domains(session)


def test_the_paper_state_port_has_no_implementation_in_the_platform() -> None:
    # Phase 6 defines the persistence port and deliberately ships no durable implementation.
    # A SQL or key-value store appearing here would be a storage decision made inside the
    # trading layer.
    for path in _source_files():
        if _domain_of(path) != "paper":
            continue
        source = path.read_text(encoding="utf-8")
        for token in ("sqlalchemy", "psycopg", "redis", "sqlite3"):
            assert token not in source, f"{path.name} references {token}"


# --- Market data boundaries ---------------------------------------------------------------------

_MARKETDATA_ALLOWED: Final[frozenset[str]] = frozenset({"core", "config", "marketdata"})

_NETWORK_CLIENTS: Final[frozenset[str]] = frozenset(
    {"requests", "httpx", "aiohttp", "ccxt", "http", "socket", "ftplib", "telnetlib"}
)
"""Ways to reach the network that the market-data layer must not use.

``websockets`` is absent deliberately — it is the one client this package *is*, and a
separate test pins it to this package alone.
"""

_SIGNING_PRIMITIVES: Final[frozenset[str]] = frozenset({"hmac", "hashlib", "base64"})
"""Everything needed to sign an authenticated venue request.

Public market-data streams require no signature. A package that cannot import these
cannot authenticate, which makes "read-only" a property of the import graph rather than a
claim in a docstring.
"""

_TRADING_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "submit",
        "cancel",
        "place_order",
        "create_order",
        "amend_order",
        "open_orders",
        "fetch_balances",
        "fetch_fills",
        "withdraw",
        "transfer",
        "authenticate",
        "sign_request",
        "get_account",
        "user_data_stream",
    }
)
"""Operation names that would mean this package had grown a trading or account surface."""

_WALL_CLOCK_READS: Final[tuple[str, ...]] = (
    "datetime.now(",
    "time.time(",
    "utcnow(",
    "date.today(",
    "time.monotonic(",
)


def _defined_names(path: Path) -> set[str]:
    """Return every function and method name a module defines."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _marketdata_files() -> tuple[Path, ...]:
    return tuple(path for path in _source_files() if _domain_of(path) == "marketdata")


def test_marketdata_depends_only_on_the_domain_and_configuration() -> None:
    for path in _marketdata_files():
        violations = sorted(_imported_domains(path) - _MARKETDATA_ALLOWED)
        assert not violations, f"{path.relative_to(PACKAGE_ROOT)} may not import {violations}"


def test_marketdata_never_reaches_the_trading_chain() -> None:
    # Stated explicitly although the budget already enforces it, because the direction is
    # the whole point: the feed is below the pipeline and must never learn what a strategy,
    # a risk decision, a broker or a portfolio is.
    forbidden = {"risk", "execution", "portfolio", "strategies", "backtesting", "paper"}
    for path in _marketdata_files():
        assert not _imported_domains(path) & forbidden, path


def test_marketdata_is_the_only_package_that_speaks_websocket() -> None:
    # One door to the outside world. A second package opening a socket would be a second
    # place where the platform's read-only guarantee has to be re-established.
    offenders: list[str] = []
    for path in _source_files():
        if _domain_of(path) == "marketdata":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if "websockets" in _imported_root_modules(node):
                offenders.append(str(path.relative_to(PACKAGE_ROOT)))
                break
    assert not offenders, f"websockets imported outside marketdata: {offenders}"


def test_marketdata_uses_no_other_network_client() -> None:
    for path in _marketdata_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            roots = _imported_root_modules(node) & _NETWORK_CLIENTS
            assert not roots, f"{path.name} may not import {sorted(roots)}"


def test_marketdata_cannot_sign_an_authenticated_request() -> None:
    for path in _marketdata_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            roots = _imported_root_modules(node) & _SIGNING_PRIMITIVES
            assert not roots, f"{path.name} may not import {sorted(roots)}"


def test_marketdata_never_handles_a_secret() -> None:
    for path in _marketdata_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported = {alias.name for alias in node.names}
            assert "SecretStr" not in imported, f"{path.name} imports SecretStr"
            assert "Settings" not in imported, f"{path.name} imports Settings"


def test_marketdata_declares_no_trading_or_account_operation() -> None:
    # Read-only enforced by vocabulary. The package cannot place a trade because nothing in
    # it can name one; adding the capability would mean adding the concept first.
    for path in _marketdata_files():
        offenders = sorted(_defined_names(path) & _TRADING_OPERATIONS)
        assert not offenders, f"{path.name} defines {offenders}"


def test_marketdata_never_imports_sqlalchemy_or_alembic() -> None:
    for path in _marketdata_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            roots = _imported_root_modules(node)
            assert not roots & {"sqlalchemy", "alembic", "psycopg", "redis"}, path


def test_marketdata_reads_no_wall_clock() -> None:
    # Every instant and every duration comes through the injected clock, which is what lets
    # a dropped connection, an expired heartbeat and a full backoff schedule be replayed in
    # microseconds and behave exactly as they would over a real hour.
    for path in _marketdata_files():
        source = path.read_text(encoding="utf-8")
        for token in _WALL_CLOCK_READS:
            assert token not in source, f"{path.name} reads the wall clock via {token}"


# --- Reporting boundaries -------------------------------------------------------------------

_REPORTING_ALLOWED: Final[frozenset[str]] = frozenset(
    {"core", "config", "paper", "backtesting", "reporting"}
)

_REPORTING_FORBIDDEN: Final[frozenset[str]] = frozenset(
    {"execution", "portfolio", "risk", "strategies", "marketdata", "storage", "data", "api", "cli"}
)
"""Packages a report must never reach for.

Reporting describes what the engines did; it must not be able to call one. Reading a
:class:`~quantplatform.paper.results.SessionResult` is enough to say what happened, and
anything more would put an observer inside the thing it is observing.
"""

_REPORTING_MUTATORS: Final[frozenset[str]] = frozenset(
    {
        "submit",
        "cancel",
        "apply_fill",
        "reserve",
        "release",
        "evaluate",
        "generate",
        "advance",
        "begin",
        "run",
    }
)
"""Pipeline verbs. A report that defined one would be driving rather than observing."""


def _reporting_files() -> tuple[Path, ...]:
    return tuple(path for path in _source_files() if _domain_of(path) == "reporting")


def test_reporting_depends_only_on_the_session_record_and_the_domain() -> None:
    for path in _reporting_files():
        violations = sorted(_imported_domains(path) - _REPORTING_ALLOWED)
        assert not violations, f"{path.relative_to(PACKAGE_ROOT)} may not import {violations}"


def test_reporting_never_reaches_an_engine_it_describes() -> None:
    for path in _reporting_files():
        forbidden = sorted(_imported_domains(path) & _REPORTING_FORBIDDEN)
        assert not forbidden, f"{path.relative_to(PACKAGE_ROOT)} may not import {forbidden}"


def test_reporting_never_imports_sqlalchemy_or_a_network_client() -> None:
    # A report is a file on disk. Reaching a database or a venue from here would make
    # observing a session something that can fail in the ways trading fails.
    for path in _reporting_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            roots = _imported_root_modules(node)
            forbidden = roots & {
                "sqlalchemy",
                "alembic",
                "psycopg",
                "redis",
                "requests",
                "httpx",
                "aiohttp",
                "websockets",
                "ccxt",
            }
            assert not forbidden, f"{path.name} may not import {sorted(forbidden)}"


def test_reporting_defines_no_pipeline_operation() -> None:
    # Observation enforced by vocabulary: the package cannot drive the chain because
    # nothing in it can name a step of the chain.
    for path in _reporting_files():
        offenders = sorted(_defined_names(path) & _REPORTING_MUTATORS)
        assert not offenders, f"{path.name} defines {offenders}"


def test_nothing_the_platform_trades_with_imports_reporting() -> None:
    # The direction that matters. If paper, backtesting, risk, execution or portfolio could
    # import reporting there would be a path from a report back into a decision, and every
    # report would become a description partly of itself.
    for path in _source_files():
        domain = _domain_of(path)
        if domain in {"orchestration", "api", "cli", "reporting"} or domain is None:
            continue
        assert "reporting" not in _imported_domains(path), (
            f"{path.relative_to(PACKAGE_ROOT)} may not import reporting"
        )


def test_the_day_rollover_observer_is_the_only_seam_into_the_session() -> None:
    # Phase 7B adds exactly one hook to a committed session, and it is a port the session
    # calls rather than a dependency it acquires.
    session = PACKAGE_ROOT / "paper" / "session.py"
    assert "DayRolloverObserver" in session.read_text(encoding="utf-8")
    assert "reporting" not in _imported_domains(session)


def test_paper_consumes_the_live_feed_through_the_port_not_by_importing_it() -> None:
    # Phase 7A adds a real feed without the paper session changing at all, because the
    # session was always written against PaperMarketDataFeed. Keeping paper free of any
    # import of marketdata is what proves the substitution is genuine rather than a rewrite:
    # a CSV replay, a recorded double and a live socket remain interchangeable, and the
    # composition root decides which one runs.
    for path in _source_files():
        if _domain_of(path) != "paper":
            continue
        assert "marketdata" not in _imported_domains(path), path
