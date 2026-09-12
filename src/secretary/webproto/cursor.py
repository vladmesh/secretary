"""The resumable position in a card's event history, and why it is a place in the store.

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

**A cursor says which position semantics it carries, because one installation has two.**
The Kanboard reader seeks to a byte in `events.ndjson`; the PostgreSQL reader counts committed rows
of the traversal its audit owner publishes (`docs/BOARD_STORE.md` §7.3), because there is no file to
seek in. Both are frozen prefixes of an append-only history, and *neither number means anything in
the other store*: byte 4212 of a journal is not the 4212th committed request. So the document
carries `pos` -- :data:`POSITION_OFFSET` or :data:`POSITION_ORDINAL` -- and a reader that is handed
the other one refuses it (:class:`~secretary.webproto.errors.InvalidCursor`) instead of seeking to a
plausible-looking wrong place. A cursor issued before the upgrade carries no `pos` at all and is
therefore read as the byte offset it is: released Kanboard cursors keep working unchanged, and the
same cursor presented to a migrated installation is refused rather than reinterpreted. What a client
does after such a refusal is read a fresh task snapshot, whose `next_cursor` is the continuation in
the store that installation actually has.

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

#: A byte offset into `<data>/board/events.ndjson`: what a cursor of the Kanboard reader is, and
#: what a cursor with no stated semantics is taken to be. It is the released spelling, so it is the
#: one that stays implicit -- an encoded cursor omits `pos` for it and is byte-identical to the ones
#: this layer issued before the PostgreSQL backend had a reader at all.
POSITION_OFFSET = "offset"

#: How many committed records of this card stand before the next one: what a cursor of the
#: PostgreSQL reader is, which pages the audit owner's ordered traversal rather than a file.
POSITION_ORDINAL = "ordinal"

#: The two spellings above, and nothing else. A third one in a cursor document is a cursor this
#: reader did not issue.
POSITIONS = (POSITION_OFFSET, POSITION_ORDINAL)


@dataclass(frozen=True, slots=True)
class Cursor:
    """A position in one card's history: what stands before the next event, in this store's terms.

    `offset` is a byte offset into the file journal when `position` is :data:`POSITION_OFFSET` and a
    count of this card's committed records when it is :data:`POSITION_ORDINAL`. The field says which,
    so neither reader can be handed the other's number and seek with it.
    """

    ref: str
    offset: int
    position: str = POSITION_OFFSET

    def encode(self) -> str:
        document: dict[str, object] = {
            "v": CURSOR_VERSION,
            "ref": self.ref,
            "offset": self.offset,
        }
        if self.position != POSITION_OFFSET:
            document["pos"] = self.position
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii").rstrip("=")


def decode(value: str, *, ref: str, position: str | None = None) -> Cursor:
    """Read a cursor this layer issued for ``ref``, or refuse it by name.

    ``position`` is what the reader asking can actually continue: given one, a cursor carrying the
    other spelling is refused rather than read as a number in a store it was not measured in. A
    cursor with no stated semantics is a released Kanboard byte offset (:data:`POSITION_OFFSET`),
    which is what makes the PostgreSQL reader refuse it and the Kanboard reader go on honouring it.
    """
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
    carried = document.get("pos", POSITION_OFFSET)
    if carried not in POSITIONS:
        raise InvalidCursor("this cursor names a position this reader does not know how to read")
    if position is not None and carried != position:
        raise InvalidCursor(
            f"this cursor carries an {carried!r} position while this installation's card history is "
            f"paged by {position!r}; read a fresh task snapshot for the cursor that continues here"
        )
    return Cursor(ref=ref, offset=offset, position=carried)
