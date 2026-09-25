#!/bin/zsh
# Launcher for the B2/BTC/4h paper session.
#
# Exists so the session can be started by launchd instead of by a human holding a terminal
# window open. It is deliberately dumb: it reads the one environment file that is the single
# source of truth for this session, and executes the CLI. It decides nothing.
#
# `--fresh` is passed explicitly. It is also the default, and it is repeated here so that a
# reader of this file — or of a process listing — can see that this session never resumes.
#
# Not installed by running this script. See docs/m27_sleep_investigation.md.

set -euo pipefail

ROOT="/Users/luisve/quant-platform"
cd "$ROOT"

# The env file quotes its JSON values precisely so this sourcing does not mangle them.
set -a
. "$ROOT/deploy/paper-b2-btc-4h.env"
set +a

export PATH="$HOME/.local/bin:$PATH"

# `paper check` opens no socket and starts nothing; it validates the deployment and exits.
# Running it first means a misconfiguration fails here rather than half-way into a session.
uv run python -m quantplatform.cli.main paper check >/dev/null

exec uv run python -m quantplatform.cli.main paper run --fresh
