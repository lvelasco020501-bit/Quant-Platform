# PAPER CHECKOUT = IMMUTABLE WHILE ACTIVE · WEB CHECKOUT = MUTABLE

**Status: policy, effective for every paper session started from now on. The two sessions
running on the VPS on 2026-10-14 do not satisfy it and are deliberately left alone** — see
§5. Nothing in this document was executed against a live session.

## 1. Root cause

One venv served two units.

```
ExecStart=/opt/quant-platform-b2/.venv/bin/quantplatform      ← quant-paper-b2.service
ExecStart=/opt/quant-platform-b2/.venv/bin/quantplatform-web  ← quant-web-b2.service
```

That venv is an editable install:

```
/opt/quant-platform-b2/.venv/lib/python3.13/site-packages/_editable_impl_quant_platform.pth
  → /opt/quant-platform-b2/src
```

So `/opt/quant-platform-b2/src` sits on the paper process's `sys.path` for the whole life of the
process, and the only way to update Mission Control was to move the session's source. On
**2026-09-28 13:59:56 UTC** somebody did, from `fb0dec8` to `becf541`, to pick up
`fix(status): count expected candles on the session's grid` — **one day and nineteen hours into
a live session that started 2026-09-26 19:17:28 UTC and has never restarted**.
`quant-web-b2.service` was started 13 seconds later, which is what the change was for.

The sharing is the root cause. The editable install is what made it dangerous.

## 2. Real risk

CPython resolves an `import` against `sys.path` **when the statement executes**, and caches only
what it has already imported. A long-lived process is therefore pinned to the revision it
started from **only for the modules it has already loaded**. Anything it has not yet imported
comes from whatever is on disk at that moment.

**What actually happened was harmless, and measuring why is the point.** Static analysis of the
revision on disk:

| | paper (`quantplatform.cli.main`) | web (`quantplatform.web.server`) |
|---|---|---|
| modules imported at startup | 116 | 43 |
| **deferred (function-level) `quantplatform` imports** | **0** | **0** |
| third-party imported lazily | 0 | — |
| dynamic import machinery anywhere in `src` | none (`importlib`, `__import__`, `import_module`, `entry_points`, `pkgutil` all absent) | — |

The two closures share 37 modules; 6 belong to web alone, and `status.brains` — the only `src`
file `becf541` changed — is one of them. `status/__init__.py` does not import it; only
`web/api.py` does. The paper session never loads it.

Forensics agree. `brains.py` carries the mtime of the checkout move, and its `.pyc` is
**older than its source**:

```
2026-09-28 13:59:56  src/quantplatform/status/brains.py
2026-09-26 19:12:52  src/quantplatform/status/__pycache__/brains.cpython-313.pyc
```

The paper process runs as `quant` and can write that `__pycache__`, so had it imported the
module after the move it would have rewritten the `.pyc`. It did not. (Mission Control runs
under `ProtectSystem=strict` and *cannot* write there, which is why the stale file persists.)

**So the running session is clean — and the risk is undiminished**, for three reasons:

1. **The safety is a property of one commit, not of the architecture.** Zero deferred imports is
   true of `becf541`. The next commit can add a function-level import and nothing would say so.
   That is now a test (§6) rather than a hope.
2. **The venv is shared too, and that is worse than the source tree.** A `uv sync` or
   `uv pip install` for Mission Control's dependencies rewrites `/opt/quant-platform-b2/.venv`
   underneath the live process. Third-party packages *do* import lazily as a matter of course —
   matplotlib loads backends and the font manager on first figure — so a dependency bump for the
   dashboard can reach the trading process at its next daily report.
3. **Both trees are writable by the service user** (`owner quant, mode 755`, `User=quant`), so
   `git checkout`, `switch`, `pull` and `reset` all succeed today.

The session's own result is undisturbed: PID 508556 since 2026-09-26, `NRestarts=0`, warm-up
107/400, 0 gaps, 0 missing bars, 0 runtime exceptions.

## 3. Target architecture

```
/opt/paper/<session-id>/
    repo/              pinned checkout, chmod -R a-w after install, never moved while active
    .venv/             built from repo/ with `uv pip install .`  — NOT -e
/opt/quant-platform-ui/
    (mutable)          Mission Control only, its own venv, updated whenever you like
/var/quant-platform-<session-id>/
    state logs reports the only paths the session may write
```

Three invariants, declared in `quantplatform.ops.paper_isolation` and checked by
`scripts/ops_check_paper_isolation.py`:

| invariant | severity | why that severity |
|---|---|---|
| `non_editable_install` | **critical** | the only one a *running* process can be reached through, with no further human action |
| `dedicated_venv` | serious | removes the motive as well as the means: nobody needs to touch the paper tree to update the UI |
| `immutable_tree` | serious | `git checkout` fails on permissions; holds even if somebody reinstalls editably later |
| `no_auto_restart` | advisory | `Restart=no`, or a `--fresh` session silently restarts at zero |
| `not_enableable` | advisory | no `[Install]`, so a reboot cannot start a fresh session |

**The non-editable install is the fix; everything else is defence in depth.** It was verified,
not argued — build a venv with `uv pip install .`, then edit the checkout:

```
checkout on sys.path: False
package resolves to : .../site-packages/quantplatform/__init__.py
checkout file now has marker: True
installed module sees marker: False
```

The checkout is not on `sys.path`, so what a later commit touches stops being a question worth
asking.

### Why there is no git hook

Git has no `pre-checkout`, `pre-switch` or `pre-reset` hook. There is nowhere to stand, so a
hook cannot block the dangerous operation. Prevention belongs to the filesystem (a read-only
tree) and to the installer (not editable); **detection** is what the checker does.

## 4. Safe to do now

Everything in this change is repo-side. No VPS write, no service touched.

* `src/quantplatform/ops/paper_isolation.py` — the invariants, pure and tested. **Not imported
  by the CLI, the paper runner or Mission Control**, and a test enforces that: a module whose job
  is to police the paper process's import surface has no business being in it.
* `scripts/ops_check_paper_isolation.py` — read-only collector. Runs `systemctl show`, reads
  `.pth` files and `stat`s the trees. Starts nothing, writes nothing. Exit 1 on a violation,
  **exit 2 when it finds no paper unit at all** — an empty result is a failure to look, not a
  clean bill of health.
* `deploy/vps/quant-paper-immutable.service.template` — the unit every future session uses.
* This document.

Run against the VPS as it stands today, the checker reports:

```
quant-paper.service     [ACTIVE]  CRITICAL non_editable_install
                                  SERIOUS  immutable_tree
                                  ADVISORY not_enableable
quant-paper-b2.service  [ACTIVE]  CRITICAL non_editable_install
                                  SERIOUS  dedicated_venv  (shares with quant-web-b2.service)
                                  SERIOUS  immutable_tree
FAIL: 6 broken invariant(s) across 2 paper unit(s)
```

**The 1h productive session has the same critical flaw.** It escaped the incident only because
Mission Control for it lives in a third tree, `/opt/quant-platform-ui`, with its own venv — the
arrangement B2 should have copied and did not.

## 5. Must wait for B2 to finish

**Nothing may move inside `/opt/quant-platform-b2` while that session lives.** Not the source,
not the venv, not a `git checkout`, not a rename, not a `uv sync`. The process holds that path
on `sys.path` and resolves `ExecStart` through it.

The single operating rule, and it is now enforced by the arrangement rather than by care:

> **Mission Control B2 is updated only in `/opt/quant-platform-ui-b2`.** Never in
> `/opt/quant-platform-b2`.

**Done on 2026-10-15.** `/opt/quant-platform-ui-b2` is a separate checkout pinned to `becf541`
with its own venv, installed non-editably. `quant-web-b2.service` was restarted onto it — the
only service touched — while `quant-paper-b2.service` kept PID 508556, `NRestarts=0` and its
2026-09-26 start timestamp, and the live tree's HEAD, three-line reflog, directory mtimes and
`src` SHA-256 were all unchanged with **zero** files newer than the baseline anywhere in it,
`.venv` included. The checker went from 6 broken invariants to 5: `dedicated_venv` is cleared
for B2.

The dashboard's own numbers were identical across the restart — session id, schema 2.0.0,
`HEALTHY`, `mixed_session_data: false`, warm-up 110/400, 110/110 candles, 0 gaps, 0 reconnects,
0 runtime errors, all three breakers OK — except snapshot age, last-bar age and uptime, which
must advance.

Also waiting, and why:

* Migrating the **live** B2 session to an immutable checkout is impossible without restarting it,
  and a `--fresh` restart discards 107 bars of a 400-bar warm-up. Not worth it: the measured
  exposure today is zero, and the warm-up completes around **2026-12-02**.
* The 1h session's `[Install]` section and editable install need the same treatment, and the same
  restart. It is halted on a latched `consecutive_losses` breaker, so there is a natural moment
  — but that is a decision about the experiment, not an ops change, and it is not taken here.
* Building a new checkout and venv on the VPS costs CPU and memory next to two live sessions on a
  4 GB box. Load average is 0.04 and there is headroom, but it is a deliberate act, not a
  side effect of this document. (The UI tree built on 2026-10-15 cost 346 MB and left the load
  average back at 0.04.)
* **A residual nobody has addressed: all four venvs share one uv-managed CPython** at
  `/home/quant/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu`, owned by `quant` and mode
  775. The standard library is imported lazily as a matter of course, so a
  `uv python install --reinstall` would put new stdlib code under both live sessions. Separating
  the trees does not touch this, and nor does a non-editable install of *our* package. The fix
  is a system interpreter, or an interpreter the service user cannot write; neither is done.

## 6. Guardrails added

1. **`tests/unit/test_paper_isolation.py::test_the_paper_entry_point_defers_no_quantplatform_import`**
   — walks the paper entry point's whole runtime closure and fails if any module imports a
   `quantplatform` module inside a function. This is the property that made the B2 incident
   harmless, held in place by CI instead of by luck. Confirmed to bite: injecting one deferred
   import produced
   `AssertionError: ['quantplatform.paper.runner:_probe -> quantplatform.risk.engine']`.
2. **`test_this_module_is_not_in_the_paper_import_closure`** — the checker stays out of the
   process it checks.
3. **`test_the_live_b2_arrangement_is_reported_as_critical`** — the deployment exactly as found,
   asserted to report critical. If the severity ladder is ever "simplified" so that arrangement
   reads as advisory, this fails.
4. **`scripts/ops_check_paper_isolation.py`** — run it on the host, or from CI over ssh, before
   and after any deployment work.
5. **An unverified permission counts as writable.** `PaperDeployment` defaults
   `checkout_writable_by_service_user` to `True`, and a test pins that: a checker that reports
   "I could not tell" as safe is a checker that lies.

## 7. Migration runbook — only when B2 ends by human decision

Not to be run now. B2 is mid-warm-up and must not be restarted.

```bash
# 0. PRESERVE EVIDENCE FIRST. Nothing below runs until this is copied off the box.
tar -czf ~/b2-evidence-$(date -u +%Y%m%dT%H%M%SZ).tar.gz \
    /var/quant-platform-b2/state /var/quant-platform-b2/logs /var/quant-platform-b2/reports
#    plus the revision it ran: becf541 (and fb0dec8, which it started from)

# 1. Close the session. SIGTERM winds down at the next boundary, persists, exits.
systemctl stop quant-paper-b2            # Restart=no, so it stays stopped

# 2. New session id. Never reuse one, and never "continue" a session onto different code:
#    a session is a claim about one strategy on one revision over one window.
SESSION=vps-b2-btc-4h-$(date -u +%Y%m%dT%H%MZ)
COMMIT=<the exact sha this session will run>

# 3. Immutable checkout
install -d -o quant -g quant /opt/paper/$SESSION
git clone https://github.com/lvelasco020501-bit/Quant-Platform /opt/paper/$SESSION/repo
git -C /opt/paper/$SESSION/repo checkout --detach "$COMMIT"

# 4. Venv built FROM that checkout, non-editable. The `.` and not `-e .` is the whole point.
uv venv --python 3.13 /opt/paper/$SESSION/.venv
VIRTUAL_ENV=/opt/paper/$SESSION/.venv uv pip install --compile-bytecode /opt/paper/$SESSION/repo

# 5. Freeze the tree. git checkout now fails on permissions.
chmod -R a-w /opt/paper/$SESSION/repo
chown -R root:root /opt/paper/$SESSION/repo      # not writable by the service user

# 6. Prove the isolation BEFORE starting anything.
/opt/paper/$SESSION/.venv/bin/python -c "import sys, quantplatform; \
  assert not any('/opt/paper/' in p for p in sys.path), sys.path; \
  assert 'site-packages' in quantplatform.__file__, quantplatform.__file__; print('isolated')"
ls /opt/paper/$SESSION/.venv/lib/python*/site-packages/_editable_impl_*.pth 2>/dev/null \
  && { echo 'EDITABLE INSTALL — STOP'; exit 1; }

# 7. State directories and env, then the unit from the template
install -d -o quant -g quant /var/quant-platform-$SESSION/{state,logs,reports}
# install the env file at /etc/quant-platform/$SESSION.env  (risk numbers verbatim from
# deploy/paper-b2-btc-4h.env; only session id and the three directories differ)
sed -e "s/@SESSION_ID@/$SESSION/g" -e 's/@STRATEGY@/breakout_trend/' \
    -e 's/@PARAMS@/--param entry_lookback=40 --param exit_lookback=20 --param trend_period=400/' \
    -e 's|@MARKET@|--symbol BTC/USDT --timeframe 4h|' \
    deploy/vps/quant-paper-immutable.service.template \
    > /etc/systemd/system/quant-paper-$SESSION.service
systemctl daemon-reload

# 8. Mission Control gets its OWN tree. Never the session's.
#    /opt/quant-platform-ui-b2, cloned and `uv pip install '.[web]'`, updated freely thereafter.

# 9. Check, then start a NEW session explicitly.
uv run python scripts/ops_check_paper_isolation.py      # must print PASS
systemctl start quant-paper-$SESSION
```

**Never** `git checkout` inside a live session's tree to "upgrade" it. A paper session is a
measurement of one revision; code swapped underneath it makes the measurement unattributable,
and the result would have to be thrown away rather than explained.
