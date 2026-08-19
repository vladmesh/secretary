#!/usr/bin/env bash
# Pinned Orca entry point for the interactive secretary. Boots a chosen head with the full
# installation runtime env, so board access and every other credential are present regardless of
# which head runs. This is the trusted operator tool, not a scoped pipeline role.
#
# Usage: secretary-start.sh [head]
#   secretary-start.sh              # claude-default
#   secretary-start.sh claude       # claude-default
#   secretary-start.sh codex        # codex TUI
#   secretary-start.sh hermes       # hermes REPL
#   secretary-start.sh claude-opus  # any heads.toml profile id
#
# No login shell: export the per-user binary dirs explicitly like the automation gate, so `claude`
# and `codex` from ~/.local/bin resolve even when Orca launches this with a bare PATH.
set -u
export PATH="$HOME/.local/bin:$HOME/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
# Same checkout precedence as the automation gate: an explicit TA_RUNTIME_PYTHONPATH, else the
# product checkout this installation is configured with, else the home default. The interactive
# secretary must not boot out of a different version than the one the host was upgraded to.
export PYTHONPATH="${TA_RUNTIME_PYTHONPATH:-${TA_SECRETARY_REPO:-$HOME/secretary}}/src${PYTHONPATH:+:$PYTHONPATH}"

head="${1:-}"
exec python3 -P -m secretary shell ${head:+--head "$head"}
