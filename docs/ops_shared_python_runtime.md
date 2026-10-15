# The Python runtime a paper session runs on must not be writable by its own service user

**Status: audited 2026-10-15, read-only. Nothing was changed.** The current runtime is untouched:
no `chmod`, no `chown`, no `uv python install`, no venv modified, no service restarted. What
follows is the scope of the risk, the recommended architecture, and the migration — to be run
only when each session closes.

Companion to [`ops_immutable_paper_checkout.md`](ops_immutable_paper_checkout.md), which closed
the same hazard one layer up. This one was found while verifying that fix.

## 1. Scope of the risk

### What is shared

**One interpreter, under every venv on the host.**

```
/home/quant/.local/share/uv/python/
    cpython-3.13.15-linux-x86_64-gnu/          drwxrwxr-x  quant:quant   ← the real installation
    cpython-3.13-linux-x86_64-gnu -> cpython-3.13.15-...   lrwxrwxrwx quant:quant  ← floating alias
```

| venv | `pyvenv.cfg home` | `bin/python` resolves to |
|---|---|---|
| `/opt/quant-platform/.venv` | the floating alias | `cpython-3.13.15-…/bin/python3.13` |
| `/opt/quant-platform-b2/.venv` | the floating alias | the same |
| `/opt/quant-platform-ui/.venv` | the floating alias | the same |
| `/opt/quant-platform-ui-b2/.venv` | the floating alias | the same |

Affected units: **`quant-paper.service`** (1h, session `vps-paper-riskv2-breakout-20260909T150000Z`,
MainPID 143200 which is `uv`, with the Python child at **143203**) and
**`quant-paper-b2.service`** (4h, `vps-b2-btc-4h-20260926T1911Z`, PID 508556). Both dashboards
too, which matters less — they hold no financial state.

### The permissions, in full

```
drwxr-x---  quant:quant  /home/quant
drwxrwxr-x  quant:quant  /home/quant/.local/share/uv/python
drwxrwxr-x  quant:quant  …/cpython-3.13.15-linux-x86_64-gnu
drwxrwxr-x  quant:quant  …/cpython-3.13.15-linux-x86_64-gnu/lib/python3.13
-rwxrwxr-x  quant:quant  …/cpython-3.13.15-linux-x86_64-gnu/bin/python3.13
-rwxr-xr-x  quant:quant  /home/quant/.local/bin/uv
```

Checked by asking, not by reading the bits: as `quant`, `test -w` succeeds on the interpreter
root, on the standard library directory **and on the interpreter binary itself**. `quant` can
also replace the floating alias symlink. Both sessions run as `User=quant`.

### What can be imported lazily

This is the part that decides whether the risk is real, so it was measured rather than assumed.

| | |
|---|---|
| stdlib `.py` files in the installation | **633** |
| stdlib top-level packages the paper entry point loads at startup | **106** |
| **stdlib top-level packages still unloaded afterwards** | **97** |
| extension modules in `lib-dynload` | **2** (`_dbm`, `_tkinter`) |

Two findings, one of which cuts against the alarming reading:

1. **`sys.path` holds the *concrete* version for the standard library**, and the floating alias
   only for `lib-dynload`. So re-pointing the alias — what `uv python install 3.13.16` would do
   — cannot move a running session's stdlib. And `lib-dynload` holds just two modules, because
   this uv build links the usual extension modules into the binary. **The alias is not the
   hazard.**
2. **The hazard is the write permission on the concrete installation.** 97 unloaded stdlib
   packages sit in a directory the service user can rewrite, and Python resolves each one from
   disk at the moment it is first imported. The realistic trigger is not application code
   reaching for `ftplib`; it is an error path inside a dependency — traceback formatting, a
   warning, a `pprint` of a failed model — months into a run.

So the mechanism is identical to the editable checkout: **a live process can load code that did
not exist when it started.** The difference is the act required. An editable checkout was tripped
by an ordinary, well-meant operator action; writing into the standard library needs a deliberate
edit or `uv python install --reinstall`. That difference is recorded here and deliberately **not**
encoded in the severity — a severity that mixes in a guess about likelihood stops being readable
as a statement about what can happen.

### Would changing the permissions break anything running now?

Asked before recommending anything, and answered by building a read-only interpreter and using
it, rather than by reasoning about it:

| | result |
|---|---|
| `uv venv` against a read-only interpreter, **by explicit path** | works |
| `uv venv --python 3.13`, **by version discovery**, read-only install dir | works |
| `uv pip install --compile-bytecode <project>` (non-editable) into that venv | works |
| **lazy stdlib imports** (`configparser`, `ftplib`, `getpass`, `difflib`, `pprint`, `doctest`, `gzip`, `tarfile`) after startup | works — the `.pyc` ship inside the installation, so nothing needs to be compiled or written |
| the `quantplatform` entry point | runs |
| isolation of a non-editable install | holds — no source tree on `sys.path` |

Running processes keep their mapped binary regardless, and reading `.py`/`.pyc` needs only read
permission. **Nothing that the platform does requires write access to the interpreter.** What
read-only blocks is exactly `uv python install`/`reinstall`/`upgrade` as `quant`, and any
in-place edit — which is the entire point.

Two cautions that remain true: the installation must not be **moved or renamed**, because every
`pyvenv.cfg` and every `.venv/bin/python` symlink names its path; and the change must be applied
to the concrete directory, not only the alias.

## 2. Architecture for future sessions

| | A. system Python | B. root-owned CPython per version | C. the uv-managed one, root-owned and read-only |
|---|---|---|---|
| 3.13 availability | needs a PPA or a build (Ubuntu 24.04 ships 3.12) | provision per version | **already installed** |
| writable by `quant` | no | no | no, after `chown`/`chmod` |
| migration cost | new interpreter, every venv rebuilt | new interpreter, every venv rebuilt | **none — same path, same version** |
| existing `pyvenv.cfg` paths | all invalid | all invalid | **all still valid** |
| upgrades | **`apt` replaces the stdlib under live sessions, unattended** | additive: a new directory, old sessions keep theirs | additive, same |
| verified working | — | — | **yes, end to end** |

**Recommendation: C.** It is the simplest and the safest at once, which is unusual enough to say
plainly: two commands, no path changes, no download, no rebuild, and every property wanted here
was measured rather than argued. A is actively worse than the status quo — it swaps a hazard that
needs a deliberate human act for one that `unattended-upgrades` performs on its own schedule,
under a live session. B is C plus provisioning, and it is the right **pattern for the next
version**: when 3.14 or a 3.13 point release is eventually needed, install it root-owned beside
the current one at `/opt/python/<version>` and point only new sessions at it. Old sessions keep
the interpreter they started on, which is the property that matters.

Target state, with the two layers this document and its companion cover:

```
/opt/python/<version>/              root:root, a-w   ← interpreter, one per version, never moved
/opt/paper/<session-id>/repo/       root:root, a-w   ← pinned checkout
/opt/paper/<session-id>/.venv/                       ← built against the CONCRETE version path,
                                                        quantplatform installed NON-editably
/opt/quant-platform-ui*/            quant, mutable   ← web, separate tree and venv
/var/quant-platform-<session-id>/   quant, writable  ← the only paths a session writes
```

And one refinement the audit produced: **pin new venvs to the concrete version directory**, not
to uv's floating `cpython-3.13-…` alias. Even root-owned, an alias that can be re-pointed is a
moving part under a session that does not need one.

## 3. Migration — only as each session closes

Not to be run now. Both sessions are live; B2 is at 111/400 warm-up bars and the 1h session holds
a latched breaker. Nothing below touches a running process.

```bash
# ---- Step 1. Freeze the runtime. Safe to do at ANY time, including with sessions live,
#              because nothing the platform does needs write access there. It is listed under
#              migration rather than under "safe now" only because it is not MY call to make.
chown -R root:root /home/quant/.local/share/uv/python/cpython-3.13.15-linux-x86_64-gnu
chmod -R a-w       /home/quant/.local/share/uv/python/cpython-3.13.15-linux-x86_64-gnu
chown -h root:root /home/quant/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu   # the alias
#  DO NOT move or rename it: every pyvenv.cfg and every .venv/bin/python names this path.
#  Verify immediately, read-only:
su - quant -c 'test -w /home/quant/.local/share/uv/python/cpython-3.13.15-linux-x86_64-gnu' \
  && echo 'STILL WRITABLE — investigate' || echo 'frozen'
/opt/quant-platform-b2/.venv/bin/python -c 'import configparser, difflib; print("lazy import ok")'
systemctl show quant-paper-b2 -p MainPID -p NRestarts   # must be unchanged

# ---- Step 2. When a session ends, give the next one its own runtime (pattern B)
install -d -o root -g root /opt/python
cp -a /home/quant/.local/share/uv/python/cpython-3.13.15-linux-x86_64-gnu /opt/python/3.13.15
chown -R root:root /opt/python/3.13.15 && chmod -R a-w /opt/python/3.13.15
/opt/python/3.13.15/bin/python3.13 -c 'import sys; print(sys.version)'

# ---- Step 3. Build the session venv against the CONCRETE path, non-editable
uv venv --python /opt/python/3.13.15/bin/python3.13 /opt/paper/$SESSION/.venv
VIRTUAL_ENV=/opt/paper/$SESSION/.venv uv pip install --compile-bytecode /opt/paper/$SESSION/repo

# ---- Step 4. Prove it before starting anything
/opt/paper/$SESSION/.venv/bin/python - <<'PY'
import os, sys, sysconfig
std = sysconfig.get_paths()["stdlib"]
assert not os.access(std, os.W_OK), f"stdlib is writable: {std}"
assert "/opt/python/" in std, std
assert not any("cpython-3.13-linux" in p for p in sys.path), "a floating alias is on sys.path"
print("runtime isolated:", std)
PY
uv run python scripts/ops_check_paper_isolation.py     # must print PASS
```

**Never** `uv python install --reinstall` or `uv python upgrade` while a paper session is live.
Install the new version beside the old one and point only new sessions at it.

## 4. What can be done now, and what must wait

**Now, with no risk and no restart:** everything in this change is repo-side — the two new
invariants, their tests, the collector reading the interpreter, the template, this document.
Nothing on the VPS was touched.

**Step 1 of the migration — freezing the current interpreter — is also safe with both sessions
live**, and that is a measured claim, not an optimistic one: no platform operation needs write
access there, and a read-only interpreter was shown to support venv creation, non-editable
installs, lazy stdlib imports and the entry point. It is listed as step 1 of the migration rather
than done, because changing ownership of a runtime under two live trading sessions is a decision
to be taken deliberately and not as a side effect of an audit.

**Must wait for each session to close:** moving to a dedicated per-version interpreter, the
non-editable install, and the read-only checkout. All three need the session restarted, and a
`--fresh` restart discards the warm-up — 111 of 400 bars for B2, which completes around
2026-12-02.

**Also unresolved, and smaller:** `/home/quant/.local/bin/uv` is `quant`-owned and writable, and
it is the 1h session's launcher (`ExecStart` runs `uv run`, MainPID 143200, with the real Python
child at 143203). The B2 unit calls its venv's console script directly and does not depend on uv
at runtime. Future units should follow B2's shape, which the template already does.

## 5. Guardrails added

Two invariants, with the collector taught to read them and the pure logic tested:

| invariant | severity | detects |
|---|---|---|
| `interpreter_not_writable` | **critical** | the service user can write the Python installation a live session runs on |
| `dedicated_interpreter` | serious | two paper sessions sharing one runtime — blast radius, and the motive for touching it |

The collector resolves the interpreter through `bin/python`, **not** through `pyvenv.cfg home`,
which on this host names the floating alias: reading `home` would report a path that can be
re-pointed rather than the installation actually in use. Its permission check also reads the
**group** bits, not only the owner's — the shared runtime is mode 775, so a tree owned by root
with the service user's group and `g+w` would be just as writable and would otherwise have been
reported as safe.

Run against the host as it stands, the checker goes from 5 broken invariants to **9**: both
sessions pick up `interpreter_not_writable` and `dedicated_interpreter`.
