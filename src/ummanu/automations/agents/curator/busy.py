"""Busy-without-advance: a supervised curator head that keeps a tick from starting and moves nothing.

Every tick a busy supervised head is answered `dispatch: supervised-busy-skip`. That is correct for
an hour, but a head that stays busy and never reaches `advance` or `memory_write` turned 8 days of
ticks into skips (issue:db32299c8). Doctor reports it as a finding instead of waiting forever.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: How long the head may stay busy without progress before doctor names it.
BUSY_WITHOUT_ADVANCE_HOURS = 6
_BUSY = frozenset({"supervised-busy-skip", "busy-skip"})
# A fresh head starts the clock again: its predecessor's stall is no longer what blocks ticks.
_STARTED = frozenset({"supervised-started", "created", "ephemeral-restart", "watchdog-restart"})


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def busy_without_advance(
    runs: Path, *, now: datetime | None = None, hours: int = BUSY_WITHOUT_ADVANCE_HOURS
) -> dict[str, object] | None:
    """The finding for `runs.jsonl`, or None while the head is not stalled past `hours`."""
    try:
        lines = runs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    since: datetime | None = None
    skips = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind, action = event.get("event"), event.get("action")
        progress = kind == "advance" or (kind == "memory_write" and event.get("result") == "ok")
        if progress or (kind == "dispatch" and action in _STARTED):
            since, skips = None, 0
        elif kind == "dispatch" and action in _BUSY:
            skips += 1
            since = since or _time(event.get("ts"))
    if since is None:
        return None
    busy = (now or datetime.now(UTC)) - since
    if busy < timedelta(hours=hours):
        return None
    return {
        "code": "automation_busy_without_advance",
        "agent": "curator",
        "busy_since": since.isoformat(),
        "busy_skips": skips,
        "threshold_hours": hours,
        "message": (
            f"supervised head busy since {since.isoformat()} ({int(busy.total_seconds() // 3600)}h, "
            f"{skips} busy skip(s)) without an advance or memory_write; threshold {hours}h"
        ),
    }
