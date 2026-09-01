"""Typed board events stored through the released TaskAudit journal."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from secretary.board.models import Event, EventKind

if TYPE_CHECKING:
    from secretary.tasks import TaskAudit


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AttemptUsageOccurrence:
    """One validated usage occurrence and whether its export append is still owed."""

    request_id: str
    event: Event
    pending: bool


@contextlib.contextmanager
def marker_comment_lock(data_dir: str | Path, ref: str) -> Iterator[None]:
    """Serialize one Card marker occurrence from its witness through commit.

    Marker prose intentionally has no request id, so the per-Card lock makes the staged matching-row
    ordinal a real occurrence witness even when two writers choose identical marker text at once.
    """
    directory = Path(data_dir) / "board" / "marker-comments"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(ref.encode("utf-8")).hexdigest() + ".lock"
    with (directory / name).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def render_marker_comment(event: Event) -> str:
    """Render the public Card marker grammar from its complete typed event."""
    data = event.data
    marker = data.get("marker")
    body = data.get("body")
    if not isinstance(marker, str) or not marker or not isinstance(body, str):
        raise ValueError("Card marker event has incomplete marker data")
    if event.reason != body or not body.strip():
        raise ValueError("Card marker event reason does not match its marker body")
    if event.kind is EventKind.CARD_REPORTED:
        status = data.get("status")
        if status not in {"done", "blocked"} or marker != f"report:{status}":
            raise ValueError("Card report event has an unsupported marker payload")
        classification = data.get("classification")
        if status == "blocked":
            if classification not in {"external_fact", "wrong_task_definition"}:
                raise ValueError("blocked Card report has an unsupported classification")
            return f"[{marker}]\nclassification: {classification}\n\n{body}"
        if classification is not None:
            raise ValueError("done Card report must not carry a classification")
    elif event.kind is EventKind.CARD_VERDICTED:
        status = data.get("status")
        if status not in {"green", "red"} or marker != f"review:{status}":
            raise ValueError("Card verdict event has an unsupported marker payload")
    elif event.kind is EventKind.CARD_DECIDED:
        decision = data.get("decision")
        if decision not in {"release", "rework", "reslice"} or marker != f"decision:{decision}":
            raise ValueError("Card decision event has an unsupported marker payload")
    else:
        raise ValueError("event is not a Card marker occurrence")
    return f"[{marker}]\n{body}"


class BoardEventPending(RuntimeError):
    """The backend effect succeeded, but its durable protocol event did not."""


class BoardEventCanon:
    """The one typed-event facade over ``board/events.ndjson``.

    Generic TaskAudit records have no ``record_type`` discriminator and are deliberately ignored by
    :meth:`events`; TaskAudit itself remains the compatibility reader for released record versions.
    """

    def __init__(self, data_dir: str | Path, *, audit: TaskAudit | None = None) -> None:
        if audit is None:
            # Kept local so secretary.tasks can import board transition values
            # without recursively loading its own TaskAudit definition.
            from secretary.tasks import TaskAudit

            audit = TaskAudit(data_dir)
        self.audit = audit

    def stage(self, request_id: str, event: Event) -> Event:
        """Durably stage the exact event before a backend mutation begins.

        The one staging route of the typed canon, and it owns same-request idempotency itself: reading
        the committed record, reading the pending record, checking event-id identity and writing the new
        pending record all happen inside a single TaskAudit lock hold. A second caller reusing the
        request id either gets the event that already owns it or fails.
        """
        record = event.to_record(request_id)
        existing = self._claim(request_id, record)
        if existing is None:
            return event
        return self._typed(existing)

    def commit(self, request_id: str, event: Event) -> Event:
        """Append the staged event and clear its pending record."""
        record = event.to_record(request_id)
        self.stage(request_id, event)
        self.audit.append(request_id, record)
        return event

    def event(self, request_id: str) -> Event | None:
        record = self.audit.event(request_id)
        return None if record is None else self._typed(record)

    def committed(self, request_id: str) -> Event | None:
        record = self.audit.committed_event(request_id)
        return None if record is None else self._typed(record)

    def events(self, *, ref: str = "") -> Sequence[Event]:
        result: list[Event] = []
        for record in self.audit.events(reference=ref):
            if record.get("record_type") != Event.RECORD_TYPE:
                continue
            result.append(Event.from_record(record))
        return tuple(result)

    def attempt_usage_occurrences(self, *, ref: str = "") -> Sequence[AttemptUsageOccurrence]:
        """Project committed and staged ``attempt.usage`` records into one canonical view.

        The audit supplies both sets under its lock so publication cannot make an occurrence vanish
        between two reads. Every usage-shaped record crosses the typed boundary here. Request and
        event ids each own one immutable payload; an exact committed-plus-pending duplicate is one
        occurrence whose export is already visible, while conflicting ownership fails closed.
        """
        records = self.audit._occurrence_projection_records()
        by_request: dict[str, tuple[Event, bool]] = {}
        request_claims: dict[str, tuple[dict[str, Any], bool]] = {}
        event_claims: dict[str, tuple[str, bool]] = {}
        phase_owners: dict[tuple[str, str, int, int, str], tuple[str, str]] = {}
        order: list[str] = []
        for record, pending in records:
            usage_shaped = record.get("kind") == EventKind.ATTEMPT_USAGE.value
            request_id = record.get("request_id")
            if isinstance(request_id, str) and request_id:
                previous_claim = request_claims.get(request_id)
                if (
                    previous_claim is not None
                    and previous_claim[0] != record
                    and (previous_claim[1] or usage_shaped)
                ):
                    raise ValueError(
                        f"attempt usage request id {request_id!r} has conflicting event payloads"
                    )
                request_claims[request_id] = (
                    record,
                    usage_shaped or bool(previous_claim and previous_claim[1]),
                )
            event_id = record.get("event_id")
            if isinstance(event_id, str) and event_id and isinstance(request_id, str):
                previous_event_claim = event_claims.get(event_id)
                if (
                    previous_event_claim is not None
                    and previous_event_claim[0] != request_id
                    and (previous_event_claim[1] or usage_shaped)
                ):
                    raise ValueError(f"attempt usage event id {event_id!r} has conflicting request owners")
                event_claims[event_id] = (
                    request_id,
                    usage_shaped or bool(previous_event_claim and previous_event_claim[1]),
                )
            if not usage_shaped:
                continue
            event = self._typed(record)
            if ref and event.ref != ref:
                continue
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("attempt usage occurrence has no request id")
            phase = (
                event.ref,
                str(event.data["role"]),
                int(event.data["attempt"]),
                int(event.data["report_generation"]),
                str(event.data["phase"]),
            )
            owner = (str(event.data["attempt_id"]), request_id)
            previous_owner = phase_owners.get(phase)
            if previous_owner is not None and previous_owner != owner:
                raise ValueError(f"attempt usage phase {phase!r} has conflicting occurrence owners")
            phase_owners[phase] = owner
            existing = by_request.get(request_id)
            if existing is None:
                by_request[request_id] = (event, pending)
                order.append(request_id)
                continue
            previous, was_pending = existing
            if previous != event:
                raise ValueError(f"attempt usage request id {request_id!r} changed after validation")
            # A committed copy makes the occurrence exported even if an exact stale pending copy
            # remains. Conversely, duplicate pending reads remain pending.
            by_request[request_id] = (event, was_pending and pending)
        return tuple(
            AttemptUsageOccurrence(request_id, by_request[request_id][0], by_request[request_id][1])
            for request_id in order
        )

    def _claim(self, request_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
        # Kept local for the same reason as the TaskAudit import above: secretary.tasks
        # imports this package for its transition registry.
        from secretary.tasks import TaskError

        try:
            return self.audit.claim(request_id, record, verify=self._require_unclaimed_event_id)
        except TaskError as exc:
            if exc.code != "validation":
                raise
            # The typed boundary speaks ValueError; a command-level TaskError would leak
            # TaskAudit's exit-code protocol into board callers.
            raise ValueError(exc.message) from None

    def _require_unclaimed_event_id(self, record: dict[str, Any]) -> None:
        """An event id names one occurrence, so refuse to publish a second one under it.

        Runs inside TaskAudit's lock, which is what makes it a real precondition of the write rather
        than an advisory check some other writer can race past.
        """
        event_id = record.get("event_id")
        owner = self.audit.event_id_owner(str(event_id))
        if owner is not None and owner != record.get("request_id"):
            raise ValueError(f"event id {event_id!r} already belongs to another request")

    @staticmethod
    def _typed(record: dict[str, Any]) -> Event:
        if record.get("record_type") != Event.RECORD_TYPE:
            raise ValueError("request id belongs to a released generic audit record")
        return Event.from_record(record)


class MutationEventTransaction:
    """Stage, effect, then commit one protocol mutation with fail-closed audit.

    The single enforcement point for a staged occurrence. The window in which the staged record may
    be discarded is exactly the ``effect`` call and nothing else; everything after it — the
    confirming read, the caller's ``finish`` work and the commit — raises :class:`BoardEventPending`
    and leaves the exact pending record for recovery.

    ``effect`` issues the single backend effect and nothing else. Its return value is ignored, and it
    may raise only while no effect has been applied. A verification read belongs in ``confirm``,
    never inside ``effect``, because a read that fails after a completed write is not evidence that
    the write did not happen.

    ``confirm`` reads the confirmed backend result without repeating the effect.

    ``finish`` is the writer's remaining idempotent backend work for the same occurrence. It runs
    after the effect is confirmed and *before* the event commits, so an incomplete one leaves the
    exact pending record the caller and :meth:`reconcile` need instead of a clean journal over a
    half-written card.
    """

    def __init__(self, canon: BoardEventCanon, *, request_id: str, event: Event) -> None:
        self.canon = canon
        self.request_id = request_id
        self.event = event

    def execute(
        self,
        effect: Callable[[], Any],
        *,
        confirm: Callable[[], T],
        finish: Callable[[T], None] | None = None,
    ) -> T:
        committed = self.canon.committed(self.request_id)
        if committed is not None:
            self._require_same_event(committed)
            # Replay still passes through the canon's ownership gate.  It is a no-op for the
            # already committed exact event, but keeps all transaction contours on the same
            # lock-protected protocol boundary.
            self._commit_or_raise()
            # A committed event is proof that `finish` already ran to completion, so a replay
            # owes the backend nothing further.
            return confirm()

        pending = self.canon.event(self.request_id)
        if pending is not None:
            self._require_same_event(pending)
            # The pending record is evidence that the caller staged before the
            # backend write.  Never run the backend effect a second time, and
            # never resolve the record on a read that could not confirm it.
            result = self._confirm_or_raise(
                confirm,
                "pending backend effect is unconfirmed; protocol event repair is required",
            )
            self._finish_or_raise(finish, result)
            self._commit_or_raise()
            return result

        self.canon.stage(self.request_id, self.event)
        try:
            effect()
        except Exception:
            # The only discard in this class, and the only place one is correct:
            # `effect` raised, so by its contract no effect was applied and this
            # staged event is not a recovery obligation.  TaskAudit's discard
            # keeps its released semantics.
            self.canon.audit.discard(self.request_id, self.event.to_record(self.request_id))
            raise
        # The effect completed.  From here the record survives every outcome.
        result = self._confirm_or_raise(
            confirm,
            "backend effect completed but is unconfirmed; protocol event repair is required",
        )
        self._finish_or_raise(finish, result)
        self._commit_or_raise()
        return result

    def _confirm_or_raise(self, confirm: Callable[[], T], message: str) -> T:
        try:
            return confirm()
        except Exception as exc:
            raise BoardEventPending(message) from exc

    def _finish_or_raise(self, finish: Callable[[T], None] | None, result: T) -> None:
        if finish is None:
            return
        try:
            finish(result)
        except Exception as exc:
            raise BoardEventPending(
                "backend write committed; board cleanup and its protocol event repair are required"
            ) from exc

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
