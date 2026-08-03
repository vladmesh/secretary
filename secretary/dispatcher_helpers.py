"""Small dispatcher helpers kept out of the runtime state machine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ASSIGN_RE = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*=\s*\S+")
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+=_-]{40,}\b")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _worker_id(task: dict[str, Any]) -> str:
    slug = task.get("workspace", {}).get("slug") or _slug(task.get("title") or task["ref"])
    return f"{task['ref']}-{slug}"[:80].strip("-")


def _legacy_worker_branch(reference: str) -> str:
    return f"pipeline/{reference}"


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:30] or "task"


def _last_marker(task: dict[str, Any], baseline: int, markers: set[str]) -> str | None:
    result = None
    for comment in (task.get("comments") or [])[baseline:]:
        marker = comment.get("marker")
        if marker in markers:
            result = marker
    return result


# How many substantive red reviews a card with nobody to decide for it may collect before it
# stops asking for another worker round (secretary-1033). Three is deliberately early and will
# fire on cards that were about to converge: without an observer, Blocked is a question to a
# person rather than lost work, and it is cheaper to ask than to let a card eat five rounds.
RED_REVIEW_CEILING = 3


def red_review_count(task: dict[str, Any]) -> int:
    """The card's own count of substantive red reviews.

    A reviewer's red verdict is one comment marked `review:red` and the board keeps it for the
    life of the card, so the count is durable, needs no sprint to live in, and is idempotent
    without any bookkeeping: a replayed verdict write dedupes on its request id and creates no
    second comment, and a replayed dispatcher tick reads the same comments and gets the same
    number. A red mechanical gate and a red CI rollup leave dispatcher comments with no verdict
    marker and are not counted, which is the separation the sprint budget already makes between
    `red_review` and `red_ci`.
    """
    return sum(
        1 for comment in (task.get("comments") or []) if comment.get("marker") == "review:red"
    )


# Fences around the observer decision in the worker's TASK.md. They delimit the decision text
# exactly, so a record lost mid-round can read back what the live worker was told to follow rather
# than guessing at a heading.
DECISION_OPEN_MARKER = "<!-- observer-decision -->"
DECISION_CLOSE_MARKER = "<!-- /observer-decision -->"

_GATE_RED_PREFIX = "The mechanical validation gate is red"
# Hidden marker line carrying the SHA-independent failure fingerprint (secretary-766): a visible
# GitHub `detail` always contains the head SHA, which changes on every rework commit, so repeat
# detection cannot key off the rendered text itself. Stripped back out before the body reaches
# the worker's TASK.md.
_GATE_FINGERPRINT_PREFIX = "<!-- gate-fingerprint: "


def _last_gate_red_body(task: dict[str, Any]) -> str | None:
    """Most recent mechanical-gate-red bounce comment, for delivery to the rework worker.

    Mirrors `_last_review_red_body`: without this the rework prompt never explains why the
    mechanical gate (CI or local validation) failed, so the worker re-reports the same red
    commit or edits code at random (secretary-766).
    """
    body = None
    for comment in task.get("comments") or []:
        if comment.get("marker") == "dispatcher" and _GATE_RED_PREFIX in (comment.get("body") or ""):
            body = comment.get("body")
    if not body:
        return None
    lines = body.splitlines()
    if lines and lines[0].strip() == "[dispatcher]":
        lines = lines[1:]
    lines = [line for line in lines if not line.startswith(_GATE_FINGERPRINT_PREFIX)]
    return "\n".join(lines).strip() or None


def _gate_red_repeat_count(task: dict[str, Any], fingerprint: str) -> int:
    """How many times this exact gate-failure fingerprint has already bounced this card.

    A worker stuck reproducing the same failure needs to see that the reason did not change
    since last time, or an identical silent bounce reads as the rework round having done
    nothing at all. Keyed on the fingerprint rather than the rendered detail text: a GitHub
    `detail` always carries the head SHA, which changes on every rework commit, so matching on
    detail text alone would never recognise a repeat (secretary-766).
    """
    if not fingerprint:
        return 0
    needle = f"{_GATE_FINGERPRINT_PREFIX}{fingerprint} -->"
    return sum(
        1
        for comment in task.get("comments") or []
        if comment.get("marker") == "dispatcher" and needle in (comment.get("body") or "")
    )


def _last_marker_body(task: dict[str, Any], marker: str) -> str | None:
    """Text of the most recent comment carrying this marker, with the marker line stripped."""
    body = None
    for comment in (task.get("comments") or []):
        if comment.get("marker") == marker:
            body = comment.get("body")
    if not body:
        return None
    lines = body.splitlines()
    if lines and lines[0].strip() == f"[{marker}]":
        lines = lines[1:]
    return "\n".join(lines).strip() or None


def _last_review_red_body(task: dict[str, Any]) -> str | None:
    """Findings from the most recent review:red verdict, for delivery to the rework worker.

    The rework prompt otherwise carries only the card description, so without this the worker
    reworks blind and re-reports the same commit, looping red forever.
    """
    return _last_marker_body(task, "review:red")


def _review_adoption_baseline(task: dict[str, Any]) -> int:
    baseline = len(task.get("comments") or [])
    for index, comment in enumerate(task.get("comments") or []):
        if comment.get("marker") == "report:done":
            baseline = index + 1
    return baseline


def _task_doc_report_generation(workspace: str) -> int:
    """The report generation the worker in this checkout was actually handed, or 0.

    A dispatcher record can be lost at any point, and the generation is dispatcher state. What is
    not lost is the document the live worker is working from: its report commands carry the round
    they belong to, so the checkout answers the question the state file no longer can.
    """
    if not workspace:
        return 0
    try:
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return 0
    generation = 0
    for request_id in re.findall(r"--request-id\s+(\S+)", document):
        if "-worker-report-" not in request_id:
            continue
        suffix = request_id.rsplit("-", 1)[-1]
        if suffix.isdigit():
            generation = max(generation, int(suffix))
    return generation


def _task_doc_decision(workspace: str) -> str:
    """The observer decision the worker in this checkout was actually handed, or "".

    The same recovery as `_task_doc_report_generation`, for the same reason and from the same file:
    the decision is frozen in dispatcher state, that state can be lost, and the document the live
    worker is following is what still names the adjudication its round was opened on. Re-reading
    the card's newest decision comment instead would be the defect this fence exists to close.
    """
    if not workspace:
        return ""
    try:
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    start = document.find(DECISION_OPEN_MARKER)
    if start < 0:
        return ""
    end = document.find(DECISION_CLOSE_MARKER, start)
    if end < 0:
        return ""
    return document[start + len(DECISION_OPEN_MARKER):end].strip()


def _spent_report_generations(task: dict[str, Any]) -> int:
    """A floor under the generations this card has already spent, from its report markers.

    Only for recovery, and only as a floor: every report on the board closed a round, so the round
    running now is past all of them. Overshooting is harmless, because an unused generation costs
    nothing; undershooting would hand a new round an id an earlier one already committed.
    """
    return sum(
        1
        for comment in task.get("comments") or []
        if comment.get("marker") in {"report:done", "report:blocked"}
    )


def _report_adoption_baseline(task: dict[str, Any]) -> int:
    """Where to start looking for worker report markers on an adopted card.

    `len(comments)` would hide a `report:done` the worker already posted: the dispatcher can lose
    its record at any point (restart, state reset, any `records.pop`), and re-adoption then blinds
    it to the finished report. It used to cost one extra tick; with the wait watchdog it burns the
    whole worker ceiling, respawns the head to redo work that is already on the board, and tells
    the operator "no worker report within Ns", which is false.

    Every report the dispatcher has consumed is followed by the dispatcher's own comment (the move
    to Validate, the review:red bounce, the gate-red bounce), so the last dispatcher comment is the
    boundary: markers after it are unconsumed.
    """
    baseline = 0
    for index, comment in enumerate(task.get("comments") or []):
        if comment.get("marker") == "dispatcher":
            baseline = index + 1
    return baseline


def scrub_host_output(text: str) -> str:
    text = _ASSIGN_RE.sub(r"\1=<redacted>", text)
    return _BLOB_RE.sub(lambda match: match.group(0) if _HEX_RE.match(match.group(0)) else "<redacted>", text)


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
