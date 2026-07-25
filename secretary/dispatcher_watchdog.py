"""Time ceilings for the dispatcher's two open-ended waits.

`waiting-worker-report` and `waiting-review-verdict` watch the persisted terminal identity on
every tick. A missing pane is restarted immediately. When Orca supplies `lastOutputAt`, a head
must also produce its first output shortly after launch; later output renews the ordinary,
generous silence ceiling. A runtime that cannot supply activity timestamps uses that ceiling as
its fallback.
"""

from __future__ import annotations

import os

# A missing pane is handled immediately. These ceilings remain deliberately generous for a head
# that has made progress and then goes quiet, and are the fallback for old Orca runtimes without
# `lastOutputAt`.
REVIEW_VERDICT_STALL_DEFAULT = 90 * 60
WORKER_REPORT_STALL_DEFAULT = 6 * 60 * 60
# A live head prints a prompt or launch output within a few dispatcher ticks. This only applies
# when an activity timestamp exists and has not advanced beyond the launch timestamp.
INITIAL_OUTPUT_STALL_DEFAULT = 3 * 60


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
