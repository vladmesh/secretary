"""`TaskAudit`'s contract, kept in `requests` and `board_events` instead of in files.

`docs/BOARD_STORE.md` §7.3 is the table this module implements, row by row.  The contract callers
see is unchanged and deliberately so: the same `request_id`, the same answer to a retry with the
same id, and the same refusal — *"request id belongs to another operation or payload"* — when an
id is reused for a different operation.  What changes is where the claim lives.

* **The namespace** is `requests.request_id`, the whole installation's one primary key (§3.9),
  in place of `TaskAudit`'s index over `events.ndjson` plus `pending-audit/v2-<sha256>.json`.
* **Staged and committed** are `requests.status`, in place of a pending file and a journal line.
  `requests.intent` holds the record itself, frozen at the claim, which is what makes the
  "same operation, same payload" comparison a column comparison rather than a file read.
* **`requests.protocol`** is what makes §3.9's one permitted replacement checkable: a generic
  stage may replace a generic staged record and may never replace a protocol one.
* **`board_events`** carries the typed occurrences — the ones whose `kind` is an `EventKind` —
  so the event stream is queryable as §3.9 describes.  A *generic* audit record (`moved`,
  `edited`, `commented`, `restored_comment`, …) has no `EventKind` and therefore no
  `board_events` row; it lives in its `requests` row alone.  That is not a loss of the record,
  it is the closed vocabulary of §3.12 refusing to be widened by an adapter.
* **The per-card marker lock** is a PostgreSQL advisory lock rather than a file lock, so it is
  released by the connection rather than by a process that might die holding a file.

What is **not** implemented, because §7.3 says it ceases to exist: nothing here can leave an
effect applied and a record unwritten.  The card effect and this claim are statements of one
transaction (§7.1), so `reconcile` has nothing to repair and answers `(0, 0)`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from secretary.board.models import EntityKind, Event, EventKind

#: Every `kind` a `board_events` row may carry (§3.12).  A record whose kind is outside it is a
#: generic audit record and stays in its `requests` row.
_TYPED_KINDS = frozenset(kind.value for kind in EventKind)


class SqlAuditError(RuntimeError):
    pass


def _advisory_key(text: str) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _now() -> datetime:
    return datetime.now(UTC)


class SqlTaskAudit:
    """The audit owner of the PostgreSQL card backend.

    It takes the same `TaskError` vocabulary as `TaskAudit`, because every caller above it
    catches those and nothing else; a `psycopg` error escaping from here would be an unhandled
    failure in a command that today has a named one.
    """

    _PROTOCOL_EVENT_RECORD_TYPE = Event.RECORD_TYPE

    def __init__(self, client: Any) -> None:
        self.client = client
        self._marker_lock_depth = threading.local()
        self.legacy_audit: Any | None = None

    # --- primitives ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.client._query(sql, params)

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return self.client._execute(sql, params)

    def _commit(self) -> None:
        self.client._commit_unless_nested()

    @classmethod
    def _is_protocol_event(cls, event: dict[str, Any]) -> bool:
        return event.get("record_type") == cls._PROTOCOL_EVENT_RECORD_TYPE

    @staticmethod
    def _document(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else json.loads(value or "{}")

    def _record(self, request_id: str, status: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT intent FROM requests WHERE request_id = %s AND status = %s",
            (request_id, status),
        )
        return self._document(rows[0][0]) if rows else None

    # --- the TaskAudit surface -------------------------------------------------------

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return self._record(request_id, "committed")

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        return self._record(request_id, "staged")

    def event(self, request_id: str) -> dict[str, Any] | None:
        committed = self.committed_event(request_id)
        return committed if committed is not None else self.pending_event(request_id)

    def events(self, reference: str = "", *, kind: str = "") -> list[dict[str, Any]]:
        """Committed records in claim order, narrowed the way the file journal narrows them."""
        from secretary.tasks import _event_action

        rows = self._query(
            "SELECT intent FROM requests WHERE status = 'committed' "
            "ORDER BY settled_at, created_at, request_id"
        )
        result = []
        for (intent,) in rows:
            event = self._document(intent)
            if reference and event.get("ref") != reference:
                continue
            if kind and event.get("kind") != kind and _event_action(event) != kind:
                continue
            result.append(event)
        return result

    def pending_events(self) -> list[dict[str, Any]]:
        return [
            self._document(intent)
            for (intent,) in self._query(
                "SELECT intent FROM requests WHERE status = 'staged' ORDER BY created_at, request_id"
            )
        ]

    def status(self) -> dict[str, int | bool]:
        if self.legacy_audit is not None:
            self.legacy_audit.require_pending_layout()
        pending = int(self._query("SELECT count(*) FROM requests WHERE status = 'staged'")[0][0])
        return {"ok": pending == 0, "pending": pending}

    def event_id_owner(self, event_id: str) -> str | None:
        rows = self._query(
            "SELECT request_id FROM requests WHERE intent->>'event_id' = %s "
            "ORDER BY created_at, request_id LIMIT 1",
            (event_id,),
        )
        return rows[0][0] if rows else None

    def require_pending_layout(self) -> None:
        """There is no pre-v2 filename layout to upgrade in a table; the gate is a no-op here."""
        if self.legacy_audit is not None:
            self.legacy_audit.require_pending_layout()

    @staticmethod
    def require_claim(
        existing: dict[str, Any],
        *,
        kind: str,
        reference: str | None,
        identity: dict[str, Any] | None,
    ) -> None:
        from secretary.tasks import TaskAudit

        TaskAudit.require_claim(existing, kind=kind, reference=reference, identity=identity)

    @staticmethod
    def _require_same_event(existing: dict[str, Any], event: dict[str, Any]) -> None:
        from secretary.tasks import TaskError

        if existing != event:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)

    # --- ownership -------------------------------------------------------------------

    def _pending_owner(
        self, request_id: str, event: dict[str, Any] | None, *, operation: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        """`TaskAudit._pending_owner`, over the two statuses of one `requests` row."""
        committed = self.committed_event(request_id)
        if committed is not None:
            if event is not None:
                self._require_same_event(committed, event)
            return committed, None, False
        pending = self.pending_event(request_id)
        if pending is None:
            return None, None, False
        if operation == "reconcile" and self._is_protocol_event(pending):
            self._require_same_event(pending, {})
        if event is not None and pending == event:
            return None, pending, False
        if (
            operation == "stage"
            and event is not None
            and not self._is_protocol_event(pending)
            and not self._is_protocol_event(event)
        ):
            return None, pending, True
        if operation == "discard" and event is None and not self._is_protocol_event(pending):
            return None, pending, False
        self._require_same_event(pending, event or {})
        raise AssertionError("unreachable")

    def _claim_row(self, request_id: str, event: dict[str, Any], *, status: str) -> None:
        """§7.1 step 1: claim the id in the one global namespace, then own it.

        `ref` is written on the replacement as well as on the insert, and that is the whole of
        the repair this row needed.  A create claims its request id before it knows the reference
        it will allocate — the reference is chosen from the board's high-water mark inside the
        same mutation — so the first statement writes `ref = NULL` and the second, which names it,
        used to update `intent` and leave the column behind.  `requests.ref` then stayed NULL for
        the life of every created card, and the column §3.9 makes the record's own subject index
        answered nothing.  `TaskWriter._create` now runs both statements in one transaction, so
        the column and the record it belongs to become visible together or not at all.
        """
        ref = str(event.get("ref") or "")
        subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
        entity_kind = str(subject.get("kind") or "card")
        self._execute(
            "INSERT INTO requests (request_id, operation, intent, status, protocol, entity_kind, "
            "ref, created_at, settled_at) VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (request_id) DO UPDATE SET intent = EXCLUDED.intent, "
            "status = EXCLUDED.status, settled_at = EXCLUDED.settled_at, "
            "protocol = EXCLUDED.protocol, operation = EXCLUDED.operation, ref = EXCLUDED.ref",
            (
                request_id,
                str(event.get("kind") or ""),
                json.dumps(event, sort_keys=True),
                status,
                self._is_protocol_event(event),
                entity_kind,
                ref or None,
                _now(),
                None if status == "staged" else _now(),
            ),
        )

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        with self._locked():
            committed, pending, replace = self._pending_owner(request_id, event, operation="stage")
            if committed is not None:
                return
            if pending is not None and not replace:
                return
            self._claim_row(request_id, event, status="staged")
            self._commit()

    def claim(
        self,
        request_id: str,
        event: dict[str, Any],
        *,
        verify: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        with self._locked():
            committed, pending, _replace = self._pending_owner(request_id, event, operation="claim")
            if committed is not None:
                return committed
            if pending is not None:
                return pending
            if verify is not None:
                verify(event)
            self._claim_row(request_id, event, status="staged")
            self._commit()
            return None

    def append(self, request_id: str, event: dict[str, Any]) -> str:
        with self._locked():
            committed, _pending, _replace = self._pending_owner(request_id, event, operation="append")
            if committed is None:
                self._claim_row(request_id, event, status="committed")
                self._write_board_event(request_id, event)
                self._commit()
            return str(event["event_id"])

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        with self._locked():
            committed, pending, _replace = self._pending_owner(request_id, event, operation="discard")
            if committed is not None or pending is None:
                return
            self._execute(
                "DELETE FROM requests WHERE request_id = %s AND status = 'staged'", (request_id,)
            )
            self._commit()

    def reconcile(self) -> tuple[int, int]:
        """Nothing to repair: §7.1 makes the effect and the record one transaction (§7.3)."""
        return 0, 0

    def _write_board_event(self, request_id: str, event: dict[str, Any]) -> None:
        """Mirror a typed occurrence into `board_events`; a generic record has no row there."""
        kind = str(event.get("kind") or "")
        if kind not in _TYPED_KINDS or not self._is_protocol_event(event):
            return
        try:
            typed = Event.from_record(event)
        except (TypeError, ValueError):
            return
        if self._query("SELECT 1 FROM board_events WHERE event_id = %s", (typed.event_id,)):
            return
        self._execute(
            "INSERT INTO board_events (event_id, request_id, kind, entity_kind, ref, actor_role, "
            "actor_id, head_run_ref, reason, source_state, target_state, related_refs, data, "
            "occurred_at, committed, committed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, true, %s)",
            (
                typed.event_id,
                request_id,
                typed.kind.value,
                typed.entity_kind.value,
                typed.ref,
                typed.actor.role,
                typed.actor.id,
                typed.actor.head_run_ref,
                typed.reason,
                typed.source_state,
                typed.target_state,
                list(typed.related_refs.refs),
                json.dumps(typed.data, sort_keys=True),
                typed.occurred_at,
                _now(),
            ),
        )

    # --- locks -----------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """The successor of `.audit.lock`: one advisory lock over the request namespace.

        Inside a mutation transaction the whole claim is already serialized by the row locks the
        statements take, so this is only what keeps two *separate* claims from interleaving their
        read-then-write of one id.
        """
        key = _advisory_key("secretary.board.requests")
        if self.client._depth:
            self._execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            yield
            return
        self._execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            yield
        finally:
            self._execute("SELECT pg_advisory_unlock(%s)", (key,))
            self._commit()

    @contextlib.contextmanager
    def marker_comment_lock(self, reference: str) -> Iterator[None]:
        """`marker_comment_lock`, as an advisory lock scoped to one card's marker identity."""
        held = getattr(self._marker_lock_depth, "held", None)
        if held is None:
            held = self._marker_lock_depth.held = {}
        depth = held.get(reference, 0)
        if depth:
            held[reference] = depth + 1
            try:
                yield
            finally:
                held[reference] -= 1
            return
        key = _advisory_key(f"secretary.board.marker:{reference}")
        in_transaction = bool(self.client._depth)
        if in_transaction:
            self._execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        else:
            self._execute("SELECT pg_advisory_lock(%s)", (key,))
            self._commit()
        held[reference] = 1
        try:
            yield
        finally:
            del held[reference]
            if not in_transaction:
                self._execute("SELECT pg_advisory_unlock(%s)", (key,))
                self._commit()

    # --- marker reservations ---------------------------------------------------------

    def pending_marker_owner(
        self, reference: str, content: str, *, request_id: str | None = None
    ) -> str | None:
        owners = self.pending_marker_owners([(reference, content, request_id or "")])
        return owners.get(request_id or "")

    def pending_marker_owners(self, candidates: Iterable[tuple[str, str, str]]) -> dict[str, str]:
        from secretary.board.events import render_marker_comment

        wanted = {(reference, content): request_id for reference, content, request_id in candidates}
        owners: dict[str, str] = {}
        for record in self.pending_events():
            candidate = str(record.get("request_id") or "")
            identity: tuple[str, str] | None = None
            if self._is_protocol_event(record):
                try:
                    event = Event.from_record(record)
                except (TypeError, ValueError):
                    continue
                if event.entity_kind is EntityKind.CARD:
                    identity = (event.ref, render_marker_comment(event))
            else:
                payload = record.get("payload")
                if (
                    record.get("kind") == "restored_comment"
                    and isinstance(record.get("ref"), str)
                    and isinstance(payload, dict)
                    and isinstance(payload.get("restore_body"), str)
                ):
                    identity = (record["ref"], payload["restore_body"])
            request_id = wanted.get(identity) if identity is not None else None
            if request_id is not None and candidate != request_id:
                owners[request_id] = candidate
        return owners

    def _occurrence_projection_records(self) -> list[tuple[dict[str, Any], bool]]:
        """The fail-closed usage projection's input: committed first, then staged."""
        return [(record, False) for record in self.events()] + [
            (record, True) for record in self.pending_events()
        ]


__all__ = ["SqlAuditError", "SqlTaskAudit"]
