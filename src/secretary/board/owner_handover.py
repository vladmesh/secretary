"""A `decision`/`operation` card the PO handed to the owner: the mark, and how it is read (secretary-1761).

`task handover --to owner` sets the mark on an In progress card the PO service executes and writes a
`[handover:owner]` PO comment with the reason, in one transaction. The mark is three fields of the
card's extension bag (`extensions.extra`, docs/BOARD_STORE.md §8.2), so no column and no migration:
`waiting_owner` (when, RFC 3339 UTC), `waiting_owner_reason` and `waiting_owner_by` (the PO actor).
Only `task handover` writes them, and a card that leaves In progress (`task complete` above all)
clears them in the transition's own transaction.

A mark is read only through :func:`waiting_owner`, which validates the three fields together: a bag
holding some of them, or a timestamp that does not parse, is no mark at all rather than a guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from secretary.board.extension_bag import EXTENSION_BAG

WAITING_OWNER = "waiting_owner"
WAITING_OWNER_REASON = "waiting_owner_reason"
WAITING_OWNER_BY = "waiting_owner_by"
MARK_KEYS = (WAITING_OWNER, WAITING_OWNER_REASON, WAITING_OWNER_BY)
#: The metadata write that removes the mark: an empty value is a removal from the bag.
CLEAR_MARK = {key: "" for key in MARK_KEYS}

#: The one recipient a card is handed to today.
OWNER = "owner"
#: The comment role, and actor, of the owner's answer on a card (`task comment --role owner`).
OWNER_ROLE = "owner"
#: The first content line of the PO comment `task handover` writes.
HANDOVER_MARKER = "handover:owner"
#: The audit kind of a handover; the owner-event card turns it into an owner event.
HANDED_TO_OWNER = "handed_to_owner"


def mark_values(since: str, reason: str, by: str) -> dict[str, str]:
    """The metadata write that sets the mark."""
    return {WAITING_OWNER: since, WAITING_OWNER_REASON: reason, WAITING_OWNER_BY: by}


def _bag(task: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    return bag if isinstance(bag, Mapping) else {}


def carries_mark_fields(task: Mapping[str, Any]) -> bool:
    """Whether any of the three fields is on the card, well-formed or not."""
    bag = _bag(task)
    return any(str(bag.get(key) or "") for key in MARK_KEYS)


def waiting_owner(task: Mapping[str, Any]) -> dict[str, str] | None:
    """The card's mark as `{since, reason, by}`, or None when it carries no well-formed one."""
    bag = _bag(task)
    since = str(bag.get(WAITING_OWNER) or "").strip()
    reason = str(bag.get(WAITING_OWNER_REASON) or "").strip()
    by = str(bag.get(WAITING_OWNER_BY) or "").strip()
    if not (since and reason and by):
        return None
    try:
        datetime.fromisoformat(since)
    except ValueError:
        return None
    return {"since": since, "reason": reason, "by": by}


def render_handover_comment(reason: str) -> str:
    """The comment body `task handover` writes as the PO (the role line is added by the writer)."""
    return f"[{HANDOVER_MARKER}]\n\n{reason.strip()}\n"


def _content_lines(comment: Mapping[str, Any]) -> list[str]:
    lines = str(comment.get("body") or "").splitlines()
    marker = comment.get("marker")
    if lines and marker and lines[0].strip() == f"[{marker}]":
        lines = lines[1:]
    return lines


def owner_comments_since_handover(comments: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The owner's comments after the card's latest `[handover:owner]` PO comment, oldest first."""
    found: list[dict[str, str]] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        lines = _content_lines(comment)
        if comment.get("marker") == "po" and lines and lines[0].strip() == f"[{HANDOVER_MARKER}]":
            found = []
        elif comment.get("marker") == OWNER_ROLE:
            found.append(
                {"created_at": str(comment.get("created_at") or ""), "body": "\n".join(lines).strip()}
            )
    return found


def owner_answer_event_ids(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Event ids of the owner's comments after the card's latest handover, in audit order."""
    found: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == HANDED_TO_OWNER:
            found = []
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if kind == "commented" and payload.get("marker") == OWNER_ROLE and event.get("event_id"):
            found.append(str(event["event_id"]))
    return found


__all__ = [
    "CLEAR_MARK",
    "HANDED_TO_OWNER",
    "HANDOVER_MARKER",
    "MARK_KEYS",
    "OWNER",
    "OWNER_ROLE",
    "WAITING_OWNER",
    "WAITING_OWNER_BY",
    "WAITING_OWNER_REASON",
    "carries_mark_fields",
    "mark_values",
    "owner_answer_event_ids",
    "owner_comments_since_handover",
    "render_handover_comment",
    "waiting_owner",
]
