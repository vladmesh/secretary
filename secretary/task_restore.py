"""Audit-aware board restore operations for ``TaskWriter``."""

from __future__ import annotations

from typing import Any


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
    from secretary.tasks import _CommittedWriteError, _STATE_BY_COLUMN, TaskError, _task_number

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
        "restored", "steward", "restore", reference, request_id, payload, mutation,
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
    if "codex_launch_mode" in sanitized:
        if str(sanitized["codex_launch_mode"] or "") not in _CODEX_LAUNCH_MODES:
            sanitized["codex_launch_mode"] = ""
    return sanitized


def restore_comment(
    writer: Any, reference: str, body: str, occurrence: int, request_id: str | None
) -> dict[str, Any]:
    """Append one comment and retain pending state when its reply is lost."""
    from secretary.tasks import _CommittedWriteError, TaskError, _digest, _task_number

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
    return writer._write(
        "restored_comment", "steward", "restore", reference, request_id, payload, mutation,
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
        or (
            not swimlane
            and normalized.get("extensions", {}).get("kanboard", {}).get("swimlane") is not None
        )
        or (swimlane and normalized.get("extensions", {}).get("kanboard", {}).get("swimlane") != swimlane)
    ):
        raise TaskError("backend_error", "pending restore cleanup remains incomplete", 1)


def finish_pending_restore_comment(writer: Any, event: dict[str, Any], payload: dict[str, Any]) -> None:
    """Deduplicate an ambiguous comment write before closing its audit event."""
    from secretary.tasks import TaskError, _digest, _task_number

    ref = str(event.get("ref") or "")
    body = payload.get("restore_body")
    occurrence = payload.get("restore_occurrence")
    if not ref or not isinstance(body, str) or not isinstance(occurrence, int) or occurrence < 0:
        raise TaskError("backend_error", "pending restore comment is invalid", 1)
    digest = payload.get("body_sha256")
    matches = sum(
        _digest(str(comment.get("body") or "")) == digest
        for comment in writer.reader.show(ref).get("comments", [])
    )
    if matches <= occurrence:
        writer.client.call("createComment", task_id=_task_number(writer.reader.show(ref)), user_id=0, content=body)
        matches += 1
    if matches <= occurrence:
        raise TaskError("backend_error", "pending restore comment remains incomplete", 1)
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
    if task["state"] != target or (position is not None and raw_position != position) or (
        _positive_int(raw.get("swimlane_id")) != swimlane_id
    ):
        writer._move_raw(task, target, position=position or 1, swimlane_id=swimlane_id)
