"""The resumable position in a card's event history, and why it is a count of committed records.

A card's history is the ordered traversal its audit owner publishes (`requests`/`board_events`,
`docs/BOARD_STORE.md` §7.3): committed records only ever join the end of it and none is ever
rewritten. A count of the records that stand before the next one is therefore the only thing that
can answer "give me what I have not read yet" without either losing an event or replaying one:

* it is not a timestamp. Two events can share a wall-clock second, a clock can go backwards, and
  ``occurred_at`` is stamped by the writer rather than by the commit -- so a time cursor either
  drops the second event of a pair or repeats the first;
* it is not an index into a filtered list the client saw. That list is recomputed on every read,
  so an event committed for another card would shift every position in it.

A cursor is opaque to the client: base64 of a small versioned document, carrying that count, the
card it belongs to, and ``pos`` -- :data:`POSITION_ORDINAL`, the one position this reader
continues. The ref binding is what makes a cursor from another card an error rather than a
plausible-looking wrong answer.

Opaque, but not encrypted or signed: it names a position in a history the caller may already read,
so there is nothing in it to protect. Tampering with one gets a client an
:class:`~secretary.webproto.errors.InvalidCursor`, never another card's history.

A document without ``pos``, or with any other spelling, is not a cursor this reader issued. That is
also what a cursor from the pre-2026-09-10 file journal is -- a byte offset into a file, with no
``pos`` -- so it is refused like any other malformed cursor rather than read as a count. What a
client does after such a refusal is read a fresh task snapshot, whose `next_cursor` continues here.

**One reader pages the whole audit rather than one card, and its position is the same count.**
:meth:`~secretary.webproto.command_reads.CommandReadLayer.command_history` is a cross-entity page
built on the same traversal, so its ``offset`` is how many committed records stand before the row
the next page continues at. The ref binding keeps the two apart with no second codec: a card's
cursor names its card, and a cross-entity one names no entity at all.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from secretary.webproto.errors import InvalidCursor

#: Bumped when the document below changes shape. An older or newer spelling is refused rather than
#: guessed at, because a misread position is a silently skipped event.
CURSOR_VERSION = 1

#: How many committed records stand before the next one, in the traversal the audit owner
#: publishes: the one position a cursor carries, and required in every cursor document.
POSITION_ORDINAL = "ordinal"


@dataclass(frozen=True, slots=True)
class Cursor:
    """A position in one card's history: how many of its committed records stand before the next."""

    ref: str
    offset: int

    def encode(self) -> str:
        document: dict[str, object] = {
            "v": CURSOR_VERSION,
            "ref": self.ref,
            "offset": self.offset,
            "pos": POSITION_ORDINAL,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii").rstrip("=")


def decode(value: str, *, ref: str) -> Cursor:
    """Read a cursor this layer issued for ``ref``, or refuse it by name.

    A document that does not say it carries :data:`POSITION_ORDINAL` -- a released byte-offset
    cursor of the pre-2026-09-10 file journal among them -- is refused rather than read as a count
    of records it was not measured in.
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
    if document.get("pos") != POSITION_ORDINAL:
        raise InvalidCursor(
            "this cursor names no position this reader can continue; "
            "read a fresh task snapshot for the cursor that continues here"
        )
    return Cursor(ref=ref, offset=offset)
