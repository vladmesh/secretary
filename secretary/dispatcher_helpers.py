"""Small dispatcher helpers kept out of the runtime state machine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from secretary.dispatcher_state import attempt_request_id, request_token

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


WORKER_REPORT_MARKERS = {"report:done", "report:blocked"}
# The actions the dispatcher issues a report request id for. One per command in `TASK.md`: the done
# report and one per block classification, since a request id claims its payload.
_REPORT_ACTIONS = (
    "worker-report-done",
    "worker-report-blocked-external_fact",
    "worker-report-blocked-wrong_task_definition",
)


def _round_report_ids(workspace: str, attempt_id: str, reference: str, generation: int) -> set[str]:
    """The report request ids this round issued: the exact commands its worker was handed.

    Every one of them is unique to the round. The attempt is in there, so a later attempt on the
    same card never inherits an earlier one's reports, and so is the generation, so a later round of
    one attempt never inherits an earlier round's.

    The checkout answers first, because the document is what the live worker is holding and the
    dispatcher's own attempt id may have moved under it: a record that was lost and re-adopted gets
    a fresh attempt id while the worker keeps reporting through the commands it was given. Only ids
    that name the open generation are taken from it, so a document that is a round behind, a
    generation persisted and the tick that rewrites the document lost, cannot hand back the previous
    round's ids. What the dispatcher would issue itself is the fallback for a checkout it cannot
    read.
    """
    from_document = {
        request_id for request_id in _task_doc_report_request_ids(workspace)
        if request_id.endswith(f"-{request_token(reference)}-{generation}")
    }
    if from_document:
        return from_document
    return {
        attempt_request_id(attempt_id, action, reference, str(generation))
        for action in _REPORT_ACTIONS
    }


def _round_report_marker(audit: Any, reference: str, round_ids: set[str]) -> str | None:
    """The marker of a report that belongs to this round, or None if the round has none yet.

    A round ends only on a marker for the round that is open, and the marker on the board cannot
    answer which round that is: the comment is `[report:done]` whoever wrote it and for whichever
    round. What names the round is the request id its command carried, and the audit keeps that
    beside the marker, so the round is read from there rather than from the card's comments. A
    report filed under any other id records nothing of this round: not a command from a round that
    is over, not one from an earlier attempt on this card, and not an id a head invented for itself.

    Committed events only. `TaskWriter` stages its event before it writes the comment, so a staged
    event is a report that may not be on the board at all yet; consuming one would end the round on
    a call that had not happened. A report whose audit append failed after its comment landed is
    repaired by retrying the same command or by `reconcile`, and until then the round is unreported,
    which is a state the wait watchdog already bounds.
    """
    marker = None
    for event in audit.events(reference, kind="reported"):
        if str(event.get("request_id") or "") not in round_ids:
            continue
        candidate = (event.get("payload") or {}).get("marker")
        if candidate in WORKER_REPORT_MARKERS:
            marker = candidate
    return marker


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


def _task_doc_report_request_ids(workspace: str) -> list[str]:
    """The report request ids in this checkout's `TASK.md`, in document order.

    This is what the live worker is holding, which is the only durable record of a round's identity
    outside the dispatcher's own state: the ids name the attempt that handed them out and the round
    they belong to, and the document survives everything the state file does not.
    """
    if not workspace:
        return []
    try:
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    return [
        request_id for request_id in re.findall(r"--request-id\s+(\S+)", document)
        if "-worker-report-" in request_id
    ]


def _task_doc_report_generation(workspace: str) -> int:
    """The report generation the worker in this checkout was actually handed, or 0.

    A dispatcher record can be lost at any point, and the generation is dispatcher state. What is
    not lost is the document the live worker is working from: its report commands carry the round
    they belong to, so the checkout answers the question the state file no longer can.
    """
    generation = 0
    for request_id in _task_doc_report_request_ids(workspace):
        suffix = request_id.rsplit("-", 1)[-1]
        if suffix.isdigit():
            generation = max(generation, int(suffix))
    return generation


def _spent_report_generations(task: dict[str, Any]) -> int:
    """A floor under the generations this card has already spent, from its report markers.

    Only for recovery, and only as a floor: a round whose report the dispatcher has consumed is
    over, so the round running now is past all of them. Undershooting would hand a new round an id
    an earlier one already committed.

    Only consumed reports count, which is the same boundary `_report_adoption_baseline` reads. A
    report that is still waiting to be read belongs to the round that is running: stepping over it
    would leave the adopted record holding a generation no report on the board names, and a report
    is attributed to its round by the id its command carried (`_round_report_marker`). The document
    in the checkout usually answers this on its own; this is the floor for when it cannot.
    """
    return sum(
        1
        for comment in (task.get("comments") or [])[:_report_adoption_baseline(task)]
        if comment.get("marker") in WORKER_REPORT_MARKERS
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
