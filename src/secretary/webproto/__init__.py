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

**It does not know about transports.** No HTTP, no sockets, no framework, no rendering and no
templates in this layer's own surface: nothing here answers a caller in the language of a
transport. That is deliberately a promise about the surface and not about the import graph, which
could not carry it -- the layer reaches the board through `KanboardClient`, a Kanboard is an HTTP
service, and `secretary.tasks` has therefore imported `urllib` under `reads`, `admission`, `ops`
and `run_events` since this package existed. Failures are typed exceptions
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

**And the sprint half, added by secretary-1569, is the same layer over the other entity.** A run is
one head on one card; a sprint is the thing that decides which cards there are, and an operator who
can only start runs cannot open one. So beside the reads and the run operations there is one
operation and three reads over sprints, with the same properties and no others:

* :meth:`~secretary.webproto.sprint_ops.SprintOperationLayer.sprint_create` -- open one sprint,
  with the product, goal, definition of done, issues, projects and observer it is opened with, and
  optionally the worker and reviewer profiles its cards run on;
* :meth:`~secretary.webproto.sprint_reads.SprintReadLayer.sprint_options` -- what a sprint of this
  installation can be built from: its products, the issues those products still have open, its
  registered projects and its installed head profiles with the model and effort each names;
* :meth:`~secretary.webproto.sprint_reads.SprintReadLayer.sprint_state` -- one sprint as a page
  watches it, including whether its observer is really up and what the sprint is doing;
* :meth:`~secretary.webproto.sprint_reads.SprintReadLayer.sprint_list` -- every sprint of the
  installation with what each one is doing, in the same sections, from the same one read.

Three properties are the point of that half, and each has a test:

**Every rule stays with the writer that owns it.** `SprintWriter.create` decides what a sprint may
be -- product, open issue of that product, registered projects, reservations, observer, executor
pins -- and the operation calls it. There is no second admission gate, no second audit and no
second reservation index here.

**Opening a sprint with an observer *is* starting it.** There is no launch operation, because there
is no launch action: the production tick raises one head per open sprint that lacks one. A
scheduler of this layer's own would be a second thing racing it for the same head, so what the
operation returns instead is the launch state read off the dispatcher's own production state.

**An absent executor pin stays absent.** `None` is the caller saying nothing about a role, and it
reaches the entity as a field that was never written -- never as an empty string and never as a
default nobody chose.

**And the pause half, added by secretary-1576, is the same layer over the switch that stops all of
it.** A run is one head, a sprint is what decides which cards there are, and the pause is the one
flag that stops the pipeline claiming any of them. So beside the rest there are two operations and
two reads over the pause:

* :meth:`~secretary.webproto.pause_ops.PauseOperationLayer.pause_drain` -- set the pipeline-wide
  soft pause;
* :meth:`~secretary.webproto.pause_ops.PauseOperationLayer.pause_resume` -- lift whatever pause is
  set, and report what was actually put back;
* :meth:`~secretary.webproto.pause_reads.PauseReadLayer.pause_state` -- whether the pipeline is
  paused, in what mode, since when, and what is behind its cards;
* :meth:`~secretary.webproto.pause_reads.PauseReadLayer.pause_scope` -- what a pause command would
  reach, answered *before* it is issued: the flag it acts on, the open sprints and the cards inside
  the pipeline-wide scope, and the heads that are running.

Four properties are the point of that half, and each has a test:

**The pause is pipeline-wide, and every document says so.** There is no per-sprint pause and no
document of this layer that could be read as one: the extent is stated on every answer, including
one where every source refused.

**A drain stops no running head.** No field, name or sentence of these documents says otherwise, and
the heads a drain leaves alone are listed as running.

**A freeze is never reached implicitly.** There is no freeze operation, `pause_drain` takes no mode,
and the existing refusal to change mode while paused is preserved and surfaces as `owner_conflict`.

**The reads write nothing.** No flag, no lock, no head, no wake -- pinned by a snapshot of the data
plane taken around the call.

`secretary web-read` and `secretary web-run` (:mod:`secretary.webproto.commands`) are the operator's
way to call the first six without a web transport existing at all. They are callers of this layer,
not a part of it, and so are `secretary pause`, `secretary resume`, `secretary pause-status` and
`secretary pause-scope`.
"""

from __future__ import annotations

from secretary.webproto.agents import AGENT_STATES, LIVENESS_INVARIANT
from secretary.webproto.boundary import IMPLEMENTATION_FAILURES, ProtocolBoundary
from secretary.webproto.cursor import Cursor
from secretary.webproto.errors import (
    InstallationUnavailable,
    InvalidCursor,
    OperationPending,
    OwnerConflict,
    ReadError,
    RunNotFound,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from secretary.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from secretary.webproto.ops import OperationLayer
from secretary.webproto.pause_ops import PauseOperationLayer
from secretary.webproto.pause_reads import PauseReadLayer
from secretary.webproto.reads import SCHEMA_VERSION, ReadLayer
from secretary.webproto.runs import ProductRun, RunStore
from secretary.webproto.sprint_ops import SprintOperationLayer
from secretary.webproto.sprint_reads import SprintReadLayer
from secretary.webproto.sprint_requests import SprintRequestStore

__all__ = [
    "AGENT_STATES",
    "DEFAULT_LIMIT",
    "IMPLEMENTATION_FAILURES",
    "LIVENESS_INVARIANT",
    "MAX_LIMIT",
    "SCHEMA_VERSION",
    "Cursor",
    "InstallationUnavailable",
    "InvalidCursor",
    "OperationLayer",
    "OperationPending",
    "OwnerConflict",
    "PauseOperationLayer",
    "PauseReadLayer",
    "ProductRun",
    "ProtocolBoundary",
    "ReadError",
    "ReadLayer",
    "RunNotFound",
    "RunStore",
    "RuntimeUnavailable",
    "SprintOperationLayer",
    "SprintReadLayer",
    "SprintRequestStore",
    "TaskNotFound",
    "ValidationRefused",
]
