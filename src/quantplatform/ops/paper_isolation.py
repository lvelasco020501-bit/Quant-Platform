"""Is a paper session's code immune to what happens in a git checkout while it runs?

**The hazard, stated once.** A paper session is a single long-lived process. CPython resolves
every ``import`` against ``sys.path`` *at the moment the statement executes*, and caches only
what it has already imported. An editable install puts the checkout's ``src`` directory on
``sys.path`` for the life of the process, so a ``git checkout`` inside that tree re-points code
the process has not imported yet. The session then runs a mixture of two revisions, and nothing
in its logs says so.

B2 is where this was found, on 2026-09-28: the checkout was moved from ``fb0dec8`` to
``becf541`` one and three-quarter days into a live session, to pick up a Mission Control fix,
because **both units ran out of the same venv**. It happened to be harmless -- the one changed
module, ``status.brains``, is imported by ``web.server`` and by nothing in the paper closure --
but "happened to be" is the whole problem. That the live code survived was a property of which
file that commit touched, not of the arrangement.

**The fix is to delete the mechanism, not to be careful with it.** A non-editable install
copies the package into ``site-packages`` and leaves the checkout off ``sys.path`` entirely, so
the question of what a later commit touches stops being a question. Everything else here --
read-only trees, one venv per unit -- is defence in depth behind that.

**What this module does and does not do.** It reports whether a deployment holds the invariants;
it changes nothing. It also cannot *prevent* a ``git checkout``: git has no pre-checkout hook,
so there is no place to stand. Prevention is the filesystem's job (a read-only tree) and the
installer's job (not editable); detection is this module's.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from quantplatform.core.models.base import DomainModel, Text

__all__ = [
    "Isolation",
    "PaperDeployment",
    "Severity",
    "Violation",
    "venv_for",
    "violations",
]

_LAUNCHER: Final[re.Pattern[str]] = re.compile(r"^(?P<dir>/\S+)/bin/(?P<exe>[\w.-]+)")
_UV_LAUNCHERS: Final[frozenset[str]] = frozenset({"uv", "uvx"})
_ARGV: Final[re.Pattern[str]] = re.compile(r"argv\[\]=(?P<argv>.*?)\s+;")
PAPER_COMMAND: Final[str] = "paper run"
"""What makes a unit a paper session rather than a unit whose name begins the same way.

``quant-paper-alert.service`` matches any sensible glob over paper units and runs a shell
script: it is the ``OnFailure=`` handler, holds no session and has no environment to check.
Filtering on the command rather than on the name is what keeps it, and anything else added
later beside it, out of the result."""


def command_line(exec_start: str) -> str:
    """Return the command line from a systemd ``ExecStart`` property value.

    ``systemctl show`` does not print the command: it prints a record,
    ``{ path=... ; argv[]=... ; ignore_errors=no ; ... }``, and the two paths inside it differ
    for a wrapped launcher -- ``path`` is ``uv`` while ``argv[]`` carries what uv was asked to
    run. The argv is the command line; a bare string is returned unchanged so this is safe to
    apply to either form.
    """
    match = _ARGV.search(exec_start)
    return match.group("argv").strip() if match else exec_start.strip()


def venv_for(exec_start: str, working_directory: str | None) -> str | None:
    """Return the virtualenv a unit's ``ExecStart`` actually runs out of, or ``None``.

    Not as simple as reading the first path in the command, which is the mistake this function
    exists to stop making. Two shapes are in use on the host and they resolve differently:

    * ``/opt/x/.venv/bin/quantplatform`` runs the venv's own console script, so the venv is the
      grandparent of the executable.
    * ``/home/quant/.local/bin/uv run quantplatform ...`` runs ``uv``, whose own install
      directory has nothing to do with the environment it then selects: that comes from the
      unit's ``WorkingDirectory``, as ``.venv`` beside the project.

    Reading the first path for the second shape reports ``/home/quant/.local`` as the
    environment, finds no ``.pth`` files there, and concludes the session is not editable --
    a false pass on the exact question being asked. ``None`` is returned rather than a guess
    when the shape is neither of these, or when ``uv`` is used with no working directory,
    because an invented environment would be checked instead of the real one.
    """
    match = _LAUNCHER.match(exec_start.strip())
    if match is None:
        return None
    if match.group("exe") in _UV_LAUNCHERS:
        if not working_directory:
            return None
        return str(PurePosixPath(working_directory) / ".venv")
    return match.group("dir")


class Severity(StrEnum):
    """How bad a broken invariant is for a session that is running right now."""

    CRITICAL = "critical"
    """The running process can load code that was not there when it started."""

    SERIOUS = "serious"
    """The process is safe today, but one ordinary operator action makes it unsafe."""

    ADVISORY = "advisory"
    """Policy drift. Nothing can reach the process through it on its own."""


class Isolation(StrEnum):
    """What has to be true for a paper session's code to be fixed for its whole life."""

    NON_EDITABLE_INSTALL = "non_editable_install"
    """The venv must not put a source tree on ``sys.path``.

    The one invariant that actually closes the hole. With the package copied into
    ``site-packages``, a ``git checkout`` in the original tree is invisible to the process:
    verified by installing non-editably, editing the tree, and finding the imported module
    unchanged."""

    DEDICATED_VENV = "dedicated_venv"
    """No other unit may run out of the paper session's venv.

    This is the invariant B2 broke. Sharing is what gave somebody a reason to touch the paper
    tree at all -- the Mission Control fix was in the same checkout, so updating the dashboard
    meant moving the session's source. Separate venvs remove the motive as well as the means,
    and dependency work for the UI stops landing in the trading venv."""

    INTERPRETER_NOT_WRITABLE = "interpreter_not_writable"
    """The Python runtime itself must not be writable by the session's service user.

    The same hazard as an editable checkout, one layer down and found the same way. The
    interpreter's standard library is 633 files of ordinary Python, imported lazily: the paper
    entry point loads 106 top-level stdlib packages at startup and leaves **97 unloaded**, so an
    error path that reaches for ``configparser`` or ``difflib`` months into a run reads whatever
    is on disk at that moment. On the VPS that directory is ``quant``-owned and mode 775, which
    is to say the service user can rewrite the runtime of its own live process.

    Verified to cost nothing to fix: with the interpreter read-only, ``uv venv`` still works by
    version and by path, a non-editable install still works, lazy stdlib imports still work
    (the ``.pyc`` ship inside it), and the entry point still runs."""

    DEDICATED_INTERPRETER = "dedicated_interpreter"
    """No other paper session may run on the same Python runtime.

    Not a way for code to change by itself -- it is blast radius and motive. One upgrade
    performed for one session lands under every session at once, and the reason to perform it
    at all is usually the other session. Two sessions on one runtime is the same mistake as two
    units on one venv, one level further down."""

    IMMUTABLE_TREE = "immutable_tree"
    """The checkout a live session was built from must not be writable by its service user.

    Defence in depth, and the only guard that holds if somebody reinstalls editably later: with
    the tree read-only, ``git checkout`` fails on permissions before it can rewrite a file."""

    NO_AUTO_RESTART = "no_auto_restart"
    """``Restart=no``. A ``--fresh`` session that restarted would silently discard its warm-up
    and begin again at zero, and the dashboard would show a healthy session with a new clock."""

    NOT_ENABLEABLE = "not_enableable"
    """No ``[Install]`` section, so the unit is ``static`` and cannot be enabled. A reboot then
    leaves the session stopped rather than quietly starting a fresh one."""


_PREVENTS_LOADING_NEW_CODE = frozenset(
    {Isolation.NON_EDITABLE_INSTALL, Isolation.INTERPRETER_NOT_WRITABLE}
)
"""Severity is assigned by mechanism, not by how likely the trigger is.

Both of these let a running process load code that did not exist when it started, so both are
critical. The editable install was tripped by an ordinary, well-meant operator action --
updating a dashboard. Writing into the standard library needs a rarer act: a deliberate edit,
or ``uv python install --reinstall``. That difference belongs in the operating notes, not in the
severity, because a severity that encodes a guess about likelihood stops being readable as a
statement about what can happen."""

_ONE_ACTION_AWAY = frozenset(
    {Isolation.DEDICATED_VENV, Isolation.DEDICATED_INTERPRETER, Isolation.IMMUTABLE_TREE}
)


def severity_of(invariant: Isolation) -> Severity:
    """Return how urgent a broken invariant is while a session is live."""
    if invariant in _PREVENTS_LOADING_NEW_CODE:
        return Severity.CRITICAL
    if invariant in _ONE_ACTION_AWAY:
        return Severity.SERIOUS
    return Severity.ADVISORY


class PaperDeployment(DomainModel):
    """One paper session as it is installed on a host. Observed, never inferred."""

    unit: Text
    """The systemd unit, e.g. ``quant-paper-b2.service``."""

    session_id: Text
    venv: Text
    """Absolute path of the virtualenv the unit's ``ExecStart`` runs from."""

    editable_paths: tuple[str, ...] = ()
    """Source directories the venv puts on ``sys.path``, read from its ``.pth`` files. Empty is
    the goal: it means the package was copied in rather than linked to a working tree."""

    checkout: Text | None = None
    """The tree the venv was built from, when it is still on the host."""

    checkout_writable_by_service_user: bool = True
    """Whether the service user can write inside ``checkout``. Assumed true when unknown,
    because an unverified permission is not a protection."""

    other_units_sharing_venv: tuple[str, ...] = ()
    """Other units whose ``ExecStart`` lives under the same venv."""

    interpreter: Text | None = None
    """The base Python installation the venv was built from, as ``bin/python`` resolves to."""

    interpreter_writable_by_service_user: bool = True
    """Whether the service user can write inside the interpreter installation. Assumed true
    when unknown, for the same reason the checkout permission is."""

    other_paper_units_sharing_interpreter: tuple[str, ...] = ()
    """Other paper sessions running on the same Python runtime."""

    restart: Text = "no"
    has_install_section: bool = False
    active: bool = True
    """Whether the session is running. A stopped session's checkout is free to move."""


class Violation(DomainModel):
    """One broken invariant, with the evidence that broke it."""

    unit: Text
    invariant: Isolation
    severity: Severity
    detail: Text


def violations(deployment: PaperDeployment) -> tuple[Violation, ...]:
    """Return every isolation invariant this deployment does not hold, worst first.

    Read from how the deployment is installed, never from how it has behaved. A session that
    has not yet loaded a changed module is not thereby isolated from one -- it is a session
    whose luck has held, and this function is deliberately unable to tell the difference.
    """
    found: list[Violation] = []

    def fail(invariant: Isolation, detail: str) -> None:
        found.append(
            Violation(
                unit=deployment.unit,
                invariant=invariant,
                severity=severity_of(invariant),
                detail=detail,
            )
        )

    if deployment.editable_paths:
        fail(
            Isolation.NON_EDITABLE_INSTALL,
            "the venv puts a source tree on sys.path: "
            + ", ".join(deployment.editable_paths)
            + " — any import the process has not yet executed resolves against that tree",
        )
    if deployment.other_units_sharing_venv:
        fail(
            Isolation.DEDICATED_VENV,
            "shares its venv with " + ", ".join(deployment.other_units_sharing_venv),
        )
    if (
        deployment.active
        and deployment.checkout is not None
        and deployment.checkout_writable_by_service_user
    ):
        fail(
            Isolation.IMMUTABLE_TREE,
            f"{deployment.checkout} is writable while the session is live, so a git "
            "checkout, switch, pull or reset there would succeed",
        )
    if deployment.active and deployment.interpreter_writable_by_service_user:
        fail(
            Isolation.INTERPRETER_NOT_WRITABLE,
            f"the service user can write {
                deployment.interpreter or 'the interpreter (path unknown)'
            }, so a lazy standard-library import can load code written after the "
            "session started",
        )
    if deployment.other_paper_units_sharing_interpreter:
        fail(
            Isolation.DEDICATED_INTERPRETER,
            "shares its Python runtime with "
            + ", ".join(deployment.other_paper_units_sharing_interpreter),
        )
    if deployment.restart != "no":
        fail(Isolation.NO_AUTO_RESTART, f"Restart={deployment.restart}, expected Restart=no")
    if deployment.has_install_section:
        fail(
            Isolation.NOT_ENABLEABLE,
            "the unit has an [Install] section, so it can be enabled and a reboot would start "
            "a fresh session",
        )

    order = {Severity.CRITICAL: 0, Severity.SERIOUS: 1, Severity.ADVISORY: 2}
    return tuple(sorted(found, key=lambda v: (order[v.severity], v.invariant.value)))


def shares_tree(paper: PaperDeployment, web_venv: str) -> bool:
    """Return whether a Mission Control venv sits inside the paper session's own tree.

    Asked separately from :func:`violations` because it is the arrangement rather than the
    deployment: two paths, one containing the other. ``/opt/x/.venv`` and ``/opt/x-b2/.venv``
    are different trees despite the shared prefix, which is why this compares path parts and
    not strings.
    """
    paper_root = PurePosixPath(paper.venv).parent.parts
    return PurePosixPath(web_venv).parts[: len(paper_root)] == paper_root
