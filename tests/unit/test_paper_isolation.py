"""The invariants that decide whether a live paper session's code can change underneath it.

The test that matters most is ``test_the_live_b2_arrangement_is_reported_as_critical``: it
encodes the deployment exactly as it was found on 2026-09-28 and demands that this module call
it critical. If somebody later "simplifies" the severity ladder and that arrangement starts
reporting as advisory, this fails — which is the point, because the arrangement was harmless
only by accident and a checker that agrees with the accident is worse than none.

``test_this_module_is_not_in_the_paper_import_closure`` is the other one. A module whose job is
to police what the paper process imports must not be something the paper process imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from quantplatform.ops import (
    PAPER_COMMAND,
    Isolation,
    PaperDeployment,
    Severity,
    Violation,
    command_line,
    violations,
)
from quantplatform.ops.paper_isolation import severity_of, shares_tree, venv_for

SRC = Path(__file__).resolve().parents[2] / "src"


def deployment(**overrides: object) -> PaperDeployment:
    """Return a correctly isolated deployment, so one override isolates one violation."""
    fields: dict[str, object] = {
        "unit": "quant-paper-x.service",
        "session_id": "paper-x",
        "venv": "/opt/paper/paper-x/.venv",
        "editable_paths": (),
        "checkout": "/opt/paper/paper-x/repo",
        "checkout_writable_by_service_user": False,
        "other_units_sharing_venv": (),
        "interpreter": "/opt/python/3.13.15",
        "interpreter_writable_by_service_user": False,
        "other_paper_units_sharing_interpreter": (),
        "restart": "no",
        "has_install_section": False,
        "active": True,
    }
    return PaperDeployment(**{**fields, **overrides})  # type: ignore[arg-type]


def keys(found: tuple[Violation, ...]) -> list[Isolation]:
    return [v.invariant for v in found]


# --- The target arrangement holds everything ------------------------------------------------------


def test_a_dedicated_non_editable_read_only_deployment_has_no_violations() -> None:
    assert violations(deployment()) == ()


def test_a_stopped_session_may_have_a_writable_checkout_and_runtime() -> None:
    # Immutability is a property a session needs *while it runs*. Freezing the tree of a
    # finished session would only make the next deployment harder for no safety gained.
    assert (
        violations(
            deployment(
                active=False,
                checkout_writable_by_service_user=True,
                interpreter_writable_by_service_user=True,
            )
        )
        == ()
    )


# --- Each invariant fails on its own cause --------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"editable_paths": ("/opt/x/src",)}, Isolation.NON_EDITABLE_INSTALL),
        ({"other_units_sharing_venv": ("quant-web-x.service",)}, Isolation.DEDICATED_VENV),
        ({"checkout_writable_by_service_user": True}, Isolation.IMMUTABLE_TREE),
        (
            {"interpreter_writable_by_service_user": True},
            Isolation.INTERPRETER_NOT_WRITABLE,
        ),
        (
            {"other_paper_units_sharing_interpreter": ("quant-paper.service",)},
            Isolation.DEDICATED_INTERPRETER,
        ),
        ({"restart": "on-failure"}, Isolation.NO_AUTO_RESTART),
        ({"has_install_section": True}, Isolation.NOT_ENABLEABLE),
    ],
)
def test_each_invariant_fails_on_its_own_cause(
    overrides: dict[str, object], expected: Isolation
) -> None:
    assert keys(violations(deployment(**overrides))) == [expected]


def test_an_unknown_permission_counts_as_writable() -> None:
    # The fields default to writable on purpose. An unverified permission is not a protection,
    # and a checker that treats "I could not tell" as "safe" is a checker that lies.
    bare = PaperDeployment(
        unit="u.service", session_id="s", venv="/opt/s/.venv", checkout="/opt/s/repo"
    )

    assert Isolation.IMMUTABLE_TREE in keys(violations(bare))
    assert Isolation.INTERPRETER_NOT_WRITABLE in keys(violations(bare))


def test_a_writable_interpreter_is_reported_even_when_its_path_is_unknown() -> None:
    # The permission is the hazard, not the path. Requiring a path before complaining would let
    # a deployment the collector could not fully read pass as isolated.
    found = violations(deployment(interpreter=None, interpreter_writable_by_service_user=True))

    assert keys(found) == [Isolation.INTERPRETER_NOT_WRITABLE]
    assert "path unknown" in found[0].detail


# --- Severity is ordered by what can reach a running process --------------------------------------


def test_severity_follows_the_mechanism_not_the_likelihood() -> None:
    # Critical means a running process can load code that was not there when it started. Two
    # things do that: an editable checkout and a writable interpreter. Writing into the standard
    # library is a rarer act than a git checkout, and that difference deliberately does NOT
    # change the severity -- a severity that encodes a guess about likelihood stops being
    # readable as a statement about what can happen.
    assert severity_of(Isolation.NON_EDITABLE_INSTALL) is Severity.CRITICAL
    assert severity_of(Isolation.INTERPRETER_NOT_WRITABLE) is Severity.CRITICAL
    assert severity_of(Isolation.DEDICATED_VENV) is Severity.SERIOUS
    assert severity_of(Isolation.DEDICATED_INTERPRETER) is Severity.SERIOUS
    assert severity_of(Isolation.IMMUTABLE_TREE) is Severity.SERIOUS
    assert severity_of(Isolation.NO_AUTO_RESTART) is Severity.ADVISORY
    assert severity_of(Isolation.NOT_ENABLEABLE) is Severity.ADVISORY


def test_violations_are_reported_worst_first() -> None:
    found = violations(
        deployment(
            editable_paths=("/opt/x/src",),
            other_units_sharing_venv=("quant-web-x.service",),
            has_install_section=True,
        )
    )

    assert [v.severity for v in found] == [Severity.CRITICAL, Severity.SERIOUS, Severity.ADVISORY]


def test_the_live_b2_arrangement_is_reported_as_critical() -> None:
    # The deployment as found on the VPS: one venv, editable, serving both the paper session
    # and Mission Control, with the checkout writable. Three invariants broken at once.
    b2 = PaperDeployment(
        unit="quant-paper-b2.service",
        session_id="vps-b2-btc-4h-20260926T1911Z",
        venv="/opt/quant-platform-b2/.venv",
        editable_paths=("/opt/quant-platform-b2/src",),
        checkout="/opt/quant-platform-b2",
        checkout_writable_by_service_user=True,
        other_units_sharing_venv=("quant-web-b2.service",),
        interpreter="/home/quant/.local/share/uv/python/cpython-3.13.15-linux-x86_64-gnu",
        interpreter_writable_by_service_user=True,
        restart="no",
        has_install_section=False,
    )

    found = violations(b2)

    # Worst first, then alphabetically inside each severity. Two criticals, because the
    # interpreter under both sessions is quant-owned and mode 775 as well.
    assert keys(found) == [
        Isolation.INTERPRETER_NOT_WRITABLE,
        Isolation.NON_EDITABLE_INSTALL,
        Isolation.DEDICATED_VENV,
        Isolation.IMMUTABLE_TREE,
    ]
    assert [v.severity for v in found[:2]] == [Severity.CRITICAL, Severity.CRITICAL]


def test_the_productive_1h_arrangement_separates_its_web_tree() -> None:
    # The 1h session got this right: Mission Control runs from /opt/quant-platform-ui, a
    # different tree with its own venv. It is still editable, so still critical — but the
    # sharing that gave somebody a reason to touch B2's tree is absent here.
    assert not shares_tree(
        PaperDeployment(
            unit="quant-paper.service", session_id="1h", venv="/opt/quant-platform/.venv"
        ),
        "/opt/quant-platform-ui/.venv",
    )
    assert shares_tree(
        PaperDeployment(
            unit="quant-paper-b2.service", session_id="b2", venv="/opt/quant-platform-b2/.venv"
        ),
        "/opt/quant-platform-b2/.venv",
    )


def test_a_shared_path_prefix_is_not_a_shared_tree() -> None:
    # /opt/quant-platform and /opt/quant-platform-b2 share a string prefix and nothing else.
    assert not shares_tree(
        PaperDeployment(unit="u", session_id="s", venv="/opt/quant-platform-b2/.venv"),
        "/opt/quant-platform/.venv",
    )


# --- This checker must not become part of what it checks ------------------------------------------


def _quantplatform_imports(path: Path) -> set[str]:
    """Return the runtime quantplatform imports of one module, ignoring TYPE_CHECKING blocks."""
    out: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.guarded = 0

        def visit_If(self, node: ast.If) -> None:
            if "TYPE_CHECKING" in ast.dump(node.test):
                return  # never executed at runtime
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            out.update(a.name for a in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                out.add(node.module)
                out.update(f"{node.module}.{a.name}" for a in node.names)

    Visitor().visit(tree)
    return {name for name in out if name.startswith("quantplatform")}


def _closure(entry: str) -> set[str]:
    """Return every quantplatform module an entry point imports, transitively, at runtime."""
    seen: set[str] = set()
    queue = [entry]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        base = SRC / Path(*name.split("."))
        path = next(
            (c for c in (base.with_suffix(".py"), base / "__init__.py") if c.is_file()), None
        )
        if path is None:
            continue
        seen.add(name)
        queue.extend(_quantplatform_imports(path) - seen)
    return seen


def test_this_module_is_not_in_the_paper_import_closure() -> None:
    # Policing the paper process's import surface from inside it would be absurd, and would put
    # this checker's own dependencies into a live trading process.
    closure = _closure("quantplatform.cli.main")

    assert "quantplatform.cli.main" in closure  # the walk works
    assert not any(module.startswith("quantplatform.ops") for module in closure)


def test_the_paper_entry_point_defers_no_quantplatform_import() -> None:
    # The property that made B2's checkout move harmless, asserted so a later commit cannot
    # quietly remove it. A function-level import of a quantplatform module would let a running
    # session load code written after it started.
    deferred: list[str] = []
    for module in sorted(_closure("quantplatform.cli.main")):
        base = SRC / Path(*module.split("."))
        path = next(
            (c for c in (base.with_suffix(".py"), base / "__init__.py") if c.is_file()), None
        )
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import):
                    deferred += [
                        f"{module}:{node.name} -> {a.name}"
                        for a in inner.names
                        if a.name.startswith("quantplatform")
                    ]
                elif isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith(
                    "quantplatform"
                ):
                    deferred.append(f"{module}:{node.name} -> {inner.module}")

    assert deferred == []


# --- Which environment a unit actually runs out of ------------------------------------------------


def test_a_direct_console_script_names_its_own_venv() -> None:
    assert (
        venv_for("/opt/quant-platform-b2/.venv/bin/quantplatform paper run --fresh", "/opt/x")
        == "/opt/quant-platform-b2/.venv"
    )


def test_uv_run_resolves_to_the_working_directorys_venv_not_to_uvs_own() -> None:
    # The false pass this function exists to prevent. Reading the first path in this command
    # gives /home/quant/.local, which is where uv is installed and has no .pth files at all --
    # so the 1h session would be reported as not editable, which is the opposite of the truth.
    assert (
        venv_for("/home/quant/.local/bin/uv run quantplatform paper run", "/opt/quant-platform")
        == "/opt/quant-platform/.venv"
    )
    assert venv_for("/home/quant/.local/bin/uv run quantplatform", "/opt/x") != "/home/quant/.local"


def test_uv_run_without_a_working_directory_is_unknown_rather_than_guessed() -> None:
    assert venv_for("/home/quant/.local/bin/uv run quantplatform", None) is None
    assert venv_for("/home/quant/.local/bin/uv run quantplatform", "") is None


@pytest.mark.parametrize("command", ["", "quantplatform paper run", "relative/bin/uv run"])
def test_a_command_of_an_unrecognised_shape_is_unknown(command: str) -> None:
    assert venv_for(command, "/opt/x") is None


# --- Reading systemd's own output, not a command line ---------------------------------------------

_B2_EXEC = (
    "{ path=/opt/quant-platform-b2/.venv/bin/quantplatform ; "
    "argv[]=/opt/quant-platform-b2/.venv/bin/quantplatform paper run --fresh "
    "--session-id vps-b2-btc-4h-20260926T1911Z ; ignore_errors=no ; start_time=[n/a] ; "
    "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
)
_1H_EXEC = (
    "{ path=/home/quant/.local/bin/uv ; "
    "argv[]=/home/quant/.local/bin/uv run quantplatform paper run --strategy breakout "
    "--session-id vps-paper-riskv2-breakout-20260909T150000Z ; ignore_errors=no ; pid=0 }"
)
_ALERT_EXEC = (
    "{ path=/usr/local/lib/quant-platform/paper_failure_alert.sh ; "
    "argv[]=/usr/local/lib/quant-platform/paper_failure_alert.sh ; ignore_errors=no ; pid=0 }"
)


def test_the_command_is_taken_from_argv_not_from_path() -> None:
    # For a wrapped launcher the two differ, and `path` is the wrapper. Taking `path` would
    # describe uv rather than the session uv was asked to run.
    assert command_line(_1H_EXEC).startswith("/home/quant/.local/bin/uv run quantplatform")
    assert "paper run" in command_line(_1H_EXEC)


def test_a_plain_command_line_passes_through_unchanged() -> None:
    assert command_line("/opt/x/.venv/bin/quantplatform paper run") == (
        "/opt/x/.venv/bin/quantplatform paper run"
    )


def test_the_two_real_units_resolve_to_their_own_venvs() -> None:
    # Both shapes the host actually uses, end to end from systemd's output.
    assert (
        venv_for(command_line(_B2_EXEC), "/opt/quant-platform-b2") == "/opt/quant-platform-b2/.venv"
    )
    assert venv_for(command_line(_1H_EXEC), "/opt/quant-platform") == "/opt/quant-platform/.venv"


def test_the_failure_alert_unit_is_not_a_paper_session() -> None:
    # It matches any glob over paper units and runs a shell script. Filtering on the command
    # instead of the name is what keeps it out.
    assert PAPER_COMMAND not in command_line(_ALERT_EXEC)
    assert PAPER_COMMAND in command_line(_B2_EXEC)
    assert PAPER_COMMAND in command_line(_1H_EXEC)
