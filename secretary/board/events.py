"""Typed board events stored through the released TaskAudit journal.

This module owns the new protocol shape only.  TaskAudit remains the owner of
the append-only file, request-id index, pending-record layout, lock and atomic
file operations.  Consequently released generic records remain untouched and
their existing consumers can continue to read them as before.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from secretary.board.models import Event

if TYPE_CHECKING:
    from secretary.tasks import TaskAudit


T = TypeVar("T")


class BoardEventPending(RuntimeError):
    """The backend effect succeeded, but its durable protocol event did not."""


class BoardEventCanon:
    """The one typed-event facade over ``board/events.ndjson``.

    Generic TaskAudit records have no ``record_type`` discriminator and are
    deliberately ignored by :meth:`events`.  They are still read by TaskAudit
    itself, which is the compatibility reader for released record versions.
    """

    def __init__(self, data_dir: str | Path, *, audit: TaskAudit | None = None) -> None:
        if audit is None:
            # Kept local so secretary.tasks can import board transition values
            # without recursively loading its own TaskAudit definition.
            from secretary.tasks import TaskAudit
            audit = TaskAudit(data_dir)
        self.audit = audit

    def stage(self, request_id: str, event: Event) -> Event:
        """Durably stage the exact event before a backend mutation begins."""
        record = event.to_record(request_id)
        existing = self.audit.event(request_id)
        if existing is not None:
            self._require_same(existing, record)
            return Event.from_record(existing)
        self.audit.stage(request_id, record)
        return event

    def commit(self, request_id: str, event: Event) -> Event:
        """Append the staged event and clear its pending record."""
        record = event.to_record(request_id)
        self.stage(request_id, event)
        self.audit.append(request_id, record)
        return event

    def event(self, request_id: str) -> Event | None:
        record = self.audit.event(request_id)
        if record is None:
            return None
        if record.get("record_type") != Event.RECORD_TYPE:
            raise ValueError("request id belongs to a released generic audit record")
        return Event.from_record(record)

    def committed(self, request_id: str) -> Event | None:
        record = self.audit.committed_event(request_id)
        if record is None:
            return None
        if record.get("record_type") != Event.RECORD_TYPE:
            raise ValueError("request id belongs to a released generic audit record")
        return Event.from_record(record)

    def events(self, *, ref: str = "") -> Sequence[Event]:
        result: list[Event] = []
        for record in self.audit.events(reference=ref):
            if record.get("record_type") != Event.RECORD_TYPE:
                continue
            result.append(Event.from_record(record))
        return tuple(result)

    @staticmethod
    def _require_same(existing: dict[str, Any], expected: dict[str, Any]) -> None:
        if existing != expected:
            raise ValueError("request id belongs to another operation or payload")


class MutationEventTransaction:
    """Stage, effect, then commit one protocol mutation with fail-closed audit.

    ``replay`` must read the confirmed backend result without repeating the
    effect.  It is used for both a committed request-id replay and recovery of a
    pending event whose backend effect already happened.  This is the reusable
    seam future KanboardBoardHost writers need: no caller can report the effect
    as successful after ``commit`` fails.
    """

    def __init__(self, canon: BoardEventCanon, *, request_id: str, event: Event) -> None:
        self.canon = canon
        self.request_id = request_id
        self.event = event

    def execute(self, effect: Callable[[], T], *, replay: Callable[[], T]) -> T:
        committed = self.canon.committed(self.request_id)
        if committed is not None:
            self._require_same_event(committed)
            return replay()

        pending = self.canon.event(self.request_id)
        if pending is not None:
            self._require_same_event(pending)
            # The pending record is evidence that the caller staged before the
            # backend write.  Never run the backend effect a second time.
            result = replay()
            self._commit_or_raise()
            return result

        self.canon.stage(self.request_id, self.event)
        try:
            result = effect()
        except Exception:
            # No effect completed, so its exact staged event is not a recovery
            # obligation.  TaskAudit's discard keeps its released semantics.
            self.canon.audit.discard(self.request_id)
            raise
        self._commit_or_raise()
        return result

    def _commit_or_raise(self) -> None:
        try:
            self.canon.commit(self.request_id, self.event)
        except OSError as exc:
            raise BoardEventPending(
                "backend write committed; board protocol event repair is required"
            ) from exc

    def _require_same_event(self, existing: Event) -> None:
        if existing != self.event:
            raise ValueError("request id belongs to another operation or payload")
