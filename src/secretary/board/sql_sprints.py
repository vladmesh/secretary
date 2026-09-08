"""Typed Sprint rows exposed through the small board-client vocabulary.

This is an adapter, not a second store.  Every value is read from or written to the
normalized Sprint tables and every statement uses ``SqlCardClient``'s connection.
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
from datetime import UTC, datetime
from typing import Any

from secretary.board.backend import record_key

_NUMBERED = re.compile(r"^sprint:([0-9]+)$")


def _now() -> datetime:
    return datetime.now(UTC)


def _epoch(value: datetime | None) -> str:
    return "" if value is None else str(int(value.timestamp()))


def _rfc3339(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def sprint_key(reference: str) -> int:
    """A stable positive transport handle; it is not the Sprint identity."""
    return record_key("sprint", reference)


class SqlSprintRecords:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.staged: dict[int, dict[str, Any]] = {}

    def _error(self, message: str) -> Exception:
        from secretary.board.sql_cards import SqlCardError

        return SqlCardError(message)

    def _reference(self, task_id: int) -> str:
        matches = [str(ref) for (ref,) in self.client._query(
            "SELECT ref FROM sprints WHERE board_key = %s", (int(task_id),)
        )]
        matches += [row["reference"] for key, row in self.staged.items() if key == int(task_id)]
        if len(matches) != 1:
            raise self._error(f"no unique Sprint carries transport key {task_id}")
        return matches[0]

    def rows(self) -> list[dict[str, Any]]:
        rows = [self._row(values) for values in self.client._query(
            "SELECT ref, board_key, goal, created_at, updated_at FROM sprints ORDER BY ref"
        )]
        rows.extend(self._staged_row(key, value) for key, value in sorted(self.staged.items()))
        keys: dict[int, str] = {}
        for row in rows:
            previous = keys.setdefault(int(row["id"]), str(row["reference"]))
            if previous != row["reference"]:
                raise self._error(
                    f"Sprint transport-key collision between {previous!r} and {row['reference']!r}"
                )
        return rows

    @staticmethod
    def _row(values: tuple[Any, ...]) -> dict[str, Any]:
        reference, board_key, goal, created, updated = values
        return {
            "id": int(board_key), "reference": str(reference), "title": str(goal),
            "description": "", "column_id": 1, "position": 0, "swimlane_id": 0,
            "date_creation": _epoch(created), "date_modification": _epoch(updated), "is_active": 1,
        }

    @staticmethod
    def _staged_row(key: int, row: dict[str, Any]) -> dict[str, Any]:
        created = row["created_at"]
        return {
            "id": key, "reference": row["reference"], "title": row["title"],
            "description": row.get("description", ""), "column_id": 1, "position": 0,
            "swimlane_id": 0, "date_creation": _epoch(created),
            "date_modification": _epoch(created), "is_active": 1,
        }

    def row_by_reference(self, reference: str) -> dict[str, Any] | None:
        key = sprint_key(reference)
        if key in self.staged and self.staged[key]["reference"] == reference:
            return self._staged_row(key, self.staged[key])
        rows = self.client._query(
            "SELECT ref, board_key, goal, created_at, updated_at FROM sprints WHERE ref = %s",
            (reference,),
        )
        return self._row(rows[0]) if rows else None

    def create(self, *, title: str, description: str, reference: str) -> int:
        if not reference.startswith("sprint:"):
            raise self._error("a SQL Sprint create requires its final sprint: reference")
        if self.row_by_reference(reference) is not None:
            raise self._error(f"{reference} already exists")
        key = sprint_key(reference)
        if any(row["reference"] != reference for row in self.staged.values() if sprint_key(row["reference"]) == key):
            raise self._error("Sprint transport-key collision")
        self.staged[key] = {
            "reference": reference, "title": title, "description": description or "",
            "created_at": _now(), "metadata": {},
        }
        return key

    def update(self, task_id: int, fields: dict[str, Any]) -> bool:
        key = int(task_id)
        if key in self.staged:
            row = self.staged[key]
            if fields.get("reference") and fields["reference"] != row["reference"]:
                raise self._error("a Sprint reference is immutable")
            if "title" in fields:
                row["title"] = str(fields["title"])
            return True
        reference = self._reference(key)
        if fields.get("reference") and fields["reference"] != reference:
            raise self._error("a Sprint reference is immutable")
        if "title" in fields:
            self.client._execute(
                "UPDATE sprints SET goal = %s, updated_at = %s WHERE ref = %s",
                (str(fields["title"]), _now(), reference),
            )
        return True

    def remove(self, task_id: int) -> bool:
        if int(task_id) in self.staged:
            del self.staged[int(task_id)]
            return True
        reference = self._reference(int(task_id))
        self.client._execute("DELETE FROM sprints WHERE ref = %s", (reference,))
        return True

    def metadata(self, task_id: int) -> dict[str, str]:
        key = int(task_id)
        if key in self.staged:
            return dict(self.staged[key]["metadata"])
        reference = self._reference(key)
        row = self.client._query(
            "SELECT goal, definition_of_done, product_id, status, observer, worker_pin, reviewer_pin, "
            "current_task_ref, source_audit FROM sprints WHERE ref = %s", (reference,)
        )[0]
        goal, dod, product, status, observer, worker, reviewer, current, source = row
        values: dict[str, str] = {
            "sprint_goal": str(goal), "sprint_definition_of_done": str(dod),
            "sprint_status": str(status), "sprint_current_task": str(current or ""),
        }
        repositories = [r[0] for r in self.client._query(
            "SELECT r.path FROM sprint_repositories sr JOIN repositories r USING (repository_id) "
            "WHERE sr.sprint_ref = %s ORDER BY sr.ordinal", (reference,)
        )]
        values["sprint_repositories"] = json.dumps(repositories, separators=(",", ":"))
        if product is not None:
            values["sprint_product"] = str(product)
        issues = [f"issue:{r[0]}" for r in self.client._query(
            "SELECT issue_id FROM sprint_issues WHERE sprint_ref = %s ORDER BY ordinal", (reference,)
        )]
        if product is not None or issues:
            values["sprint_issues"] = json.dumps(issues, separators=(",", ":"))
        projects = [r[0] for r in self.client._query(
            "SELECT project_id FROM sprint_projects WHERE sprint_ref = %s ORDER BY ordinal",
            (reference,),
        )]
        if product is not None or projects:
            values["sprint_reservations"] = json.dumps(projects, separators=(",", ":"))
        if observer is not None:
            values["sprint_observer"] = json.dumps(observer, sort_keys=True, separators=(",", ":"))
        if worker is not None:
            values["sprint_worker"] = str(worker)
        if reviewer is not None:
            values["sprint_reviewer"] = str(reviewer)
        if source is not None:
            values["sprint_source_audit"] = json.dumps(source, sort_keys=True, separators=(",", ":"))
        resume = self.client._query(
            "SELECT selected_step, selected_why, rejected_alternatives, current_task, dod_state, "
            "next_safe_step, recorded_at, recorded_at_source FROM sprint_resumes WHERE resume_id = "
            "(SELECT resume_id FROM sprints WHERE ref = %s)", (reference,)
        )
        if resume:
            names = ("selected_step", "selected_why", "rejected_alternatives", "current_task", "dod_state", "next_safe_step")
            document = dict(zip(names, resume[0][:6], strict=True))
            document["recorded_at"] = str(resume[0][7] or _rfc3339(resume[0][6]))
            values["sprint_resume"] = json.dumps(document, separators=(",", ":"))
        else:
            values["sprint_resume"] = ""
        counts = {str(kind): int(count) for kind, count in self.client._query(
            "SELECT event_type, count(*) FROM sprint_budget_events WHERE sprint_ref = %s AND charged "
            "GROUP BY event_type", (reference,)
        )}
        values["sprint_budget"] = json.dumps({"by_type": counts}, separators=(",", ":"))
        uncharged = {str(kind): int(count) for kind, count in self.client._query(
            "SELECT event_type, count(*) FROM sprint_budget_events WHERE sprint_ref = %s AND NOT charged "
            "GROUP BY event_type", (reference,)
        )}
        if uncharged:
            values["sprint_budget_uncharged"] = json.dumps(uncharged, separators=(",", ":"))
        return values

    def save_metadata(self, task_id: int, values: dict[str, Any]) -> bool:
        key = int(task_id)
        if key in self.staged:
            self.staged[key]["metadata"].update({str(k): str(v) for k, v in values.items()})
            self._finish_staged(key)
            return True
        reference = self._reference(key)
        self._apply(reference, values)
        return True

    def _finish_staged(self, key: int) -> None:
        row = self.staged[key]
        meta = row["metadata"]
        required = {"sprint_goal", "sprint_definition_of_done", "sprint_status"}
        if not required <= set(meta):
            return
        reference = row["reference"]
        match = _NUMBERED.fullmatch(reference)
        now = row["created_at"]
        observer = json.loads(meta["sprint_observer"]) if meta.get("sprint_observer") else None
        worker = self._pin(meta.get("sprint_worker"))
        reviewer = self._pin(meta.get("sprint_reviewer"))
        status = meta.get("sprint_status", "open")
        self.client._execute(
            "INSERT INTO sprints (ref, board_key, sprint_number, goal, definition_of_done, product_id, status, "
            "observer, worker_pin, reviewer_pin, current_task_ref, source_audit, created_at, updated_at, closed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,NULL,%s::jsonb,%s,%s,%s)",
            (reference, sprint_key(reference), int(match.group(1)) if match else None, meta["sprint_goal"],
             meta["sprint_definition_of_done"], meta.get("sprint_product") or None, status,
             json.dumps(observer) if observer is not None else None, worker, reviewer,
             meta.get("sprint_source_audit") or None, now, now, None if status == "open" else now),
        )
        self._replace_relations(reference, meta)
        del self.staged[key]

    @staticmethod
    def _pin(value: Any) -> str | None:
        if not value:
            return None
        try:
            parsed = json.loads(str(value))
        except ValueError:
            return str(value)
        if isinstance(parsed, dict):
            return str(parsed.get("profile") or "") or None
        return str(value)

    def _replace_relations(self, reference: str, values: dict[str, Any]) -> None:
        if "sprint_repositories" in values:
            self.client._execute("DELETE FROM sprint_repositories WHERE sprint_ref = %s", (reference,))
            for ordinal, path in enumerate(json.loads(str(values["sprint_repositories"]) or "[]")):
                rows = self.client._query(
                    "INSERT INTO repositories (path) VALUES (%s) ON CONFLICT (path) DO UPDATE SET path=EXCLUDED.path RETURNING repository_id",
                    (str(path),),
                )
                self.client._execute(
                    "INSERT INTO sprint_repositories (sprint_ref, repository_id, ordinal) VALUES (%s,%s,%s)",
                    (reference, rows[0][0], ordinal),
                )
        if "sprint_issues" in values:
            self.client._execute("DELETE FROM sprint_issues WHERE sprint_ref = %s", (reference,))
            for ordinal, issue in enumerate(json.loads(str(values["sprint_issues"]) or "[]")):
                self.client._execute(
                    "INSERT INTO sprint_issues (sprint_ref, issue_id, ordinal) VALUES (%s,%s,%s)",
                    (reference, str(issue).removeprefix("issue:"), ordinal),
                )
        if "sprint_reservations" in values:
            for ordinal, project in enumerate(json.loads(str(values["sprint_reservations"]) or "[]")):
                self.client._execute(
                    "INSERT INTO sprint_projects (sprint_ref, project_id, reserved, reserved_at, ordinal) "
                    "VALUES (%s,%s,true,%s,%s) ON CONFLICT (sprint_ref, project_id) DO UPDATE SET "
                    "reserved=true, released_at=NULL, ordinal=EXCLUDED.ordinal",
                    (reference, str(project), _now(), ordinal),
                )

    def _request(self, reference: str, operation: str | None = None) -> tuple[str, dict[str, Any]] | None:
        clause = " AND operation = %s" if operation else ""
        params: tuple[Any, ...] = (reference, operation) if operation else (reference,)
        rows = self.client._query(
            "SELECT request_id, intent FROM requests WHERE ref = %s AND status = 'staged'" + clause +
            " ORDER BY created_at DESC LIMIT 1", params,
        )
        if not rows:
            return None
        intent = rows[0][1] if isinstance(rows[0][1], dict) else json.loads(rows[0][1])
        return str(rows[0][0]), intent

    def _apply(self, reference: str, values: dict[str, Any]) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        scalar = {
            "sprint_goal": "goal", "sprint_definition_of_done": "definition_of_done",
            "sprint_product": "product_id",
            "sprint_current_task": "current_task_ref",
        }
        for key, column in scalar.items():
            if key in values:
                text = str(values[key])
                if key == "sprint_current_task" and text and not self.client._query(
                    "SELECT 1 FROM tasks WHERE task_ref = %s AND sprint_ref = %s",
                    (text, reference),
                ):
                    raise self._error(
                        f"Sprint current task {text!r} is not a Card linked to {reference}"
                    )
                assignments.append(f"{column} = %s")
                params.append(
                    (text or None)
                    if key in {"sprint_product", "sprint_current_task"}
                    else text
                )
        if "sprint_observer" in values:
            assignments.append("observer = %s::jsonb")
            params.append(str(values["sprint_observer"]) or None)
        for key, column in (("sprint_worker", "worker_pin"), ("sprint_reviewer", "reviewer_pin")):
            if key in values:
                assignments.append(f"{column} = %s")
                params.append(self._pin(values[key]))
        if "sprint_source_audit" in values:
            assignments.append("source_audit = %s::jsonb")
            params.append(str(values["sprint_source_audit"]) or None)
        if "sprint_status" in values:
            status = str(values["sprint_status"])
            assignments.extend(["status = %s", "closed_at = %s"])
            params.extend([status, None if status == "open" else _now()])
            if status == "open":
                self.client._execute(
                    "UPDATE sprint_projects SET reserved=true, released_at=NULL WHERE sprint_ref=%s",
                    (reference,),
                )
            else:
                self.client._execute(
                    "UPDATE sprint_projects SET reserved=false, released_at=%s WHERE sprint_ref=%s AND reserved",
                    (_now(), reference),
                )
        if assignments:
            assignments.append("updated_at = %s")
            params.extend([_now(), reference])
            self.client._execute(
                f"UPDATE sprints SET {', '.join(assignments)} WHERE ref = %s", tuple(params)
            )
        self._replace_relations(reference, values)
        if "sprint_resume" in values and str(values["sprint_resume"]):
            entry = json.loads(str(values["sprint_resume"]))
            names = ("selected_step", "selected_why", "rejected_alternatives", "current_task", "dod_state", "next_safe_step")
            source_timestamp = str(entry["recorded_at"])
            parsed_timestamp = datetime.fromisoformat(source_timestamp)
            malformed_source = source_timestamp if parsed_timestamp.utcoffset() is None else None
            row = self.client._query(
                "INSERT INTO sprint_resumes (sprint_ref, selected_step, selected_why, rejected_alternatives, "
                "current_task, dod_state, next_safe_step, recorded_at, recorded_at_source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING resume_id",
                (
                    reference,
                    *(entry[name] for name in names),
                    _now() if malformed_source else parsed_timestamp,
                    malformed_source,
                ),
            )
            self.client._execute("UPDATE sprints SET resume_id=%s WHERE ref=%s", (row[0][0], reference))
        if "sprint_budget" in values or "sprint_budget_uncharged" in values:
            claimed = self._request(reference, "budget_recorded")
            if claimed and not self.client._query(
                "SELECT 1 FROM sprint_budget_events WHERE request_id=%s", (claimed[0],)
            ):
                payload = claimed[1].get("payload", {})
                event_type = str(payload.get("event_type") or "")
                if event_type:
                    charged = event_type != "infrastructure_blocked"
                    self.client._execute(
                        "INSERT INTO sprint_budget_events (sprint_ref,event_type,charged,reason,request_id,occurred_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (reference, event_type, charged, str(payload.get("source_event_id") or ""), claimed[0], _now()),
                    )
            restored = self._request(reference, "restored")
            if restored:
                self.client._execute(
                    "DELETE FROM sprint_budget_events WHERE sprint_ref=%s", (reference,)
                )
                charged_counts = json.loads(str(values.get("sprint_budget") or "{}"))
                uncharged_counts = json.loads(
                    str(values.get("sprint_budget_uncharged") or "{}")
                )
                for charged, counts in (
                    (True, charged_counts.get("by_type", {})),
                    (False, uncharged_counts),
                ):
                    for event_type, count in counts.items():
                        for occurrence in range(int(count)):
                            self.client._execute(
                                "INSERT INTO sprint_budget_events "
                                "(sprint_ref,event_type,charged,reason,request_id,occurred_at) "
                                "VALUES (%s,%s,%s,%s,%s,%s)",
                                (
                                    reference,
                                    str(event_type),
                                    charged,
                                    f"restored occurrence {occurrence + 1}",
                                    restored[0],
                                    _now(),
                                ),
                            )

    def comments(self, task_id: int) -> list[dict[str, Any]]:
        reference = self._reference(int(task_id))
        return [
            {"id": identifier, "date_creation": _epoch(created), "comment": body}
            for identifier, body, created in self.client._query(
                "SELECT comment_id, body, created_at FROM sprint_comments WHERE sprint_ref=%s ORDER BY created_at, comment_id",
                (reference,),
            )
        ]

    def create_comment(self, task_id: int, content: str, *, created_at: Any = None) -> int:
        reference = self._reference(int(task_id))
        first = content.splitlines()[0] if content else ""
        marker = first[1:-1] if first.startswith("[") and first.endswith("]") else None
        claimed = self._request(reference)
        request_id = claimed[0] if claimed else None
        actor = claimed[1].get("actor", {}) if claimed else {}
        rows = self.client._query(
            "INSERT INTO sprint_comments (sprint_ref,marker,body,actor_role,actor_id,request_id,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING comment_id",
            (
                reference, marker, content, actor.get("role"), actor.get("id"), request_id,
                datetime.fromisoformat(str(created_at)) if created_at else _now(),
            ),
        )
        return int(rows[0][0])

    def save_close(
        self,
        reference: str,
        request_id: str,
        decisions: dict[str, list[dict[str, str]]],
        *,
        reason: str,
        closeout_document: str | None,
    ) -> None:
        """Persist the declared terminal decisions under their close request."""
        self.client._execute(
            "UPDATE sprints SET close_reason=%s, closeout_document=%s WHERE ref=%s",
            (reason or None, closeout_document, reference),
        )
        for subject_kind, entries in (("issue", decisions.get("issues", [])), ("card", decisions.get("cards", []))):
            for entry in entries:
                subject = str(entry["ref"])
                self.client._execute(
                    "INSERT INTO sprint_decisions (sprint_ref,subject_kind,issue_id,task_ref,verdict,actual,reason,request_id,decided_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        reference, subject_kind,
                        subject.removeprefix("issue:") if subject_kind == "issue" else None,
                        subject if subject_kind == "card" else None,
                        str(entry["verdict"]), entry.get("actual"), str(entry["reason"]),
                        request_id, _now(),
                    ),
                )


class SqlSprintTransaction:
    """The legacy coordinator surface with no journal outside the SQL transaction."""

    def __init__(self, client: Any, audit: Any, lock_dir: Any) -> None:
        self.client = client
        self.audit = audit
        self.lock_dir = lock_dir

    @staticmethod
    def _intent(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return payload.get("intent") if isinstance(payload.get("intent"), dict) else {}

    def existing(
        self, request_id: str, *, kind: str, intent: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        event = self.audit.committed_event(request_id)
        if event is not None:
            if event.get("kind") != kind or self._intent(event) != intent:
                from secretary.tasks import TaskError

                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            return None, event
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            if pending.get("kind") != kind or self._intent(pending) != intent:
                from secretary.tasks import TaskError

                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            return {
                "version": 1, "request_id": request_id, "kind": kind,
                "intent": intent, "event": pending, "progress": {},
            }, None
        return None, None

    def begin(
        self, request_id: str, *, kind: str, intent: dict[str, Any], event: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        document, committed = self.existing(request_id, kind=kind, intent=intent)
        if document is not None or committed is not None:
            return document, committed
        self.audit.stage(request_id, event)
        return {
            "version": 1, "request_id": request_id, "kind": kind,
            "intent": intent, "event": event, "progress": {},
        }, None

    def save(self, document: dict[str, Any]) -> None:
        # Keep the transaction-local result current while the immutable payload intent stays
        # unchanged.  Generic SQL claims permit this replacement until commit.
        self.audit.stage(str(document["request_id"]), document["event"])

    def complete(self, document: dict[str, Any]) -> None:
        self.audit.stage(str(document["request_id"]), document["event"])
        self.audit.append(str(document["request_id"]), document["event"])

    def discard(self, document: dict[str, Any]) -> None:
        self.audit.discard(str(document["request_id"]), document.get("event"))

    def drop(self, request_id: str) -> None:
        self.audit.discard(request_id)

    def status(self) -> dict[str, int | bool]:
        return self.audit.status()

    @contextlib.contextmanager
    def reference_lock(self, reference: str):
        # Admission is serialized by the transaction advisory lock. The anonymous descriptor is
        # only a port for the backend-neutral close routine's flock calls, never durable state.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as lock:
            yield lock


__all__ = ["SqlSprintRecords", "SqlSprintTransaction", "sprint_key"]
