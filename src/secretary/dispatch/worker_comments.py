"""The card comments a worker is handed: the PO's, the owner's and the observer's (secretary-1768).

A comment from one of these roles reads, to the person who wrote it, as an instruction to whoever
executes the card. Until this module the worker never saw one: TASK.md was built from the
description, the observer decision and the red bodies only, so a comment that narrowed the scope
was silently ignored (issue:bf753bbad004921bab5d). The selector here is the one answer to "which
comments does the worker get": TASK.md renders it on every launch and rework, and a comment that
lands while the worker runs is pointed at mid-round (`worker_report.deliver_worker_comments`).

Every other role is left out on purpose. The dispatcher's comments are its own bookkeeping and the
observer's Assessment decision reaches the worker through the dispatcher's prose; the worker's are
its own reports; the reviewer's verdict has its own section; steward and retro do not address the
worker.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The comment markers whose comments reach the worker, in the order the heading names them.
WORKER_COMMENT_ROLES: tuple[str, ...] = ("po", "owner", "observer")

WORKER_COMMENTS_HEADING = "## Comments from the PO, the owner and the observer"
WORKER_COMMENTS_RULE = "These refine the spec; when one contradicts the description, the comment wins."


@dataclass(frozen=True)
class WorkerComment:
    """One comment the worker is handed, and the key its mid-round delivery is recorded under."""

    key: str
    role: str
    at: str
    body: str


def _comment_text(comment: dict[str, Any], role: str) -> str:
    """The comment as its author wrote it: the stored body without its `[role]` marker line."""
    body = str(comment.get("body") or "")
    lines = body.split("\n", 1)
    if lines[0].strip() == f"[{role}]":
        return lines[1] if len(lines) > 1 else ""
    return body


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else event.get("payload")
    return data if isinstance(data, dict) else {}


def select_worker_comments(
    task: dict[str, Any], events: Iterable[dict[str, Any]]
) -> tuple[WorkerComment, ...]:
    """Every `po`, `owner` and `observer` comment on the card, oldest first.

    The board lists a card's comments in creation order, so that order is kept. Each comment is
    keyed by the audit event that wrote it, paired by marker and body digest, the n-th comment with
    a given pair taking the n-th such event. A comment no event accounts for (a restored or
    migrated one) is keyed by that same pair and its occurrence instead, which is as stable across
    ticks as an event id.
    """
    comments = [
        comment
        for comment in (task.get("comments") or [])
        if isinstance(comment, dict) and comment.get("marker") in WORKER_COMMENT_ROLES
    ]
    if not comments:
        return ()
    written: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("kind") != "commented" or event.get("outcome", "success") != "success":
            continue
        payload = _event_payload(event)
        marker = str(payload.get("marker") or "")
        if marker not in WORKER_COMMENT_ROLES or not event.get("event_id"):
            continue
        written.setdefault((marker, str(payload.get("body_sha256") or "")), []).append(event)
    seen: dict[tuple[str, str], int] = {}
    selected: list[WorkerComment] = []
    for comment in comments:
        role = str(comment["marker"])
        text = _comment_text(comment, role)
        pair = (role, hashlib.sha256(text.encode("utf-8")).hexdigest())
        occurrence = seen.get(pair, 0)
        seen[pair] = occurrence + 1
        matches = written.get(pair, [])
        event = matches[occurrence] if occurrence < len(matches) else None
        if event is not None:
            key = str(event["event_id"])
            at = str(event.get("occurred_at") or comment.get("created_at") or "")
        else:
            key = f"comment:{pair[0]}:{pair[1]}:{occurrence + 1}"
            at = str(comment.get("created_at") or "")
        selected.append(WorkerComment(key=key, role=role, at=at or "unknown time", body=text.strip()))
    return tuple(selected)


def worker_comments_section(comments: Iterable[WorkerComment]) -> list[str]:
    """The TASK.md section for these comments, or nothing at all when there are none."""
    comments = tuple(comments)
    if not comments:
        return []
    lines = [
        WORKER_COMMENTS_HEADING,
        "",
        WORKER_COMMENTS_RULE,
        "Oldest first, each under its author's role and the time it was written.",
        "",
    ]
    for comment in comments:
        lines += [f"### {comment.role}, {comment.at}", "", comment.body or "(empty comment)", ""]
    return lines


# The keys of the comments a TASK.md renders, recorded once more on a hidden line so the mid-round
# arm can tell which comments the live worker's document already carries. Base64 for the reason the
# observer-decision line gives in `helpers`: a comment body is arbitrary Markdown and must not be
# able to end or forge the field. Written last, so the final line in the file is the dispatcher's.
_COMMENTS_RECORD_RE = re.compile(r"^<!-- worker-comments keys=([A-Za-z0-9+/]*={0,2}) -->$", re.MULTILINE)


def worker_comments_record_line(comments: Iterable[WorkerComment]) -> str:
    joined = "\n".join(comment.key for comment in comments)
    encoded = base64.b64encode(joined.encode("utf-8")).decode("ascii")
    return f"<!-- worker-comments keys={encoded} -->"


def task_doc_comment_keys(workspace: str) -> frozenset[str]:
    """The comment keys the TASK.md in this checkout was rendered with; empty when it names none."""
    if not workspace:
        return frozenset()
    try:
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return frozenset()
    records = _COMMENTS_RECORD_RE.findall(document)
    if not records:
        return frozenset()
    try:
        decoded = base64.b64decode(records[-1].encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return frozenset()
    return frozenset(key for key in decoded.split("\n") if key)


def worker_comments_note(generation: int) -> str:
    """The tail of the mid-round pointer: what changed, and that the round is the same one.

    Short on purpose: `nudge_for` refuses a line over its ceiling, path included.
    """
    return f"Re-read its comments section: new PO/owner/observer comments. Same generation {generation}."
