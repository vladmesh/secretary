"""Audit-aware board restore operations for ``TaskWriter``."""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RestoreCommentOccurrence:
    """One exported comment occurrence and its stable backend/audit identity."""

    reference: str
    task_id: int
    body: str
    occurrence: int
    request_id: str
    entity: str = "card"


def restore_comments_batched(writer: Any, occurrences: list[RestoreCommentOccurrence]) -> None:
    """Restore ordered comment histories without the interactive writer read cycle.

    One wave contains at most one comment for any entity.  JSON-RPC does not
    promise execution order inside a batch, so this is the boundary that permits
    batching across entities without weakening order within one history.
    """
    from secretary.tasks import _BATCH_CHUNK, TaskError, _now

    grouped: dict[str, list[RestoreCommentOccurrence]] = {}
    for item in occurrences:
        grouped.setdefault(item.reference, []).append(item)
    for items in grouped.values():
        # ``occurrence`` is the duplicate-body ordinal in the public audit
        # contract, not the history index.
        seen: dict[str, int] = {}
        for item in items:
            expected = seen.get(item.body, 0)
            if item.occurrence != expected:
                raise TaskError("validation", "restore comment occurrence identity is invalid", 2)
            seen[item.body] = expected + 1

    positions = {reference: 0 for reference in grouped}
    references = sorted(grouped)
    while any(positions[reference] < len(grouped[reference]) for reference in references):
        active = [reference for reference in references if positions[reference] < len(grouped[reference])]
        for start in range(0, len(active), _BATCH_CHUNK):
            chunk_refs = active[start : start + _BATCH_CHUNK]
            with ExitStack() as locks:
                for reference in chunk_refs:
                    locks.enter_context(writer.audit.marker_comment_lock(reference))
                histories = _read_comment_histories(
                    writer.client,
                    [(reference, grouped[reference][0].task_id) for reference in chunk_refs],
                )
                writes: list[tuple[str, dict[str, Any]]] = []
                staged: list[tuple[RestoreCommentOccurrence, dict[str, Any]]] = []
                next_items: list[RestoreCommentOccurrence] = []
                for reference in chunk_refs:
                    target = grouped[reference]
                    live = histories[reference]
                    target_bodies = [item.body for item in target]
                    if len(live) > len(target_bodies) or live != target_bodies[: len(live)]:
                        raise TaskError(
                            "backend_error",
                            f"restored comment history does not match normalized prefix for {reference}",
                            1,
                        )
                    # Reconcile every already-present prefix occurrence before
                    # considering a new write.  This closes lost replies and
                    # interruption after write without duplicating a sibling.
                    for index in range(positions[reference], len(live)):
                        item = target[index]
                        event = _restore_comment_event(writer, item, _now)
                        _commit_proven_comment(writer, item, event, len(live))
                    positions[reference] = len(live)
                    if positions[reference] == len(target):
                        continue
                    next_items.append(target[positions[reference]])
                owners = writer.audit.pending_marker_owners(
                    (item.reference, item.body, item.request_id) for item in next_items
                )
                for item in next_items:
                    event = _restore_comment_event(writer, item, _now)
                    if writer.audit.committed_event(
                        item.request_id
                    ) is not None or "restore_body" not in event.get("payload", {}):
                        raise TaskError(
                            "backend_error",
                            f"committed restore comment is absent from {reference}",
                            1,
                        )
                    owner = owners.get(item.request_id)
                    if owner is not None:
                        raise TaskError(
                            "audit_pending",
                            f"an earlier identical Card marker occurrence is pending; reconcile request {owner} first",
                            4,
                        )
                    writer.audit.stage(item.request_id, event)
                    writes.append(
                        ("createComment", {"task_id": item.task_id, "user_id": 0, "content": item.body})
                    )
                    staged.append((item, event))
                if not writes:
                    continue
                try:
                    answers = writer.client.call_batch(writes)
                    if any(
                        not isinstance(answer, int) or isinstance(answer, bool) or answer <= 0
                        for answer in answers
                    ):
                        raise TaskError("backend_error", "Kanboard rejected a batched comment write", 1)
                except Exception:  # noqa: BLE001 - any aggregate failure makes every answer uncertain.
                    _reconcile_uncertain_chunk(writer, grouped, positions, staged)
                    raise TaskError(
                        "audit_pending",
                        "batched comment write is uncertain; pending occurrences were reconciled",
                        4,
                    ) from None
                for item, event in staged:
                    _commit_proven_comment(writer, item, event, positions[item.reference] + 1)
                    positions[item.reference] += 1


def _read_comment_histories(client: Any, subjects: list[tuple[str, int]]) -> dict[str, list[str]]:
    """Read histories in Kanboard's stable creation order.

    Pinned Kanboard v1.2.46 orders CommentModel.getAll by
    ``date_creation ASC, id ASC``.  The id tie-break is the durable creation
    order when several restores share the same one-second timestamp; the
    disposable backend contract canary is recorded in the recovery docs.
    """
    from secretary.tasks import TaskError

    answers = client.call_batch(("getAllComments", {"task_id": task_id}) for _, task_id in subjects)
    result: dict[str, list[str]] = {}
    for (reference, _task_id), answer in zip(subjects, answers, strict=True):
        if not isinstance(answer, list) or any(not isinstance(value, dict) for value in answer):
            raise TaskError("backend_error", "Kanboard returned invalid task comments", 1)
        result[reference] = [str(value.get("comment") or "") for value in answer]
    return result


def _restore_comment_event(writer: Any, item: RestoreCommentOccurrence, now: Any) -> dict[str, Any]:
    from secretary.tasks import TaskError, _digest

    identity = {"body_sha256": _digest(item.body), "restore_occurrence": item.occurrence}
    committed = writer.audit.committed_event(item.request_id)
    if committed is not None:
        writer.audit.require_claim(
            committed, kind="restored_comment", reference=item.reference, identity=identity
        )
        return committed
    pending = writer.audit.pending_event(item.request_id)
    if pending is not None:
        writer.audit.require_claim(
            pending, kind="restored_comment", reference=item.reference, identity=identity
        )
        body = pending.get("payload", {}).get("restore_body")
        if body is not None and body != item.body:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        return pending
    prefix = "sprint_kanboard_" if item.entity == "sprint" else "task_kanboard_"
    return {
        "event_id": "evt_" + uuid.uuid4().hex,
        "schema_version": 1,
        "occurred_at": now(),
        "actor": {"role": "steward", "id": "restore"},
        "kind": "restored_comment",
        "outcome": "success",
        "task_id": f"{prefix}{item.task_id}",
        "ref": item.reference,
        "backend": {"kind": "kanboard", "task_id": item.task_id, "revision": "pending"},
        "request_id": item.request_id,
        "payload": {**identity, "restore_body": item.body},
    }


def _commit_proven_comment(
    writer: Any, item: RestoreCommentOccurrence, event: dict[str, Any], history_size: int
) -> None:
    from secretary.tasks import TaskError

    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise TaskError("backend_error", "pending restore comment is invalid", 1)
    if writer.audit.committed_event(item.request_id) is not None:
        return
    event["backend"]["revision"] = f"comments:{history_size}"
    payload.pop("restore_body", None)
    writer.audit.stage(item.request_id, event)
    try:
        writer.audit.append(item.request_id, event)
    except OSError:
        raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None


def _reconcile_uncertain_chunk(
    writer: Any,
    grouped: dict[str, list[RestoreCommentOccurrence]],
    positions: dict[str, int],
    staged: list[tuple[RestoreCommentOccurrence, dict[str, Any]]],
) -> None:
    subjects = [(item.reference, item.task_id) for item, _event in staged]
    try:
        histories = _read_comment_histories(writer.client, subjects)
    except Exception:  # noqa: BLE001 - a failed evidence read must preserve every pending body.
        return
    for item, event in staged:
        target = grouped[item.reference]
        live = histories[item.reference]
        bodies = [value.body for value in target]
        if len(live) <= len(bodies) and live == bodies[: len(live)] and len(live) > positions[item.reference]:
            _commit_proven_comment(writer, item, event, len(live))
            positions[item.reference] += 1


def restore_card(
    writer: Any,
    reference: str,
    metadata: dict[str, str],
    target: str,
    position: int | None,
    swimlane: str,
    request_id: str | None,
) -> dict[str, Any]:
    """Apply an audited restore-only metadata and placement update."""
    from secretary.tasks import (
        _STATE_BY_COLUMN,
        TaskError,
        _CommittedWriteError,
        _task_number,
    )

    if target not in _STATE_BY_COLUMN.values():
        raise TaskError("validation", "restore target is invalid", 2)
    if position is not None and position < 1:
        raise TaskError("validation", "restore position must be positive", 2)
    metadata = _without_retired_launch_mode(metadata)

    def mutation(task: dict[str, Any]) -> None:
        writer.client.call("saveTaskMetadata", task_id=_task_number(task), values=metadata)
        try:
            _restore_placement(writer, task, target, position, swimlane)
        except Exception as exc:
            raise _CommittedWriteError() from exc

    payload = {
        "target": target,
        "metadata_keys": sorted(metadata),
        "position": position,
        "swimlane": swimlane or None,
    }
    return writer._write(
        "restored",
        "steward",
        "restore",
        reference,
        request_id,
        payload,
        mutation,
        identity=payload,
    )


def _without_retired_launch_mode(metadata: dict[str, str]) -> dict[str, str]:
    """The restore payload with no retired Codex launch mode left in it.

    This is the write boundary, so it is where the retirement has to be enforced: everything below
    hands `metadata` to `saveTaskMetadata` verbatim, and a checkpoint taken before the TUI-only
    rule still carries `codex_launch_mode=exec` in its raw export. Normalized readers already hide
    such a value, but hiding is not removing — restoring it would put a launch shape the product
    no longer has back onto a live board, where the next export would carry it forward again.

    The key is cleared rather than dropped: `saveTaskMetadata` is a partial update, so dropping it
    would leave a retired value already on the card exactly where it was. Every mode the product
    still has passes through untouched.
    """
    from secretary.tasks import _CODEX_LAUNCH_MODES

    sanitized = dict(metadata)
    if (
        "codex_launch_mode" in sanitized
        and str(sanitized["codex_launch_mode"] or "") not in _CODEX_LAUNCH_MODES
    ):
        sanitized["codex_launch_mode"] = ""
    return sanitized


def restore_comment(
    writer: Any, reference: str, body: str, occurrence: int, request_id: str | None
) -> dict[str, Any]:
    """Append one comment and retain pending state when its reply is lost."""
    from secretary.tasks import TaskError, _CommittedWriteError, _digest, _task_number

    payload: dict[str, Any] = {
        "body_sha256": _digest(body),
        "restore_occurrence": occurrence,
        "restore_body": body,
    }

    def mutation(task: dict[str, Any]) -> None:
        try:
            writer.client.call("createComment", task_id=_task_number(task), user_id=0, content=body)
        except TaskError as exc:
            if exc.code == "backend_unavailable":
                raise _CommittedWriteError() from exc
            raise
        payload.pop("restore_body", None)

    # `restore_body` is dropped from the payload once the comment is known to be on the card, so
    # it is not part of what the id claims; the body digest and the occurrence are.
    identity = {"body_sha256": payload["body_sha256"], "restore_occurrence": occurrence}
    with writer.audit.marker_comment_lock(reference):
        owner = writer.audit.pending_marker_owner(reference, body, request_id=request_id)
        if owner is not None:
            raise TaskError(
                "audit_pending",
                f"an earlier identical Card marker occurrence is pending; reconcile request {owner} first",
                4,
            )
        return writer._write(
            "restored_comment",
            "steward",
            "restore",
            reference,
            request_id,
            payload,
            mutation,
            identity=identity,
        )


def finish_pending_restore(writer: Any, event: dict[str, Any], payload: dict[str, Any]) -> None:
    """Finish a restore whose metadata committed before its placement failed."""
    from secretary.tasks import _STATE_BY_COLUMN, TaskError

    target = payload.get("target")
    if target not in _STATE_BY_COLUMN.values():
        raise TaskError("backend_error", "pending restore is missing its target state", 1)
    ref = str(event.get("ref") or "")
    if not ref:
        raise TaskError("backend_error", "pending restore is missing its task ref", 1)
    task = writer.reader.show(ref)
    position = payload.get("position")
    if not isinstance(position, int) or position < 1:
        position = None
    swimlane = payload.get("swimlane")
    if not isinstance(swimlane, str):
        swimlane = ""
    _restore_placement(writer, task, target, position, swimlane)
    normalized = writer.reader.show(ref)
    if (
        normalized["state"] != target
        or (position is not None and normalized["position"] != position)
        or (not swimlane and normalized.get("extensions", {}).get("kanboard", {}).get("swimlane") is not None)
        or (swimlane and normalized.get("extensions", {}).get("kanboard", {}).get("swimlane") != swimlane)
    ):
        raise TaskError("backend_error", "pending restore cleanup remains incomplete", 1)


def finish_pending_restore_comment(writer: Any, event: dict[str, Any], payload: dict[str, Any]) -> None:
    """Prove or finish one ambiguous Card/Sprint comment before audit append."""
    from secretary.tasks import TaskError, _digest, _positive_int

    ref = str(event.get("ref") or "")
    body = payload.get("restore_body")
    digest = payload.get("body_sha256")
    occurrence = payload.get("restore_occurrence")
    backend = event.get("backend") if isinstance(event.get("backend"), dict) else {}
    task_id = _positive_int(backend.get("task_id"))
    if (
        not ref
        or task_id is None
        or (body is not None and not isinstance(body, str))
        or not isinstance(digest, str)
        or not isinstance(occurrence, int)
        or occurrence < 0
    ):
        raise TaskError("backend_error", "pending restore comment is invalid", 1)
    with writer.audit.marker_comment_lock(ref):
        if isinstance(body, str):
            owner = writer.audit.pending_marker_owner(
                ref, body, request_id=str(event.get("request_id") or "")
            )
            if owner is not None:
                raise TaskError(
                    "audit_pending",
                    "an earlier identical Card/Sprint marker occurrence is pending; "
                    f"reconcile request {owner} first",
                    4,
                )

        def matching_count() -> int:
            answers = writer.client.call_batch([("getAllComments", {"task_id": task_id})])
            comments = answers[0]
            if not isinstance(comments, list) or any(not isinstance(comment, dict) for comment in comments):
                raise TaskError("backend_error", "Kanboard returned invalid task comments", 1)
            return sum(_digest(str(comment.get("comment") or "")) == digest for comment in comments)

        matches = matching_count()
        if matches <= occurrence:
            if body is None:
                raise TaskError(
                    "audit_pending",
                    "legacy Sprint restore comment is not present and cannot be retried without its body",
                    4,
                )
            answer = writer.client.call("createComment", task_id=task_id, user_id=0, content=body)
            if not isinstance(answer, int) or isinstance(answer, bool) or answer <= 0:
                raise TaskError("backend_error", "Kanboard rejected the restored comment", 1)
            matches = matching_count()
        if matches <= occurrence:
            raise TaskError("backend_error", "pending restore comment remains incomplete", 1)
        event["backend"]["revision"] = f"comments:{matches}"
        payload.pop("restore_body", None)


def _restore_placement(
    writer: Any, task: dict[str, Any], target: str, position: int | None, swimlane: str
) -> None:
    from secretary.tasks import (
        TaskError,
        _nonnegative_int,
        _positive_int,
        project_card_by_reference,
    )

    board_id, _, swimlanes = writer.reader._board()
    raw = project_card_by_reference(writer.client, board_id, task["ref"])
    if not isinstance(raw, dict):
        raise TaskError("not_found", "task was not found", 2)
    swimlane_id = 0
    if swimlane:
        swimlane_id = next((identifier for identifier, name in swimlanes.items() if name == swimlane), 0)
        if not swimlane_id:
            raise TaskError("backend_error", "restored swimlane is unavailable", 1)
    raw_position = _nonnegative_int(raw.get("position"))
    if (
        task["state"] != target
        or (position is not None and raw_position != position)
        or (_positive_int(raw.get("swimlane_id")) != swimlane_id)
    ):
        writer._move_raw(task, target, position=position or 1, swimlane_id=swimlane_id)
