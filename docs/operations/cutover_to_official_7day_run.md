# Cutover to the official 7-day paper trading run

**Status: inventory and plan only. Nothing below has been executed.** No process has been
stopped, no file archived, no configuration changed, no session started. This document is
the review artifact requested before any of that happens.

**Inventory taken:** 2026-08-14T05:0x UTC. Every figure below is a snapshot; PIDs,
`bars_processed` and log sizes will have moved by the time this is read, but the
*structure* — which processes exist, which code they run, what they write, what the
dashboard reads — will not, and that structure is what this document is about.

---

## Why this document exists

Two independent `quantplatform paper run` processes have been alive simultaneously since
2026-08-13T12:14 UTC, both writing into the same shared `var/` tree, one of them running
code from before this week's incident-response and transport-hardening fixes. The Control
Center dashboard reads a *third*, independently-configured pointer into that same tree.
Nothing about this is coordinated by the platform itself — every piece is a human decision
(which `session_id` to pass, which directory to point a dashboard at) with no single place
that states which one is *the* run. That absence is the actual defect this cutover fixes;
archiving two processes is just the first time it has to be exercised.

---

## 1–7. Full inventory

### 1. Live paper processes

| PID | PPID | Command | Started | Uptime at inventory |
|---|---|---|---|---|
| 49612 | 1 | `uv run quantplatform paper run --resume` | 2026-08-12 23:02:51 | ~1d 06h |
| 49613 | 49612 | `caffeinate -i uv run quantplatform paper run --resume` | 2026-08-12 23:02:51 | ~1d 06h |
| **49614** | 49612 | `.../quantplatform paper run --resume` (the actual Python process) | 2026-08-12 23:02:51 | ~1d 06h |
| 61714 | 1 | `uv run quantplatform paper run` | 2026-08-13 12:14:11 | ~17h |
| 61716 | 61714 | `caffeinate -i uv run quantplatform paper run` | 2026-08-13 12:14:11 | ~17h |
| **61717** | 61714 | `.../quantplatform paper run` (the actual Python process) | 2026-08-13 12:14:11 | ~17h |

Both trees are **orphaned** (`PPID 1`) — detached from any shell, launched with `nohup` +
`caffeinate -i` per the standing operational pattern, and will not stop when a terminal
closes. Neither will stop on its own; both require an explicit `SIGTERM`.

No other `quantplatform paper` process exists on this machine.

### 2. Every `session_id` on disk

| `session_id` | State file | `started_at` | `bars_processed` | `restarts` |
|---|---|---|---|---|
| `paper-7c-week2` | `var/state/paper-7c-week2.json` | 2026-08-11T23:38:40Z | 44 | 1 |
| `paper-7c-soak-test` | `var/state/paper-7c-soak-test.json` | 2026-08-13T18:14:13Z | 11 | 0 |

Two more `session_id`s appear **only** as stale tracking files, with no live process:

| Tracking file | Recorded PID | Live? |
|---|---|---|
| `var/observation/run.pid` | 60995 | No — dead. Leftover from the very first `paper-7c-week1` launch. |
| `var/observation/week2.pid` | 20533 | No — dead. `paper-7c-week2` was later relaunched as PID 49612 via `--resume`, outside of any command this operator tracked in a pid file, so this file was never updated to the PID actually running today. |

**This mismatch — a tracking file naming a dead PID while a same-named session runs under
a different, untracked PID — is itself a finding.** There is currently no reliable,
single file that answers "what PID is `paper-7c-week2` actually running under" without
cross-referencing `ps` against the state file by hand, which is exactly what this
inventory just did.

### 3. What the Control Center reads

Confirmed directly from the live process's own environment (`ps eww -p 52517`), not from
a config file default:

```
CONTROL_CENTER_SESSION_ID=paper-7c-week2
CONTROL_CENTER_STATE_DIRECTORY=/Users/luisve/quant-platform/var/state
CONTROL_CENTER_LOGS_DIRECTORY=/Users/luisve/quant-platform/var/logs
CONTROL_CENTER_REPORTS_DIRECTORY=/Users/luisve/quant-platform/var/reports
```

The dashboard (PID 52517, running from the separate `quant-platform-control-center` git
worktree, started 2026-08-13 06:17:03) reads `MonitoringReadModel`, which opens a
`FilePaperStateRepository` pointed at the **main repository's** `var/state` — not its own
worktree's `var/state`, which exists and is empty. It is hard-pinned to `session_id =
paper-7c-week2` by an explicit environment variable, not a default that happens to match.

**The candles the dashboard shows come from `paper-7c-week2` — the process running
pre-hardening code — not from the soak test.**

### 4. What each process writes to `var/logs/`

This is where the inventory surfaced the most serious operational gap. Every log stream
defaults to the *same* `var/logs/` directory for both sessions (`log_directory` was never
overridden for either), and the platform's daily log rotation renames files rather than
truncating them — a process that has been running across a rotation boundary keeps
whatever file handle it opened, correct or not.

| Logger stream | `paper-7c-week2` (PID 49614) writes to | `paper-7c-soak-test` (PID 61717) writes to |
|---|---|---|
| `marketdata` | `marketdata.log.2026-08-10` — **0 bytes, never written** | `marketdata.log` (current) — actively growing |
| `paper` | `paper.log.2026-08-10` — **0 bytes, never written** | `paper.log` (current) — actively growing |
| `reporting` | `reporting.log` — 0 bytes (shared handle, neither session has logged here) | same file, same handle |
| `orchestration` | `orchestration.log.2026-08-13` — non-empty, actively growing, but a **stale, rotated-out name** as of today (2026-08-14) | `orchestration.log` (current) — actively growing |

**Consequence: `paper-7c-week2` has no retrievable structured log output at all for the
`marketdata` or `paper` domains — not "hard to find," genuinely absent, because the file
its handle points to has zero bytes.** Its only observable log stream is
`orchestration.log.2026-08-13`, which itself is a day behind the live file name. This was
confirmed by inode, not by filename — `lsof -p 49614` and `lsof -p 61717` were compared
directly; two of `week2`'s four file descriptors point at files distinct from, and empty
relative to, the ones the currently-hardened code writes to.

`reporting.log` is a genuinely shared file descriptor (same inode) between both processes
and has zero content from either — a live collision this inventory did not need to
resolve because nothing has been written there yet, but a future write from either session
would be indistinguishable from the other's without inspecting the JSON payload's own
`session_id` field.

### 5. State files

Already covered in full under §2. One file per `session_id`, both under the same shared
`var/state/` directory, distinguished only by filename — no collision today because the
two `session_id`s differ, but nothing prevents a third process using an *existing*
`session_id` from silently overwriting either file, since `FilePaperStateRepository` does
not lock or check for another writer.

`var/reports/` is likewise one shared tree for both sessions, distinguished only by the
`session_id` field *inside* each day's `daily.json` — confirmed directly:

| Report | `session_id` recorded inside it |
|---|---|
| `var/reports/2026/08/12/daily.json` | `paper-7c-week2` |
| `var/reports/2026/08/13/daily.json` | `paper-7c-soak-test` |

### 6. PID ↔ session_id ↔ code version, consolidated

| `session_id` | PID (python) | PID (uv wrapper) | PID (caffeinate) |
|---|---|---|---|
| `paper-7c-week2` | 49614 | 49612 | 49613 |
| `paper-7c-soak-test` | 61717 | 61714 | 61716 |

### 7. Old code vs. new code — determined by commit timestamp, not inference

This is the one question in this inventory answerable with certainty rather than
circumstantial evidence: a running Python process has whatever code was on disk at
`import` time, permanently, regardless of what is committed afterward. Comparing each
process's start time against the three most recent fix commits settles it exactly:

| Commit | Committed at | In `paper-7c-week2` (started 2026-08-12 23:02:51)? | In `paper-7c-soak-test` (started 2026-08-13 12:14:11)? |
|---|---|---|---|
| `e5e5aed` — SymbolRules refresh statelessness + account funding fix | 2026-08-11 17:38:20 | ✅ Yes (committed before it started) | ✅ Yes |
| `36da210` — `log_extra()` fix, structured instrumentation, `StallWatchdog` | 2026-08-13 07:36:13 | ❌ **No** (committed after it started) | ✅ Yes |
| `647d853` — transport socket-timeout hardening, hard-close backstop | 2026-08-13 12:13:37 | ❌ **No** | ✅ Yes (started 34s after this commit) |

**`paper-7c-week2` is running code with none of this week's incident-response or
transport-hardening fixes.** Concretely, it means: a caught `QuantPlatformError` could
still crash the process while trying to log it (`log_extra()` does not exist in its
loaded module); nothing is watching for a silent stall (`StallWatchdog` does not exist in
its loaded module — if this session freezes the way it did on 2026-08-13, nothing will
raise an alert, exactly as happened before); and the raw transport socket has no explicit
read timeout of its own during ordinary operation (the specific gap `647d853` closes).

`paper-7c-soak-test` was deliberately started 34 seconds after `647d853` landed, and its
entire purpose was to validate that exact commit. It is running the fully current code as
of `HEAD` (`647d853bd117971509cd8858ef19e51ba0dfaaf0`), verified clean:

```
$ git status --porcelain
(empty)
$ git rev-parse HEAD
647d853bd117971509cd8858ef19e51ba0dfaaf0
```

---

## What this inventory concludes

- **Two live sessions, two different code vintages, one shared filesystem tree.** Nothing
  in the platform enforces that only one paper session may run against a given `var/`
  root at a time; two independently-launched processes discovered this the way any two
  unrelated writers to a shared directory would — by silently interleaving.
- **The dashboard is truthful about what it reads, but what it reads is the stale
  session.** `paper-7c-week2` is not a bad choice a user made — it is the *only* session
  that has existed under that name since before the Control Center was pointed at it, and
  nothing told the dashboard operator that a second, better session had since appeared
  under a different name.
- **`paper-7c-week2`'s own logs cannot answer basic operational questions about itself**
  — not because logging is broken in general (the soak test proves the current logging
  and rotation code works correctly), but because this specific process's file handles
  were opened by an older version of that code and never followed a subsequent rotation.
  This is additional, independent evidence for retiring it rather than continuing it.

None of this is a reason to panic about data loss: both sessions' **state** (the thing
that actually matters — balances, positions, bar counts) has been reliably persisted the
whole time, confirmed by direct inspection in §2. The gap is entirely in *observability*
of `week2`, not in the integrity of what it did.

---

## Cutover plan

### What must be archived (preserved, then removed from the live tree)

- `paper-7c-week2`: state file, both log locations it actually wrote to
  (`marketdata.log.2026-08-10`, `paper.log.2026-08-10` — empty, but archived for
  completeness — and the relevant slice of `orchestration.log.2026-08-13`), and its
  `var/reports/2026/08/12/` output. This is now a *closed, historical* run — a real
  record of ~2.5 days of paper trading under the pre-hardening code, useful evidence,
  not a session to keep extending.
- `paper-7c-soak-test`: state file, the (correctly split) `marketdata.log` /
  `marketdata.log.2026-08-13`, `paper.log` / `paper.log.2026-08-13`, and its
  `var/reports/2026/08/13/` output. This is the validation record for `647d853` and
  belongs in the audit trail permanently, alongside the transport-hardening test suite —
  it is the empirical evidence that commit was sound.

### What must be stopped

- **`paper-7c-week2`** (PID 49612/49613/49614): `SIGTERM`, same clean-shutdown path
  already used for every prior session in this project (persists a final snapshot, closes
  the socket, flushes logs). Its `orchestration.log.2026-08-13` handle should be given a
  moment to flush before archiving.
- **`paper-7c-soak-test`** (PID 61714/61716/61717): `SIGTERM`, once its evidence value is
  fully captured — it has already cleared the 6-hour floor cleanly (see the soak-test
  report already delivered); nothing further is gained by leaving it running once the
  official run starts, and leaving it running would recreate the exact two-sessions
  problem this cutover exists to close.

### What must be conserved as evidence (not deleted, ever)

- Both archived trees above, moved under `var/audit/`, matching the pattern already
  established for the first incident (`var/audit/2026-08-11-run1-paper-7c-week1/`) —
  read-only once written, never edited.
- This document itself, and the soak-test report already delivered in conversation,
  belong in the same audit trail as the record of *why* the cutover happened.

### How to clean the environment completely

1. Confirm both processes have exited (not merely sent a signal — poll until the PID is
   gone).
2. Move each session's state file, its correctly-attributable log slices, and its report
   directory into a dedicated `var/audit/<date>-<session_id>/` folder, exactly as done for
   the first incident.
3. Remove the *live* copies from `var/state/`, `var/logs/`, `var/reports/` once the
   archive copy is confirmed intact (verified by re-reading the archived files, not by
   trusting the move succeeded).
4. Remove or correct the stale `var/observation/*.pid` files — `run.pid` and `week2.pid`
   currently name dead PIDs and should not survive the cutover pointing at nothing.
5. Confirm `var/state/`, `var/logs/`, `var/reports/` are empty of anything from either
   retired session before the new one starts, so the new run's very first log line is
   genuinely the first line in each file — no ambiguity about what a byte offset belongs
   to.

### How to guarantee only one official session exists going forward

This is the actual gap identified in §"Why this document exists," and cleaning up two
processes once does not close it by itself. Two complementary safeguards, both purely
operational (no code change required for either):

- **One canonical `session_id`, stated once, in one place.** This document nominates
  `paper-7c-week3` as that name for the run this cutover starts. Every command in the
  "start the new session" step below must use it, and it should be the only `session_id`
  any operator (or this assistant) types from this point forward until the run concludes.
- **Before starting any future paper session, check for an existing live one first.** A
  one-line standing check — `pgrep -fl "quantplatform paper run"` — answers "is anything
  already running" before a second process is ever launched. This was the exact step
  skipped when the soak test was started without first confirming `paper-7c-week2` was
  the only thing alive; it will be the first line of the "start the new session" runbook
  entry so it cannot be skipped by omission again.

### How to make the Control Center read only that session

Update its runtime environment — the same three variables identified in §3 — to name the
new session before it (or, if already running, before it is restarted to pick up the
change):

```
CONTROL_CENTER_SESSION_ID=paper-7c-week3
CONTROL_CENTER_STATE_DIRECTORY=/Users/luisve/quant-platform/var/state
CONTROL_CENTER_LOGS_DIRECTORY=/Users/luisve/quant-platform/var/logs
CONTROL_CENTER_REPORTS_DIRECTORY=/Users/luisve/quant-platform/var/reports
```

The directory values do not change — both sessions have always lived under the same
`var/` root — only `CONTROL_CENTER_SESSION_ID` does. The dashboard process must be
restarted (or its process manager told to reload the environment) for this to take
effect; `MonitoringReadModel` reads `settings.session_id` at construction, not per-request.

### How to verify dashboard, logs, state and process all agree

A single verification pass, run *after* the new session starts, checking five
independent sources against one name:

1. **Process**: `ps` shows exactly one `quantplatform paper run` tree, and its `session_id`
   (visible via the `--session-id` value or the resulting state filename) is
   `paper-7c-week3`.
2. **State file**: `var/state/paper-7c-week3.json` exists, `session_id` field inside it
   reads `paper-7c-week3`, and it is the *only* `.json` file in `var/state/`.
3. **Logs**: `lsof -p <PID>` for the new process shows all four log file descriptors
   pointing at the current, unrotated `var/logs/*.log` names (not a dated, rotated
   variant) — the same check that surfaced `week2`'s problem is the check that proves the
   new run does not have it.
4. **Dashboard**: `ps eww -p <control-center PID>` shows `CONTROL_CENTER_SESSION_ID=
   paper-7c-week3`, and the dashboard's own displayed `bars_processed` / last-bar
   timestamp matches the state file's, confirmed by direct comparison, not by eyeballing
   that the numbers look plausible.
5. **Reports**: once the first daily report is written, its `session_id` field matches.

Only when all five agree is the system in the single-source-of-truth state this cutover
is meant to produce.

---

## Cutover checklist for review

Nothing below has been executed. Presented for approval before any step is taken.

- [ ] **1.** Stop `paper-7c-week2` (PID 49612/49613/49614) via `SIGTERM`; confirm clean
      shutdown log entry and process exit.
- [ ] **2.** Stop `paper-7c-soak-test` (PID 61714/61716/61717) via `SIGTERM`; confirm
      clean shutdown log entry and process exit.
- [ ] **3.** Archive `paper-7c-week2`'s state, logs and reports to
      `var/audit/2026-08-14-week2-paper-7c-week2/`.
- [ ] **4.** Archive `paper-7c-soak-test`'s state, logs and reports to
      `var/audit/2026-08-14-soak-test-paper-7c-soak-test/`.
- [ ] **5.** Verify both archives are complete and readable before touching the live
      copies.
- [ ] **6.** Remove the archived sessions' files from the live `var/state/`, `var/logs/`,
      `var/reports/` trees.
- [ ] **7.** Remove or correct the stale `run.pid` / `week2.pid` tracking files.
- [ ] **8.** Confirm `var/state/` and `var/logs/` are empty of any prior session.
- [ ] **9.** Start the new official session, `paper-7c-week3`, using the current,
      fully-hardened code at `HEAD` (`647d853` or later) — after first confirming via
      `pgrep -fl "quantplatform paper run"` that nothing else is running.
- [ ] **10.** Update the Control Center's environment to
      `CONTROL_CENTER_SESSION_ID=paper-7c-week3` and restart it.
- [ ] **11.** Run the five-point verification in "How to verify dashboard, logs, state
      and process all agree" and confirm all five agree.
- [ ] **12.** Update `var/observation/RUN.md` (or its successor) to name
      `paper-7c-week3` as the single active session, superseding all prior runbook
      entries.

**This checklist is what needs your approval. No step executes until you confirm.**
