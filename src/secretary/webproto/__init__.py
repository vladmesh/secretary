"""The transport-independent layer: what a dashboard, a bot or a CLI all read and drive the same way.

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

Four properties are the point of that half, and each has a test:

**It is read-only.** Nothing in `reads`, `journal`, `agents` or `cursor` writes to the board, the
dispatcher state, the journal or the installation. None of the three reads mutates anything, and
none of them takes an actor.

**It does not know about transports.** No HTTP, no sockets, no framework, no rendering, no
templates -- not even indirectly through an import. Failures are typed exceptions
(:mod:`secretary.webproto.errors`) and availability fields (:mod:`secretary.webproto.sources`),
never status codes, so the transport is what decides that "not_found" is a 404 or a "no such
card" message. That is a promise about *every* operation, and it is kept in one place rather than
one call site at a time: :mod:`secretary.webproto.boundary` wraps every public method of both
layers, so an implementation failure -- the run store's, the filesystem's, a document that does not
parse -- becomes `backend_unavailable` on its way out whether or not the operation remembered.

**Sources fail apart.** Each section of each snapshot carries its own availability record with a
reason and the age of what is being shown instead. A dead Kanboard blanks the card list, not the
page.

**Liveness is process state.** A pane, terminal or window is not evidence that an agent is
running, and :mod:`secretary.webproto.agents` reads none of them.

**And the operations, added by secretary-1562, are the other half of the same layer.** A dashboard
that can only watch is a dashboard nobody opens twice, so beside the three reads there are three
operations (:mod:`secretary.webproto.ops`) with the same two properties and no others: they are
transport-independent, and their failures are typed codes rather than status numbers.

* :meth:`~secretary.webproto.ops.OperationLayer.run_start` -- raise a real worker head for one card,
  in a workspace this product cut and under a supervisor this product owns;
* :meth:`~secretary.webproto.ops.OperationLayer.run_review` -- raise a real reviewer head by that
  worker run's result;
* :meth:`~secretary.webproto.ops.OperationLayer.run_state` -- read one run, and settle its ending.

Four more properties are the point of that half, and each has a test:

**Secretary owns the run.** The workspace, the process, the pid, the logs and the result belong to
the product: nothing on the start path or the result-reading path speaks to Orca -- not its CLI,
not its RPC, not a terminal and not its repository inventory.

**A request id owns a run.** Repeating a start, or reconnecting, returns the same run; it never
raises a second head or cuts a second workspace.

**One owner of a card.** :mod:`secretary.webproto.admission` is the single gate every start goes
through, and it is built from rules that already exist -- an open sprint's project reservations and
the card's own state -- rather than from a scheduler of its own.

**A run's outcome is visible where the card's history already is.** The run's two events go onto the
board's own journal, so `task_events` and `task_snapshot` show them with no second history and no
second outcome store.

`secretary web-read` and `secretary web-run` (:mod:`secretary.webproto.commands`) are the operator's
way to call all six without a web transport existing at all. They are callers of this layer, not a
part of it.
"""

from __future__ import annotations

from secretary.webproto.agents import AGENT_STATES, LIVENESS_INVARIANT
from secretary.webproto.boundary import IMPLEMENTATION_FAILURES, ProtocolBoundary
from secretary.webproto.cursor import Cursor
from secretary.webproto.errors import (
    InstallationUnavailable,
    InvalidCursor,
    OwnerConflict,
    ReadError,
    RunNotFound,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from secretary.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from secretary.webproto.ops import OperationLayer
from secretary.webproto.reads import SCHEMA_VERSION, ReadLayer
from secretary.webproto.runs import ProductRun, RunStore

__all__ = [
    "AGENT_STATES",
    "DEFAULT_LIMIT",
    "LIVENESS_INVARIANT",
    "MAX_LIMIT",
    "SCHEMA_VERSION",
    "IMPLEMENTATION_FAILURES",
    "Cursor",
    "InstallationUnavailable",
    "InvalidCursor",
    "OperationLayer",
    "OwnerConflict",
    "ProductRun",
    "ProtocolBoundary",
    "ReadError",
    "ReadLayer",
    "RunNotFound",
    "RunStore",
    "RuntimeUnavailable",
    "TaskNotFound",
    "ValidationRefused",
]
