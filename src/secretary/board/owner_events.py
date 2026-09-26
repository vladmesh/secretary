"""Owner events: what needs the owner, and what the owner should know, on the board (secretary-1770).

One table, `owner_events` (revision `0018_owner_events`), and this module is the only code that
touches it. Producers write through :func:`record`; the web reads and marks through
:class:`OwnerEventStore`; the stay-unread rule runs through :func:`settle`.

**Kinds and classes.** Every kind belongs to one class, and the class is derived from the kind here
(:data:`KIND_CLASS`), never passed by a producer. `needs_owner` is a fact only the owner can move on:
a card the PO handed to the owner, the steward's report card that needs a human. `notice` is a fact
the owner should know: a sprint closed or stopped, the budget signal, a dead head nobody relaunched, a
failed PO turn, a red provider. The database holds both vocabularies and the kind-to-class rule as
CHECK constraints (`board/schema.py`), from the same lists.

**The writer never fails its caller.** :func:`record` is idempotent under its dedup key (a unique
column: a repeat inserts nothing) and swallows every failure after logging it: a store that does not
answer, a board without migration `0018` (merged code runs before the upgrade applies the migration),
a store that is not configured at all. A producer's own work never depends on the bell.

**Stay-unread.** A `needs_owner` event whose subject card carries the `waiting_owner` mark
(`board.owner_handover`) is never marked read by a click or by "mark all read": :meth:`mark_read`
refuses it and :meth:`mark_all_read` takes notices only. Its `read_at` is set by :func:`settle` when
the card's mark clears, which `TaskWriter._reset_transition_metadata` calls in the transition's own
transaction.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from secretary.board.extension_bag import EXTENSION_BAG
from secretary.board.owner_handover import MARK_KEYS

TABLE = "owner_events"

NEEDS_OWNER = "needs_owner"
NOTICE = "notice"
#: The CHECK `owner_event_class_in_vocabulary`, in this order.
CLASSES = (NEEDS_OWNER, NOTICE)

CARD_HANDED_TO_OWNER = "card_handed_to_owner"
STEWARD_NEEDS_HUMAN = "steward_needs_human"
SPRINT_CLOSED = "sprint_closed"
SPRINT_STOPPED = "sprint_stopped"
BUDGET_SIGNAL = "budget_signal"
OBSERVER_DEAD = "observer_dead"
HEAD_DEAD = "head_dead"
PO_TURN_FAILED = "po_turn_failed"
PROVIDER_RED = "provider_red"

#: Every kind and the class it belongs to: the CHECKs `owner_event_kind_in_vocabulary` and
#: `owner_event_class_follows_kind` (board/schema.py, 0018) are these two lists. A new kind joins it
#: here and in a migration together.
KIND_CLASS: dict[str, str] = {
    CARD_HANDED_TO_OWNER: NEEDS_OWNER,
    STEWARD_NEEDS_HUMAN: NEEDS_OWNER,
    SPRINT_CLOSED: NOTICE,
    SPRINT_STOPPED: NOTICE,
    BUDGET_SIGNAL: NOTICE,
    OBSERVER_DEAD: NOTICE,
    HEAD_DEAD: NOTICE,
    PO_TURN_FAILED: NOTICE,
    PROVIDER_RED: NOTICE,
}
KINDS = tuple(KIND_CLASS)
NEEDS_OWNER_KINDS = tuple(kind for kind, value in KIND_CLASS.items() if value == NEEDS_OWNER)

#: The subject of a PO turn that no card input started: its session, `po-session:<session id>`.
PO_SESSION_PREFIX = "po-session:"
#: The longest text an event keeps; a producer's reason is cut, never refused.
TEXT_LIMIT = 2000
#: The most events one list read returns.
LIST_LIMIT = 500

_COLUMNS = 'id, kind, "class", subject_ref, text, created_at, read_at, dedup_key'

logger = logging.getLogger(__name__)


class OwnerEventError(RuntimeError):
    """An owner event could not be read or written."""


class OwnerEventsUnavailable(OwnerEventError):
    """The board store did not answer, or has no `owner_events` table yet (migration 0018)."""


class OwnerEventNotFound(OwnerEventError):
    pass


class ReadRefused(OwnerEventError):
    """A `needs_owner` event whose card still waits for the owner is not marked read by hand."""


@dataclass(frozen=True)
class OwnerEvent:
    id: int
    kind: str
    event_class: str
    subject_ref: str | None
    text: str
    created_at: datetime
    read_at: datetime | None
    dedup_key: str
    #: Whether the subject card carries the `waiting_owner` mark now (filled by the list read).
    held: bool = False

    @property
    def unread(self) -> bool:
        return self.read_at is None

    @property
    def pinned(self) -> bool:
        """An open `needs_owner` event: listed above the notices, whatever its date."""
        return self.event_class == NEEDS_OWNER and self.read_at is None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "class": self.event_class,
            "subject_ref": self.subject_ref,
            "text": self.text,
            "created_at": _iso(self.created_at),
            "read_at": _iso(self.read_at),
            "dedup_key": self.dedup_key,
            "unread": self.unread,
            "pinned": self.pinned,
            "held": self.held,
        }


def class_of(kind: str) -> str:
    """The class a kind belongs to; an unknown kind is a programming error."""
    try:
        return KIND_CLASS[kind]
    except KeyError:
        raise ValueError(f"{kind!r} is not an owner event kind; the kinds are {', '.join(KINDS)}") from None


def po_session_subject(session_id: str) -> str:
    return PO_SESSION_PREFIX + session_id


def list_order_key(event: OwnerEvent) -> tuple[int, float, int]:
    """The list order: open `needs_owner` events first, then newest first (the SQL read's ORDER BY)."""
    return (0 if event.pinned else 1, -event.created_at.timestamp(), -event.id)


#: The heading of the steward report's section for what only a human can do (steward skill, step 5).
NEEDS_HUMAN_HEADING = "needs a human"


def _heading(line: str) -> str:
    return line.strip().lstrip("#").strip().strip("*_").strip().rstrip(":").strip().casefold()


def needs_human_section(text: str) -> str | None:
    """The body of a "Needs a human" section in `text` (a markdown heading or a line of its own), or None.

    The body runs to the next markdown heading. The steward's report always carries the section, with a
    one-line "none" when nothing needs a human (steward skill, report structure): an empty or "none"
    body is no section here.
    """
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if _heading(line) != NEEDS_HUMAN_HEADING:
            continue
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.lstrip().startswith("#"):
                break
            body.append(following)
        section = "\n".join(body).strip()
        if section.strip(" .-*_()").casefold() in {"", "none", "nothing"}:
            return None
        return section
    return None


def card_holds_mark(card: Mapping[str, Any] | None) -> bool:
    """Whether a card row's extension bag carries any field of the `waiting_owner` mark."""
    if not isinstance(card, Mapping):
        return False
    extensions = card.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    return isinstance(bag, Mapping) and any(str(bag.get(key) or "") for key in MARK_KEYS)


# --- the PostgreSQL store ------------------------------------------------------------------

#: A card carrying the mark, spelled once for the SQL reads and the refusal.
_HELD = (
    "EXISTS (SELECT 1 FROM tasks t WHERE t.task_ref = e.subject_ref "
    f"AND (t.extensions -> '{EXTENSION_BAG}') ?| ARRAY[{', '.join(repr(key) for key in MARK_KEYS)}])"
)


class OwnerEventStore:
    """`owner_events` over the board store: one short connection per operation.

    `client` is a `SqlCardClient` whose open transaction a write on the same thread joins under a
    savepoint (:func:`settle` inside a card transition): the settle then commits or rolls back with
    the transition, and its own failure rolls back only the savepoint.
    """

    def __init__(self, credentials: Any, *, client: Any = None) -> None:
        self.credentials = credentials
        self.client = client

    @classmethod
    def for_instance(cls, instance_dir: Path | str, *, role: str = "app") -> OwnerEventStore:
        from secretary.board.store import resolve_role

        return cls(resolve_role(instance_dir, role))

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        import psycopg

        try:
            client = self.client
            if client is not None and getattr(client, "_depth", 0):
                connection = client.connection
                with connection.transaction():
                    yield connection
                return
            with psycopg.connect(self.credentials.conninfo(), connect_timeout=5) as connection:
                yield connection
        except psycopg.errors.UndefinedTable:
            raise OwnerEventsUnavailable(
                "the board store has no owner_events table yet: migration 0018 is not applied"
            ) from None
        except psycopg.Error as exc:
            raise OwnerEventsUnavailable(f"the board store did not answer an owner event operation: {exc}") from exc

    def insert(self, kind: str, subject_ref: str | None, text: str, dedup_key: str) -> bool:
        """One new event, or nothing when `dedup_key` is already recorded; True when this call wrote it."""
        event_class = class_of(kind)
        with self._connection() as connection:
            row = connection.execute(
                'INSERT INTO owner_events (kind, "class", subject_ref, text, created_at, dedup_key) '
                "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
                (kind, event_class, subject_ref, text, dedup_key),
            ).fetchone()
        return row is not None

    def events(self, *, unread_only: bool = False, limit: int = LIST_LIMIT) -> list[OwnerEvent]:
        """Open `needs_owner` events first, then everything newest first; `unread_only` keeps the unread."""
        where = "WHERE e.read_at IS NULL " if unread_only else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {', '.join('e.' + column.strip() for column in _COLUMNS.split(','))}, {_HELD} "
                f"FROM owner_events e {where}"
                "ORDER BY (e.\"class\" = 'needs_owner' AND e.read_at IS NULL) DESC, e.created_at DESC, e.id DESC "
                "LIMIT %s",
                (limit,),
            ).fetchall()
        return [OwnerEvent(*row) for row in rows]

    def unread_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT count(*) FROM owner_events WHERE read_at IS NULL").fetchone()[0])

    def mark_read(self, event_id: int) -> OwnerEvent:
        """Mark one event read; an already read one answers as it is. Refuses a held `needs_owner` event."""
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {', '.join('e.' + column.strip() for column in _COLUMNS.split(','))}, {_HELD} "
                "FROM owner_events e WHERE e.id = %s FOR UPDATE",
                (event_id,),
            ).fetchone()
            if row is None:
                raise OwnerEventNotFound(f"there is no owner event {event_id}")
            event = OwnerEvent(*row)
            if event.read_at is not None:
                return event
            if event.event_class == NEEDS_OWNER and event.held:
                raise ReadRefused(_held_refusal(event))
            row = connection.execute(
                f"UPDATE owner_events SET read_at = now() WHERE id = %s RETURNING {_COLUMNS}",
                (event_id,),
            ).fetchone()
        return OwnerEvent(*row, held=event.held)

    def mark_all_read(self) -> int:
        """Mark every unread notice read; a `needs_owner` event is never touched here. How many were."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE owner_events SET read_at = now() WHERE read_at IS NULL AND \"class\" = 'notice'"
            )
            return cursor.rowcount

    def settle_subject(self, subject_ref: str) -> int:
        """The stay-unread rule's end: every unread `needs_owner` event of this card is read now."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE owner_events SET read_at = now() "
                "WHERE subject_ref = %s AND \"class\" = 'needs_owner' AND read_at IS NULL",
                (subject_ref,),
            )
            return cursor.rowcount


def _held_refusal(event: OwnerEvent) -> str:
    return (
        f"owner event {event.id} needs the owner and stays unread until {event.subject_ref} leaves "
        "waiting_owner: the PO completes the card or the mark is cleared"
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# --- the writer every producer calls ------------------------------------------------------


def _sink(to: Any) -> Any:
    """Where `to` writes: an event store as given, a board client's, or an installation's; None for nowhere.

    A card client names its own sink when it has an `owner_events` attribute (a test's fake does);
    a PostgreSQL client (`credentials`) writes to its own store, joining its open transaction. A path
    is an instance directory: its board store, or nowhere when it has none configured.
    """
    if to is None:
        return None
    if hasattr(to, "insert") and hasattr(to, "settle_subject"):
        return to
    own = getattr(to, "owner_events", None)
    if own is not None:
        return own
    credentials = getattr(to, "credentials", None)
    if credentials is not None and hasattr(credentials, "conninfo"):
        return OwnerEventStore(credentials, client=to if hasattr(to, "transaction") else None)
    if isinstance(to, (str, Path)):
        from secretary.board.store import store_path

        if not store_path(to).exists():
            return None
        return OwnerEventStore.for_instance(to)
    return None


def record(kind: str, subject_ref: str | None, text: str, dedup_key: str, *, to: Any) -> bool:
    """Write one owner event, at most once per `dedup_key`; True when this call wrote it.

    The one writer every producer calls. It never raises: an unknown kind is logged as the defect it
    is, and a store that refuses or does not answer is logged and skipped. `to` is where to write
    (see :func:`_sink`); None, or an installation with no board store, writes nowhere.
    """
    try:
        class_of(kind)
        if not str(dedup_key or "").strip():
            raise ValueError("an owner event needs a dedup key")
        sink = _sink(to)
        if sink is None:
            logger.info("owner event %s (%s) not recorded: no board store to record it in", kind, dedup_key)
            return False
        return bool(sink.insert(kind, subject_ref or None, _bounded(text), dedup_key))
    except Exception as exc:  # noqa: BLE001 - the bell never fails the fact it reports
        logger.warning("owner event %s (%s) not recorded: %s: %s", kind, dedup_key, type(exc).__name__, exc)
        return False


def settle(subject_ref: str, *, to: Any) -> int:
    """Mark read every unread `needs_owner` event of a card whose mark just cleared; never raises."""
    try:
        sink = _sink(to)
        if sink is None:
            return 0
        return int(sink.settle_subject(subject_ref) or 0)
    except Exception as exc:  # noqa: BLE001 - the card's transition never fails on the bell
        logger.warning("owner events of %s not settled: %s: %s", subject_ref, type(exc).__name__, exc)
        return 0


def _bounded(text: str) -> str:
    text = str(text or "").strip() or "(no text)"
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


__all__ = [
    "BUDGET_SIGNAL",
    "CARD_HANDED_TO_OWNER",
    "CLASSES",
    "HEAD_DEAD",
    "KINDS",
    "KIND_CLASS",
    "NEEDS_OWNER",
    "NEEDS_OWNER_KINDS",
    "NOTICE",
    "OBSERVER_DEAD",
    "PO_TURN_FAILED",
    "PROVIDER_RED",
    "SPRINT_CLOSED",
    "SPRINT_STOPPED",
    "STEWARD_NEEDS_HUMAN",
    "OwnerEvent",
    "OwnerEventError",
    "OwnerEventNotFound",
    "OwnerEventStore",
    "OwnerEventsUnavailable",
    "ReadRefused",
    "card_holds_mark",
    "class_of",
    "list_order_key",
    "needs_human_section",
    "po_session_subject",
    "record",
    "settle",
]
