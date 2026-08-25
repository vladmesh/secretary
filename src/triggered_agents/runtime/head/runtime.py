"""`HeadRuntime`: the one typed boundary a head's life is lived through.

`operations` holds three free functions and every caller supplies its own `SessionHost`, its own
transport and its own error handling around them. That worked while there was one backend. It stops
working the moment there are two, because "which backend is this head on" would then have to be
decided again at every call site — and because half of what a second backend can do (observe a
head, ask it to wind down, attach to it) has no home among three functions that open, write into and
close a pane.

So the six verbs live on one object:

  * **`start`** brings a head up and, when it is given a pointer, points it at its task;
  * **`deliver`** puts one prompt into a head that is already running;
  * **`observe`** reads what the backend can actually say about the head right now;
  * **`request_drain`** asks the head to take no more work;
  * **`stop`** ends it and records who ended it;
  * **`attach`** hands a caller the live head's own stream.

Three decisions make this a boundary rather than a namespace:

  * **every verb answers with a receipt, never with a bool, a dict or an exception the caller has
    to `isinstance` its way through.** A receipt says which of four things happened — it worked, it
    was refused because the head was busy or draining, it was refused with something still alive, or
    it was refused and nothing of the attempt is left — and it carries the operation's own refusal
    unchanged in `failure`, so `HeadSpawnAborted` never arrives looking like `HeadSpawnFailed`;
  * **a verb a backend cannot honestly perform answers `unsupported`, and says so in the receipt.**
    Not `False`, not an empty dict, not an invented `busy=False`. A caller can tell "this head is
    not busy" from "this backend cannot tell you whether it is busy" only if the two are different
    values;
  * **busyness is not a lifecycle state.** `HeadRun.working` is the durable history of a head having
    been given its task; whether a turn is running *right now* is a `TurnLease` and a monotonic
    activity epoch, both of which live here, in the runtime, next to the backend that can see them.
    A record read back from disk cannot answer a question about the present, and this is where that
    stops being pretended.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from ..tui_delivery import DeliveryOutcome
from .operations import HeadOperationError, NudgePointer
from .run import HeadRun, StopInitiator
from .spec import HeadSpec
from .task_ref import TaskRef

# The verb did what it says it does.
HEAD_OK = "ok"
# Refused because the head was busy — mid-turn, or a pane held in a dialog. Nothing of this attempt
# is left behind and the attempt is worth making again.
HEAD_BUSY = "busy"
# Refused because a drain was requested for this head: this runtime hands it no more work.
HEAD_DRAINING = "draining"
# Refused with something still alive — a pane that would not close, a stop that could not be
# confirmed, a head whose process this refusal says nothing about. The caller still owns it.
HEAD_ALIVE = "alive"
# Refused, and nothing of what the verb touched survived it.
HEAD_GONE = "gone"
# This backend cannot honestly perform or answer this verb. Never a disguised `no`.
HEAD_UNSUPPORTED = "unsupported"
RECEIPT_STATUSES = (HEAD_OK, HEAD_BUSY, HEAD_DRAINING, HEAD_ALIVE, HEAD_GONE, HEAD_UNSUPPORTED)

# Why an observation says what it says. A reason is a token, not a sentence, because callers route
# on it: the dispatcher's observer status turns each of these into the failure its callers already
# read, and a free-text reason would make that a substring match.
OBSERVE_NO_ADDRESS = "no_address"
OBSERVE_INVENTORY_UNREADABLE = "inventory_unreadable"
OBSERVE_PANE_ABSENT = "pane_absent"
OBSERVE_PANE_DISCONNECTED = "pane_disconnected"
OBSERVE_READINESS_UNKNOWN = "readiness_unknown"


class TurnLeaseError(RuntimeError):
    """A lease that would say something untrue about which turn a head is running."""


@dataclass(frozen=True)
class TurnLease:
    """One turn a head was handed, for as long as that turn is running.

    Deliberately not a field of `HeadRun`: a run is written to disk and read back a tick later by
    another process, and a value that outlives the turn it describes cannot be the answer to "is a
    turn running now". The lease lives in the runtime, is granted when a prompt is delivered, and is
    released when the backend sees that turn end.

    `granted_at_epoch` is the activity epoch at the moment of the grant, so a caller holding an old
    lease can tell that the head has been doing things since.
    """

    lease_id: str
    run_id: str
    subject: str = ""
    granted_at_epoch: int = 0

    def __post_init__(self) -> None:
        if not self.lease_id or not self.run_id:
            raise TurnLeaseError("a turn lease names its head and itself")


class HeadActivity:
    """The monotonic activity epoch of the heads one runtime owns, and their outstanding leases.

    Two values, not one, because they answer different questions. The epoch only ever goes up, and
    goes up whenever the backend sees a head *do* something — a pane opened, a prompt taken, output
    printed. It is how a caller tells "nothing has happened since I last looked" from "I cannot
    tell". The lease says a turn is running, and there is at most one per head.

    Neither is durable. A runtime that has just been constructed knows nothing, and says so by
    holding no lease and an epoch of zero rather than by guessing.
    """

    def __init__(self) -> None:
        self._epoch = 0
        self._leases: dict[str, TurnLease] = {}
        self._output_marks: dict[str, float] = {}

    @property
    def epoch(self) -> int:
        """The activity epoch as it now stands, across every head this runtime owns."""
        return self._epoch

    def observed(self, run_id: str = "", *, output_at: float = 0.0) -> int:
        """Record that a head was seen doing something, and return the epoch that follows.

        `output_at` is the pane's own output clock when a caller has one. Passing the same clock
        twice is not new activity and does not move the epoch: an inventory read that keeps
        returning the same timestamp says the head has been quiet, which is exactly the fact a
        caller watching for progress needs to keep.
        """
        if output_at and run_id:
            if self._output_marks.get(run_id, 0.0) >= output_at:
                return self._epoch
            self._output_marks[run_id] = output_at
        self._epoch += 1
        return self._epoch

    def lease(self, run_id: str) -> TurnLease | None:
        """The turn this head is running, when it is running one this runtime granted."""
        return self._leases.get(run_id)

    def busy(self, run_id: str) -> bool:
        """Whether this runtime has an outstanding turn for this head."""
        return run_id in self._leases

    def grant(self, run_id: str, subject: str = "") -> TurnLease:
        """Hand this head a turn. Refuses while one is outstanding — a head runs one turn."""
        if not run_id:
            raise TurnLeaseError("a turn lease names the head it was granted to")
        held = self._leases.get(run_id)
        if held is not None:
            raise TurnLeaseError(f"the head is already running turn {held.lease_id}")
        lease = TurnLease(
            lease_id=uuid.uuid4().hex,
            run_id=run_id,
            subject=subject,
            granted_at_epoch=self._epoch,
        )
        self._leases[run_id] = lease
        return lease

    def renew(self, run_id: str, subject: str = "") -> TurnLease:
        """Grant a turn, releasing whatever this head was holding first.

        A delivery that this runtime does not itself serialise still starts a turn, and the newest
        one is the one running. Keeping the older lease would leave the runtime claiming a turn that
        the delivery it just performed has superseded.
        """
        self.release(run_id)
        return self.grant(run_id, subject)

    def release(self, run_id: str) -> TurnLease | None:
        """Close this head's turn, and hand back the lease that was closed, if there was one."""
        return self._leases.pop(run_id, None)

    def forget(self, run_id: str) -> None:
        """Drop everything this runtime remembers about a head that has ended."""
        self._leases.pop(run_id, None)
        self._output_marks.pop(run_id, None)


@dataclass(frozen=True)
class HeadReceipt:
    """What one verb did, in terms a caller can route on without catching anything.

    `failure` carries the operation's own refusal object unchanged, so a caller that needs the
    distinction the operation drew — an aborted bring-up is not a failed one — reads it from the
    same type it always did rather than from a re-derived string.
    """

    status: str
    run: HeadRun | None = None
    reason: str = ""
    failure: HeadOperationError | None = None
    evidence: Any = None
    epoch: int = 0
    lease: TurnLease | None = None

    def __post_init__(self) -> None:
        if self.status not in RECEIPT_STATUSES:
            raise ValueError(
                f"a head receipt's status is one of {', '.join(RECEIPT_STATUSES)}, "
                f"not {self.status!r}"
            )

    @property
    def ok(self) -> bool:
        """Whether the verb did what it says it does."""
        return self.status == HEAD_OK

    @property
    def deferred(self) -> bool:
        """Whether this refusal is one to make again rather than one to recover from."""
        return self.status in (HEAD_BUSY, HEAD_DRAINING)

    @property
    def left_alive(self) -> bool:
        """Whether this refusal left something the caller still has to account for."""
        return self.status == HEAD_ALIVE

    @property
    def unsupported(self) -> bool:
        """Whether this backend simply cannot do or answer this."""
        return self.status == HEAD_UNSUPPORTED


@dataclass(frozen=True)
class StartReceipt(HeadReceipt):
    """A bring-up, and the delivery it made when it was given a pointer to deliver."""

    delivery: DeliveryOutcome | None = None
    fallback_reason: str = ""


@dataclass(frozen=True)
class DeliverReceipt(HeadReceipt):
    """One prompt put in front of a running head, and what the delivery boundary saw."""

    delivery: DeliveryOutcome | None = None


@dataclass(frozen=True)
class ObserveReceipt(HeadReceipt):
    """What the backend can say about this head right now — and nothing it cannot.

    Every field that a backend may be unable to answer is `None` rather than a default, because a
    caller must be able to tell "not busy" from "not knowable". `busy` in particular is read from
    the turn lease and the pane's own readiness, never from `HeadRun.working`: a lifecycle state
    read back from disk is history, not a statement about this second.
    """

    handle: str = ""
    leaf: str = ""
    connected: bool | None = None
    readiness: str = ""
    last_output_at: float = 0.0
    busy: bool | None = None


@dataclass(frozen=True)
class DrainReceipt(HeadReceipt):
    """A request that a head take no more work.

    Two separate facts, because a backend can honestly own one without the other: `draining` is
    whether this runtime will hand the head further work, and `head_signalled` is whether the head
    itself was told to wind down. A backend that can only do the first says so here rather than
    reporting a drain it did not perform.
    """

    draining: bool = False
    head_signalled: bool = False


@dataclass(frozen=True)
class StopReceipt(HeadReceipt):
    """A head ended, or a stop that could not be confirmed and what it left behind."""


@dataclass(frozen=True)
class AttachReceipt(HeadReceipt):
    """A caller joined to a live head's stream, or the reason this backend cannot join it.

    `handle` and `leaf` are how the head is addressed in whatever the backend calls its session, for
    a caller that can do something with an address even when it cannot be handed a stream.
    """

    handle: str = ""
    leaf: str = ""


class HeadRuntime(Protocol):
    """One head backend, as everything above it is allowed to see one.

    Six verbs, each answering with its own receipt. An implementation may take backend-specific
    keyword options — `orca-legacy` takes the transport and the commit hooks the operations have
    always taken — but nothing above this boundary may reach past it to a pane, a session manager or
    a pty to perform any part of a head's lifecycle.
    """

    def start(
        self,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        *,
        command: str,
        title: str,
        pointer: NudgePointer | None = None,
        **options: Any,
    ) -> StartReceipt:
        """Bring one head up, and point it at its task when a pointer is given."""

    def deliver(
        self, run: HeadRun, pointer: NudgePointer, *, subject: str = "", **options: Any
    ) -> DeliverReceipt:
        """Put one prompt in front of a head that is already running."""

    def observe(self, run: HeadRun) -> ObserveReceipt:
        """Read what this backend can actually say about the head as it is now."""

    def request_drain(self, run: HeadRun, initiator: StopInitiator) -> DrainReceipt:
        """Ask this head to take no more work, and say how much of that was really done."""

    def stop(self, run: HeadRun, initiator: StopInitiator, **options: Any) -> StopReceipt:
        """End this head, recording who ended it."""

    def attach(self, run: HeadRun) -> AttachReceipt:
        """Join a caller to this head's live stream, or say that this backend has none."""
