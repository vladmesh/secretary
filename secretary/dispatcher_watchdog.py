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

REVIEW_VERDICT_STALL_SECONDS = int(
    os.environ.get("SECRETARY_REVIEW_VERDICT_STALL_SECONDS", str(90 * 60))
)
WORKER_REPORT_STALL_SECONDS = int(
    os.environ.get("SECRETARY_WORKER_REPORT_STALL_SECONDS", str(6 * 3600))
)

WAIT_KINDS = ("worker", "review")


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
