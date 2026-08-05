"""Time ceilings for the dispatcher's two open-ended waits.

`waiting-worker-report` and `waiting-review-verdict` watch the persisted terminal identity on
every tick. A missing pane is restarted immediately. A pane whose head process has exited while
the pane itself is still connected — a shell left behind, `secretary-751` — is caught the same
tick via `head_process_status`. When Orca supplies `lastOutputAt`, a head must also produce its
first output shortly after launch; later output renews the ordinary, generous silence ceiling. A
runtime that cannot supply activity timestamps, or cannot expose the pid heartbeat (a raw
`SECRETARY_DISPATCHER_*_COMMAND` override), uses that ceiling as its fallback.

A confirmed pid answers whether the process runs, not whether it is doing anything. A head that is
ready for input has stopped working, and if nothing lands for the round being waited on while it
stays that way, that ends the wait too (`secretary-1063`): otherwise a finished head sitting at its
prompt holds a card in `waiting-worker-report` for as long as the process lives.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from secretary.dispatcher_state import request_token

# A missing pane is handled immediately. These ceilings remain deliberately generous for a head
# that has made progress and then goes quiet, and are the fallback for old Orca runtimes without
# `lastOutputAt`.
REVIEW_VERDICT_STALL_DEFAULT = 90 * 60
WORKER_REPORT_STALL_DEFAULT = 6 * 60 * 60
# A live head prints a prompt or launch output within a few dispatcher ticks. This only applies
# when an activity timestamp exists and has not advanced beyond the launch timestamp.
INITIAL_OUTPUT_STALL_DEFAULT = 3 * 60
# How long a head that is ready for input, with nothing delivered for the round the dispatcher is
# waiting on, is left alone before the watchdog acts (secretary-1063). A head between turns reads
# as ready for a moment — a delivered prompt whose turn has not started, a retained conversation
# just resumed — so this is a window rather than a single reading. It is short next to the silence
# ceilings above because it is not measuring silence: readiness says the head is not working.
IDLE_STALL_DEFAULT = 5 * 60


def stall_seconds(kind: str) -> int:
    """Ceiling for a wait, read per call. A typo in the env var falls back to the default rather
    than raising out of module import and taking the whole dispatcher down with it."""
    if kind == "review":
        name, default = "SECRETARY_REVIEW_VERDICT_STALL_SECONDS", REVIEW_VERDICT_STALL_DEFAULT
    else:
        name, default = "SECRETARY_WORKER_REPORT_STALL_SECONDS", WORKER_REPORT_STALL_DEFAULT
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def initial_output_stall_seconds() -> int:
    """Short grace period for a pane whose activity has never passed its launch timestamp."""
    try:
        value = int(os.environ.get("SECRETARY_INITIAL_OUTPUT_STALL_SECONDS", "") or INITIAL_OUTPUT_STALL_DEFAULT)
    except ValueError:
        return INITIAL_OUTPUT_STALL_DEFAULT
    return value if value > 0 else INITIAL_OUTPUT_STALL_DEFAULT


def idle_stall_seconds() -> int:
    """How long an idle head with nothing delivered is given before the watchdog acts."""
    try:
        value = int(os.environ.get("SECRETARY_HEAD_IDLE_STALL_SECONDS", "") or IDLE_STALL_DEFAULT)
    except ValueError:
        return IDLE_STALL_DEFAULT
    return value if value > 0 else IDLE_STALL_DEFAULT


def pid_file_path(kind: str, reference: str) -> str:
    """Where a launched head's pid-heartbeat file lives.

    Outside the workspace, like the report and verdict bodies (`_body_file_path`), so it never
    dirties `git status` for the done-report check. Keyed on kind and reference only: a respawn in
    the same workspace reuses the same path on purpose, since the dispatcher clears it before every
    fresh launch and the new head overwrites it with its own pid the moment it starts.
    """
    root = os.environ.get("SECRETARY_DISPATCHER_BODY_DIR", "/tmp").rstrip("/") or "/tmp"
    return f"{root}/secretary-{kind}-pid-{request_token(reference)}.pid"


def _is_zombie(pid: int) -> bool:
    """A process the kernel has not reaped yet still answers `kill(pid, 0)`, so a check right at
    exit can read one tick stale as still alive. Reading its own `/proc` status closes that gap."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return "Z" in line
    return False


def _is_stopped(pid: int) -> bool:
    """Whether a live process is suspended with SIGSTOP/SIGTSTP."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return "T" in line
    return False


def head_process_status(pid_file: str) -> dict[str, Any]:
    """Whether the OS process named by a pid-heartbeat file is still alive.

    `known: False` covers a file that does not exist yet (a fresh launch has not written its pid
    yet) and one that never will (a raw `SECRETARY_DISPATCHER_*_COMMAND` override skips the
    heartbeat wrapper entirely). Neither is evidence the head died, so callers must fall back to
    the ordinary timing ceiling instead of reading `known: False` as "exited".
    """
    try:
        raw = Path(pid_file).read_text(encoding="utf-8").strip()
    except OSError:
        return {"known": False}
    try:
        pid = int(raw)
    except ValueError:
        return {"known": False}
    if pid <= 0:
        return {"known": False}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"known": True, "alive": False}
    except PermissionError:
        # Exists, owned by someone else. Cannot happen for a head this dispatcher launched itself,
        # but existing beats dead here rather than guessing. `/proc` is still readable, so the
        # suspended flag stays available to retention rather than silently reading as "running".
        return {"known": True, "alive": True, "stopped": _is_stopped(pid)}
    except OSError:
        return {"known": False}
    alive = not _is_zombie(pid)
    return {"known": True, "alive": alive, "stopped": alive and _is_stopped(pid)}


def wait_outcome(
    *,
    waiting_since: float,
    now: float,
    stall_seconds: int,
    respawns: int,
) -> str:
    """Decide a waiting tick: "wait", "respawn" or "escalate"."""
    if now - waiting_since <= stall_seconds:
        return "wait"
    return "respawn" if respawns < 1 else "escalate"


def reset_wait(record, kind: str) -> None:
    """Clear a wait's watchdog bookkeeping so the next wait of that kind starts fresh."""
    setattr(record, f"{kind}_waiting_since", 0.0)
    setattr(record, f"{kind}_respawns", 0)
    setattr(record, f"{kind}_progress_at", 0.0)
    setattr(record, f"{kind}_idle_since", 0.0)
    setattr(record, f"{kind}_idle_confirmations", 0)


def wait_cycle_token(record) -> str:
    """Per-cycle discriminator for every request-id the watchdog path can emit.

    attempt_id outlives the card (production adopts under a constant `production-adopt-<ref>`)
    and the record is dropped whenever the card lands in Blocked, so a bare attempt-scoped id
    repeats on the next stall. TaskWriter answers a repeated request-id with success and no
    mutation, so the tick reports "blocked" while the card stays put and the next tick re-adopts
    it: the card hangs forever, which is the failure this whole watchdog exists to end.

    comment_baseline is re-read from the board on adoption and every cycle leaves at least one
    comment behind, so it is stable within a cycle and distinct across them. The respawn counters
    separate the request-ids emitted before and after a respawn inside one cycle.
    """
    return f"{record.comment_baseline}-{record.worker_respawns}-{record.review_respawns}"
