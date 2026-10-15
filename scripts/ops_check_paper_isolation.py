"""Check, on a host, that no live paper session can have its code changed underneath it.

Read-only. It runs ``systemctl show`` and reads files; it starts nothing, stops nothing and
writes nothing. Run it on the box, or from CI against a host over ssh:

    uv run python scripts/ops_check_paper_isolation.py
    uv run python scripts/ops_check_paper_isolation.py --json
    ssh vps 'cd /opt/ops && uv run python scripts/ops_check_paper_isolation.py'

Exit status is 1 if any invariant is broken, 2 if no paper unit could be found at all -- an
empty result is reported as a failure to look rather than as a clean bill of health.

The judgement lives in :mod:`quantplatform.ops.paper_isolation`, which is pure and tested. This
file only observes: what ``ExecStart`` runs, what the venv puts on ``sys.path``, who else runs
out of that venv, and whether the tree is writable by the service user.
"""

from __future__ import annotations

import argparse
import contextlib
import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

from quantplatform.ops import (
    PAPER_COMMAND,
    PaperDeployment,
    Severity,
    command_line,
    venv_for,
    violations,
)

PAPER_UNIT_GLOB: Final[str] = "quant-paper*.service"
UNIT_DIR: Final[Path] = Path("/etc/systemd/system")
_SESSION_ID: Final[re.Pattern[str]] = re.compile(r"--session-id[= ]+(\S+)")
_SYSTEMCTL: Final[str | None] = shutil.which("systemctl")
_PROPERTIES: Final[tuple[str, ...]] = (
    "ExecStart",
    "WorkingDirectory",
    "Restart",
    "User",
    "ActiveState",
    "UnitFileState",
)


def _show(unit: str, *properties: str) -> dict[str, str]:
    """Return systemd properties for one unit. Read-only; ``show`` never changes state."""
    if _SYSTEMCTL is None:
        return {}
    out = subprocess.run(  # noqa: S603
        [_SYSTEMCTL, "show", unit, *(f"-p{p}" for p in properties)],
        capture_output=True,
        text=True,
        check=False,
    )
    found: dict[str, str] = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            found[key] = value
    return found


def _editable_paths(venv: Path) -> tuple[str, ...]:
    """Return the source directories this venv puts on ``sys.path``.

    Read from the ``.pth`` files rather than by running the interpreter, because running it
    would execute whatever those files contain. ``_virtualenv.pth`` and coverage hooks are
    machinery rather than source trees, so they are skipped by name; anything else that names
    an existing directory is a working tree the venv is linked to.
    """
    found: list[str] = []
    for site in venv.glob("lib/python*/site-packages"):
        for pth in sorted(site.glob("*.pth")):
            if pth.name in {"_virtualenv.pth", "a1_coverage.pth", "distutils-precedence.pth"}:
                continue
            for raw in pth.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line.startswith(("import ", "#")) or not line:
                    continue
                if Path(line).is_dir():
                    found.append(line)
    return tuple(dict.fromkeys(found))


def _groups_of(user: str) -> frozenset[str]:
    """Return every group ``user`` belongs to, primary and supplementary."""
    try:
        record = pwd.getpwnam(user)
    except KeyError:
        return frozenset()
    names = {group.gr_name for group in grp.getgrall() if user in group.gr_mem}
    with contextlib.suppress(KeyError):
        names.add(grp.getgrgid(record.pw_gid).gr_name)
    return frozenset(names)


def _writable_by(path: Path, user: str) -> bool:
    """Return whether ``user`` can write inside ``path``, assuming yes when unknowable.

    Computed from ownership and mode rather than by attempting a write. An unverified
    permission is reported as writable on purpose: this check exists to find things that are
    not protected, and "I could not tell" is not protection.

    All three permission classes are read, not just the owner's. The host's shared interpreter
    is ``quant:quant`` at mode 775: owner-write alone answers it, but a tree owned by root with
    the service user's group and ``g+w`` would be just as writable and would otherwise be
    reported as safe.
    """
    try:
        info = path.stat()
    except OSError:
        return True
    mode = stat.S_IMODE(info.st_mode)
    try:
        owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        return True
    if owner == user and mode & stat.S_IWUSR:
        return True
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = None
    if group is not None and group in _groups_of(user) and mode & stat.S_IWGRP:
        return True
    return bool(mode & stat.S_IWOTH)


def _interpreter_of(venv: Path) -> Path | None:
    """Return the base Python installation a venv was built from, or ``None``.

    Resolved through ``bin/python``, which uv points at the concrete versioned directory. The
    ``pyvenv.cfg`` ``home`` key is deliberately not used: on this host it names a *floating*
    alias (``cpython-3.13-...`` symlinked to ``cpython-3.13.15-...``), so reading it would
    report a path that can be re-pointed under a running session rather than the installation
    that session is actually using.
    """
    try:
        binary = (venv / "bin" / "python").resolve(strict=True)
    except OSError:
        return None
    # <install>/bin/python3.13 -> <install>
    return binary.parent.parent if binary.parent.name == "bin" else None


def _units() -> list[str]:
    """Return every installed unit file name, so sharing can be detected across all of them."""
    try:
        return sorted(p.name for p in UNIT_DIR.glob("*.service"))
    except OSError:
        return []


def collect() -> list[PaperDeployment]:
    """Return one record per installed paper unit, read from the host as it is."""
    properties = {unit: _show(unit, *_PROPERTIES) for unit in _units()}
    # Resolved, not read off the command line: a unit launched through `uv run` names uv's own
    # install directory first, and taking that for the environment reports a session with no
    # .pth files and therefore no editable install -- a false pass on the only question that
    # matters here.
    command_of = {
        unit: command_line(props.get("ExecStart", "")) for unit, props in properties.items()
    }
    venv_of = {
        unit: venv_for(command_of[unit], props.get("WorkingDirectory"))
        for unit, props in properties.items()
    }

    found: list[PaperDeployment] = []
    for unit in sorted(UNIT_DIR.glob(PAPER_UNIT_GLOB)):
        name = unit.name
        props = properties.get(name) or _show(name, *_PROPERTIES)
        command = command_of.get(name) or command_line(props.get("ExecStart", ""))
        if PAPER_COMMAND not in command:
            continue  # named like a paper unit, but holds no session -- e.g. the OnFailure alert
        resolved = venv_of.get(name) or venv_for(command, props.get("WorkingDirectory"))
        if resolved is None:
            sys.stderr.write(
                f"{name}: cannot tell which environment ExecStart runs out of; not checked\n"
            )
            continue
        venv = Path(resolved)
        user = props.get("User") or "root"
        sharers = [
            other
            for other, other_venv in venv_of.items()
            if other != name and other_venv == resolved
        ]
        editable = _editable_paths(venv)
        interpreter = _interpreter_of(venv)
        # With an editable install the tree is known exactly; without one its provenance is not
        # recorded on the host, and guessing the venv's parent would invent a fact.
        checkout = str(Path(editable[0]).parent) if editable else None
        session = _SESSION_ID.search(command)
        found.append(
            PaperDeployment(
                unit=name,
                session_id=session.group(1) if session else "unknown",
                venv=str(venv),
                editable_paths=editable,
                checkout=checkout,
                checkout_writable_by_service_user=(
                    _writable_by(Path(checkout), user) if checkout else True
                ),
                other_units_sharing_venv=tuple(sorted(sharers)),
                interpreter=str(interpreter) if interpreter else None,
                interpreter_writable_by_service_user=(
                    _writable_by(interpreter, user) if interpreter else True
                ),
                other_paper_units_sharing_interpreter=(),  # filled in once every unit is read
                restart=props.get("Restart", "unknown"),
                has_install_section=props.get("UnitFileState") != "static",
                active=props.get("ActiveState") == "active",
            )
        )
    # Sharing is a relation between the records, so it can only be decided once they all exist.
    by_interpreter: dict[str, list[str]] = {}
    for deployment in found:
        if deployment.interpreter:
            by_interpreter.setdefault(deployment.interpreter, []).append(deployment.unit)
    return [
        deployment.model_copy(
            update={
                "other_paper_units_sharing_interpreter": tuple(
                    sorted(
                        other
                        for other in by_interpreter.get(deployment.interpreter or "", [])
                        if other != deployment.unit
                    )
                )
            }
        )
        for deployment in found
    ]


def main() -> int:
    """Report every isolation invariant the host's paper sessions do not hold."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if os.name != "posix":
        sys.stderr.write("this check reads systemd and POSIX permissions; run it on the host\n")
        return 2

    properties = {unit: _show(unit, *_PROPERTIES) for unit in _units()}
    deployments = collect()
    if not deployments:
        sys.stderr.write(
            f"no unit matching {PAPER_UNIT_GLOB} found in {UNIT_DIR}. An empty result is "
            "reported as a failure rather than as a pass: nothing was checked.\n"
        )
        return 2

    report: dict[str, Any] = {"deployments": [], "violations": []}
    worst: Severity | None = None
    for deployment in deployments:
        found = violations(deployment)
        report["deployments"].append(
            {
                "unit": deployment.unit,
                "session_id": deployment.session_id,
                "active": deployment.active,
                "venv": deployment.venv,
                "editable_paths": list(deployment.editable_paths),
                "shares_venv_with": list(deployment.other_units_sharing_venv),
                "interpreter": deployment.interpreter,
                "interpreter_writable": deployment.interpreter_writable_by_service_user,
                "shares_interpreter_with": list(deployment.other_paper_units_sharing_interpreter),
            }
        )
        for violation in found:
            report["violations"].append(
                {
                    "unit": violation.unit,
                    "invariant": violation.invariant.value,
                    "severity": violation.severity.value,
                    "detail": violation.detail,
                }
            )
            if worst is None or violation.severity is Severity.CRITICAL:
                worst = violation.severity

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        for deployment in deployments:
            state = "ACTIVE" if deployment.active else "stopped"
            shared = ", ".join(deployment.other_units_sharing_venv)
            runtime_shared = ", ".join(deployment.other_paper_units_sharing_interpreter)
            props_user = (properties.get(deployment.unit) or {}).get("User") or "the service user"
            runtime_note = (
                f"WRITABLE by {props_user}"
                if deployment.interpreter_writable_by_service_user
                else "read-only — good"
            )
            sys.stdout.write(
                f"{deployment.unit}  [{state}]  session={deployment.session_id}\n"
                f"  venv      {deployment.venv}\n"
                f"  on sys.path {', '.join(deployment.editable_paths) or '(nothing — good)'}\n"
                f"  shares venv {shared or '(nobody — good)'}\n"
                f"  interpreter {deployment.interpreter or '(unknown)'} {runtime_note}\n"
                f"  shares runtime {runtime_shared or '(nobody — good)'}\n"
            )
            for violation in violations(deployment):
                sys.stdout.write(
                    f"  {violation.severity.value.upper():8} {violation.invariant.value}\n"
                    f"           {violation.detail}\n"
                )
            sys.stdout.write("\n")
        total = len(report["violations"])
        sys.stdout.write(
            "PASS: every paper session is isolated from its checkout\n"
            if not total
            else f"FAIL: {total} broken invariant(s) across {len(deployments)} paper unit(s)\n"
        )
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
