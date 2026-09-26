"""The owner's bell: owner events read from the board, and marked read (secretary-1770).

Every answer comes from the board store's `owner_events` table through
:class:`secretary.board.owner_events.OwnerEventStore`, read at the moment it is asked: this layer holds
nothing between two requests, so the count on the bell and the list on the page are the board's.

A board without the table (migration `0018` not applied yet) or a store that does not answer reads as
no events, with the source state `unavailable` and the reason: the bell says it cannot count, and no
page fails over it. The two writes refuse instead, since writing to a table that is not there is not
something a page can pretend to have done.

The rules are the store's (`board.owner_events`): a click marks one event read, and refuses a
`needs_owner` event whose card still carries `waiting_owner`; "mark all read" takes notices only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.board.owner_events import (
    OwnerEventError,
    OwnerEventNotFound,
    OwnerEventStore,
    OwnerEventsUnavailable,
    ReadRefused,
)
from secretary.webproto import sources
from secretary.webproto.errors import OwnerConflict, OwnerEventMissing, RuntimeUnavailable

SCHEMA_VERSION = 1
AVAILABLE = "available"
UNAVAILABLE = "unavailable"


class OwnerEventLayer:
    """One installation's owner events. Construction does no I/O; `store` is the seam a test supplies."""

    def __init__(
        self,
        instance: str | Path,
        *,
        store: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._store = store
        self._clock = clock

    def _events(self) -> Any:
        if self._store is not None:
            return self._store
        instance = self.instance.parent if self.instance.is_file() else self.instance
        try:
            return OwnerEventStore.for_instance(instance)
        except Exception as exc:  # noqa: BLE001 - an unusable store configuration is an unavailable source
            raise OwnerEventsUnavailable(f"the board store is not usable: {exc}") from None

    # -- reads -----------------------------------------------------------------------------

    def owner_event_list(self, *, unread_only: bool = False) -> dict[str, Any]:
        """Every event, open `needs_owner` ones first then newest first; `unread_only` keeps the unread."""
        now = self._clock()
        try:
            store = self._events()
            events = store.events(unread_only=unread_only)
            unread = store.unread_count()
        except OwnerEventError as exc:
            return self._document(now, state=UNAVAILABLE, reason=str(exc), events=[], unread=0, unread_only=unread_only)
        return self._document(
            now,
            state=AVAILABLE,
            reason=None,
            events=[event.to_json() for event in events],
            unread=unread,
            unread_only=unread_only,
        )

    def unread_count(self) -> dict[str, Any]:
        """The bell: how many events are unread, or why that cannot be said."""
        try:
            return {"state": AVAILABLE, "reason": None, "count": int(self._events().unread_count())}
        except OwnerEventError as exc:
            return {"state": UNAVAILABLE, "reason": str(exc), "count": 0}

    # -- writes ----------------------------------------------------------------------------

    def mark_read(self, event_id: int | str) -> dict[str, Any]:
        """One click: this event read, unless it needs the owner and its card still waits for them."""
        try:
            identifier = int(str(event_id))
        except ValueError:
            raise OwnerEventMissing(f"there is no owner event {event_id!r}") from None
        try:
            event = self._events().mark_read(identifier)
        except OwnerEventNotFound as exc:
            raise OwnerEventMissing(str(exc)) from None
        except ReadRefused as exc:
            raise OwnerConflict(str(exc)) from None
        except OwnerEventError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        return {"schema_version": SCHEMA_VERSION, "kind": "owner_event_read", "event": event.to_json()}

    def mark_all_read(self) -> dict[str, Any]:
        """Every unread notice read; a `needs_owner` event is never touched by this."""
        try:
            marked = int(self._events().mark_all_read())
        except OwnerEventError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        return {"schema_version": SCHEMA_VERSION, "kind": "owner_events_read", "marked": marked}

    @staticmethod
    def _document(
        now: float,
        *,
        state: str,
        reason: str | None,
        events: list[dict[str, Any]],
        unread: int,
        unread_only: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "owner_events",
            "observed_at": sources.isoformat(now),
            "source": {"state": state, "reason": reason},
            "unread_only": unread_only,
            "unread": unread,
            "events": events,
        }


__all__ = ["OwnerEventLayer"]
