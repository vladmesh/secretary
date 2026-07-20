"""Card comment parsing shared by the two prompt renderers.

TASK.md (taskdoc, for the worker) and REVIEW.md (reviewer, for validation layer 3) both turn the
same Kanboard comment list into prompt sections, and both need the same two primitives: a readable
timestamp and the `[marker]`-prefixed body split. They live here so the marker grammar and the
operator-marker set cannot drift apart between the two prompts.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

MARKER_RE = re.compile(r"^\[([^\]]+)\]\n?(.*)\Z", re.DOTALL)

# Markers whose comments carry human intent about the spec, as opposed to machine reports: the PO's
# own comments, the secretary's, and the steward's (including its Blocked->Done override note).
OPERATOR_MARKERS = {"po", "secretary", "steward", "steward:blocked-done"}


def format_ts(ts) -> str:
    """A comment's `date_creation` (unix seconds, from Kanboard) as a readable UTC stamp."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(ts) if ts else "?"


def split_marker(text: str) -> tuple[str, str]:
    """`[marker]\\nbody` -> ("marker", "body"). An unmarked comment -> ("", whole text)."""
    m = MARKER_RE.match(text)
    if not m:
        return "", text.strip()
    return m.group(1), m.group(2).strip()
