#!/usr/bin/env bash
# Pinned Orca entry point for the interactive ummanu. Boots a chosen head with the full
# installation runtime env, so board access and every other credential are present regardless of
# which head runs. This is the trusted operator tool, not a scoped pipeline role.
#
# Usage: ummanu-start.sh [head]
#   ummanu-start.sh              # claude-default
#   ummanu-start.sh claude       # claude-default
#   ummanu-start.sh codex        # codex TUI
#   ummanu-start.sh hermes       # hermes REPL
#   ummanu-start.sh claude-opus  # any heads.toml profile id
#
# No login shell: export the per-user binary dirs explicitly like the automation gate, so `claude`
# and `codex` from ~/.local/bin resolve even when Orca launches this with a bare PATH.
set -u
export PATH="$HOME/.local/bin:$HOME/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
# Same checkout precedence as the automation gate: an explicit TA_RUNTIME_PYTHONPATH, else the
# product checkout this installation is configured with, else the home default. The interactive
# ummanu must not boot out of a different version than the one the host was upgraded to.
export PYTHONPATH="${TA_RUNTIME_PYTHONPATH:-${UMMANU_REPO:-$HOME/ummanu}}/src${PYTHONPATH:+:$PYTHONPATH}"

head="${1:-}"
exec python3 -P -m ummanu shell ${head:+--head "$head"}
