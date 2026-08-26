"""Helpers for tests that drive one card through the tick's per-card decision.

The production tick opens an attempt per claim (`dispatcher_production`) and reads the running
one off the record for a card already in flight. A test that calls `_tick_task` directly has
neither, so it opens one here.
"""

from __future__ import annotations

from typing import Any

from secretary.dispatcher_state import new_attempt_id, record_attempt


def ensure_attempt(payload: dict[str, Any], reference: str, actor: str, owner: str) -> str:
    """The attempt the payload already carries, or a freshly recorded one."""
    attempt_id = str(payload.get("attempt_id") or "")
    if attempt_id:
        return attempt_id
    attempt_id = new_attempt_id()
    payload["attempt_id"] = attempt_id
    record_attempt(payload, attempt_id, reference, actor, owner)
    return attempt_id
