# The Python runtime a paper session runs on must not be writable by its own service user

**Status: audited and then frozen on 2026-10-15, with both sessions live throughout.** The
concrete runtime `cpython-3.13.15-linux-x86_64-gnu` is now `root:root` with no group or other
write, so the service user can no longer modify the Python its own live sessions run on. No
service was restarted, no PID changed, no venv was modified, no file content changed: a SHA-256
manifest of all 3 701 files is identical before and after, and the interpreter binary still
hashes `93a93f4c…`. See §3 for exactly what was run and §6 for what is still open.

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

## 3. Step 1 — freeze the runtime. **Done 2026-10-15, with both sessions live.**

Two commands, scoped to the concrete version directory. The path is unchanged, which is what
makes it safe: every `pyvenv.cfg` and every `.venv/bin/python` names this directory, so moving
or renaming it would break all four venvs, while changing its owner and mode breaks nothing.

```bash
R=/home/quant/.local/share/uv/python/cpython-3.13.15-linux-x86_64-gnu
chown -R root:root "$R"
chmod -R go-w      "$R"      # 775 -> 755 on dirs, 664 -> 644 on files; read and execute kept
```

`go-w` rather than `a-w`: root keeps owner-write, which it has anyway, and the service user
loses write through both the group and other classes. `chmod -R` skips the 1 047 symlinks in the
tree and reaches their targets through the ordinary walk.

Pre-flight, all read-only, all of it checked before the commands ran:

| | |
|---|---|
| real world-writable files (excluding symlinks) | 0 — the 1 047 "world-writable" entries were symlinks, which always report `lrwxrwxrwx` |
| modes present | 3 654 files at 664, 47 files at 775, 226 dirs at 775 |
| entries writable by group | 3 927 |
| open write-mode descriptors into the runtime, held by either live session | **0** |
| setuid/setgid files | 0 |

Verification, as `quant`, immediately after:

```
runtime root  not writable          touch …/lib/python3.13/.probe   -> Permission denied
stdlib        not writable          echo x >> …/lib/python3.13/filecmp.py -> Permission denied
python binary not writable
stdlib pycache not writable
interpreter ok 3.13.15
lazy stdlib imports: OK   (configparser, difflib, pprint, ftplib, getpass, gzip, tarfile, doctest)
/opt/quant-platform-b2/.venv/bin/quantplatform --help  -> runs
```

Permission bits *and* a real write attempt, because a bit says what should happen and a failed
`touch` says what did.

Content proof — nothing inside the runtime changed, only its ownership and mode:

| | before | after |
|---|---|---|
| interpreter binary SHA-256 | `93a93f4c…9840` | `93a93f4c…9840` |
| file count | 3 701 | 3 701 |
| **SHA-256 over the manifest of all 3 701 file hashes** | `626d2fb4…9ccb` | `626d2fb4…9ccb` |
| manifest diff | — | **identical** |
| files newer than the freeze | — | 0 |
| the three `.pyc` added at 01:11:41 | `6c4d3856…`, `ea4636fb…`, `4c54d61c…` | byte-identical, same mtime |

And the sessions, before and after, unchanged in every field that is not a clock:

| | before | after |
|---|---|---|
| `quant-paper-b2` | PID 508556, NRestarts 0, started 2026-09-26 19:17:28Z | identical |
| `quant-paper` | MainPID 143200 (`uv`), Python child 143203, NRestarts 0, started 2026-09-11 06:03:27Z | identical |
| B2 warm-up | 110/400 | **115/400** — kept processing straight through |
| B2 health | HEALTHY, `mixed_session_data` false | identical |
| B2 gaps / reconnects / runtime errors / restarts | 0 / 0 / 0 / 0 | identical |
| `/opt/quant-platform-b2` HEAD, reflog, `src` SHA-256 | `becf541`, 3 lines, `8bd9cf3a…` | identical |
| `/opt/quant-platform` HEAD, reflog, `src` SHA-256 | `refs/heads/main`, 5 lines, `7d569aed…` | identical |
| `:8800` / `:8801` | HTTP 200 | HTTP 200 |

Checker: **9 broken invariants before, 7 after; criticals 4 → 2.** `interpreter_not_writable`
passes on both sessions.

## 3b. Remaining steps — only as each session closes

Not to be run now. Both sessions are live; B2 is at 115/400 warm-up bars and the 1h session holds
a latched breaker. Nothing below touches a running process.

```bash

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

## 4. The standing rules

1. **Never `uv python install`, `uv python reinstall` or `uv python upgrade` against a runtime a
   live paper session uses.** Install the new version side by side and point only new sessions at
   it. The running session keeps the interpreter it started on.
2. **A shared runtime under a live session is read-only.** `root:root`, no group or other write,
   and never moved or renamed — the path is named by every `pyvenv.cfg` and every
   `.venv/bin/python`.
3. **New sessions pin the concrete version directory**, never uv's floating `cpython-3.13-…`
   alias. An alias is a moving part a session does not need.
4. **Each future session gets its own venv, the package installed non-editably, and a read-only
   checkout** — the three invariants in
   [`ops_immutable_paper_checkout.md`](ops_immutable_paper_checkout.md).
5. **Freezing a runtime is safe while sessions run; replacing one is not.** Ownership and mode
   changed under two live sessions with zero effect, measured. Content changes are a different
   act entirely.

## 5. What can be done now, and what must wait

**Done, with both sessions live and no restart:** the runtime freeze (§3), plus the repo-side
work — the two invariants, their tests, the collector reading the interpreter, the template, this
document.

**Must wait for each session to close:** the dedicated per-version interpreter, the non-editable
install, and the read-only checkout. All three need the session restarted, and a `--fresh`
restart discards the warm-up — 115 of 400 bars for B2, which completes around 2026-12-02.

## 6. What the freeze did not close

Scoped deliberately to the concrete version directory, so three things remain, none of which can
reach a live session's standard library:

1. **The parent `…/share/uv/python/` is still `quant:quant` 775, and so is the floating alias.**
   `quant` can therefore still create a *new* version directory there with `uv python install`
   and re-point `cpython-3.13-…` at it. That cannot touch 3.13.15, which is now root-owned, so a
   live session's stdlib is safe either way. What it reaches is `lib-dynload`, the one `sys.path`
   entry that goes through the alias — and that directory holds exactly two modules, `_dbm` and
   `_tkinter`, which this platform never imports. The larger consequence is forward-looking: a
   future `uv venv --python 3.13` resolving *by version* could pick up a `quant`-owned
   interpreter. Completing the freeze is one line, `chown -h root:root` on the alias plus
   `chown root:root` and `chmod go-w` on the parent, and it is not done because this step was
   scoped to the runtime itself.
2. **`dedicated_interpreter` still fails on both sessions.** They share one runtime, which is
   blast radius rather than a path for code to change: the runtime is now read-only, so sharing
   it costs nothing until somebody wants to upgrade one session. Left as future debt, by
   decision, and the fix is step 2 of §3b.
3. **`/home/quant/.local/bin/uv` is `quant`-owned and writable**, and it is the 1h session's
   launcher (`ExecStart` runs `uv run`, MainPID 143200, with the real Python child at 143203).
   The B2 unit calls its venv's console script directly and does not depend on uv at runtime.
   Future units follow B2's shape, which the template already does.

## 7. Guardrails added

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

Against the host these took the count from 5 broken invariants to 9 when the runtime was still
writable, and the freeze brought it to **7**, with criticals down from 4 to 2.
