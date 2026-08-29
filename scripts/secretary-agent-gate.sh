#!/usr/bin/env bash
# Secretary automation precheck gate. It is installed only during the explicit cutover.
# It lives as a versioned, unit-testable script rather than the inline `bash -lc '...'` one-liner it
# replaced, so the branch logic is reviewable and covered by tests instead of hiding in a systemd
# unit string (triggered-agents-276).
#
# Exit-code protocol of `python3 -P -m triggered_agents <agent> precheck` (all agents, see each cli.py
# and runtime/state.py PRECHECK_SKIP):
#   0              -> there is work: exec the dispatch, the head wakes up.
#   100            -> deliberate skip (nothing changed / paused): no new skill dispatch, but still
#                     run `dispatch --cleanup-only` (triggered-agents-445) so an ephemeral agent's
#                     already-finished or stuck terminal gets torn down on THIS tick instead of
#                     waiting for a future tick that happens to have real work — dispatch (and
#                     every cleanup path inside it) is otherwise never invoked at all on a skip.
#   101            -> the board refused the connection for the client's whole retry window: nothing
#                     was evaluated, so the tick is deferred rather than answered. This gate waits
#                     and re-runs the precheck a bounded number of times before giving up with 101.
#                     A `Persistent=true` timer catches its missed run up seconds after boot, which
#                     is exactly when the docker-hosted board is least likely to be listening, and a
#                     daily unit that crashed there lost the whole day: the timer does not re-fire
#                     (secretary-964). The waiting lives here rather than in the unit because
#                     systemd refuses `RestartForceExitStatus=` on a `Type=oneshot` service, and a
#                     oneshot has no start timeout to run into.
#   102            -> a short role-local settlement transaction is busy. The tick is deliberately
#                     unclaimed: exit successfully without dispatch or cleanup, because either can
#                     race the live transaction holder. Curator uses this for watermark/pending
#                     settlement contention (secretary-1501).
#   any other code -> precheck itself broke (crash, bad env): propagate the code so the unit is
#                     recorded `failed` in systemctl, distinguishable from a skip.
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
export PYTHONPATH="${TA_RUNTIME_PYTHONPATH:-${TA_SECRETARY_REPO:-$HOME/secretary}}/src${PYTHONPATH:+:$PYTHONPATH}"

agent="${1:?usage: ta-gate.sh <agent> [variant]}"
variant="${2:-}"

run_role_env() {
    python3 -P -m triggered_agents.runtime.role_env exec --role "$agent" -- "$@"
}

exec_role_env() {
    exec python3 -P -m triggered_agents.runtime.role_env exec --role "$agent" -- "$@"
}

if [ -n "$variant" ]; then
    exec_role_env python3 -P -m triggered_agents "$agent" dispatch "$variant"
fi

# How long the gate keeps re-attempting a precheck that could not reach the board: attempts spaced
# by TA_GATE_BOARD_WAIT seconds, on top of the retry window the client itself already spends inside
# each attempt. The total has to comfortably cover a board coming up a minute or two after a reboot,
# and has to stay bounded so a board that is really gone still ends the unit `failed` and visible.
board_attempts="${TA_GATE_BOARD_ATTEMPTS:-5}"
board_wait="${TA_GATE_BOARD_WAIT:-120}"

attempt=1
while : ; do
    run_role_env python3 -P -m triggered_agents "$agent" precheck
    rc=$?
    if [ "$rc" -ne 101 ] || [ "$attempt" -ge "$board_attempts" ]; then
        break
    fi
    echo "[ta-$agent] precheck: board unreachable (attempt $attempt/$board_attempts); retrying in ${board_wait}s" >&2
    sleep "$board_wait"
    attempt=$((attempt + 1))
done

if [ "$rc" -eq 0 ]; then
    exec_role_env python3 -P -m triggered_agents "$agent" dispatch
elif [ "$rc" -eq 100 ]; then
    echo "[ta-$agent] precheck: no change, skill dispatch skipped"
    exec_role_env python3 -P -m triggered_agents "$agent" dispatch --cleanup-only
elif [ "$rc" -eq 101 ]; then
    # No dispatch and no cleanup: both talk to the same board none of the attempts could reach.
    echo "[ta-$agent] precheck: board unreachable after $board_attempts attempts; run not taken" >&2
    exit "$rc"
elif [ "$rc" -eq 102 ]; then
    echo "[ta-$agent] precheck: settlement busy, tick deferred" >&2
    exit 0
else
    echo "[ta-$agent] precheck: ERROR (rc=$rc); see runs.jsonl" >&2
    exit "$rc"
fi
