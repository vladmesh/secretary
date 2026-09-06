"""Paged, resumable reading of one card's slice of the board audit journal.

The journal is `TaskAudit`'s: ``<data>/board/events.ndjson``, one JSON document per line, appended
under a lock and never rewritten. This module adds exactly two things to
:meth:`secretary.tasks.TaskAudit.events`, and deliberately nothing else -- no second store, no
index, no cache:

* a *position*, so a reader can continue where it stopped. The audit's own committed index is byte
  offsets into this same file, so an offset is not a new idea here, only a published one; and
* a *page*, so a client is not handed a card's whole history to show its last ten events.

The predicate is the audit's: a record whose top-level ``ref`` is this card. Both record shapes on
the journal are returned -- the typed board protocol events (``record_type`` of
``board.protocol_event``) and the released generic audit records beside them -- because a client
asking "what happened to this card" must not be shown a history with the transitions in it and the
creations missing. ``typed`` says which shape each row came from, and a record whose typed payload
does not parse is still returned as the row it is rather than dropped: losing a written event is
the one failure this reader may not have.

Pending (staged, not yet committed) records are not events yet: they describe an effect whose
backend write may still fail. They are not on the page, and no cursor can be positioned inside
them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
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


class EventJournal:
    """The read-only reader of the committed board journal."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.path = Path(os.fspath(data_dir)) / "board" / "events.ndjson"

    def page(self, ref: str, *, cursor: Cursor | None, limit: int, now: float) -> EventPage:
        """The next ``limit`` events for ``ref`` at or after ``cursor``.

        Reading the same cursor twice returns the same page: the file is append-only, so every byte
        before the cursor's offset is frozen, and nothing before it can enter a later page. A cursor
        issued before newer events were appended returns exactly those newer events, for the same
        reason.
        """
        start = 0 if cursor is None else cursor.offset
        bounded = max(1, min(int(limit), MAX_LIMIT))
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            return self._unreadable(ref, cursor, exc, now=now)
        if start > size:
            raise InvalidCursor(
                "this cursor is past the end of the event journal, which only ever grows; "
                "the journal it was issued for is not the journal being read"
            )
        needle = f'"{ref}"'
        items: list[dict[str, Any]] = []
        position = start
        try:
            with self.path.open("rb") as journal:
                journal.seek(start)
                for raw in journal:
                    position += len(raw)
                    record = _record(raw, needle)
                    if record is None or record.get("ref") != ref:
                        continue
                    items.append(_item(record, Cursor(ref=ref, offset=position)))
                    if len(items) >= bounded:
                        break
        except OSError as exc:
            return self._unreadable(ref, cursor, exc, now=now)
        return EventPage(
            items=tuple(items),
            next_cursor=Cursor(ref=ref, offset=position),
            has_more=len(items) >= bounded and position < size,
            source=sources.available(now),
        )

    def tail(self, ref: str, *, limit: int, now: float) -> EventPage:
        """The last ``limit`` events for ``ref``, and the cursor that continues after them.

        What a task page opens on. The cursor it returns is the end of the journal rather than the
        end of the page, so a client that polls with it is never handed an event it has just been
        shown.
        """
        bounded = max(1, min(int(limit), MAX_LIMIT))
        needle = f'"{ref}"'
        items: list[dict[str, Any]] = []
        position = 0
        try:
            with self.path.open("rb") as journal:
                for raw in journal:
                    position += len(raw)
                    record = _record(raw, needle)
                    if record is None or record.get("ref") != ref:
                        continue
                    items.append(_item(record, Cursor(ref=ref, offset=position)))
                    if len(items) > bounded:
                        del items[0]
        except OSError as exc:
            return self._unreadable(ref, None, exc, now=now)
        return EventPage(
            items=tuple(items),
            next_cursor=Cursor(ref=ref, offset=position),
            has_more=False,
            source=sources.available(now),
        )

    def _unreadable(self, ref: str, cursor: Cursor | None, exc: OSError, *, now: float) -> EventPage:
        """A journal that could not be read is a source fact, not an empty history.

        The cursor handed back is the one the caller came with, so a client that keeps polling
        resumes from where it actually stopped once the source answers again, instead of silently
        restarting at the beginning of a history it has already read.
        """
        return EventPage(
            items=(),
            next_cursor=cursor or Cursor(ref=ref, offset=0),
            has_more=False,
            source=sources.unavailable(
                f"the board event journal at {self.path} could not be read: {exc}",
                now=now,
                evidence=self.path,
            ),
        )


def _record(raw: bytes, needle: str) -> dict[str, Any] | None:
    """One journal line as an object, or nothing when the line is not this card's or not JSON.

    The substring prefilter is only that: a line naming the card anywhere -- in a related ref, in a
    body -- still has its top-level ``ref`` checked by the caller. It exists because a card's page
    reads a journal of tens of thousands of lines to show ten of them.
    """
    if needle not in raw.decode("utf-8", "replace"):
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


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
