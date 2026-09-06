"""The transport-independent read layer: what a dashboard, a bot or a CLI all read the same way.

Sprint 1425 wants an operator dashboard, and after it a Telegram head asking the same questions.
Both need the same three answers -- what is the system doing, what is this card doing, what has
happened to this card since I last looked -- and the one thing that must not happen is each
transport growing its own version of them. Two answers to "is the worker alive" is not a UI
inconsistency; it is two different beliefs about production, and the operator has to debug which
one is lying before they can act.

So the answers live here, once, and know nothing about who is asking:

* :func:`~secretary.webproto.reads.ReadLayer.system_snapshot` -- installation health, the
  registered projects, the cards in flight, and the agents running right now with the card and
  project each belongs to;
* :func:`~secretary.webproto.reads.ReadLayer.task_snapshot` -- one card: its state, its project,
  its recent history, the heads working it, and the report, verdict, decision and result it has;
* :func:`~secretary.webproto.reads.ReadLayer.task_events` -- a page of that card's history and an
  opaque cursor to continue from, so a client can keep reading without re-reading.

Four properties are the point of the module, and each has a test:

**It is read-only.** Nothing here writes to the board, the dispatcher state, the journal or the
installation. There is no mutation operation, and no operation takes an actor.

**It does not know about transports.** No HTTP, no sockets, no framework, no rendering, no
templates -- not even indirectly through an import. Failures are typed exceptions
(:mod:`secretary.webproto.errors`) and availability fields (:mod:`secretary.webproto.sources`),
never status codes, so the transport is what decides that "not_found" is a 404 or a "no such
card" message.

**Sources fail apart.** Each section of each snapshot carries its own availability record with a
reason and the age of what is being shown instead. A dead Kanboard blanks the card list, not the
page.

**Liveness is process state.** A pane, terminal or window is not evidence that an agent is
running, and :mod:`secretary.webproto.agents` reads none of them.

`secretary web-read` (:mod:`secretary.webproto.commands`) is the operator's way to call all three
without a web transport existing at all. It is a caller of this layer, not a part of it.
"""

from __future__ import annotations

from secretary.webproto.agents import AGENT_STATES, LIVENESS_INVARIANT
from secretary.webproto.cursor import Cursor
from secretary.webproto.errors import (
    InstallationUnavailable,
    InvalidCursor,
    ReadError,
    TaskNotFound,
)
from secretary.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from secretary.webproto.reads import SCHEMA_VERSION, ReadLayer

__all__ = [
    "AGENT_STATES",
    "DEFAULT_LIMIT",
    "LIVENESS_INVARIANT",
    "MAX_LIMIT",
    "SCHEMA_VERSION",
    "Cursor",
    "InstallationUnavailable",
    "InvalidCursor",
    "ReadError",
    "ReadLayer",
    "TaskNotFound",
]
