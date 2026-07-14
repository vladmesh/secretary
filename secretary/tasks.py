"""Read-only Phase 5 task protocol backed by the Pipeline Kanboard."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any


class TaskError(Exception):
    """A task command failed without exposing backend credentials."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


class _CommittedWriteError(Exception):
    """A later step failed after a Kanboard mutation was committed."""


_STATE_BY_COLUMN = {
    "Идеи": "ideas",
    "Ready": "ready",
    "In progress": "in_progress",
    "Validate": "validate",
    "Blocked": "blocked",
    "Done": "done",
}
_KNOWN_METADATA = {
    "task_type", "project", "blocked_by", "claim", "slug", "base_branch",
    "head", "resolved_head", "review_head", "resolved_review_head", "retry_same",
    "retry_switch", "retry_heads", "complexity", "family_preference", "routing_reason",
    "quota_snapshot_at", "codex_launch_mode",
}
_TASK_TYPES = {"code", "research"}
_COMPLEXITIES = {"cheap", "standard", "hard", "frontier"}
_FAMILY_PREFERENCES = {"auto", "claude", "codex"}
_CODEX_LAUNCH_MODES = {"exec", "tui"}
_ROLES = {"po", "dispatcher", "worker", "reviewer", "steward", "retro"}
_COMMENT_ROLES = _ROLES
_CREATE_ROLES = {"po", "steward", "worker", "reviewer", "retro"}
_TRANSITIONS = {
    "po": {("ideas", "ready"), ("blocked", "ready")},
    "dispatcher": {
        ("in_progress", "validate"), ("in_progress", "blocked"),
        ("in_progress", "ready"), ("validate", "in_progress"),
        ("validate", "blocked"), ("validate", "done"),
    },
    "worker": set(), "reviewer": set(), "retro": set(),
    "steward": {
        ("ideas", "ready"), ("blocked", "ready"), ("blocked", "done"),
        ("in_progress", "done"), ("ideas", "blocked"), ("ready", "blocked"),
        ("in_progress", "blocked"), ("validate", "blocked"),
    },
}
_READY_RESET_METADATA = {
    "claim": "",
    "resolved_head": "",
    "resolved_review_head": "",
    "retry_same": "",
    "retry_switch": "",
    "retry_heads": "",
}
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")


class KanboardClient:
    """Small JSON-RPC client. Credentials are supplied only by runtime env."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self.url = env.get("KANBOARD_URL", "")
        self.user = env.get("KANBOARD_API_USER", "")
        self.token = env.get("KANBOARD_API_TOKEN", "")
        if not (self.url and self.user and self.token):
            raise TaskError("backend_unavailable", "Kanboard runtime configuration is unavailable", 1)

    def call(self, method: str, **params: Any) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            payload["params"] = params
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic " + base64.b64encode(
                    f"{self.user}:{self.token}".encode("utf-8")
                ).decode("ascii"),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1) from None
        if not isinstance(document, dict) or "error" in document:
            raise TaskError("backend_error", "Kanboard rejected the read request", 1)
        return document.get("result")


class TaskReader:
    def __init__(self, client: KanboardClient, board_name: str = "Pipeline") -> None:
        self.client = client
        self.board_name = board_name

    def list(
        self, *, states: set[str] | None = None, project: str | None = None
    ) -> list[dict[str, Any]]:
        project_id, columns, swimlanes = self._board()
        cards = self.client.call("getAllTasks", project_id=project_id, status_id=1) or []
        if not isinstance(cards, list):
            raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
        result = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            normalized = self._normalize(card, columns, swimlanes, comments=None)
            if states and normalized["state"] not in states:
                continue
            if project is not None and normalized["project"] != project:
                continue
            result.append(normalized)
        return sorted(result, key=lambda task: (task["state"], task["position"], task["ref"], task["id"]))

    def show(self, reference: str) -> dict[str, Any]:
        project_id, columns, swimlanes = self._board()
        card = self.client.call("getTaskByReference", project_id=project_id, reference=reference)
        if not isinstance(card, dict):
            raise TaskError("not_found", "task was not found", 2)
        task_id = _positive_int(card.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
        raw_comments = self.client.call("getAllComments", task_id=task_id) or []
        comments = [
            _normalize_comment(comment)
            for comment in raw_comments
            if isinstance(comment, dict)
        ]
        return self._normalize(card, columns, swimlanes, comments=comments)

    def _board(self) -> tuple[int, dict[int, str], dict[int, str]]:
        board = self.client.call("getProjectByName", name=self.board_name)
        if not isinstance(board, dict) or (project_id := _positive_int(board.get("id"))) is None:
            raise TaskError("backend_error", "Pipeline board is unavailable", 1)
        columns = {
            identifier: str(column.get("title") or "")
            for column in (self.client.call("getColumns", project_id=project_id) or [])
            if isinstance(column, dict) and (identifier := _positive_int(column.get("id"))) is not None
        }
        swimlanes = {
            identifier: str(swimlane.get("name") or "")
            for swimlane in (self.client.call("getActiveSwimlanes", project_id=project_id) or [])
            if isinstance(swimlane, dict) and (identifier := _positive_int(swimlane.get("id"))) is not None
        }
        return project_id, columns, swimlanes

    def _normalize(
        self,
        card: dict[str, Any],
        columns: dict[int, str],
        swimlanes: dict[int, str],
        *,
        comments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        task_id = _positive_int(card.get("id"))
        column = columns.get(_positive_int(card.get("column_id")) or -1)
        if task_id is None or column not in _STATE_BY_COLUMN:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        metadata = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(metadata, dict):
            raise TaskError("backend_error", "Kanboard returned invalid task metadata", 1)
        meta = {str(key): _text(value) for key, value in metadata.items()}
        ref = _text(card.get("reference"))
        result: dict[str, Any] = {
            "id": f"task_kanboard_{task_id}", "ref": ref, "title": _text(card.get("title")),
            "description": _text(card.get("description")), "state": _STATE_BY_COLUMN[column],
            "position": _nonnegative_int(card.get("position")), "project": _text(meta.get("project")),
            "type": _text(meta.get("task_type")), "blocked_by": _null_if_empty(meta.get("blocked_by")),
            "claim": {"worker": _null_if_empty(meta.get("claim")), "claimed_at": None},
            "routing": {
                "complexity": _enum_or_default(
                    meta.get("complexity"), _COMPLEXITIES, "standard"
                ),
                "family_preference": _enum_or_default(
                    meta.get("family_preference"), _FAMILY_PREFERENCES, "auto"
                ),
                "head_override": _null_if_empty(meta.get("head")), "review_head_override": _null_if_empty(meta.get("review_head")),
                "resolved_worker_family": None, "resolved_worker_head": _null_if_empty(meta.get("resolved_head")),
                "resolved_review_family": None, "resolved_review_head": _null_if_empty(meta.get("resolved_review_head")),
                "routing_reason": _null_if_empty(meta.get("routing_reason")), "quota_snapshot_at": _null_if_empty(meta.get("quota_snapshot_at")),
                "codex_launch_mode": _enum_or_none(meta.get("codex_launch_mode"), _CODEX_LAUNCH_MODES),
            },
            "workspace": {"slug": _null_if_empty(meta.get("slug")), "base_branch": _null_if_empty(meta.get("base_branch"))},
            "retry": {"same": _nonnegative_int(meta.get("retry_same")), "switched": _nonnegative_int(meta.get("retry_switch")), "heads": _split_heads(meta.get("retry_heads"))},
            "audit": {"created_at": _rfc3339(card.get("date_creation")), "updated_at": _rfc3339(card.get("date_modification")), "backend": {"kind": "kanboard", "kanboard_task_id": task_id, "board": self.board_name}},
        }
        extensions = {key: value for key, value in meta.items() if key not in _KNOWN_METADATA}
        lane = swimlanes.get(_positive_int(card.get("swimlane_id")) or -1)
        if lane:
            extensions["swimlane"] = lane
        if extensions:
            result["extensions"] = {"kanboard": extensions}
        if comments is not None:
            result["comments"] = comments
        return result


class TaskAudit:
    """Durable, append-only audit log with retry-safe pending records."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.board_dir = os.path.join(os.fspath(data_dir), "board")
        self.events_path = os.path.join(self.board_dir, "events.ndjson")
        self.pending_dir = os.path.join(self.board_dir, "pending-audit")
        self.lock_path = os.path.join(self.board_dir, ".audit.lock")

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        os.makedirs(self.pending_dir, exist_ok=True)
        self._atomic_json(os.path.join(self.pending_dir, f"{request_id}.json"), event)

    def append(self, request_id: str, event: dict[str, Any]) -> str:
        os.makedirs(self.board_dir, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if not self._has_request(request_id):
                    with open(self.events_path, "a", encoding="utf-8") as events:
                        events.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                        events.flush()
                        os.fsync(events.fileno())
                pending = os.path.join(self.pending_dir, f"{request_id}.json")
                if os.path.exists(pending):
                    os.unlink(pending)
                return str(event["event_id"])
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def discard(self, request_id: str) -> None:
        try:
            os.unlink(os.path.join(self.pending_dir, f"{request_id}.json"))
        except FileNotFoundError:
            pass

    def reconcile(self) -> tuple[int, int]:
        if not os.path.isdir(self.pending_dir):
            return 0, 0
        repaired = 0
        unresolved = 0
        for name in sorted(os.listdir(self.pending_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.pending_dir, name)
            try:
                with open(path, encoding="utf-8") as source:
                    event = json.load(source)
                request_id = str(event["request_id"])
                self.append(request_id, event)
                repaired += 1
            except (OSError, ValueError, KeyError, TypeError):
                unresolved += 1
        return repaired, unresolved

    def pending_events(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self.pending_dir):
            return []
        result = []
        for name in sorted(os.listdir(self.pending_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.pending_dir, name), encoding="utf-8") as source:
                    result.append(json.load(source))
            except (OSError, ValueError):
                continue
        return result

    def status(self) -> dict[str, int | bool]:
        pending = 0
        if os.path.isdir(self.pending_dir):
            pending = sum(name.endswith(".json") for name in os.listdir(self.pending_dir))
        return {"ok": pending == 0, "pending": pending}

    def event(self, request_id: str) -> dict[str, Any] | None:
        committed = self.committed_event(request_id)
        if committed is not None:
            return committed
        return self.pending_event(request_id)

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        try:
            with open(self.events_path, encoding="utf-8") as events:
                for line in events:
                    if line.strip():
                        candidate = json.loads(line)
                        if candidate.get("request_id") == request_id:
                            return candidate
        except FileNotFoundError:
            pass
        return None

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        pending = os.path.join(self.pending_dir, f"{request_id}.json")
        try:
            with open(pending, encoding="utf-8") as source:
                return json.load(source)
        except FileNotFoundError:
            return None

    def _has_request(self, request_id: str) -> bool:
        try:
            with open(self.events_path, encoding="utf-8") as events:
                return any(json.loads(line).get("request_id") == request_id for line in events if line.strip())
        except FileNotFoundError:
            return False

    @staticmethod
    def _atomic_json(path: str, document: dict[str, Any]) -> None:
        directory = os.path.dirname(path)
        fd, temp = tempfile.mkstemp(prefix=".pending-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(document, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


class TaskWriter:
    """Protocol writes, role guards and normalized audit events."""

    def __init__(self, client: KanboardClient, *, data_dir: str | os.PathLike[str]) -> None:
        self.client = client
        self.reader = TaskReader(client)
        self.audit = TaskAudit(data_dir)

    def create(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        task_type: str,
        title: str,
        description: str = "",
        target: str = "ideas",
        reference: str = "",
        blocked_by: str = "",
        head: str = "",
        review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        complexity: str = "standard",
        family_preference: str = "auto",
        codex_launch_mode: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._role(role, _CREATE_ROLES)
        project = project.strip()
        task_type = task_type.strip()
        title = title.strip()
        target = target.strip()
        reference = reference.strip()
        blocked_by = blocked_by.strip()
        head = head.strip()
        review_head = review_head.strip()
        slug = slug.strip()
        base_branch = base_branch.strip()
        complexity = complexity.strip() or "standard"
        family_preference = family_preference.strip() or "auto"
        codex_launch_mode = codex_launch_mode.strip()
        if not project:
            raise TaskError("validation", "create requires a non-empty project", 2)
        if task_type not in _TASK_TYPES:
            known = ", ".join(sorted(_TASK_TYPES))
            raise TaskError("validation", f"unknown task type {task_type!r} (known: {known})", 2)
        if not title:
            raise TaskError("validation", "create requires a non-empty title", 2)
        if target not in {"ideas", "ready"}:
            raise TaskError("validation", "create target must be ideas or ready", 2)
        if role in {"worker", "reviewer", "retro"} and target != "ideas":
            raise TaskError("role_forbidden", f"{role} may create only ideas cards", 3)
        if complexity not in _COMPLEXITIES:
            raise TaskError("validation", "complexity must be one of: " + ", ".join(sorted(_COMPLEXITIES)), 2)
        if family_preference not in _FAMILY_PREFERENCES:
            raise TaskError("validation", "family preference must be one of: " + ", ".join(sorted(_FAMILY_PREFERENCES)), 2)
        if codex_launch_mode and codex_launch_mode not in _CODEX_LAUNCH_MODES:
            raise TaskError("validation", "codex launch mode must be exec or tui", 2)
        if slug and not _SLUG_RE.match(slug):
            raise TaskError("validation", "slug must match [a-z0-9-]{1,30}", 2)

        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": "created", "task": self.reader.show(str(committed["ref"])), "event_id": event_id}
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            try:
                self._finish_pending_cleanup(pending)
                task = self.reader.show(str(pending["ref"]))
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": "created", "task": self.reader.show(str(pending["ref"])), "event_id": event_id}

        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": "created",
            "outcome": "success",
            "task_id": "",
            "ref": reference,
            "backend": {"kind": "kanboard", "task_id": None, "revision": "pending"},
            "request_id": request_id,
            "payload": {
                "project": project,
                "task_type": task_type,
                "target": target,
                "reference": reference or None,
                "blocked_by": blocked_by or None,
                "head": head or None,
                "review_head": review_head or None,
                "slug": slug or None,
                "base_branch": base_branch or None,
                "complexity": complexity,
                "family_preference": family_preference,
                "codex_launch_mode": codex_launch_mode or None,
                "title_sha256": _digest(title),
                "description_sha256": _digest(description),
            },
        }
        self.audit.stage(request_id, event)
        try:
            created_ref = self._create_backend(
                project=project,
                task_type=task_type,
                title=title,
                description=description,
                target=target,
                reference=reference,
                blocked_by=blocked_by,
                head=head,
                review_head=review_head,
                slug=slug,
                base_branch=base_branch,
                complexity=complexity,
                family_preference=family_preference,
                codex_launch_mode=codex_launch_mode,
                event=event,
                request_id=request_id,
            )
        except _CommittedWriteError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        except Exception:
            self.audit.discard(request_id)
            raise
        try:
            task = self.reader.show(created_ref)
        except Exception:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        event["task_id"] = task["id"]
        event["ref"] = created_ref
        event["backend"]["revision"] = _revision(task)
        self.audit.stage(request_id, event)
        try:
            event_id = self.audit.append(request_id, event)
        except OSError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        return {"action": "created", "task": task, "event_id": event_id}

    def _create_backend(
        self,
        *,
        project: str,
        task_type: str,
        title: str,
        description: str,
        target: str,
        reference: str,
        blocked_by: str,
        head: str,
        review_head: str,
        slug: str,
        base_branch: str,
        complexity: str,
        family_preference: str,
        codex_launch_mode: str,
        event: dict[str, Any],
        request_id: str,
    ) -> str:
        board_id, columns, swimlanes = self.reader._board()
        if reference and self.client.call("getTaskByReference", project_id=board_id, reference=reference):
            raise TaskError("validation", "task reference already exists", 2)
        column_id = next((identifier for identifier, name in columns.items() if _STATE_BY_COLUMN.get(name) == target), None)
        if column_id is None:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        swimlane_id = _matching_swimlane(swimlanes, project)
        task_id = _positive_int(self.client.call(
            "createTask",
            project_id=board_id,
            title=title,
            description=description,
            column_id=column_id,
            swimlane_id=swimlane_id or 0,
        ))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard rejected the write", 1)
        created_ref = reference or f"{project}-{task_id}"
        event["ref"] = created_ref
        event["task_id"] = f"task_kanboard_{task_id}"
        event["backend"]["task_id"] = task_id
        self.audit.stage(request_id, event)
        try:
            ok = self.client.call("updateTask", id=task_id, reference=created_ref)
            if not ok:
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            values = {
                "task_type": task_type,
                "project": project,
                "complexity": complexity,
                "family_preference": family_preference,
            }
            if blocked_by:
                values["blocked_by"] = blocked_by
            if head:
                values["head"] = head
            if review_head:
                values["review_head"] = review_head
            if slug:
                values["slug"] = slug
            if base_branch:
                values["base_branch"] = base_branch
            if codex_launch_mode:
                values["codex_launch_mode"] = codex_launch_mode
            self.client.call("saveTaskMetadata", task_id=task_id, values=values)
        except Exception as exc:
            raise _CommittedWriteError() from exc
        return created_ref

    def comment(self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, _COMMENT_ROLES)
        return self._write("commented", role, actor, reference, request_id, {"marker": role, "body_sha256": _digest(body)}, lambda task: self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{body}"))

    def report(self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"worker"})
        if kind not in {"done", "blocked"} or (kind == "blocked" and not body.strip()):
            raise TaskError("validation", "blocked reports require a non-empty body", 2)
        marker = f"report:{kind}"
        return self._write("reported", role, actor, reference, request_id, {"marker": marker, "body_sha256": _digest(body)}, lambda task: self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{marker}]\n{body}"))

    def verdict(self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"reviewer"})
        if kind not in {"green", "red"} or (kind == "red" and not body.strip()):
            raise TaskError("validation", "red verdicts require a non-empty body", 2)
        marker = f"review:{kind}"
        return self._write("verdict", role, actor, reference, request_id, {"marker": marker, "body_sha256": _digest(body)}, lambda task: self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{marker}]\n{body}"))

    def claim(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        worker: str,
        resolved_head: str = "",
        resolved_review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        cap: int = 3,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._role(role, {"dispatcher"})
        worker = worker.strip()
        if not worker:
            raise TaskError("validation", "claim requires a non-empty worker id", 2)
        if cap < 1:
            raise TaskError("validation", "claim cap must be positive", 2)

        def mutation(task: dict[str, Any]) -> Any:
            if task["state"] != "ready":
                raise TaskError("claim_conflict", "claim requires a Ready task", 3)
            if task["claim"]["worker"] is not None:
                raise TaskError("claim_conflict", "task is already claimed", 3)
            blocked_by = task.get("blocked_by")
            if blocked_by:
                predecessor = self.reader.show(str(blocked_by))
                if predecessor["state"] != "done":
                    raise TaskError("predecessor_open", "blocked_by task is not Done", 3)
            for active in self.reader.list(states={"in_progress", "validate"}):
                if active["id"] == task["id"] or _is_steward_report(active):
                    continue
                if active["type"] == "code" and task["type"] == "code" and active["project"] == task["project"]:
                    raise TaskError("capacity_reached", "one active code task per project is already claimed", 3)
            active_count = sum(
                1
                for active in self.reader.list(states={"in_progress", "validate"})
                if active["id"] != task["id"] and not _is_steward_report(active)
            )
            if active_count >= cap:
                raise TaskError("capacity_reached", "active task capacity is reached", 3)

            values = {
                "claim": worker,
                "resolved_head": resolved_head or task["routing"]["head_override"] or "",
            }
            if resolved_review_head:
                values["resolved_review_head"] = resolved_review_head
            if slug:
                values["slug"] = slug
            if base_branch:
                values["base_branch"] = base_branch
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values=values)
            try:
                self._move_raw(task, "in_progress")
            except Exception as exc:
                raise _CommittedWriteError() from exc

        return self._write(
            "claimed",
            role,
            actor,
            reference,
            request_id,
            {
                "worker": worker,
                "resolved_head": resolved_head or None,
                "resolved_review_head": resolved_review_head or None,
                "slug": slug or None,
                "base_branch": base_branch or None,
                "cap": cap,
            },
            mutation,
        )

    def move(self, *, role: str, actor: str, reference: str, target: str, reason: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, _ROLES)
        def mutation(task: dict[str, Any]) -> Any:
            source = task["state"]
            if (source, target) not in _TRANSITIONS[role]:
                raise TaskError("transition_forbidden", f"{role} may not move {source} to {target}", 3)
            if role == "steward" and (target == "blocked" or (source, target) == ("blocked", "done")) and not reason.strip():
                raise TaskError("validation", "this steward transition requires a non-empty reason", 2)
            self._move_raw(task, target)
            try:
                if target == "ready":
                    self.client.call("saveTaskMetadata", task_id=_task_number(task), values=_READY_RESET_METADATA)
                elif source == "validate":
                    self.client.call("saveTaskMetadata", task_id=_task_number(task), values={"resolved_review_head": ""})
                if reason.strip():
                    self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{reason}")
            except Exception as exc:
                raise _CommittedWriteError() from exc
        return self._write("moved", role, actor, reference, request_id, {"to": target, "reason_sha256": _digest(reason) if reason else None}, mutation)

    def _move_raw(self, task: dict[str, Any], target: str) -> None:
        board_id, columns, _ = self.reader._board()
        column_id = next((identifier for identifier, name in columns.items() if _STATE_BY_COLUMN.get(name) == target), None)
        if column_id is None:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=task["ref"])
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        ok = self.client.call(
            "moveTaskPosition",
            project_id=board_id,
            task_id=_task_number(task),
            column_id=column_id,
            position=1,
            swimlane_id=_positive_int(raw.get("swimlane_id")) or 0,
        )
        if not ok:
            raise TaskError("backend_error", "Kanboard rejected the write", 1)

    def _write(self, kind: str, role: str, actor: str, reference: str, request_id: str | None, payload: dict[str, Any], mutation: Any) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": kind, "task": self.reader.show(reference), "event_id": event_id}
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            try:
                self._finish_pending_cleanup(pending)
                task = self.reader.show(str(pending["ref"]))
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": kind, "task": self.reader.show(reference), "event_id": event_id}
        task = self.reader.show(reference)
        event = {"event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(), "actor": {"role": role, "id": actor}, "kind": kind, "outcome": "success", "task_id": task["id"], "ref": reference, "backend": {"kind": "kanboard", "task_id": _task_number(task), "revision": _revision(task)}, "request_id": request_id, "payload": payload}
        self.audit.stage(request_id, event)
        try:
            mutation(task)
        except _CommittedWriteError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        except Exception:
            self.audit.discard(request_id)
            raise
        try:
            task = self.reader.show(reference)
        except Exception:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        event["backend"]["revision"] = _revision(task)
        self.audit.stage(request_id, event)
        try:
            event_id = self.audit.append(request_id, event)
        except OSError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        return {"action": kind, "task": task, "event_id": event_id}

    def reconcile(self) -> tuple[int, int]:
        repaired = 0
        unresolved = 0
        for event in self.audit.pending_events():
            try:
                self._finish_pending_cleanup(event)
                task = self.reader.show(str(event["ref"]))
                event["task_id"] = task["id"]
                event["backend"]["revision"] = _revision(task)
                self.audit.stage(str(event["request_id"]), event)
                self.audit.append(str(event["request_id"]), event)
                repaired += 1
            except (TaskError, OSError, KeyError, TypeError):
                unresolved += 1
        return repaired, unresolved

    def _finish_pending_cleanup(self, event: dict[str, Any]) -> None:
        """Complete idempotent backend cleanup before recording a pending event."""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event.get("kind") == "created":
            self._finish_pending_create(event, payload)
            return
        if event.get("kind") == "claimed":
            self._finish_pending_claim(event, payload)
            return
        if event.get("kind") != "moved" or payload.get("to") != "ready":
            return
        task = self.reader.show(str(event["ref"]))
        if task["state"] != "ready":
            raise TaskError("backend_error", "pending move no longer matches task state", 1)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_READY_RESET_METADATA,
        )
        normalized = self.reader.show(str(event["ref"]))
        if (
            normalized["claim"]["worker"] is not None
            or normalized["routing"]["resolved_worker_head"] is not None
            or normalized["routing"]["resolved_review_head"] is not None
            or normalized["retry"] != {"same": 0, "switched": 0, "heads": []}
        ):
            raise TaskError("backend_error", "pending Ready cleanup remains incomplete", 1)

    def _finish_pending_claim(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        """Complete a claim whose metadata committed before the column move failed."""
        ref = str(event["ref"])
        worker = str(payload.get("worker") or "")
        if not worker:
            raise TaskError("backend_error", "pending claim is missing its worker id", 1)
        task = self.reader.show(ref)
        if task["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim no longer matches task claim", 1)
        if not _matches_optional(payload.get("resolved_head"), task["routing"]["resolved_worker_head"]):
            raise TaskError("backend_error", "pending claim worker head remains incomplete", 1)
        if not _matches_optional(payload.get("resolved_review_head"), task["routing"]["resolved_review_head"]):
            raise TaskError("backend_error", "pending claim review head remains incomplete", 1)
        if task["state"] == "ready":
            self._move_raw(task, "in_progress")
        elif task["state"] != "in_progress":
            raise TaskError("backend_error", "pending claim no longer matches task state", 1)
        normalized = self.reader.show(ref)
        if normalized["state"] != "in_progress" or normalized["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim cleanup remains incomplete", 1)

    def _finish_pending_create(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending create is missing its task ref", 1)
        task = self.reader.show(ref)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_create_metadata_values(payload),
        )
        normalized = self.reader.show(ref)
        expected_mode = _text(payload.get("codex_launch_mode"))
        if expected_mode and normalized["routing"]["codex_launch_mode"] != expected_mode:
            raise TaskError("backend_error", "pending create metadata remains incomplete", 1)

    @staticmethod
    def _role(role: str, allowed: set[str]) -> None:
        if role not in allowed:
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _task_number(task: dict[str, Any]) -> int:
    value = _positive_int(str(task.get("id", "")).removeprefix("task_kanboard_"))
    if value is None:
        raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _revision(task: dict[str, Any]) -> str:
    return "updated_at:" + str(task.get("audit", {}).get("updated_at") or "unknown")


def _null_if_empty(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _split_heads(value: Any) -> list[str]:
    return [head for head in _text(value).split(",") if head]


def _enum_or_default(value: Any, allowed: set[str], default: str) -> str:
    candidate = _text(value)
    return candidate if candidate in allowed else default


def _enum_or_none(value: Any, allowed: set[str]) -> str | None:
    candidate = _text(value)
    return candidate if candidate in allowed else None


def _matching_swimlane(swimlanes: dict[int, str], project: str) -> int | None:
    wanted = re.sub(r"[^a-z0-9]+", "", project.lower())
    for identifier, name in swimlanes.items():
        candidate = re.sub(r"[^a-z0-9]+", "", name.lower())
        if candidate == wanted:
            return identifier
    return None


def _is_steward_report(task: dict[str, Any]) -> bool:
    return task.get("extensions", {}).get("kanboard", {}).get("steward_report") == "1"


def _matches_optional(expected: Any, actual: Any) -> bool:
    expected_text = _text(expected)
    return not expected_text or actual == expected_text


def _create_metadata_values(payload: dict[str, Any]) -> dict[str, str]:
    values = {
        "task_type": _text(payload.get("task_type")),
        "project": _text(payload.get("project")),
        "complexity": _text(payload.get("complexity")) or "standard",
        "family_preference": _text(payload.get("family_preference")) or "auto",
    }
    for payload_key, metadata_key in (
        ("blocked_by", "blocked_by"),
        ("head", "head"),
        ("review_head", "review_head"),
        ("slug", "slug"),
        ("base_branch", "base_branch"),
        ("codex_launch_mode", "codex_launch_mode"),
    ):
        value = _text(payload.get(payload_key))
        if value:
            values[metadata_key] = value
    return values


def _rfc3339(value: Any) -> str | None:
    seconds = _positive_int(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    text = _text(comment.get("comment"))
    first_line = text.splitlines()[0] if text else ""
    marker = first_line[1:-1] if first_line.startswith("[") and first_line.endswith("]") else None
    return {"created_at": _rfc3339(comment.get("date_creation")), "body": text, "marker": marker}
