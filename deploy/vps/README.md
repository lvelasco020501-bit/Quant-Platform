# VPS deployment — B2 / BTC / 4h paper session

These are the files installed on the Hetzner VPS (`ubuntu-4gb-hel1-2`, Tailscale
`100.65.149.67`) for the `breakout_trend` 40/20/400 paper session. They are kept here because
a deployment that exists only on the box is a deployment nobody can review.

## What is installed where

| repo file | installed as |
|---|---|
| `quant-paper-b2.service` | `/etc/systemd/system/quant-paper-b2.service` |
| `quant-web-b2.service` | `/etc/systemd/system/quant-web-b2.service` |
| `mission-control-b2.env` | `/opt/quant-platform-b2/mission-control-b2.env` |
| `../paper-b2-btc-4h.env` + 6 deployment lines | `/opt/quant-platform-b2/.env` |

`/opt/quant-platform-b2/.env` is **not** in this directory: it carries the database DSN. It is
`deploy/paper-b2-btc-4h.env` verbatim — every risk number exactly as
`tests/unit/test_paper_b2_config.py` pins it — with only the session id, the three directories,
`QP_LOG_LEVEL`, `QP_LOG_FORMAT` and that DSN added.

## Why a second installation instead of an upgrade in place

`/opt/quant-platform` runs the productive 1h `breakout` 20/10 session and has done since
2026-09-11. Deploying 33 commits into that tree would change the code underneath a live
session. So this session gets its own everything:

| | productive 1h | B2 4h |
|---|---|---|
| source | `/opt/quant-platform` @ `727d47e` | `/opt/quant-platform-b2` @ `fb0dec8` |
| state / logs / reports | `/var/quant-platform/` | `/var/quant-platform-b2/` |
| unit | `quant-paper.service` | `quant-paper-b2.service` |
| Mission Control | `quant-web.service` :8800 | `quant-web-b2.service` :8801 |

`SessionLock` holds one session per **state directory**, so two locks in two directories are two
independent sessions. They share the host, the PostgreSQL instance the paper path never opens,
and nothing else.

## Operating it

```bash
systemctl start quant-paper-b2      # the only way a session ever begins
systemctl stop quant-paper-b2       # SIGTERM: winds down at the next boundary, persists, exits
systemctl status quant-paper-b2
journalctl -u quant-paper-b2 -n 50
```

Three properties are policy, not convenience, and match `docs/m27_sleep_investigation.md`:

* **`Restart=no`** — a session that stopped did so for a reason. Restarting would hide it, and
  because the run is `--fresh` it would silently discard the warm-up and begin again at zero.
* **No `[Install]` section** — the unit is `static` and therefore *cannot* be enabled. A reboot
  leaves the session stopped rather than quietly restarting a `--fresh` run.
* **No `caffeinate`** — this is a server that does not sleep. That was the point of leaving the
  laptop.

Mission Control B2 is the opposite on purpose: `Restart=on-failure`, because it observes, holds
no financial state, and a dashboard nobody trusts is worse than none.
