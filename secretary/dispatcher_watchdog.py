"""Liveness watchdogs for the dispatcher's two open-ended waits.

`waiting-worker-report` and `waiting-review-verdict` used to repeat every tick with nothing
watching them, so a head that exited without posting (secretary-637: the reviewer's verdict
command was rejected by the codex runtime; secretary-649: the rework worker never came up)
left the card parked forever. Both waits now end the same way: one respawn, then Blocked.
"""

from __future__ import annotations

import os

# Hard ceiling per wait, independent of liveness: covers a head that is still up but wedged.
REVIEW_VERDICT_STALL_SECONDS = int(
    os.environ.get("SECRETARY_REVIEW_VERDICT_STALL_SECONDS", str(2 * 3600))
)
WORKER_REPORT_STALL_SECONDS = int(
    os.environ.get("SECRETARY_WORKER_REPORT_STALL_SECONDS", str(6 * 3600))
)
# A terminal takes a moment to show up in orca's inventory after launch, so a "not running"
# answer only counts once the wait is at least this old.
WAIT_LIVENESS_GRACE_SECONDS = int(
    os.environ.get("SECRETARY_WAIT_LIVENESS_GRACE_SECONDS", "180")
)

WAIT_KINDS = ("worker", "review")


def wait_outcome(
    *,
    waiting_since: float,
    now: float,
    running: bool | None,
    stall_seconds: int,
    respawns: int,
) -> str:
    """Decide a waiting tick: "wait", "respawn" or "escalate".

    `running` is None when liveness is unknown (still inside the grace window, or the
    inventory probe failed) — an unknown is not evidence the head is gone, so only the
    stall ceiling can end the wait then.
    """
    elapsed = now - waiting_since
    dead = running is False and elapsed >= WAIT_LIVENESS_GRACE_SECONDS
    if not dead and elapsed <= stall_seconds:
        return "wait"
    return "respawn" if respawns < 1 else "escalate"


def wait_probe_due(waiting_since: float, now: float) -> bool:
    return now - waiting_since >= WAIT_LIVENESS_GRACE_SECONDS


def reset_wait(record, kind: str) -> None:
    """Clear a wait's watchdog bookkeeping so the next wait of that kind starts fresh."""
    setattr(record, f"{kind}_waiting_since", 0.0)
    setattr(record, f"{kind}_respawns", 0)
