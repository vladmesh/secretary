"""Time ceilings for the dispatcher's two open-ended waits.

`waiting-worker-report` and `waiting-review-verdict` used to repeat every tick with nothing
watching them, so a head that exited without posting (secretary-637: the reviewer's verdict
command was rejected by the codex runtime; secretary-649: the rework worker never came up)
left the card parked forever. Both waits now end the same way: one respawn, then Blocked.

The ceiling is the only signal. There is no usable liveness probe for a head: the terminal
title the dispatcher sets at launch is overwritten by the head's own OSC sequence seconds
later, and orca's `status:running` wedges on 'working' after a silent exit (observed on
637/649/654). Both would report a healthy head as dead and kill live cards, so the ceilings
below are set generously instead.
"""

from __future__ import annotations

import os

REVIEW_VERDICT_STALL_DEFAULT = 90 * 60
WORKER_REPORT_STALL_DEFAULT = 6 * 3600


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
