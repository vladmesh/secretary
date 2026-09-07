"""The resumable position in a card's event history, and why it is a journal offset.

The board's append-only audit journal (``<data>/board/events.ndjson``) already is the ordered
record of everything that happened to a card: `TaskAudit` appends whole lines under a lock and
never rewrites one. A position *in that file* is therefore the only thing that can answer "give me
what I have not read yet" without either losing an event or replaying one:

* it is not a timestamp. Two events can share a wall-clock second, a clock can go backwards, and
  ``occurred_at`` is stamped by the writer rather than by the append -- so a time cursor either
  drops the second event of a pair or repeats the first;
* it is not an index into a filtered list. That list is recomputed on every read, so an event
  appended for another card would shift every position in it;
* it is not a database sequence. There is no second store here, and inventing one would put the
  order of the truth somewhere other than where the truth is written.

A cursor is opaque to the client: base64 of a small versioned document, carrying the byte offset
one past the last event handed out and the card it belongs to. The ref binding is what makes a
cursor from another card an error rather than a plausible-looking wrong answer.

Opaque, but not encrypted or signed: it names a position in a file the caller may already read, so
there is nothing in it to protect. Tampering with one gets a client an
:class:`~secretary.webproto.errors.InvalidCursor`, never another card's history.

**One reader pages the same journal without seeking in it, and its position is a count.**
:meth:`~secretary.webproto.command_reads.CommandReadLayer.command_history` is a cross-entity page
built on `TaskAudit.events`, the traversal, rather than on a byte seek, so its ``offset`` is how
many committed records stand before the row the next page continues at. Both spellings are
positions in one append-only file and both are frozen for the same reason -- nothing before them
can ever change -- and the ref binding keeps them apart with no second codec: a card's cursor names
its card, and a cross-entity one names no entity at all.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from secretary.webproto.errors import InvalidCursor

#: Bumped when the document below changes shape. An older or newer spelling is refused rather than
#: guessed at, because a misread offset is a silently skipped event.
CURSOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class Cursor:
    """A position in one card's slice of the journal: the byte offset just past an event."""

    ref: str
    offset: int

    def encode(self) -> str:
        document = json.dumps(
            {"v": CURSOR_VERSION, "ref": self.ref, "offset": self.offset},
            sort_keys=True,
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(document.encode("utf-8")).decode("ascii").rstrip("=")


def decode(value: str, *, ref: str) -> Cursor:
    """Read a cursor this layer issued for ``ref``, or refuse it by name."""
    if not isinstance(value, str) or not value:
        raise InvalidCursor("a cursor is a non-empty string")
    padding = "=" * (-len(value) % 4)
    try:
        document = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError):
        raise InvalidCursor("this cursor was not issued by the task event reader") from None
    if not isinstance(document, dict) or document.get("v") != CURSOR_VERSION:
        raise InvalidCursor("this cursor uses an event cursor version this reader does not know")
    offset = document.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidCursor("this cursor names no readable journal position")
    if document.get("ref") != ref:
        raise InvalidCursor(
            f"this cursor belongs to {document.get('ref')!r} and cannot be continued on {ref!r}"
        )
    return Cursor(ref=ref, offset=offset)
