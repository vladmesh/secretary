"""Paged, resumable reading of one card's slice of the committed board audit.

`CommittedAudit` reads the card canon through the traversal its audit owner publishes, because
`requests`/`board_events` is not a file to seek in (`docs/BOARD_STORE.md` §7.3). The caller resolves
its card client and asks :func:`secretary.tasks.task_audit_for` for that client's audit owner,
exactly as every other live audit reader does. The file journal under `<data>/board` is not a card
audit owner any more; read beside a PostgreSQL client, a card's history answered from a projection
that backend never writes -- unavailable where the file was swept, and a successful empty or stale
history where an old one was left behind, with the committed records invisible.

The cursor counts the card's committed records and says so (``pos``). A released cursor that names
a byte offset into the pre-2026-09-10 file journal is refused rather than seeked with
(:mod:`secretary.webproto.cursor`).

Both record shapes the audit holds are returned -- the typed board protocol events (``record_type``
of ``board.protocol_event``) and the released generic audit records beside them -- because a client
asking "what happened to this card" must not be shown a history with the transitions in it and the
creations missing. ``typed`` says which shape each row came from, and a record whose typed payload
does not parse is still returned as the row it is rather than dropped: losing a written event is
the one failure this reader may not have.

Pending (staged, not yet committed) records are not events yet: they describe an effect whose
backend write may still fail. They are not on the page, and no cursor can be positioned inside
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from secretary.board.models import Event
from secretary.webproto import sources
from secretary.webproto.cursor import Cursor
from secretary.webproto.errors import InvalidCursor
from secretary.webproto.sources import Source

#: What a caller gets when it asks for a page without saying how big.
DEFAULT_LIMIT = 50
#: The ceiling a caller cannot raise. A page is a page; a client that wants the whole history pages
#: through it with the cursor it is given.
MAX_LIMIT = 500


@dataclass(frozen=True, slots=True)
class EventPage:
    """One page of a card's history, and where reading continues."""

    items: tuple[dict[str, Any], ...]
    next_cursor: Cursor
    has_more: bool
    source: Source


class CommittedAudit:
    """The read-only reader of a card's committed history in the store its audit owner holds.

    Takes the audit owner itself -- `SqlTaskAudit` for a PostgreSQL client -- and pages
    :meth:`~secretary.board.sql_audit.SqlTaskAudit.events`, the traversal every other reader of this
    installation's audit already uses. It opens no store of its own, holds no index and caches
    nothing: one page is one traversal, filtered to this card by the audit's own predicate.

    A *position* here is an ordinal: how many of this card's committed records stand before the next
    one. That is frozen for the reason a byte offset is -- the audit only gains records, and a
    committed record's place in claim order never changes -- so reading the same cursor twice returns
    the same page and a cursor issued before newer records returns exactly those.
    """

    #: The vocabulary a store that cannot answer raises in. `TaskError` is what `SqlTaskAudit`
    #: translates a driver failure into; the rest are the shapes a record the traversal cannot
    #: convert arrives as. An audit nobody could read is a source fact and never an empty history.
    _FAILURES = (OSError, ValueError, KeyError, TypeError)

    def __init__(self, audit: Any, *, backend: str = "postgres") -> None:
        self.audit = audit
        self.backend = backend

    def page(self, ref: str, *, cursor: Cursor | None, limit: int, now: float) -> EventPage:
        """The next ``limit`` committed records for ``ref`` at or after ``cursor``."""
        start = 0 if cursor is None else cursor.offset
        bounded = max(1, min(int(limit), MAX_LIMIT))
        try:
            records = self._records(ref)
        except self._failures() as exc:
            return self._unreadable(ref, cursor, exc, now=now)
        if start > len(records):
            raise InvalidCursor(
                "this cursor is past the end of the committed board audit, which only ever grows; "
                "the audit it was issued for is not the audit being read"
            )
        window = records[start : start + bounded]
        position = start + len(window)
        return EventPage(
            items=tuple(
                _item(record, self._at(ref, start + index + 1))
                for index, record in enumerate(window)
            ),
            next_cursor=self._at(ref, position),
            has_more=len(window) >= bounded and position < len(records),
            source=sources.available(now),
        )

    def tail(self, ref: str, *, limit: int, now: float) -> EventPage:
        """The last ``limit`` committed records for ``ref``, and the cursor that continues them.

        As in the file reader, the cursor is the end of the history rather than the end of the page,
        so a client that polls with it is never handed an event it has just been shown.
        """
        bounded = max(1, min(int(limit), MAX_LIMIT))
        try:
            records = self._records(ref)
        except self._failures() as exc:
            return self._unreadable(ref, None, exc, now=now)
        start = max(0, len(records) - bounded)
        return EventPage(
            items=tuple(
                _item(record, self._at(ref, start + index + 1))
                for index, record in enumerate(records[start:])
            ),
            next_cursor=self._at(ref, len(records)),
            has_more=False,
            source=sources.available(now),
        )

    def _records(self, ref: str) -> tuple[dict[str, Any], ...]:
        """This card's committed records in claim order, as the audit owner traverses them.

        Both record shapes come back, exactly as they do off the file: the typed board protocol
        events and the generic audit records beside them -- a product run's `product_run.started`
        among the latter. A client asking what happened to a card may not be shown a history with
        the transitions in it and the creations missing.
        """
        return tuple(
            record for record in self.audit.events(ref) if isinstance(record, dict)
        )

    def _at(self, ref: str, ordinal: int) -> Cursor:
        return Cursor(ref=ref, offset=ordinal)

    def _failures(self) -> tuple[type[BaseException], ...]:
        from secretary.tasks import TaskError

        return (TaskError, *self._FAILURES)

    def _unreadable(self, ref: str, cursor: Cursor | None, exc: Exception, *, now: float) -> EventPage:
        """An audit that would not answer, said as the source fact it is.

        The same rule the file reader holds, for the same reason: the cursor handed back is the one
        the caller came with, so a client that keeps polling resumes where it stopped instead of
        restarting at the beginning of a history it has already read -- and an empty page is never
        published for a store that did not answer.
        """
        return EventPage(
            items=(),
            next_cursor=cursor or self._at(ref, 0),
            has_more=False,
            source=sources.unavailable(
                f"the committed {self.backend} board audit could not be read: {_reason(exc)}",
                now=now,
            ),
        )


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _item(record: dict[str, Any], cursor: Cursor) -> dict[str, Any]:
    """One journal record in the layer's stable event shape."""
    typed = record.get("record_type") == Event.RECORD_TYPE
    actor = record.get("actor")
    transition = record.get("transition")
    related = record.get("related_refs")
    payload = record.get("data") if typed else record.get("payload")
    return {
        "cursor": cursor.encode(),
        "typed": typed,
        "kind": _text(record.get("kind")),
        "event_id": _text(record.get("event_id")) or None,
        "request_id": _text(record.get("request_id")) or None,
        "ref": _text(record.get("ref")),
        "occurred_at": _text(record.get("occurred_at")) or None,
        "actor": (
            {"role": _text(actor.get("role")), "id": _text(actor.get("id"))}
            if isinstance(actor, dict)
            else None
        ),
        # Typed events carry the reason their writer gave; generic audit records carry the outcome
        # of the backend effect. Neither is renamed into the other's field.
        "reason": _text(record.get("reason")) or None,
        "outcome": _text(record.get("outcome")) or None,
        "transition": (
            {"source": _text(transition.get("source")), "target": _text(transition.get("target"))}
            if isinstance(transition, dict)
            else None
        ),
        "related_refs": [ref for ref in related if isinstance(ref, str)] if isinstance(related, list) else [],
        "data": payload if isinstance(payload, dict) else {},
    }


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
