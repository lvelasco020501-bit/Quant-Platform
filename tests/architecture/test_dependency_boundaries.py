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
        "monitoring",
        "orchestration",
        "portfolio",
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
    "data": frozenset({"core", "config", "storage"}),
    "risk": frozenset({"core", "config"}),
    "portfolio": frozenset({"core", "config"}),
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
