#!/usr/bin/env bash
# Secretary automation precheck gate. It is installed only during the explicit cutover.
# It lives as a versioned, unit-testable script rather than the inline `bash -lc '...'` one-liner it
# replaced, so the branch logic is reviewable and covered by tests instead of hiding in a systemd
# unit string (triggered-agents-276).
#
# Exit-code protocol of `python3 -m triggered_agents <agent> precheck` (all agents, see each cli.py
# and runtime/state.py PRECHECK_SKIP):
#   0              -> there is work: exec the dispatch, the head wakes up.
#   100            -> deliberate skip (nothing changed / paused): no new skill dispatch, but still
#                     run `dispatch --cleanup-only` (triggered-agents-445) so an ephemeral agent's
#                     already-finished or stuck terminal gets torn down on THIS tick instead of
#                     waiting for a future tick that happens to have real work — dispatch (and
#                     every cleanup path inside it) is otherwise never invoked at all on a skip.
#   any other code -> precheck itself broke (crash, bad env, board unreachable): propagate the code
#                     so the unit is recorded `failed` in systemctl, distinguishable from a skip.
# 100 is deliberately not 1: Python's default uncaught-crash exit code is 1, so a precheck that dies
# before it can return (ImportError, a raise inside its own except handler) lands in the error branch
# below instead of masquerading as a clean skip. That masking was the bug this card fixes.
#
# With a second argument (a variant like the steward's deep-sweep) there is NO gate at all: dispatch
# runs unconditionally (triggered-agents-254). The variant exists to wake the head even when the
# deterministic signals stayed quiet, including the case where the signals themselves went blind).
#
# No login shell (-l): the unit invokes this script directly, not `bash -lc`. Export the per-user
# binary dirs explicitly: systemd's default PATH does not include ~/.local/bin/~/bin, but pipeline
# health probes call user-installed CLIs (`claude`, `codex`). Without this, probes falsely mark
# resources red with FileNotFoundError even though the CLIs are available in the normal dev shell.
set -u

export PATH="$HOME/.local/bin:$HOME/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
# Which checkout the role runs out of: the launcher's explicit TA_RUNTIME_PYTHONPATH, else the
# product checkout this installation is configured with, else the home default. An installation
# materialized from an alternate checkout sets TA_SECRETARY_REPO in the unit it renders, and
# skipping that name here would start the role out of ~/secretary — a checkout the operator may
# never have upgraded, or may not have at all.
export PYTHONPATH="${TA_RUNTIME_PYTHONPATH:-${TA_SECRETARY_REPO:-$HOME/secretary}}${PYTHONPATH:+:$PYTHONPATH}"

agent="${1:?usage: ta-gate.sh <agent> [variant]}"
variant="${2:-}"

run_role_env() {
    python3 -m triggered_agents.runtime.role_env exec --role "$agent" -- "$@"
}

exec_role_env() {
    exec python3 -m triggered_agents.runtime.role_env exec --role "$agent" -- "$@"
}

if [ -n "$variant" ]; then
    exec_role_env python3 -m triggered_agents "$agent" dispatch "$variant"
fi

run_role_env python3 -m triggered_agents "$agent" precheck
rc=$?
if [ "$rc" -eq 0 ]; then
    exec_role_env python3 -m triggered_agents "$agent" dispatch
elif [ "$rc" -eq 100 ]; then
    echo "[ta-$agent] precheck: no change, skill dispatch skipped"
    exec_role_env python3 -m triggered_agents "$agent" dispatch --cleanup-only
else
    echo "[ta-$agent] precheck: ERROR (rc=$rc); see runs.jsonl" >&2
    exit "$rc"
fi
