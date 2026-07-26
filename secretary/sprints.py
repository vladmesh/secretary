"""Sprint entities stored as tasks on a dedicated Kanboard board."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from secretary.tasks import (
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    _digest,
    _now,
    _positive_int,
    _rfc3339,
    _text,
)


SPRINT_BOARD_NAME = "Secretary sprints"
SPRINT_REFERENCE_PREFIX = "sprint:"
SPRINT_METADATA = {
    "sprint_goal",
    "sprint_definition_of_done",
    "sprint_repositories",
    "sprint_status",
    "sprint_budget",
    "sprint_current_task",
}
BUDGET_EVENT_TYPES = (
    "red_review",
    "blocked",
    "red_ci",
    "preempt",
    "recreated_task",
    "hotfix",
)


def ensure_sprint_board(client: KanboardClient) -> int:
    """Return the dedicated sprint board, creating it once when absent."""
    board = client.call("getProjectByName", name=SPRINT_BOARD_NAME)
    board_id = _positive_int(board.get("id")) if isinstance(board, dict) else None
    if board_id is None:
        board_id = _positive_int(client.call("createProject", name=SPRINT_BOARD_NAME))
    if board_id is None:
        raise TaskError("backend_error", "Kanboard did not create the sprint board", 1)
    return board_id


class SprintReader:
    def __init__(self, client: KanboardClient) -> None:
        self.client = client

    def list(self, *, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        board_id = ensure_sprint_board(self.client)
        raw_sprints = self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
        if not isinstance(raw_sprints, list):
            raise TaskError("backend_error", "Kanboard returned an invalid sprint list", 1)
        result = []
        for raw in raw_sprints:
            if not isinstance(raw, dict):
                continue
            sprint = self._normalize(raw, comments=None)
            if statuses and sprint["status"] not in statuses:
                continue
            result.append(sprint)
        return sorted(result, key=lambda sprint: (sprint["status"], sprint["ref"], sprint["id"]))

    def show(self, reference: str, *, include_cards: bool = True) -> dict[str, Any]:
        board_id = ensure_sprint_board(self.client)
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
        if not isinstance(raw, dict):
            raise TaskError("not_found", "sprint was not found", 2)
        task_id = _positive_int(raw.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
        comments_raw = self.client.call("getAllComments", task_id=task_id) or []
        comments = [
            {"created_at": _rfc3339(comment.get("date_creation")), "body": _text(comment.get("comment"))}
            for comment in comments_raw if isinstance(comment, dict)
        ]
        sprint = self._normalize(raw, comments=comments)
        if include_cards:
            sprint["cards"] = TaskReader(self.client).list(sprint=reference)
        return sprint

    def _normalize(self, raw: dict[str, Any], *, comments: list[dict[str, Any]] | None) -> dict[str, Any]:
        task_id = _positive_int(raw.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
        metadata = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(metadata, dict):
            raise TaskError("backend_error", "Kanboard returned invalid sprint metadata", 1)
        meta = {
            str(key): _text(value)
            for key, value in metadata.items()
            if str(key) in SPRINT_METADATA
        }
        repositories = _json_list(meta.get("sprint_repositories"))
        budget = _budget(meta.get("sprint_budget"))
        result: dict[str, Any] = {
            "id": f"sprint_kanboard_{task_id}",
            "ref": _text(raw.get("reference")),
            "goal": meta.get("sprint_goal", ""),
            "definition_of_done": meta.get("sprint_definition_of_done", ""),
            "repositories": repositories,
            "status": meta.get("sprint_status") if meta.get("sprint_status") in {"open", "closed"} else "open",
            "budget": budget,
            "current_task": meta.get("sprint_current_task") or None,
            "audit": {
                "created_at": _rfc3339(raw.get("date_creation")),
                "updated_at": _rfc3339(raw.get("date_modification")),
                "backend": {"kind": "kanboard", "kanboard_task_id": task_id, "board": SPRINT_BOARD_NAME},
            },
        }
        if comments is not None:
            result["comments"] = comments
        return result


class SprintWriter:
    """Sprint mutations with the task protocol's durable audit semantics."""

    def __init__(self, client: KanboardClient, *, data_dir: str | Path) -> None:
        self.client = client
        self.reader = SprintReader(client)
        self.audit = TaskAudit(data_dir)

    def create(
        self, *, role: str, actor: str, goal: str, definition_of_done: str = "",
        repositories: list[str] | None = None, reference: str = "", request_id: str | None = None,
    ) -> dict[str, Any]:
        self._role(role, {"po", "steward"})
        goal = goal.strip()
        reference = reference.strip()
        repos = _repositories(repositories or [])
        if not goal:
            raise TaskError("validation", "create requires a non-empty goal", 2)
        if reference and not reference.startswith(SPRINT_REFERENCE_PREFIX):
            raise TaskError("validation", f"sprint reference must start with {SPRINT_REFERENCE_PREFIX}", 2)
        board_id = ensure_sprint_board(self.client)
        if reference and self.client.call("getTaskByReference", project_id=board_id, reference=reference):
            raise TaskError("validation", "sprint reference already exists", 2)
        if reference:
            try:
                TaskReader(self.client).show(reference)
            except TaskError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise TaskError("validation", "sprint reference already belongs to a Pipeline card", 2)

        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            return self._committed("created", committed)
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            return self._pending("created", pending)
        event = self._event("created", role, actor, reference, request_id, {
            "goal_sha256": _digest(goal), "definition_of_done_sha256": _digest(definition_of_done),
            "repositories": repos,
        })
        self.audit.stage(request_id, event)
        try:
            columns = self.client.call("getColumns", project_id=board_id) or []
            column_id = next((_positive_int(column.get("id")) for column in columns if isinstance(column, dict)), None)
            if column_id is None:
                raise TaskError("backend_error", "sprint board has no column", 1)
            task_id = _positive_int(self.client.call(
                "createTask", project_id=board_id, title=goal, description="", column_id=column_id,
            ))
            if task_id is None:
                raise TaskError("backend_error", "Kanboard rejected the sprint write", 1)
            created_ref = reference or f"{SPRINT_REFERENCE_PREFIX}{task_id}"
            event.update({"ref": created_ref, "task_id": f"sprint_kanboard_{task_id}"})
            event["backend"]["task_id"] = task_id
            self.audit.stage(request_id, event)
            if not self.client.call("updateTask", id=task_id, reference=created_ref):
                raise TaskError("backend_error", "Kanboard rejected the sprint write", 1)
            self.client.call("saveTaskMetadata", task_id=task_id, values={
                "sprint_goal": goal,
                "sprint_definition_of_done": definition_of_done,
                "sprint_repositories": json.dumps(repos, separators=(",", ":")),
                "sprint_status": "open",
                "sprint_budget": json.dumps(_budget(), separators=(",", ":")),
                "sprint_current_task": "",
            })
        except Exception:
            # A created Kanboard task is recoverable from the staged event, so retain it.
            if event.get("backend", {}).get("task_id"):
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            self.audit.discard(request_id)
            raise
        return self._record("created", event)

    def comment(self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "worker", "reviewer", "steward", "retro"})
        if not body.strip():
            raise TaskError("validation", "comment requires a non-empty body", 2)
        return self._write("commented", role, actor, reference, request_id, {"body_sha256": _digest(body)}, lambda sprint: self.client.call("createComment", task_id=_sprint_number(sprint), user_id=0, content=f"[{role}]\n{body}"))

    def set_current_task(self, *, role: str, actor: str, reference: str, task_reference: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "steward"})
        task_reference = task_reference.strip()
        if not task_reference:
            raise TaskError("validation", "current task requires a task reference", 2)
        def mutation(sprint: dict[str, Any]) -> None:
            task = TaskReader(self.client).show(task_reference)
            if task.get("sprint") != reference:
                raise TaskError("validation", "current task is not linked to this sprint", 2)
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values={"sprint_current_task": task_reference})
        return self._write("current_task_set", role, actor, reference, request_id, {"task": task_reference}, mutation)

    def record_budget(self, *, role: str, actor: str, reference: str, event_type: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "steward"})
        if event_type not in BUDGET_EVENT_TYPES:
            raise TaskError("validation", "unknown budget event type " + repr(event_type), 2)
        def mutation(sprint: dict[str, Any]) -> None:
            budget = _budget(sprint.get("budget"))
            budget["by_type"][event_type] += 1
            budget["total"] += 1
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values={"sprint_budget": json.dumps(budget, separators=(",", ":"))})
        return self._write("budget_recorded", role, actor, reference, request_id, {"event_type": event_type}, mutation)

    def close(self, *, role: str, actor: str, reference: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"po"})
        return self._write("closed", role, actor, reference, request_id, {}, lambda sprint: self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values={"sprint_status": "closed"}))

    def _write(self, kind: str, role: str, actor: str, reference: str, request_id: str | None, payload: dict[str, Any], mutation: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            return self._committed(kind, committed)
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            return self._pending(kind, pending)
        sprint = self.reader.show(reference)
        if sprint["status"] == "closed" and kind in {"commented", "current_task_set"}:
            raise TaskError("closed", "sprint is closed", 3)
        event = self._event(kind, role, actor, reference, request_id, payload, sprint)
        self.audit.stage(request_id, event)
        try:
            mutation(sprint)
        except Exception:
            self.audit.discard(request_id)
            raise
        return self._record(kind, event)

    def _record(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        sprint = self.reader.show(str(event["ref"]))
        event["task_id"] = sprint["id"]
        event["backend"]["revision"] = "updated_at:" + str(sprint["audit"]["updated_at"] or "unknown")
        request_id = str(event["request_id"])
        self.audit.stage(request_id, event)
        event_id = self.audit.append(request_id, event)
        return {"action": kind, "sprint": sprint, "event_id": event_id}

    def _committed(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        return {"action": kind, "sprint": self.reader.show(str(event["ref"])), "event_id": self.audit.append(str(event["request_id"]), event)}

    def _pending(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        # The staged event is only retained after a successful backend mutation in the
        # simple writes. Creation stages its Kanboard id before assigning metadata.
        sprint = self.reader.show(str(event["ref"]))
        event["task_id"] = sprint["id"]
        event["backend"]["revision"] = "updated_at:" + str(sprint["audit"]["updated_at"] or "unknown")
        self.audit.stage(str(event["request_id"]), event)
        return {"action": kind, "sprint": sprint, "event_id": self.audit.append(str(event["request_id"]), event)}

    @staticmethod
    def _event(kind: str, role: str, actor: str, reference: str, request_id: str, payload: dict[str, Any], sprint: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(),
            "actor": {"role": role, "id": actor}, "kind": kind, "outcome": "success",
            "task_id": sprint["id"] if sprint else "", "ref": reference,
            "backend": {"kind": "kanboard", "task_id": _sprint_number(sprint) if sprint else None, "revision": "pending"},
            "request_id": request_id, "payload": payload,
        }

    @staticmethod
    def _role(role: str, allowed: set[str]) -> None:
        if role not in allowed:
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)


def _sprint_number(sprint: dict[str, Any] | None) -> int:
    number = _positive_int(str((sprint or {}).get("id", "")).removeprefix("sprint_kanboard_"))
    if number is None:
        raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
    return number


def _repositories(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _json_list(value: str | None) -> list[str]:
    try:
        raw = json.loads(value or "[]")
    except ValueError:
        return []
    return _repositories(raw) if isinstance(raw, list) else []


def _budget(value: Any = None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    if isinstance(value, str):
        try:
            source = json.loads(value)
        except ValueError:
            source = {}
    by_type = source.get("by_type") if isinstance(source, dict) else {}
    counts = {event_type: max(0, int(by_type.get(event_type, 0))) if isinstance(by_type, dict) else 0 for event_type in BUDGET_EVENT_TYPES}
    return {"total": sum(counts.values()), "by_type": counts}
