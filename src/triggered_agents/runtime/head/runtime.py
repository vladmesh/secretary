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
    """What one runtime knows about each head it owns: its epoch, its turn, and its admission.

    Three facts per head, in one place, because the runtime that owns them takes one lock around all
    three and a decision made of two of them must not be able to see them at different moments.

      * the **activity epoch** only ever goes up, and goes up whenever the backend sees *that head*
        do something — its pane opened, it took a prompt, it printed. It is how a caller tells
        "nothing has happened since I last looked" from "I cannot tell", and it is per head: a
        counter shared with every other head would make "this head has been quiet" false the moment
        any other head did anything;
      * the **turn lease** says a turn is running, and there is at most one per head;
      * **admission** is whether this runtime will hand this head further work. A drain closes it;
        it is not the turn, and closing it never touches the turn that is running.

    `ticks` is the runtime-wide count of everything this runtime has seen any of its heads do. It is
    kept because it is genuinely useful for diagnostics — how much has happened at all — and it is
    named separately from `epoch` so that no quiescence decision can be made on it by accident.

    Nothing here is durable, and nothing here locks: a runtime that has just been constructed knows
    nothing and says so by holding no lease, an epoch of zero and an open admission. Serialising
    access is the job of the runtime that owns this object.

    **A runtime whose backend has a durable witness puts what it knows back here rather than
    keeping a second copy of it** (secretary-1479). Knowing nothing is the right *initial* state
    and the wrong *final* one for a control plane whose every tick is a new process: `advance_to`
    and `adopt` are how a backend that can still ask the head — a supervisor on a socket, a
    journal on disk — restores the epoch and the turn that its own predecessor granted. They only
    ever move this object in the direction the head itself already went, which is why neither of
    them is a setter: an epoch cannot be lowered, and a turn cannot be adopted over one that is
    already held.
    """

    def __init__(self) -> None:
        self._ticks = 0
        self._epochs: dict[str, int] = {}
        self._leases: dict[str, TurnLease] = {}
        self._output_marks: dict[str, float] = {}
        self._closed: set[str] = set()

    @property
    def ticks(self) -> int:
        """Everything this runtime has seen any of its heads do, counted once.

        Deliberately not what a stop-if-quiescent compares: another head's activity moves this, and
        a check that used it would read a quiet head as a busy one.
        """
        return self._ticks

    def epoch(self, run_id: str) -> int:
        """This head's activity epoch, and zero for a head this runtime has never seen."""
        return self._epochs.get(run_id, 0)

    def acted(self, run_id: str) -> int:
        """Record that this head was made to do something, and return the epoch that follows.

        The runtime calls this for the things it performs itself — a pane opened, a prompt taken, a
        head ended. What a *read* of a pane says goes through `observed`, which is allowed to say
        nothing happened.
        """
        if not run_id:
            return 0
        self._ticks += 1
        epoch = self._epochs.get(run_id, 0) + 1
        self._epochs[run_id] = epoch
        return epoch

    def observed(self, run_id: str, *, output_at: float = 0.0) -> int:
        """Record what a pane read said about this head, and return the epoch that follows.

        `output_at` is the pane's own output clock. Two things are *not* activity here, and both
        would destroy the one property the epoch exists for:

          * the same clock twice — an inventory that keeps returning the same timestamp says the
            head has been quiet, which is exactly the fact a caller watching for progress needs;
          * a pane with no output clock of its own — "I looked and the pane cannot tell me when it
            last printed" is not "the head printed something", and moving the epoch on it makes
            "silent" indistinguishable from "unobserved".
        """
        if not run_id or not output_at:
            return self.epoch(run_id)
        if self._output_marks.get(run_id, 0.0) >= output_at:
            return self.epoch(run_id)
        self._output_marks[run_id] = output_at
        return self.acted(run_id)

    def advance_to(self, run_id: str, epoch: int) -> int:
        """Raise this head's epoch to what a durable witness says it is, and never lower it.

        The epoch is monotone per head, and monotone has to mean *across the process boundary*
        too: the runtime that reads it in one tick is not the object that moved it in the last
        one. A witness that could move this number down would make "nothing has happened since I
        last looked" true of a head that had been working, which is the one thing the epoch
        exists to make impossible; so a smaller number, a zero and an unknown are all no-ops, and
        the answer is always the epoch this head ends up with.
        """
        if not run_id or epoch <= 0:
            return self.epoch(run_id)
        current = self._epochs.get(run_id, 0)
        if epoch <= current:
            return current
        self._epochs[run_id] = epoch
        return epoch

    def adopt(self, run_id: str, lease: TurnLease) -> TurnLease:
        """Take over a turn this runtime did not grant, and hand back the turn that is now held.

        The counterpart of `grant` for a turn that was handed out by a process that has since
        gone: it is not a second grant, so it does not raise over an outstanding lease — the
        outstanding one *is* the answer, and a caller rehydrating a head it already knows about
        must not be told that its own knowledge is a conflict.
        """
        if not run_id or lease.run_id != run_id:
            raise TurnLeaseError("an adopted lease names the head it was adopted for")
        held = self._leases.get(run_id)
        if held is not None:
            return held
        self._leases[run_id] = lease
        return lease

    def lease(self, run_id: str) -> TurnLease | None:
        """The turn this head is running, when it is running one this runtime granted."""
        return self._leases.get(run_id)

    def busy(self, run_id: str) -> bool:
        """Whether this runtime has an outstanding turn for this head."""
        return run_id in self._leases

    def grant(self, run_id: str, subject: str = "") -> TurnLease:
        """Hand this head a turn. Refuses while one is outstanding — a head runs one turn.

        There is deliberately no `renew`. A grant over an outstanding lease used to release it
        first, which made the newest delivery the running turn and quietly evicted the one it
        interrupted; the caller that wanted that has to be refused instead, so this raises and the
        runtime turns the refusal into a receipt.
        """
        if not run_id:
            raise TurnLeaseError("a turn lease names the head it was granted to")
        held = self._leases.get(run_id)
        if held is not None:
            raise TurnLeaseError(f"the head is already running turn {held.lease_id}")
        lease = TurnLease(
            lease_id=uuid.uuid4().hex,
            run_id=run_id,
            subject=subject,
            granted_at_epoch=self.epoch(run_id),
        )
        self._leases[run_id] = lease
        return lease

    def release(self, run_id: str) -> TurnLease | None:
        """Close this head's turn, and hand back the lease that was closed, if there was one."""
        return self._leases.pop(run_id, None)

    def admits(self, run_id: str) -> bool:
        """Whether this runtime will still hand this head work."""
        return run_id not in self._closed

    def close_admission(self, run_id: str) -> None:
        """Take this head out of service. Says nothing about the turn it is running."""
        if run_id:
            self._closed.add(run_id)

    def open_admission(self, run_id: str) -> None:
        """Put this head back in service — the undo a refused stop owes its admission."""
        self._closed.discard(run_id)

    def rotatable(self, run_id: str) -> bool:
        """Whether this head is done: it takes no more work and the last turn it held has closed."""
        return not self.admits(run_id) and not self.busy(run_id)

    def forget(self, run_id: str) -> None:
        """Drop everything this runtime remembers about a head that has ended."""
        self._leases.pop(run_id, None)
        self._output_marks.pop(run_id, None)
        self._epochs.pop(run_id, None)
        self._closed.discard(run_id)


@dataclass(frozen=True)
class HeadReceipt:
    """What one verb did, in terms a caller can route on without catching anything.

    `failure` carries the operation's own refusal object unchanged, so a caller that needs the
    distinction the operation drew — an aborted bring-up is not a failed one — reads it from the
    same type it always did rather than from a re-derived string.

    `epoch` is *this head's* activity epoch, not the runtime's, so it is the value to hand back to a
    stop that must only happen while the head has stayed quiet. `rotation_ready` is the runtime
    saying that this head is done — its admission is closed and the last turn it held has ended, so
    it can be replaced. It is a value rather than something a caller derives from a drain it
    remembers requesting and a `lease` field that is `None`, because those two are read at different
    moments and the conjunction of them is not a fact anybody observed.
    """

    status: str
    run: HeadRun | None = None
    reason: str = ""
    failure: HeadOperationError | None = None
    evidence: Any = None
    epoch: int = 0
    lease: TurnLease | None = None
    rotation_ready: bool = False

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
    """One prompt put in front of a running head, and what the delivery boundary saw.

    `delivery_state` is the distinction a backend whose transport *admits* a payload before the
    payload lands owes its callers, and it is on the boundary rather than on that backend because a
    consumer must not have to know which backend it is talking to before it can tell "the head has
    it" from "the head has part of it". Its values are the substrate's own words for what a
    delivery ended as — `complete`, `stalled`, `failed` — plus `unknown`, which is not a state a
    delivery is in but the backend saying it could not establish which of them this one reached; a
    consumer must not read it as any of them, and least of all as "nothing landed". A delivery
    still in flight is deliberately not among them: a backend reports what a delivery *did*, and a
    backend that could return before its substrate had finished writing is one whose `ok` a
    consumer would have to second-guess. It is empty for a backend whose delivery is finished by
    the time the verb returns, which is what `HEAD_OK` already meant there. `delivered_bytes` and
    `offered_bytes` are the two numbers that make a partial arrival impossible to read as a whole
    one: they are only ever both reported, never one of them.
    """

    delivery: DeliveryOutcome | None = None
    delivery_state: str = ""
    delivered_bytes: int = 0
    offered_bytes: int = 0

    @property
    def arrived(self) -> bool:
        """Whether the whole payload provably reached the head.

        The predicate a caller routes on instead of `ok` when it cares about the bytes rather than
        about the attempt. A backend that reports no delivery state says so by leaving it empty,
        and there `ok` is the same statement it always was.
        """
        if not self.ok:
            return False
        return not self.delivery_state or self.delivery_state == "complete"


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
