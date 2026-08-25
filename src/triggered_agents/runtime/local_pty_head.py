"""`LocalPtyHeadRuntime`: the six verbs over a head this product owns the process of.

The second backend of `HeadRuntime`, standing on the local-pty substrate
(`head.local_pty`): a supervisor in its own session holding the head's pty, a Unix socket that
answers without ever waiting for the head, and a versioned journal that says what happened. The
verbs, the receipts and the order of the critical section are the legacy backend's — nothing here
means something different by `HEAD_BUSY`, `HEAD_DRAINING`, `HEAD_ALIVE` or `HEAD_GONE`, and the
contract suite is run against both so that it cannot start to.

What changes is what the backend can honestly do, and it is exactly the two verbs Orca could not:

  * **`attach`** is real. The substrate hands out a stream, bounded by its own attach limit, and
    detaching is closing a socket: it does not touch the head and it does not lose its output,
    because what the head printed while nobody was attached is still in the supervisor's buffer;
  * **`request_drain`** is real in the sense this backend can prove. The drain reaches the process
    that owns the head — its supervisor closes admission for it, writes `drain.requested` into the
    head's own journal, and refuses every later `input` on the socket by name — and the receipt
    only says `head_signalled` once `status` has been read back and confirms it. The agent process
    itself is still told nothing, because no wind-down protocol exists on either side of that pty
    and inventing one would mean typing into a head that is mid-turn; `DRAIN_HEAD_NOT_SIGNALLED`
    on the receipt's reason says which of the two happened, so the difference stays legible.

**Delivery is where this backend is not the legacy one, and the difference is load-bearing.** On
the substrate `ok` from `input` means *admitted*: the payload was taken on and the supervisor's
loop writes it into the pty as fast as the head reads. A `deliver` that returned `HEAD_OK` on that
answer would be reporting an intention as an arrival, which is the wound the substrate closed one
layer down and this one would re-open. So `deliver` follows the payload and then **decides what
became of it once**, in `_delivery_report`, out of `status`'s own delivery record and the journal's
`input.accepted` with its two byte counts and its `complete` flag. That decision is a name, it
travels on the report, and every branch below reads it by that name:

  * `DELIVERY_ARRIVED` — all of it landed: `HEAD_OK`, `delivery_state` `complete`, and a
    `DeliveryOutcome` carrying the delivered byte count;
  * `DELIVERY_STILL_GOING` — admitted, landing, and this runtime stopped watching it before it
    ended: `HEAD_ALIVE`, `delivery_state` `in_flight`. Not `ok`, because nothing about this
    delivery is finished, and not fatal, because a delivery that is still going has left no
    fragment behind it yet. It is the only one of the five that is not an ending, and therefore
    the only one this runtime **writes down on the head** rather than reports and forgets;
  * `DELIVERY_LEFT_A_PREFIX` — it ended part-way: `HEAD_ALIVE` (or `HEAD_GONE` when the head went
    with it), `delivery_state` `stalled` or `failed`, with what did land. Fatal, for the reason
    below;
  * `DELIVERY_LANDED_NOTHING` — it ended and the kernel took not one byte: the terminal is exactly
    as it was, no turn was started, and the head is worth delivering to again;
  * `DELIVERY_UNESTABLISHED` — it was offered, and then the supervisor stopped answering and the
    journal has no record of it either, so what landed **cannot be established**. Reported as
    `delivery_state` `unknown` with the last counts anybody could establish, and fatal.

Two of those five are load-bearing corrections and are written here so that they cannot be
re-derived by accident. A delivery's state is never inferred from a boolean predicate over the byte
counts, and never inferred from *which exception arrived*: an admitted payload followed by an
unreachable supervisor is not a refusal at admission — a refusal means the terminal was never
touched, and here it provably may have been. Both defects this file had were that same shape.

**A prefix left behind is fatal to the run, and that is a decision rather than an omission.** A
payload that reached the head's terminal in part leaves a prefix that nothing can take back: the
head has already read some of it, so flushing the pty's input queue (`TCIFLUSH` discards only what
the head has *not* read) repairs nothing, and admission re-opening would let the next payload land
against that fragment and be read as one line with it. Silently gluing them is the one thing this
must not do. So `DELIVERY_LEFT_A_PREFIX` closes admission here **and** on the substrate, and every
later `deliver` for that head is refused `HEAD_DRAINING` naming the reason. `DELIVERY_UNESTABLISHED`
closes admission for the same reason and a weaker one: a delivery whose fate is unknown *may* have
left a prefix, and admission left open over a maybe-prefix is exactly the glue this forbids.

**A delivery this runtime stopped watching is carried on the head until somebody settles it.**
`DELIVERY_STILL_GOING` is not a delivery that ended well; it is a delivery whose ending this
runtime did not stay for. The substrate's own delivery bound ends it afterwards, out of sight, and
it can end as `stalled` with a fragment on the terminal — after which the supervisor stops refusing
input, the turn that fragment opened closes on its own, and a second payload would be written
straight behind it and read by the head as the tail of the first one's sentence. That is exactly
the gluing this file forbids, arriving one delivery later than the receipt that reported it.

So the outcome travels as a value here too. `_unfinished` is this runtime's per-head register of
admitted deliveries whose fate is not yet known — one entry per head, beside `_fatal` and for the
same reason: it is this backend's own fact about a head, and it is the only thing that remembers
that a payload was handed over and never accounted for. **No verb returns having left an admitted
delivery both unresolved and unrecorded**, and `deliver` **settles the recorded one before it
admits anything**: `_settle` re-reads `status` and the journal, and only then does the verb decide.
A delivery that settles as `DELIVERY_LEFT_A_PREFIX` closes the head there exactly as it would have
closed it inside `deliver`, so the next payload meets `HEAD_DRAINING` naming the fragment rather
than the terminal it would have been glued to; one that settles as still in flight is `HEAD_BUSY`,
because the substrate is still carrying the payload this runtime gave it; one that settles as
arrived or as having landed nothing costs the head nothing and is simply forgotten. Every one of
those consequences is the one `deliver` itself draws for that outcome — a settlement is the same
question asked later, so it must not be able to answer it differently.

**What `HEAD_OK` / `DELIVERY_ARRIVED` means, and it is stronger than the byte count.** It means
the head received *this payload as its own message* — not that these bytes were written. What is
actually on the head's terminal outranks what this runtime managed to observe before it stopped
looking: a receipt that is true about its own bytes and false about the message they joined is a
false receipt. That is why settling comes before admission and not after it.

**The two delivery knobs are deliberately not tied to each other.** `delivery_timeout` is how long
*this runtime* is willing to watch, and the substrate's `delivery_seconds` is how long the
supervisor will go on writing; `start` takes the second from its caller per head and nothing here
checks it against the first. Their relation is not validated because the register above makes it
safe rather than because it does not matter: with the runtime's wait shorter than the substrate's
bound, `DELIVERY_STILL_GOING` becomes reachable, and the register is what turns that from a hole
into an entry somebody settles. Tying the knobs together instead would have closed the one way of
reaching the hole and left the hole.

**Ordering of the journal against a drain this runtime sends.** The drain that a fatal delivery
sends can only be written after that delivery's own `input.accepted`, because the state is only
fatal once the substrate has said the delivery ended — and the supervisor writes the journal record
before it answers that. The one exception is `DELIVERY_UNESTABLISHED`, where the supervisor is not
answering at all: if it recovers, its `drain.requested` can precede the `input.accepted` of the
payload it was still carrying. That ordering is given up knowingly — closing admission over a
payload whose fate cannot be established outranks the order of two records — and it is the honest
remainder of observation 2 of the substrate card's review rather than a claim that it cannot happen.

**Liveness is the launch identity, and there is no second scheme.** `head_process_status` from
`secretary.dispatcher_watchdog` — the reader the watchdog already applies, unchanged — is passed
in by whoever builds this runtime, exactly as `stop_if_quiescent` takes `head_process_alive` from
its caller on the legacy backend. It is a constructor argument rather than an import because
`triggered_agents` does not depend on `secretary`, and because inventing a second pid-file reader
here to avoid that dependency is the failure this argument exists to prevent.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .head import local_pty
from .head.local_pty import protocol
from .head.operations import (
    HeadNudgeFailed,
    HeadOperationError,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadStopFailed,
    NudgePointer,
)
from .head.run import HeadRun, StopInitiator, new_run_id
from .head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    OBSERVE_NO_ADDRESS,
    AttachReceipt,
    DeliverReceipt,
    DrainReceipt,
    HeadActivity,
    ObserveReceipt,
    StartReceipt,
    StopReceipt,
)
from .head.spec import HeadSpec
from .head.task_ref import TaskRef
from .tui_delivery import (
    DELIVERY_CONFIRMED,
    NUDGE_FILE_MODE,
    READINESS_BUSY,
    READINESS_READY,
    DeliveryEvidence,
    DeliveryOutcome,
    payload_fingerprint,
)

#: What a supervisor that cannot be spoken to raises. `LocalPtyError` is the substrate's own
#: refusal — no socket, a connection the supervisor closed — and a bare `OSError` is the same fact
#: arriving from the kernel instead: a supervisor that exits mid-request resets the connection.
#: Both mean "this head could not be reached", and telling them apart here would only let one of
#: them escape as an exception where the other became a receipt.
_UNREACHABLE = (local_pty.LocalPtyError, OSError)

#: How the launch-identity reader is called. The shape of `secretary.dispatcher_watchdog`'s
#: `head_process_status`, which is the one this runtime is meant to be given.
IdentityReader = Callable[..., Mapping[str, Any]]

#: Why an observation of a supervised head says what it says. Tokens, for the same reason the
#: pane-flavoured ones on the boundary are: callers route on them.
#: The run directory holds nothing addressable — no socket and no journal.
OBSERVE_NO_RUN_DIRECTORY = "no_run_directory"
#: The socket did not answer, and the head's launch identity says its process is still alive. The
#: head is somebody's to account for; it is emphatically not a head that has ended.
OBSERVE_SUPERVISOR_UNREACHABLE = "supervisor_unreachable"
#: The head's own process has ended, by the supervisor's answer or by the launch identity.
OBSERVE_HEAD_EXITED = "head_exited"
#: The socket answered something this runtime cannot read as a status. Not an answer about the head.
OBSERVE_STATUS_UNREADABLE = "status_unreadable"

#: What became of one delivery. The closed vocabulary this backend decides in exactly one place —
#: `_delivery_report` — and that every branch afterwards reads by name. It is a value on the report
#: rather than a predicate anybody can re-derive, because both defects this file was sent back for
#: were a re-derivation: one read "still going" out of a boolean over the byte counts, the other
#: read "nothing landed" out of which exception had arrived.
#: All of it reached the head's terminal.
DELIVERY_ARRIVED = "arrived"
#: Admitted, landing, and this runtime stopped watching before it ended. Not fatal — nothing has
#: been left behind yet — and not finished either, which is why it is the one outcome that is
#: written into `_unfinished` and settled before the next delivery is admitted.
DELIVERY_STILL_GOING = "still_going"
#: It ended part-way. Fatal: the prefix on the terminal cannot be taken back.
DELIVERY_LEFT_A_PREFIX = "left_a_prefix"
#: It ended and the kernel took nothing. The terminal is as it was and the head is still worth
#: delivering to.
DELIVERY_LANDED_NOTHING = "landed_nothing"
#: It was admitted and what became of it could not be established from either witness. Fatal,
#: because a fate that cannot be established may be a prefix.
DELIVERY_UNESTABLISHED = "unestablished"
#: The outcomes after which this runtime hands the head no more work, named as a set rather than
#: recomputed at each site.
FATAL_DELIVERY_OUTCOMES = frozenset({DELIVERY_LEFT_A_PREFIX, DELIVERY_UNESTABLISHED})

#: `delivery_state` on the receipt for a delivery whose fate could not be established. The
#: substrate's own four states say what a delivery *did*; this fifth one says that nobody could be
#: asked, which is a different fact and must not be reported as any of the four.
DELIVERY_STATE_UNKNOWN = "unknown"

#: Why a `deliver` says what it says, beside the delivery state it carries.
DELIVER_NOT_ADMITTED = "not_admitted"
DELIVER_IN_FLIGHT = "delivery_in_flight"
DELIVER_STALLED = "delivery_stalled"
DELIVER_FAILED = "delivery_failed"
DELIVER_UNESTABLISHED = "delivery_unestablished"
DELIVER_PREFIX_IS_FATAL = "partial_delivery_closed_this_head"
DELIVER_UNKNOWN_IS_FATAL = "unestablished_delivery_closed_this_head"
#: A delivery this runtime gave the head and stopped watching is still being written. Said as the
#: refusal of the *next* delivery, so that a caller reads why it was not admitted rather than only
#: that the head was busy.
DELIVER_EARLIER_IN_FLIGHT = (
    "a payload this runtime already handed this head is still being written into its terminal: "
    "the next one is refused rather than queued behind it"
)
#: A head this runtime will hand no more work because a delivery left an irreversible prefix on its
#: terminal. The reason travels on every later refusal, so the caller learns why rather than only
#: that it was refused.
DRAIN_AFTER_PARTIAL_DELIVERY = (
    "a delivery reached this head's terminal in part and could not be taken back: admission is "
    "closed rather than re-opened over the fragment it left"
)
#: The same gate for the same reason, over a delivery whose fate nobody could establish: admission
#: is not re-opened over a payload that may have left a prefix.
DRAIN_AFTER_UNESTABLISHED_DELIVERY = (
    "this head was given a payload and what became of it could not be established: admission is "
    "closed rather than re-opened over bytes that may be sitting on its terminal"
)
#: What a drain on this backend really does, said once so that every receipt can carry it.
DRAIN_HEAD_SIGNALLED = (
    "the process that owns this head was told: its supervisor closed admission, wrote "
    "drain.requested into the head's journal, and refuses every later input by name"
)
DRAIN_HEAD_NOT_SIGNALLED = (
    "this runtime hands the head no more work, but its supervisor could not be told, so the head's "
    "own socket would still admit a payload from somebody else"
)

#: Why a stop-if-quiescent refused. The same two tokens the legacy backend uses, because the
#: refusals mean the same thing and a caller must not have to tell the backends apart to read them.
STOP_TURN_IN_FLIGHT = "turn_in_flight"
STOP_ACTIVITY_SINCE = "activity_since_expected_epoch"

#: How long `stop` waits for a signalled head to actually be gone before it says it could not be
#: confirmed. Above the supervisor's own escalation grace, so a head that only dies to `SIGKILL`
#: is still seen to die rather than reported as a stop that failed.
STOP_CONFIRM_SECONDS = 10.0
_CONFIRM_POLL_SECONDS = 0.05


class LocalPtyRuntimeError(RuntimeError):
    """This runtime was built in a shape that could not describe a head truthfully."""


@dataclass(frozen=True)
class AttachedStream:
    """A live head's stream, as a caller is handed one, and the address it was joined at.

    The client is the caller's to close, and closing it is the whole of detaching: it is not a
    message to the supervisor, it does not touch the head, and the output that arrives while
    nobody holds a stream is still in the supervisor's buffer for the next caller.
    """

    client: local_pty.SupervisorClient
    socket_path: str
    backlog: bytes = b""
    dropped_bytes: int = 0
    total_bytes: int = 0

    def close(self) -> None:
        self.client.close()


@dataclass(frozen=True)
class DeliveryReport:
    """What one delivery did to the head's terminal: the decision, and the numbers behind it.

    `outcome` is the whole point of this object. It is one of the five names in this module's
    vocabulary, it is decided once by `_delivery_report`, and nothing downstream re-derives it: a
    caller — or a branch in this file — asks *which outcome this is*, never "did more than zero
    bytes land and was it not complete", which is the predicate that made a delivery still in
    flight look like one that had stalled.

    `state` is what the receipt carries as `delivery_state`: the substrate's own state verbatim,
    or `unknown` when the fate could not be established. `written` is what the kernel took from the
    supervisor and `offered` is what the caller handed over — always both, never one. `journalled`
    says whether the journal's own `input.accepted` was found for this delivery, so a reader can
    tell a fact corroborated in two places from one that only `status` could give.

    `floor` is the journal sequence this delivery was offered after. It is on the report because an
    unfinished report is kept and settled later: asking the journal the same question a second time
    needs the same bound on which of its records may answer it.
    """

    outcome: str
    state: str
    written: int
    offered: int
    delivery_id: int = 0
    journalled: bool = False
    floor: int = 0
    detail: str = ""

    @property
    def fatal(self) -> bool:
        """Whether this outcome closes the head, read from the outcome and from nothing else."""
        return self.outcome in FATAL_DELIVERY_OUTCOMES


def _delivery_report(
    *,
    state: str,
    written: int,
    offered: int,
    established: bool,
    delivery_id: int = 0,
    journalled: bool = False,
    floor: int = 0,
    detail: str = "",
) -> DeliveryReport:
    """Decide what became of one delivery. The only place in this backend that decides it.

    `established` is whether either witness — the supervisor's `status` or the head's journal —
    actually said what the delivery did. A delivery that was admitted and then went unwitnessed is
    `DELIVERY_UNESTABLISHED`, and it is deliberately not folded into any of the four states the
    substrate has words for: "I could not find out" is not "nothing landed", and reporting it as
    the latter is what let a payload be written straight behind a fragment that had landed.
    """
    if not established:
        return DeliveryReport(
            outcome=DELIVERY_UNESTABLISHED,
            state=DELIVERY_STATE_UNKNOWN,
            written=written,
            offered=offered,
            delivery_id=delivery_id,
            journalled=journalled,
            floor=floor,
            detail=detail,
        )
    if state == protocol.DELIVERY_COMPLETE and written >= offered:
        outcome = DELIVERY_ARRIVED
    elif state == protocol.DELIVERY_IN_FLIGHT:
        outcome = DELIVERY_STILL_GOING
    elif written > 0:
        outcome = DELIVERY_LEFT_A_PREFIX
    else:
        outcome = DELIVERY_LANDED_NOTHING
    return DeliveryReport(
        outcome=outcome,
        state=state,
        written=written,
        offered=offered,
        delivery_id=delivery_id,
        journalled=journalled,
        floor=floor,
        detail=detail,
    )


class LocalPtyHeadRuntime:
    """The six verbs over a head whose process, terminal and journal this product owns.

    `root` is where run directories live. It is deliberately a short path: a Unix socket address is
    bounded at about a hundred bytes, and a workspace path with a run id under it does not fit —
    `protocol.socket_path_for` refuses rather than failing opaquely later.

    `head_process_status` is the launch-identity reader, and it is required. There is no reading of
    a head's process this runtime invents for itself: the product has exactly one scheme for that —
    `pid`, `boot_id`, `proc_starttime_ticks` written by the head's own shell — and exactly one
    reader of it, in `secretary.dispatcher_watchdog`. Passing it in is what keeps the two from
    drifting apart while `triggered_agents` stays free of a dependency on `secretary`.

    `delivery_timeout` is how long this runtime is willing to watch a delivery, and it is
    deliberately **not** checked against the substrate's own `delivery_seconds`, which `start`
    takes per head from its caller. The two are independent on purpose: whichever way round they
    are set, a delivery this runtime stops watching before the substrate finishes it is recorded
    on the head and settled before that head is offered anything else. Validating the relation
    would close one way of reaching an unwatched delivery and leave the delivery unaccounted for
    every other way.

    One lock, as on the legacy backend, and for the same reason: `deliver`, `request_drain`, `stop`
    and `stop_if_quiescent` are the four things that can contradict each other about one head, so
    they run one at a time under a lock this object owns.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        head_process_status: IdentityReader,
        activity: HeadActivity | None = None,
        spawn: Callable[..., local_pty.HeadHandle] = local_pty.spawn_head,
        connect_timeout: float = 5.0,
        delivery_timeout: float = protocol.INPUT_DELIVERY_SECONDS + 5.0,
        delivery_poll: float = 0.02,
        stop_timeout: float = STOP_CONFIRM_SECONDS,
    ) -> None:
        if not callable(head_process_status):
            raise LocalPtyRuntimeError(
                "this runtime is given the product's launch-identity reader; it does not invent a "
                "second way to ask whether a head's process is alive"
            )
        self.root = Path(root)
        self.activity = activity or HeadActivity()
        self._identity = head_process_status
        self._spawn = spawn
        self._connect_timeout = connect_timeout
        self._delivery_timeout = delivery_timeout
        self._delivery_poll = delivery_poll
        self._stop_timeout = stop_timeout
        # Reentrant, so `stop_if_quiescent` can perform `stop`.
        self._lock = threading.RLock()
        # Heads whose terminal holds an unfinished payload's prefix. Kept beside the activity
        # rather than inside it because it is this backend's fact, not the boundary's: no other
        # backend can leave a head in this state, and a caller reads it as the reason on a receipt.
        self._fatal: dict[str, str] = {}
        # Heads that were handed a payload whose ending this runtime did not stay for. One entry
        # per head, for the same reason and in the same place: it is the only thing that remembers
        # an admitted delivery nobody has accounted for yet, and `deliver` settles it — from
        # `status` or from the journal — before it admits anything else onto that terminal.
        self._unfinished: dict[str, DeliveryReport] = {}

    # -- the six verbs ------------------------------------------------------------------------

    def start(
        self,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        *,
        command: str,
        title: str,
        pointer: NudgePointer | None = None,
        run_id: str = "",
        role: str = "",
        run: HeadRun | None = None,
        subject: str = "",
        rows: int = 24,
        cols: int = 80,
        quiet_seconds: float | None = None,
        delivery_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
        **ignored: Any,
    ) -> StartReceipt:
        """Bring one head up under a supervisor of its own, and point it at its task.

        `title` is what Orca puts on a pane and this backend has no pane, so it is accepted and
        unused rather than refused: the caller above the boundary hands both backends the same
        arguments, and a verb that rejected one of them would make the boundary a lie.

        A bring-up that names a run this runtime is already holding a turn for is refused before
        anything is started, exactly as on the legacy backend: the invariant belongs to the
        boundary, not to a convention its callers keep.
        """
        del title, ignored
        with self._lock:
            claimed = run.run_id if run is not None else (run_id or "")
            if claimed:
                held = self.activity.lease(claimed)
                if held is not None:
                    return StartReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=(
                            f"this runtime is already running turn {held.lease_id} for "
                            f"{held.subject or 'a caller'} on run {claimed}: a bring-up over it "
                            "would claim a head that is already up"
                        ),
                        epoch=self.activity.epoch(claimed),
                        lease=held,
                    )
            identity = claimed or new_run_id()
            try:
                handle = self._spawn(
                    root=self.root,
                    run_id=identity,
                    role=role or (run.role if run is not None else ""),
                    task=task_ref.ref or task_ref.document,
                    command=command,
                    cwd=workspace,
                    rows=rows,
                    cols=cols,
                    quiet_seconds=quiet_seconds,
                    delivery_seconds=delivery_seconds,
                    env=env,
                )
            except local_pty.LocalPtySpawnError as exc:
                return StartReceipt(
                    status=_spawn_status(exc),
                    run=run,
                    reason=str(exc),
                    failure=_spawn_failure(exc, run),
                    evidence={"reason": exc.reason, "detail": exc.detail},
                    epoch=self.activity.epoch(identity),
                )
            live = (run or HeadRun(
                run_id=identity, spec=spec, workspace=workspace, task_ref=task_ref, role=role,
            )).rebound(str(handle.socket_path), leaf=identity)
            live = _with_pid_file(live, str(handle.pid_file))
            epoch = self.activity.acted(identity)
            if pointer is None:
                return StartReceipt(
                    status=HEAD_OK,
                    run=live,
                    epoch=epoch,
                    rotation_ready=self.activity.rotatable(identity),
                )
            lease = self.activity.grant(identity, subject or "head-launch")
            try:
                report, refusal = self._put(live, pointer, subject or "head-launch")
            except BaseException:
                # Whatever went wrong, the turn this bring-up handed itself is not running: a
                # lease left behind here would refuse every later delivery to a head nobody is
                # working on. `deliver` has guarded this since it was written; this one had not.
                self.activity.release(identity)
                raise
            if refusal is not None or report is None or report.outcome != DELIVERY_ARRIVED:
                # A bring-up whose prompt did not reach the head is a bring-up that left a head
                # nobody has given a task to. It is ended here rather than handed back running,
                # and which of the two refusals this is depends on whether that stop was confirmed
                # — the same distinction `spawn` draws between an aborted and a failed bring-up.
                self.activity.release(identity)
                return self._abandon_bring_up(live, report, refusal, epoch)
            self.activity.acted(identity)
            return StartReceipt(
                status=HEAD_OK,
                run=live.working(),
                delivery=_outcome_of(live, pointer, report, subject or "head-launch"),
                epoch=self.activity.epoch(identity),
                lease=lease,
                rotation_ready=self.activity.rotatable(identity),
            )

    def deliver(
        self, run: HeadRun, pointer: NudgePointer, *, subject: str = "", **ignored: Any
    ) -> DeliverReceipt:
        """Put one prompt in front of a running head, and say what became of the bytes.

        The refusals before anything is written are the legacy backend's, and they mean the same
        things: `HEAD_DRAINING` is a head this runtime hands no more work — a drain was requested,
        or a partial delivery closed it — and `HEAD_BUSY` is a turn this runtime handed out that is
        still running. Neither is a queue.

        What follows the admission is this backend's own, and it is the reason `ok` can be trusted
        here: the payload is followed, what became of it is decided once as one of the five names
        in this module's vocabulary, and the receipt carries that decision as its status, its
        `delivery_state` and its two byte counts. `ok` is only ever `DELIVERY_ARRIVED`, and it
        means the head received *this payload as its own message* rather than that these bytes were
        written — which is why the first thing this verb does is settle any earlier delivery it
        stopped watching, before it decides anything and long before it admits anything. See the
        module docstring for which of the outcomes close this head and why.
        """
        del ignored
        with self._lock:
            # Before anything is decided, let alone admitted: a delivery this runtime handed this
            # head and stopped watching is settled now, out of `status` and the journal. What is
            # actually on the head's terminal outranks what was observed before the watching
            # stopped, so a settlement that finds a fragment closes the head here and this
            # delivery meets that refusal below rather than the fragment.
            unfinished = self._settle(run)
            if not self.activity.admits(run.run_id):
                # Admission is checked before the earlier delivery is reported on, and in this
                # order deliberately: a head this runtime hands no more work is `HEAD_DRAINING`
                # whatever else is true of it, and answering `HEAD_BUSY` for a drained head would
                # tell a caller to come back to a head that will never take a payload again. The
                # settlement above still had to run first — it is what may have closed admission.
                return DeliverReceipt(
                    status=HEAD_DRAINING,
                    run=run,
                    reason=self._fatal.get(run.run_id) or DELIVER_NOT_ADMITTED,
                    evidence=unfinished,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            if unfinished is not None:
                # Refused, and refused before this payload was offered: the byte counts are left
                # at zero exactly as they are for every other refusal of this verb. What the
                # *earlier* delivery did is a fact about that delivery and travels on `evidence`,
                # because a receipt whose numbers are true of somebody else's payload is a receipt
                # that reads as true and says the wrong thing.
                return DeliverReceipt(
                    status=HEAD_BUSY,
                    run=run,
                    reason=DELIVER_EARLIER_IN_FLIGHT,
                    evidence=unfinished,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            held = self.activity.lease(run.run_id)
            if held is not None:
                running = self._turn_still_running(run)
                if running:
                    return DeliverReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=(
                            f"this head is running turn {held.lease_id} for "
                            f"{held.subject or 'a caller'} ({running}): one head runs one turn, "
                            "and this delivery is not queued behind it"
                        ),
                        epoch=self.activity.epoch(run.run_id),
                        lease=held,
                    )
                self.activity.release(run.run_id)
            lease = self.activity.grant(run.run_id, subject or "head-nudge")
            try:
                report, refusal = self._put(run, pointer, subject or "head-nudge")
            except BaseException:
                self.activity.release(run.run_id)
                raise
            if refusal is not None:
                # Refused at admission: nothing of this attempt reached the terminal, so the turn
                # it would have started is handed back.
                self.activity.release(run.run_id)
                return DeliverReceipt(
                    status=refusal.status,
                    run=run,
                    reason=refusal.reason,
                    failure=refusal.failure,
                    evidence=refusal.evidence,
                    epoch=self.activity.epoch(run.run_id),
                    lease=None,
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            assert report is not None
            epoch = self.activity.acted(run.run_id)
            if report.outcome == DELIVERY_ARRIVED:
                return DeliverReceipt(
                    status=HEAD_OK,
                    run=run.working() if run.running else run,
                    delivery=_outcome_of(run, pointer, report, subject or "head-nudge"),
                    delivery_state=report.state,
                    delivered_bytes=report.written,
                    offered_bytes=report.offered,
                    evidence=report,
                    epoch=epoch,
                    lease=lease,
                )
            return self._unfinished_delivery(run, report, lease, epoch)

    def observe(self, run: HeadRun) -> ObserveReceipt:
        """What the substrate can say about this head now: its status, its journal, its process.

        Nothing is guessed. `busy` comes from the supervisor's own turn state and from the turn
        lease this runtime is holding, and stays `None` for every answer that is not one — a socket
        that did not answer is not a head that is idle. The epoch moves on new output rather than
        on the fact of having looked: the supervisor's output counter is a monotonic clock, so the
        same count twice is a head that has been quiet, which is exactly the fact a caller watching
        for progress needs to be able to read.
        """
        with self._lock:
            epoch = self.activity.epoch(run.run_id)
            lease = self.activity.lease(run.run_id)
            rotatable = self.activity.rotatable(run.run_id)
            address = self._address(run)
            if address is None:
                return _unobservable(run, OBSERVE_NO_ADDRESS, epoch, lease, rotatable)
            if not address.journal_path.exists() and not address.socket_path.exists():
                return _unobservable(run, OBSERVE_NO_RUN_DIRECTORY, epoch, lease, rotatable)
            try:
                with self._connect(address) as client:
                    status = client.status()
            except _UNREACHABLE as exc:
                return self._unreachable(run, address, epoch, lease, rotatable, exc)
            if not isinstance(status, dict) or not status.get("ok"):
                return _unobservable(
                    run, OBSERVE_STATUS_UNREADABLE, epoch, lease, rotatable, evidence=status,
                )
            if not status.get("alive"):
                # The supervisor reaped the head and says so. A dead head runs no turn, and the
                # lease this runtime may still be holding names a turn that ended with the process.
                self.activity.release(run.run_id)
                return ObserveReceipt(
                    status=HEAD_GONE,
                    run=run,
                    reason=OBSERVE_HEAD_EXITED,
                    evidence=status,
                    epoch=epoch,
                    lease=None,
                    rotation_ready=self.activity.rotatable(run.run_id),
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                    connected=False,
                    busy=False,
                )
            # The supervisor's output counter is the head's own output clock, and the only property
            # `observed` needs of it is that it never goes backwards. Bytes rather than seconds is
            # deliberate: a timestamp of the last read would move whenever the supervisor looked,
            # and the epoch has to move only when the *head* did something.
            epoch = self.activity.observed(
                run.run_id, output_at=float(int(status.get("output_bytes") or 0))
            )
            turn_open = bool(status.get("turn_open"))
            delivering = _in_flight(status)
            if lease is not None and not turn_open and not delivering:
                # The turn this runtime handed out has ended, and this is where it learns that.
                # It is also where a drained head becomes rotatable, because the last turn it was
                # holding has just closed.
                self.activity.release(run.run_id)
                lease = None
                rotatable = self.activity.rotatable(run.run_id)
            return ObserveReceipt(
                status=HEAD_OK,
                run=run,
                evidence=status,
                epoch=epoch,
                lease=lease,
                rotation_ready=rotatable,
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
                connected=True,
                readiness=READINESS_BUSY if (turn_open or delivering) else READINESS_READY,
                last_output_at=_last_event_at(address),
                busy=turn_open or delivering or lease is not None,
            )

    def request_drain(self, run: HeadRun, initiator: StopInitiator) -> DrainReceipt:
        """Take this head out of service, at this runtime *and* at the process that owns it.

        Two facts, and they are separate because a backend can own one without the other. This one
        owns both when the socket answers: admission closes here, and the supervisor closes it
        there — `drain.requested` in the head's own journal, and every later `input` refused by
        name — which is read back from `status` before `head_signalled` is claimed. When the socket
        cannot be reached the gate is still real locally and the receipt says exactly that instead.

        What a drain closes is admission, never the turn. A head that is mid-turn keeps running it
        and keeps its lease; nothing is interrupted, cancelled or written into its terminal. When
        that last turn closes the head is done, and `rotation_ready` says so.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a drain names who requested it")
        with self._lock:
            self.activity.close_admission(run.run_id)
            signalled, evidence = self._close_substrate_admission(run, initiator)
            return DrainReceipt(
                status=HEAD_OK if signalled else HEAD_ALIVE,
                run=run,
                reason=DRAIN_HEAD_SIGNALLED if signalled else DRAIN_HEAD_NOT_SIGNALLED,
                evidence=evidence,
                draining=True,
                head_signalled=signalled,
                epoch=self.activity.epoch(run.run_id),
                lease=self.activity.lease(run.run_id),
                rotation_ready=self.activity.rotatable(run.run_id),
            )

    def stop(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        signal_name: str = "TERM",
        **ignored: Any,
    ) -> StopReceipt:
        """End this head, and confirm the ending against the head's own launch identity.

        Unconditional, as on the legacy backend: a freeze, an operator taking a head down and a
        bring-up cleaning up after itself all mean "end this now" and must not be refused because
        the head happens to be mid-turn. A stop meant to happen only while the head is quiet asks
        for that by name, through `stop_if_quiescent`.

        The initiator is recorded on the run before the signal is sent, so a stop that outlives
        this process still names who began it. The confirmation is the launch identity going dead —
        not the socket disappearing, which says the supervisor let go and says nothing about the
        head.
        """
        del ignored
        with self._lock:
            finishing = run.finishing(initiator)
            address = self._address(run)
            if address is None:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason="this head has no run directory to address, so nothing could be stopped",
                    failure=HeadStopFailed("a head with no address cannot be stopped", run=finishing),
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                )
            asked = self._ask_to_stop(address, initiator, signal_name)
            gone = self._await_head_gone(address, run)
            if not gone:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason=(
                        f"this head was asked to stop and its process was still there "
                        f"{self._stop_timeout:g}s later"
                    ),
                    failure=HeadStopFailed(
                        "the head's process outlived the stop it was sent", run=finishing
                    ),
                    evidence=asked,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            epoch = self.activity.acted(run.run_id)
            self.activity.forget(run.run_id)
            self._fatal.pop(run.run_id, None)
            # A delivery to a terminal that no longer exists has no ending left to settle.
            self._unfinished.pop(run.run_id, None)
            return StopReceipt(
                status=HEAD_OK, run=finishing.exited(), evidence=asked, epoch=epoch
            )

    def attach(self, run: HeadRun) -> AttachReceipt:
        """Join a caller to this head's live stream, through the substrate's own bounded attach.

        The stream travels on `evidence` as an `AttachedStream`: the connected client, the output
        the head produced before this caller arrived, and how much of it the supervisor had already
        dropped. `handle` is what the attachment is addressed by — the socket — so a caller that
        can do something with an address still gets one when it is refused a stream.

        Detaching is closing that client. It is not a message, it does not reach the head, and the
        head's output goes on being buffered for whoever attaches next.
        """
        with self._lock:
            epoch = self.activity.epoch(run.run_id)
            lease = self.activity.lease(run.run_id)
            rotatable = self.activity.rotatable(run.run_id)
            address = self._address(run)
            if address is None or not address.socket_path.exists():
                return AttachReceipt(
                    status=HEAD_GONE,
                    run=run,
                    reason=OBSERVE_NO_RUN_DIRECTORY if address is not None else OBSERVE_NO_ADDRESS,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=run.handle,
                    leaf=run.leaf or run.run_id,
                )
            try:
                client = self._connect(address)
            except _UNREACHABLE as exc:
                return AttachReceipt(
                    status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
                    run=run,
                    reason=OBSERVE_SUPERVISOR_UNREACHABLE,
                    evidence=str(exc),
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            try:
                answer = client.attach()
            except _UNREACHABLE as exc:
                # Connected, and then nothing came back. A verb of this boundary answers with a
                # receipt for that too: an attachment that did not happen against a head that is
                # still the caller's to account for, classified by the head's process rather than
                # by the socket that dropped.
                client.close()
                return AttachReceipt(
                    status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
                    run=run,
                    reason=OBSERVE_SUPERVISOR_UNREACHABLE,
                    evidence=str(exc),
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            if not answer.get("ok"):
                client.close()
                error = str(answer.get("error") or "")
                return AttachReceipt(
                    status=_refusal_status(error),
                    run=run,
                    reason=error or "the supervisor refused the attachment",
                    evidence=answer,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            return AttachReceipt(
                status=HEAD_OK,
                run=run,
                evidence=AttachedStream(
                    client=client,
                    socket_path=str(address.socket_path),
                    backlog=bytes(answer.get("bytes_data") or b""),
                    dropped_bytes=int(answer.get("dropped_bytes") or 0),
                    total_bytes=int(answer.get("total_bytes") or 0),
                ),
                epoch=epoch,
                lease=lease,
                rotation_ready=rotatable,
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
            )

    # -- not a verb ---------------------------------------------------------------------------

    def stop_if_quiescent(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        expected_activity_epoch: int,
        head_process_alive: bool,
        signal_name: str = "TERM",
    ) -> StopReceipt:
        """End this head only while it is still quiet, with the check and the stop indivisible.

        The composition the legacy backend owes and this one owes identically, in the order
        secretary-1462 fixed and for the same reasons:

          1. **the head's epoch against the caller's**, first and before anything is probed. It
             moved, so the judgement that decided this head was finished has expired;
          2. **the head's process, by the fact the caller established.** A process established not
             alive makes any outstanding lease stale by definition — the turn it names ended when
             the process did — so the lease is closed and the supervisor is **not** asked. Liveness
             outranks terminal readiness here exactly as it does on Orca: a substrate that is
             answering about a head that is gone would still report the turn it last saw, and a
             signal that cannot tell "working" from "not there" cannot hold a veto over "not
             there";
          3. **only for a live process, the end of the turn**, read from the supervisor;
          4. **admission closed, then the stop**, with a refusal putting admission back exactly as
             it found it — including when an earlier drain had already closed it.

        `head_process_alive` is a required argument rather than something read here, for the same
        reason it is one on the legacy backend: it is the caller's own launch-identity evidence,
        the very reading that made it decide this head needed replacing, and re-deriving it here
        would answer a different question at a different moment.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a stop names who ended the head")
        with self._lock:
            epoch = self.activity.epoch(run.run_id)
            if epoch != expected_activity_epoch:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=run,
                    reason=STOP_ACTIVITY_SINCE,
                    evidence={"expected_epoch": expected_activity_epoch, "epoch": epoch},
                    epoch=epoch,
                )
            held = self.activity.lease(run.run_id)
            if held is not None and not head_process_alive:
                self.activity.release(run.run_id)
                held = None
            if held is not None:
                running = self._turn_still_running(run)
                if running:
                    return StopReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=STOP_TURN_IN_FLIGHT,
                        evidence=running,
                        epoch=epoch,
                        lease=held,
                    )
                self.activity.release(run.run_id)
            admitted = self.activity.admits(run.run_id)
            self.activity.close_admission(run.run_id)
            try:
                receipt = self.stop(run, initiator, signal_name=signal_name)
            except BaseException:
                if admitted:
                    self.activity.open_admission(run.run_id)
                raise
            if not receipt.ok and admitted:
                # Nothing was stopped, so nothing was taken out of service either.
                self.activity.open_admission(run.run_id)
            return receipt

    def forget_head(self, run_id: str) -> None:
        """Drop what this runtime remembers about a head somebody else's stop has ended."""
        if not run_id:
            return
        with self._lock:
            self.activity.forget(run_id)
            self._fatal.pop(run_id, None)
            self._unfinished.pop(run_id, None)

    # -- the substrate ------------------------------------------------------------------------

    def _address(self, run: HeadRun) -> _Address | None:
        """Where this head is addressed, derived from the run id rather than remembered.

        Deriving is what makes this backend answer about a head a *later* dispatcher process asks
        about: the run directory is `root/run_id` and everything in it has a predictable name, so a
        runtime constructed a tick ago knows how to reach a head it never started.
        """
        if not run.run_id:
            return None
        try:
            run_dir = protocol.run_dir_for(self.root, run.run_id)
            socket_path = protocol.socket_path_for(run_dir)
        except protocol.ProtocolError:
            return None
        return _Address(
            run_dir=run_dir,
            socket_path=socket_path,
            journal_path=run_dir / protocol.JOURNAL_NAME,
            pid_file=Path(run.pid_file) if run.pid_file else run_dir / protocol.PID_FILE_NAME,
        )

    def _connect(self, address: _Address) -> local_pty.SupervisorClient:
        return local_pty.SupervisorClient.connect(
            address.socket_path, timeout=self._connect_timeout
        )

    def _process_alive(self, address: _Address, run: HeadRun) -> bool:
        """Whether the head's own process is alive, by the launch identity and nothing else.

        The expectation is handed over whole when the run carries one, because the reader treats a
        partial expectation as unprovable rather than as a wildcard — deliberately, and this must
        not work around it. A run with no role or task recorded is checked the only way that is
        left: the record has to be a live match on its own terms and name this run.
        """
        expected = {"run_id": run.run_id, "role": run.role, "task": _task_of(run)}
        if all(expected.values()):
            status = self._identity(str(address.pid_file), expected=expected)
            return bool(status.get("alive")) and bool(status.get("match"))
        status = self._identity(str(address.pid_file))
        record = status.get("record") or {}
        return (
            bool(status.get("alive"))
            and bool(status.get("match"))
            and str(record.get("run_id") or "") == run.run_id
        )

    def _identity_says_dead(self, address: _Address) -> bool:
        """Whether the launch identity positively says the head's process has ended.

        Positively: a record that cannot be read at all is not evidence of a dead head, and this
        answers `False` for it. The expectation is deliberately not passed here — the question is
        about the process the record names, and a mismatch is not a death.
        """
        return bool(self._identity(str(address.pid_file)).get("state") == "dead")

    def _put(
        self, run: HeadRun, pointer: NudgePointer, subject: str
    ) -> tuple[DeliveryReport | None, _Refusal | None]:
        """Offer one payload and follow it until this backend can say what became of it.

        Two answers, and exactly one of them is ever not `None`. The line between them is
        **whether the payload can still be known not to have been offered**, and it is drawn
        deliberately rather than by where an exception happens to be caught. Before the request
        goes onto the socket, a supervisor that cannot be spoken to — or one that answers the
        question asked there with a refusal of its own — is a `_Refusal`: the head's terminal was
        provably never touched. From that moment on nothing is a refusal any more —
        every ending is a `DeliveryReport`, and the worst one of those can say is that what
        became of the payload could not be established.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return None, _Refusal(
                status=HEAD_GONE,
                reason="this head has no socket to deliver through",
                failure=HeadNudgeFailed("the head's supervisor can no longer be addressed"),
            )
        payload = _payload_of(pointer)
        try:
            client = self._connect(address)
        except _UNREACHABLE as exc:
            return None, self._unreachable_refusal(address, run, exc)
        with client:
            try:
                # The journal's own sequence, read before this payload is offered, is the second
                # key that `input.accepted` records are matched on. Delivery ids restart at 1 in
                # every supervisor incarnation while the journal is append-only across all of
                # them, so an id alone would let a record from a previous incarnation of a reused
                # run directory answer for this delivery. Nothing has been offered yet at this
                # point, so a supervisor that cannot answer here is a refusal.
                status = client.status()
            except _UNREACHABLE as exc:
                return None, self._unreachable_refusal(address, run, exc)
            if not status.get("ok"):
                # A refusal the supervisor *stated*, and the frame it stated it in is the whole of
                # this caller's news. At the connection bound the supervisor accepts the socket,
                # writes this frame and lets go without registering the connection or reading a
                # byte of any request — so the payload below was never offered and the head's
                # terminal was never touched. Believing the frame's contents without believing its
                # `ok` was how a live head at a self-clearing bound got closed for good: the write
                # that followed met `EPIPE` and became "admitted, then unanswerable", which is
                # fatal by design. It also left `floor` at 0, which is the bound this backend
                # introduced precisely so that a reused run directory's older records cannot
                # answer for a fresh delivery.
                return None, _stated_refusal(status)
            floor = int(status.get("journal_seq") or 0)
            try:
                answer = client.send_input(payload, subject=subject)
            except _UNREACHABLE as exc:
                # The request is on the socket and no answer came back. Whether the supervisor
                # read it and admitted the payload cannot be established from here, and "probably
                # not" is precisely the assumption that writes the next payload behind a fragment.
                # So this is an unestablished delivery and not a refusal, even though it is one
                # step earlier than the case below: the line is drawn at what can be established,
                # never at how far down the call the failure happened to be.
                return self._remember(run, _delivery_report(
                    state=DELIVERY_STATE_UNKNOWN,
                    written=0,
                    offered=len(payload),
                    established=False,
                    floor=floor,
                    detail=f"the head's supervisor stopped answering as it was offered: {exc}",
                )), None
            if not answer.get("ok"):
                return None, _admission_refusal(answer)
            admitted = dict(answer.get("delivery") or {})
            return self._remember(
                run, self._follow(address, client, admitted, len(payload), floor)
            ), None

    def _follow(
        self,
        address: _Address,
        client: local_pty.SupervisorClient,
        admitted: Mapping[str, Any],
        offered: int,
        floor: int,
    ) -> DeliveryReport:
        """Watch an admitted delivery until the substrate says it is no longer in flight.

        The waiting is done here rather than through the client's own `wait_for_delivery` for one
        reason: that one answers a delivery it could not follow to the end by raising, and a
        caller then has to read the state of the delivery out of *which exception arrived*. This
        backend's whole correction is that the state travels as a value, so the polling is done
        where every ending — arrived, still going when the wait ran out, and the supervisor
        falling silent — can be turned into one.
        """
        delivery_id = int(admitted.get("id") or 0)
        last: Mapping[str, Any] = admitted
        deadline = time.monotonic() + self._delivery_timeout
        while True:
            try:
                status = client.status()
            except _UNREACHABLE as exc:
                # Admitted, and then nobody left to ask. The journal is the other witness and it
                # is on this host's disk, so it is read before anything is concluded; only if it
                # has nothing about this delivery either is the fate reported as unestablished.
                return self._report_of(
                    address, last, offered, floor, established=False, detail=str(exc)
                )
            if not status.get("ok"):
                # A frame that declines the question is not an answer about this delivery, and
                # believing its (absent) `delivery` key would read a supervisor that refused as a
                # supervisor that said nothing had landed. It ends this watch the same way a
                # silent socket does, and for the same reason: the journal is the witness left.
                return self._report_of(
                    address, last, offered, floor,
                    established=False, detail=_refusal_detail(status),
                )
            delivery = status.get("delivery")
            if isinstance(delivery, dict) and int(delivery.get("id") or 0) == delivery_id:
                last = delivery
                if delivery.get("state") != protocol.DELIVERY_IN_FLIGHT:
                    return self._report_of(address, last, offered, floor, established=True)
            if time.monotonic() >= deadline:
                # The wait this runtime was willing to do ran out while the substrate was still
                # writing. That is `DELIVERY_STILL_GOING` and it is an answer, not a failure: the
                # supervisor's own delivery bound is what ends a delivery, and it may outlive this
                # wait by however much an operator set the two knobs to. What ends that delivery
                # afterwards happens out of this runtime's sight, which is precisely why the
                # outcome is written onto the head by `_remember` and settled before the head is
                # offered anything else.
                return self._report_of(address, last, offered, floor, established=True)
            time.sleep(self._delivery_poll)

    def _report_of(
        self,
        address: _Address,
        delivery: Mapping[str, Any],
        offered: int,
        floor: int,
        *,
        established: bool,
        detail: str = "",
    ) -> DeliveryReport:
        """What reached the head's terminal, from `status` and corroborated by the journal.

        `status` is the authority while it answers and the journal is the second witness, in that
        order and not the other way round: a delivery of which not one byte landed is a real fact
        about a real payload, and the record of it is in `status` first. When `status` cannot be
        had at all the order reverses of necessity — the journal is then the only witness left,
        and a journalled delivery is an established one however the socket ended.
        """
        state = str(delivery.get("state") or protocol.DELIVERY_IN_FLIGHT)
        written = int(delivery.get("written_bytes") or 0)
        delivery_id = int(delivery.get("id") or 0)
        journalled = False
        for event in self._accepted_records(address, floor):
            if int(event.get("delivery") or 0) != delivery_id:
                continue
            journalled = True
            # The journal counts what the kernel took, which is the same number `status` reports.
            # Where they can differ is time: the record is written when the delivery ends, so a
            # journal that has it is a journal that saw the end of it.
            written = int(event.get("bytes") or written)
            state = str(event.get("state") or state)
        return _delivery_report(
            state=state,
            written=written,
            offered=int(delivery.get("size_bytes") or offered),
            established=established or journalled,
            delivery_id=delivery_id,
            journalled=journalled,
            floor=floor,
            detail=str(delivery.get("detail") or "") or detail,
        )

    def _accepted_records(self, address: _Address, floor: int) -> tuple[dict[str, Any], ...]:
        """The journal's `input.accepted` records written after this delivery was offered.

        Bounded by the journal's own sequence rather than matched on the delivery id alone. The id
        is unique within one supervisor incarnation and the journal outlives incarnations, so in a
        run directory that was ever reused the id by itself would let an old record answer for a
        new payload. A journal that cannot be read at all is not evidence of anything and says so
        by being empty.
        """
        try:
            events = local_pty.read_events(address.journal_path).of_kind(local_pty.INPUT_ACCEPTED)
        except OSError:
            return ()
        return tuple(event for event in events if int(event.get("seq") or 0) > floor)

    def _unreachable_refusal(
        self, address: _Address, run: HeadRun, exc: Exception
    ) -> _Refusal:
        """A payload that was never admitted, because the supervisor could not be spoken to."""
        return _Refusal(
            status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
            reason=OBSERVE_SUPERVISOR_UNREACHABLE,
            failure=HeadNudgeFailed(f"the head's supervisor could not be reached: {exc}"),
            evidence=str(exc),
        )

    def _unfinished_delivery(
        self, run: HeadRun, report: DeliveryReport, lease: Any, epoch: int
    ) -> DeliverReceipt:
        """A delivery that was offered and did not arrive, said as the outcome it is.

        Every branch here is keyed on `report.outcome` — the name decided once, upstream — and on
        nothing else. What each outcome costs the head:

          * `DELIVERY_STILL_GOING`: an entry in `_unfinished`, written by `_remember` before this
            branch is reached. The lease stays and admission stays open, because nothing has been
            left behind yet — but the payload is now a fact about this head that the next
            `deliver` settles before it admits anything, rather than an outcome reported and
            forgotten;
          * `DELIVERY_LEFT_A_PREFIX` and `DELIVERY_UNESTABLISHED`: the head. Admission closes here
            and at the supervisor, the reason is remembered so that every later refusal carries
            it, and the lease is kept because the head has been given something and may be working
            on it;
          * `DELIVERY_LANDED_NOTHING`: the turn only. Nothing reached the terminal, so no turn was
            started and the lease is handed back; the head is still worth delivering to.
        """
        if report.fatal:
            self._close_head(run, report)
        elif report.outcome == DELIVERY_LANDED_NOTHING:
            self.activity.release(run.run_id)
            lease = None
        return DeliverReceipt(
            status=self._unfinished_status(run, report),
            run=run,
            reason=self._unfinished_reason(report),
            failure=HeadNudgeFailed(report.detail or report.outcome),
            evidence=report,
            delivery_state=report.state,
            delivered_bytes=report.written,
            offered_bytes=report.offered,
            epoch=epoch,
            lease=lease,
            rotation_ready=self.activity.rotatable(run.run_id),
        )

    def _remember(self, run: HeadRun, report: DeliveryReport) -> DeliveryReport:
        """Write an unfinished delivery down on the head, and forget one that has ended.

        The rule this backend keeps: no verb returns having left an admitted delivery both
        unresolved and unrecorded. Every outcome but one is an ending and is carried on the receipt
        the verb returns; `DELIVERY_STILL_GOING` is the one that is not, and dropping it was how a
        payload the substrate later abandoned came to be followed by one written behind its
        fragment. Recorded here, it is settled by `_settle` before this head is offered anything
        else. Returned rather than stored silently so that `_put` reads as one expression.
        """
        if report.outcome == DELIVERY_STILL_GOING:
            self._unfinished[run.run_id] = report
        else:
            self._unfinished.pop(run.run_id, None)
        return report

    def _settle(self, run: HeadRun) -> DeliveryReport | None:
        """Account for a delivery this runtime stopped watching. The answer, or `None`.

        `None` means there is nothing outstanding for this head — either nothing was recorded, or
        what was recorded has now ended and its consequences have been applied here. They are the
        consequences `deliver` would have drawn for the same outcome, drawn by the same names: a
        fragment on the terminal closes the head through `_close_head`, an ending that took not one
        byte hands the turn back, and an arrival costs the head nothing. A report is returned only
        while the substrate is *still* writing the earlier payload, which is the one case where the
        caller must be refused rather than admitted.
        """
        pending = self._unfinished.get(run.run_id)
        if pending is None:
            return None
        report = self._resolve(run, pending)
        if report.outcome == DELIVERY_STILL_GOING:
            # Still going, and now with fresher numbers than the ones it was recorded with.
            self._unfinished[run.run_id] = report
            return report
        self._unfinished.pop(run.run_id, None)
        if report.fatal:
            self._close_head(run, report)
        elif report.outcome == DELIVERY_LANDED_NOTHING:
            # Nothing reached the terminal, so no turn was ever started by it and the lease that
            # was granted for it is handed back — the same consequence the in-line path draws for
            # the same outcome, in one of the two places a delivery can end.
            self.activity.release(run.run_id)
        return None

    def _resolve(self, run: HeadRun, pending: DeliveryReport) -> DeliveryReport:
        """Ask both witnesses what became of a delivery that was left unfinished.

        The same two witnesses `_report_of` reads at the end of a delivery, in the same order and
        bounded by the same journal sequence — this is the identical question asked later, not a
        second way of answering it. A supervisor that has moved on to somebody else's delivery, or
        that cannot be reached at all, leaves the journal as the only witness; a delivery neither
        of them can account for is `DELIVERY_UNESTABLISHED`, which closes the head, because a fate
        that cannot be established may be a fragment.
        """
        address = self._address(run)
        last: Mapping[str, Any] = {
            "id": pending.delivery_id,
            "state": pending.state,
            "written_bytes": pending.written,
            "size_bytes": pending.offered,
        }
        if address is None:
            return _delivery_report(
                state=DELIVERY_STATE_UNKNOWN,
                written=pending.written,
                offered=pending.offered,
                established=False,
                delivery_id=pending.delivery_id,
                floor=pending.floor,
                detail="this head has no run directory left to settle its delivery against",
            )
        detail = ""
        established = False
        try:
            with self._connect(address) as client:
                status = client.status()
        except _UNREACHABLE as exc:
            detail = str(exc)
        else:
            if not status.get("ok"):
                # The supervisor declined the question rather than answering it, so it witnessed
                # nothing here: the journal is left to say what this delivery did.
                detail = _refusal_detail(status)
            else:
                delivery = status.get("delivery")
                if (
                    isinstance(delivery, dict)
                    and int(delivery.get("id") or 0) == pending.delivery_id
                ):
                    # A witness answered about this delivery, which is what `established` means —
                    # not that the delivery has ended. A supervisor still writing it says
                    # `in_flight`, and that is an answer: it settles as `DELIVERY_STILL_GOING`
                    # again rather than as a fate nobody could establish.
                    last = delivery
                    established = True
        return self._report_of(
            address, last, pending.offered, pending.floor,
            established=established, detail=detail,
        )

    def _close_head(self, run: HeadRun, report: DeliveryReport) -> None:
        """Hand this head no more work, here and at the process that owns it.

        One place for both callers — the delivery that ends fatally inside `deliver`, and the one
        that is found to have ended fatally when it is settled a delivery later — because they are
        the same fact about the same terminal and must not be able to drift apart.
        """
        self._fatal[run.run_id] = (
            DRAIN_AFTER_PARTIAL_DELIVERY
            if report.outcome == DELIVERY_LEFT_A_PREFIX
            else DRAIN_AFTER_UNESTABLISHED_DELIVERY
        )
        self.activity.close_admission(run.run_id)
        self._close_substrate_admission(
            run,
            StopInitiator(
                actor="local-pty-runtime",
                reason=(
                    DELIVER_PREFIX_IS_FATAL
                    if report.outcome == DELIVERY_LEFT_A_PREFIX
                    else DELIVER_UNKNOWN_IS_FATAL
                ),
            ),
        )

    def _unfinished_status(self, run: HeadRun, report: DeliveryReport) -> str:
        """`HEAD_GONE` only for a head established to have ended; `HEAD_ALIVE` for the rest.

        A delivery that failed says the head's terminal closed under it, which is the head ending.
        An unestablished delivery says nothing about the head at all, so the launch identity is
        asked — and answers `HEAD_ALIVE` for every reading that is not a positive death, because a
        head that cannot be read about is emphatically not a head that has gone.
        """
        if report.state == protocol.DELIVERY_FAILED:
            return HEAD_GONE
        if report.outcome == DELIVERY_UNESTABLISHED:
            address = self._address(run)
            if address is not None and self._identity_says_dead(address):
                return HEAD_GONE
        return HEAD_ALIVE

    def _unfinished_reason(self, report: DeliveryReport) -> str:
        """Why this delivery says what it says: the outcome first, the substrate's state after."""
        reason = {
            DELIVERY_STILL_GOING: DELIVER_IN_FLIGHT,
            DELIVERY_UNESTABLISHED: DELIVER_UNESTABLISHED,
        }.get(report.outcome) or {
            protocol.DELIVERY_STALLED: DELIVER_STALLED,
            protocol.DELIVERY_FAILED: DELIVER_FAILED,
        }.get(report.state, DELIVER_STALLED)
        if report.outcome == DELIVERY_LEFT_A_PREFIX:
            return f"{reason}: {DRAIN_AFTER_PARTIAL_DELIVERY}"
        if report.outcome == DELIVERY_UNESTABLISHED:
            return f"{reason}: {DRAIN_AFTER_UNESTABLISHED_DELIVERY}"
        return reason

    def _abandon_bring_up(
        self,
        run: HeadRun,
        report: DeliveryReport | None,
        refusal: _Refusal | None,
        epoch: int,
    ) -> StartReceipt:
        """End a head whose prompt never arrived, and say what the ending left behind."""
        detail = refusal.reason if refusal is not None else (report.detail if report else "")
        stopped = self.stop(
            run, StopInitiator(actor="head-launch", reason="the head was never given its task")
        )
        message = f"this head came up and its prompt did not reach it: {detail}"
        if stopped.ok:
            return StartReceipt(
                status=HEAD_GONE,
                run=stopped.run,
                reason=message,
                failure=HeadSpawnFailed(message),
                evidence=report or (refusal.evidence if refusal is not None else None),
                epoch=epoch,
            )
        return StartReceipt(
            status=HEAD_ALIVE,
            run=stopped.run,
            reason=f"{message}; the head it left behind could not be stopped",
            failure=HeadSpawnAborted(message, run=stopped.run),
            evidence=report or (refusal.evidence if refusal is not None else None),
            epoch=epoch,
        )

    def _close_substrate_admission(
        self, run: HeadRun, initiator: StopInitiator
    ) -> tuple[bool, Any]:
        """Tell the process that owns this head to take no more input, and read the answer back.

        `head_signalled` is claimed from `status` rather than from the request having been sent:
        the drain is only true of the head once the supervisor says it is draining.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return False, OBSERVE_NO_RUN_DIRECTORY
        try:
            with self._connect(address) as client:
                answer = client.drain(initiator.actor or "dispatcher")
                if not answer.get("ok"):
                    return False, answer
                status = client.status()
                if not status.get("ok"):
                    # The drain was accepted and the read-back was declined. `head_signalled` is
                    # claimed from what `status` says, so a frame that says nothing about draining
                    # cannot support the claim — and the refusal it does carry is the evidence.
                    return False, status
                return bool(status.get("draining")), answer
        except _UNREACHABLE as exc:
            return False, str(exc)

    def _ask_to_stop(
        self, address: _Address, initiator: StopInitiator, signal_name: str
    ) -> Any:
        """Ask the supervisor to end its head. A supervisor that is gone is not a failure here.

        A head whose supervisor has died is exactly the case where the confirmation below matters:
        nothing was asked, and whether the head is gone is still decided by its launch identity.

        This is the one reader in this file that does not test `ok` before it returns, and that is
        the point of it rather than an omission: the answer is carried as evidence and is never
        believed by anything. A refusal, an unknown op and a supervisor that died mid-request all
        mean the same thing here — the stop was not taken by the socket — and `_await_head_gone`
        decides the outcome from the launch identity either way.
        """
        try:
            with self._connect(address) as client:
                return client.stop(initiator.actor or "dispatcher", signal_name)
        except _UNREACHABLE as exc:
            return {"ok": False, "error": OBSERVE_SUPERVISOR_UNREACHABLE, "detail": str(exc)}

    def _await_head_gone(self, address: _Address, run: HeadRun) -> bool:
        """Wait for the head's own process to be gone, by the identity that named it.

        Not the socket disappearing: that says the supervisor let go, which is a fact about the
        supervisor. A head whose identity record was never written — a bring-up stopped before its
        shell got that far — is answered by the journal's `run.exited` instead, because there is
        nothing else that could ever say yes.
        """
        deadline = time.monotonic() + self._stop_timeout
        while True:
            if self._identity_says_dead(address):
                return True
            if not address.pid_file.exists() and _has_exited(address):
                return True
            if time.monotonic() >= deadline:
                return self._identity_says_dead(address) or (
                    not address.pid_file.exists() and _has_exited(address)
                )
            time.sleep(_CONFIRM_POLL_SECONDS)

    def _turn_still_running(self, run: HeadRun) -> str:
        """Whether the turn this runtime holds a lease for is still running, and how it knows.

        The supervisor is asked rather than assumed: a lease granted three ticks ago and never seen
        to close is stale knowledge, and refusing every later delivery on it would strand the head.
        A supervisor that cannot be reached is not permission — "I could not tell" is a refusal the
        caller is told about, not a prompt written over a running turn.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return "its supervisor can no longer be addressed to ask whether the turn ended"
        try:
            with self._connect(address) as client:
                status = client.status()
        except _UNREACHABLE as exc:
            return f"its supervisor could not be asked whether the turn ended ({exc})"
        if not status.get("ok"):
            return "its supervisor answered nothing this runtime can read as a turn"
        if not status.get("alive"):
            # The head's process has ended, so the turn it was running ended with it.
            return ""
        if _in_flight(status):
            return "a payload is still being written into its terminal"
        return "its supervisor still shows the turn open" if status.get("turn_open") else ""

    def _unreachable(
        self,
        run: HeadRun,
        address: _Address,
        epoch: int,
        lease: Any,
        rotatable: bool,
        exc: Exception,
    ) -> ObserveReceipt:
        """A socket that did not answer, classified by the head's process rather than by the socket.

        The two are different facts and the product has killed live heads by collapsing them: a
        supervisor that died leaves a head running and orphaned, and reporting that as a head that
        has gone is how its replacement is opened beside it.
        """
        if self._identity_says_dead(address):
            # A lease outstanding on a process that no longer exists is stale by definition: the
            # turn it names ended when the process did.
            self.activity.release(run.run_id)
            return ObserveReceipt(
                status=HEAD_GONE,
                run=run,
                reason=OBSERVE_HEAD_EXITED,
                evidence=str(exc),
                epoch=epoch,
                lease=None,
                rotation_ready=self.activity.rotatable(run.run_id),
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
                connected=False,
                busy=False,
            )
        return ObserveReceipt(
            status=HEAD_ALIVE,
            run=run,
            reason=OBSERVE_SUPERVISOR_UNREACHABLE,
            evidence=str(exc),
            epoch=epoch,
            lease=lease,
            rotation_ready=rotatable,
            handle=str(address.socket_path),
            leaf=run.leaf or run.run_id,
            connected=False,
            busy=None,
        )


@dataclass(frozen=True)
class _Address:
    """Everything about one head that is derived from its run directory."""

    run_dir: Path
    socket_path: Path
    journal_path: Path
    pid_file: Path


@dataclass(frozen=True)
class _Refusal:
    """A delivery refused before anything reached the terminal."""

    status: str
    reason: str
    failure: HeadOperationError | None = None
    evidence: Any = None


def _refusal_status(error: str) -> str:
    """Which status a refusal the supervisor *stated* is, by what it refused with.

    One mapping for every verb that is refused before it asked for anything, because the fact is
    the same one whichever verb met it: the supervisor said no, and it said no with a name.

    Only a head the supervisor says has gone is `HEAD_GONE`. Both bounds — the attach limit and
    the connection limit — are refusals worth making again the moment somebody else lets go, and
    reporting a live head sitting at one of them as a head that ended is exactly the collapse
    ("alive looks dead") this sprint exists to remove. Anything else the supervisor can refuse
    with is an answer *from* a live supervisor about a live head, so it is `HEAD_ALIVE` too: what
    was asked did not happen, and the head is still the caller's to account for.
    """
    if error in (protocol.ERROR_ATTACH_LIMIT, protocol.ERROR_CONNECTION_LIMIT):
        return HEAD_BUSY
    if error == protocol.ERROR_HEAD_GONE:
        return HEAD_GONE
    return HEAD_ALIVE


def _refusal_detail(answer: Mapping[str, Any]) -> str:
    """What a supervisor's refusal frame says, in the order a reader wants it: detail, then name."""
    return (
        str(answer.get("detail") or "")
        or str(answer.get("error") or "")
        or "the head's supervisor answered nothing this runtime can read"
    )


def _stated_refusal(answer: Mapping[str, Any]) -> _Refusal:
    """A refusal the supervisor stated to a question asked *before* any payload was offered.

    The second half of this backend's rule about delivery state, and it is the half a stated
    refusal makes the difference for: state is never invented, and a state the substrate stated is
    never thrown away. A frame that is not `ok` is the supervisor declining the question — the
    connection bound is the reachable one, and it is a bound the substrate itself treats as normal
    and self-clearing — so it classifies as a refusal *before* the offer, never as an unknown fate
    after one. Reading such a frame for its contents and going on to write is how a head nothing
    had touched came to be closed for the rest of this runtime's life.
    """
    error = str(answer.get("error") or "")
    detail = _refusal_detail(answer)
    return _Refusal(
        status=_refusal_status(error),
        reason=detail,
        failure=HeadNudgeFailed(detail),
        evidence=answer,
    )


def _admission_refusal(answer: Mapping[str, Any]) -> _Refusal:
    """The substrate's own refusal of a payload, as the status the boundary already has for it.

    Every one of these left the head's terminal exactly as it was, which is why none of them is
    `HEAD_OK` with a delivery attached and why the turn is handed back for all of them.
    """
    error = str(answer.get("error") or "")
    detail = str(answer.get("detail") or error)
    if error == protocol.ERROR_DRAINING:
        return _Refusal(HEAD_DRAINING, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_HEAD_GONE:
        return _Refusal(HEAD_GONE, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_INPUT_IN_FLIGHT:
        # Somebody else's payload holds the floor. Worth making again once it lands, and never
        # interleaved with it.
        return _Refusal(HEAD_BUSY, detail, HeadNudgeFailed(detail), answer)
    # An oversized payload and everything else: the head is untouched and still the caller's.
    return _Refusal(HEAD_ALIVE, detail, HeadNudgeFailed(detail), answer)


def _payload_of(pointer: NudgePointer) -> bytes:
    """One pointer as the bytes a head's terminal receives: the line, and the Enter that sends it."""
    return (pointer.text + "\n").encode("utf-8")


def _outcome_of(
    run: HeadRun, pointer: NudgePointer, report: DeliveryReport, subject: str
) -> DeliveryOutcome:
    """The delivery evidence for a payload that provably reached the head's terminal.

    `confirmed` rather than `accepted`, and it is the stronger word on purpose: on this backend the
    proof is the supervisor's own count of the bytes the kernel took, corroborated by the journal,
    rather than a session manager's report that a send was accepted.
    """
    payload_bytes, payload_hash = payload_fingerprint(pointer.text)
    evidence = DeliveryEvidence(
        handle=run.handle,
        subject=subject,
        payload_bytes=payload_bytes,
        payload_sha256=payload_hash,
        delivery_mode=NUDGE_FILE_MODE if pointer.document else "",
        document_path=pointer.document,
        adapter=run.spec.adapter,
        body_write_accepted=True,
        body_bytes_written=report.written,
        body_write_count=1,
        send_accepted=True,
        bytes_written=report.written,
        attempts=1,
        turn_confirmed=True,
        reason=report.detail,
    )
    return DeliveryOutcome(DELIVERY_CONFIRMED, evidence)


def _spawn_status(exc: local_pty.LocalPtySpawnError) -> str:
    """Which of the four things a refused bring-up left behind.

    A timeout and an `already_running` refusal both mean something may be running that this
    bring-up did not account for, which is `HEAD_ALIVE` — the same distinction `HeadSpawnAborted`
    draws on the legacy path, and for the same reason: treating it as a failure is how live heads
    get a second head opened beside them.
    """
    if exc.reason in ("timeout", "already_running"):
        return HEAD_ALIVE
    return HEAD_GONE


def _spawn_failure(
    exc: local_pty.LocalPtySpawnError, run: HeadRun | None
) -> HeadOperationError:
    if _spawn_status(exc) == HEAD_ALIVE and run is not None:
        return HeadSpawnAborted(str(exc), run=run)
    return HeadSpawnFailed(str(exc))


def _with_pid_file(run: HeadRun, pid_file: str) -> HeadRun:
    """The same run, holding the launch-identity record its head writes."""
    if run.pid_file == pid_file:
        return run
    payload = run.to_json()
    payload["pid_file"] = pid_file
    return HeadRun.from_json(payload)


def _task_of(run: HeadRun) -> str:
    return run.task_ref.ref or run.task_ref.document


def _in_flight(status: Mapping[str, Any]) -> bool:
    delivery = status.get("delivery")
    return (
        isinstance(delivery, dict)
        and delivery.get("state") == protocol.DELIVERY_IN_FLIGHT
    )


def _has_exited(address: _Address) -> bool:
    """Whether the journal says the head's process ended."""
    return bool(local_pty.read_events(address.journal_path).of_kind(local_pty.RUN_EXITED))


def _last_event_at(address: _Address) -> float:
    """The head's own clock, as the newest thing its journal has to say about it."""
    events = local_pty.read_events(address.journal_path).events
    return float(events[-1].get("at") or 0.0) if events else 0.0


def _unobservable(
    run: HeadRun, reason: str, epoch: int, lease: Any, rotatable: bool, *, evidence: Any = None
) -> ObserveReceipt:
    """An observation this backend could not make, said as that and not as an answer."""
    return ObserveReceipt(
        status=HEAD_UNSUPPORTED,
        run=run,
        reason=reason,
        evidence=evidence,
        epoch=epoch,
        lease=lease,
        rotation_ready=rotatable,
        handle=run.handle,
        leaf=run.leaf or run.run_id,
    )
