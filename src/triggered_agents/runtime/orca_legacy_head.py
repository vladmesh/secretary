"""`OrcaLegacyHeadRuntime`: the Orca path this product has always run, behind the six verbs.

Nothing here is new machinery. `start`, `deliver` and `stop` are `spawn`, `nudge` and `stop` from
`operations`, performed against the same `SessionHost` and the same `HeadTransport` they have always
been given; `observe` is the pane inventory and the idle probe the dispatcher already read. What
changes is who owns them: one object, six verbs, typed receipts, so a second backend can be
introduced beside this one instead of underneath every call site.

The three verbs Orca does not really have are the interesting ones, and they are the reason the
receipts have an `unsupported` status:

  * **`attach`** — Orca hands out a pane handle, not a stream. There is no session-manager call that
    joins this process to a running head's input and output, so `attach` says so and passes the
    address along for a caller that can use one. Reporting a successful attach and then handing over
    a handle would be a stub with an invented value;
  * **`request_drain`** — Orca has no wind-down protocol. A head cannot be told "finish this turn and
    take no more". What this runtime *can* honestly do is stop handing that head work itself, so the
    receipt reports exactly that split: `draining` is true, `head_signalled` is false, and the status
    is `unsupported` so that no caller mistakes it for a head that has been asked to finish;
  * **`observe`** — Orca answers about a pane, not about a process. Pane presence, connectivity, its
    output clock and its idle probe are real and are reported; whether the head's process is alive is
    the pid heartbeat's answer and is left `None` here rather than guessed.

It lives here rather than in the `head` package on purpose: that package is backend-independent by
construction and names no session manager, which is what makes its contract suite runnable with no
Orca installed. Orca-specific code lives beside `pane_host`, where the rest of this product's Orca
argument vectors already do.

One lock sits under all of it. `deliver`, `request_drain`, `stop` and `stop_if_quiescent` are the
four things that can contradict each other about one head — a delivery that starts a turn a stop is
about to end, a drain whose gate a delivery reads a moment before it closes — so they run one at a
time, under a lock this object owns. Nothing above the boundary has to hold anything to get that:
the dispatcher core keeps no lock of its own and could not, because it does not know which of its
paths reach the same head.

`stop_workspace` and `stop_if_quiescent` sit beside the six verbs and are deliberately not among
them. `stop_workspace` is the container teardown Orca offers, one call for every pane of a worktree,
and it names no head; it lives here so that no session-manager call for a head's life is made
anywhere else. `stop_workspace` takes the same lock as the verbs, so that the teardown an observer's
stop really is cannot run beside a delivery into one of the panes it is removing. `stop_if_quiescent`
is `stop` under a precondition — the head's own epoch still where the caller last saw it, and, for a
head whose process the caller has established is still alive, no turn the backend still shows as
running — with the check, the closing of admission and the stop itself inside one critical section,
so that none of the three is observable without the others. It is the single place that decides
whether a head may be taken down for a replacement, which is why the caller's pid-heartbeat evidence
comes in as a required argument rather than being re-guessed here from a pane: Orca answers about
ptys, and a pty cannot tell a working head from a dead head's leftover shell. It is not a seventh
verb on the protocol: the six are what every backend owes, and this is a composition of one of them
with state that is the runtime's own. The backend that comes next will owe the same composition, and
the protocol grows then, once, on purpose.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from . import head as head_ops
from .head.operations import (
    Commit,
    HeadPaneBusy,
    HeadSpawnAborted,
    HeadTransport,
    LaunchPreflight,
    NudgePointer,
)
from .head.run import HeadRun, StopInitiator
from .head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    OBSERVE_INVENTORY_UNREADABLE,
    OBSERVE_NO_ADDRESS,
    OBSERVE_PANE_ABSENT,
    OBSERVE_PANE_DISCONNECTED,
    OBSERVE_READINESS_UNKNOWN,
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
from .pane_host import Pane, PaneHostError, SessionHost
from .tui_delivery import (
    READINESS_BUSY,
    READINESS_READY,
    READINESS_UNKNOWN,
    terminal_readiness,
)

# What Orca can be asked to give a caller of `attach`, which is an address and not a stream.
ATTACH_UNSUPPORTED = (
    "orca-legacy heads have no attachable stream: a pane is addressed by handle, not joined"
)
DRAIN_UNSUPPORTED = (
    "orca-legacy heads have no wind-down protocol: this runtime stops handing the head work, "
    "and the head itself is not signalled"
)
# Why a stop-if-quiescent refused. Tokens rather than sentences, for the same reason the observation
# reasons are: the two refusals mean different things to a caller and must not be told apart by a
# substring match. A turn is still running; or the head has done something since the caller looked.
STOP_TURN_IN_FLIGHT = "turn_in_flight"
STOP_ACTIVITY_SINCE = "activity_since_expected_epoch"


class OrcaLegacyHeadRuntime:
    """The Orca-backed head runtime: `spawn`/`nudge`/`stop` and a pane inventory, as six verbs.

    `session` is either a `SessionHost` or something that returns one. The dispatcher passes the
    latter, because it builds a fresh `OrcaSessionHost` around its command runner for every call and
    that behaviour is deliberately unchanged here — what had to become long-lived is the runtime,
    which holds the activity epoch and the turn leases, not the host.
    """

    def __init__(
        self,
        session: SessionHost | Callable[[], SessionHost],
        *,
        activity: HeadActivity | None = None,
    ) -> None:
        self._session = session
        self.activity = activity or HeadActivity()
        # One lock for every verb of every head this runtime owns, held across the session-manager
        # call each verb makes. Per-head locks would let two heads be worked on at once, which is
        # true but buys nothing here: a verb is one Orca call, the dispatcher drives them from one
        # tick, and a single lock is the version of this whose critical sections are obviously
        # non-overlapping. It is reentrant because `stop_if_quiescent` performs `stop`.
        self._lock = threading.RLock()

    @property
    def host(self) -> SessionHost:
        """The session manager one verb is about to be performed against."""
        session = self._session
        return session() if callable(session) else session

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
        pid_file: str = "",
        split_from: str = "",
        run_id: str = "",
        role: str = "",
        run: HeadRun | None = None,
        preflight: LaunchPreflight | None = None,
        commit: Commit | None = None,
        transport: HeadTransport | None = None,
        subject: str = "",
    ) -> StartReceipt:
        """Open this head's pane and, when there is a pointer, point it at its task.

        Only the operation's own refusals become receipts. A session manager that fails on its own
        terms — an unreadable answer to `terminal create` — still raises, because that is not a
        classified head failure and this boundary must not invent a classification for it.

        A bring-up that names a run id this runtime is already holding a turn for is refused here,
        before the session manager is touched. The caller that passes its own run id — the observer
        bring-up does — would otherwise reach the grant below with a lease outstanding and get a
        `TurnLeaseError` where every other refusal on this boundary is a receipt. The invariant is
        the boundary's, not a convention its callers keep.

        That lease is the only witness this backend has, and saying so is the answer rather than a
        gap. The local-pty backend refuses a second bring-up over a head that survived the process
        which started it by reading that head's own launch identity — `pid`, `boot_id`,
        `proc_starttime_ticks` — because it owns the record and is handed the product's reader for
        it. Orca answers about a pane, not about a process: this runtime is given a `SessionHost`
        and no launch-identity reader at all, which is exactly why `observe` leaves head liveness
        `None` and why `stop_if_quiescent` takes the caller's pid-heartbeat evidence as a required
        argument instead of guessing it from an inventory. A durable refusal invented here would
        have to be derived from pane presence, and a pane outlives the head that ran in it, so it
        would fence live cards out on debris. The question is therefore answered *above* this
        backend, by the dispatcher record that decides a run has a head, and nothing here pretends
        otherwise.
        """
        with self._lock:
            claimed = run.run_id if run is not None else run_id
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
            try:
                outcome = head_ops.spawn(
                    spec,
                    workspace,
                    task_ref,
                    host=self.host,
                    command=command,
                    title=title,
                    pointer=pointer,
                    pid_file=pid_file,
                    split_from=split_from,
                    run_id=run_id,
                    role=role,
                    run=run,
                    preflight=preflight,
                    commit=commit,
                    transport=transport,
                    subject=subject,
                )
            except head_ops.HeadOperationError as exc:
                failed = getattr(exc, "run", None)
                return StartReceipt(
                    status=_bring_up_status(exc),
                    run=failed,
                    reason=str(exc),
                    failure=exc,
                    evidence=getattr(exc, "evidence", None),
                    epoch=self.activity.epoch(failed.run_id if failed is not None else ""),
                )
            epoch = self.activity.acted(outcome.run.run_id)
            lease = None
            if pointer is not None:
                # A pointer that was delivered is a turn this head is now running. A head whose
                # prompt went on its own command line was given no turn *here*, and this runtime
                # does not claim one it did not hand out. The grant cannot be refused: either this
                # run id was minted by the bring-up above inside this same critical section, or it
                # came from the caller and was checked against the outstanding leases on the way in.
                lease = self.activity.grant(outcome.run.run_id, subject or "head-launch")
            return StartReceipt(
                status=HEAD_OK,
                run=outcome.run,
                delivery=outcome.delivery,
                fallback_reason=outcome.fallback_reason,
                epoch=epoch,
                lease=lease,
                rotation_ready=self.activity.rotatable(outcome.run.run_id),
            )

    def deliver(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        *,
        subject: str = "",
        transport: HeadTransport | None = None,
    ) -> DeliverReceipt:
        """Put one prompt in front of a running head, or refuse — explicitly — to.

        Two refusals, and they are different values because a caller does different things with
        them. `HEAD_DRAINING` says this head takes no more work at all: a drain was requested and
        nothing this caller does later will make this delivery happen, so the work belongs to
        another head. `HEAD_BUSY` says a turn this runtime handed out is still running: the head is
        fine, the delivery is worth making again when that turn ends.

        Neither is a queue. Nothing is held, retried, or delivered later by itself — a refusal here
        is the end of this delivery, and the caller is told so in the receipt rather than being left
        to find out from a prompt that arrives on top of a running turn.

        The turn is taken *before* the prompt is put in front of the head, not after the delivery
        succeeds, and that ordering is the whole serialisation: a second delivery that reaches this
        runtime while the first is inside its session-manager call finds the lease already held and
        is refused, instead of typing into the same pane behind it. A delivery that then fails hands
        the turn back, because a refused delivery started no turn.
        """
        with self._lock:
            if not self.activity.admits(run.run_id):
                return DeliverReceipt(
                    status=HEAD_DRAINING,
                    run=run,
                    reason=f"a drain was requested for this head: {DRAIN_UNSUPPORTED}",
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
                            f"{held.subject or 'a caller'} ({running}): one head runs one turn, and "
                            "this delivery is not queued behind it"
                        ),
                        epoch=self.activity.epoch(run.run_id),
                        lease=held,
                    )
                # The turn this runtime handed out has ended and this is where it learns that,
                # exactly as `observe` does. Releasing it here is not the eviction `renew` used to
                # perform: the head is not mid-turn, and the fact that it is not was read from the
                # backend inside this critical section rather than assumed from the delivery that
                # was about to happen.
                self.activity.release(run.run_id)
            lease = self.activity.grant(run.run_id, subject or "head-nudge")
            try:
                outcome = head_ops.nudge(
                    run,
                    pointer,
                    host=self.host,
                    transport=transport,
                    subject=subject,
                )
            except head_ops.HeadOperationError as exc:
                # Nothing of this attempt is left, the turn included.
                self.activity.release(run.run_id)
                return DeliverReceipt(
                    status=_delivery_status(exc),
                    run=getattr(exc, "run", None) or run,
                    reason=str(exc),
                    failure=exc,
                    evidence=getattr(exc, "evidence", None),
                    epoch=self.activity.epoch(run.run_id),
                    lease=None,
                )
            except BaseException:
                self.activity.release(run.run_id)
                raise
            epoch = self.activity.acted(outcome.run.run_id)
            return DeliverReceipt(
                status=HEAD_OK,
                run=outcome.run,
                delivery=outcome.delivery,
                epoch=epoch,
                lease=lease,
            )

    def observe(self, run: HeadRun) -> ObserveReceipt:
        """What Orca can say about this head now: its pane, that pane's clock, and its idle probe.

        Never a statement about the process. Orca answers about ptys, and a pane that is gone is not
        the same fact as a head that exited — the pid heartbeat is what answers that, above this
        boundary. So `busy` is read from the turn lease and the pane's readiness, and stays `None`
        whenever neither could be read.
        """
        with self._lock:
            epoch = self.activity.epoch(run.run_id)
            lease = self.activity.lease(run.run_id)
            rotatable = self.activity.rotatable(run.run_id)
            if not run.workspace or not (run.handle or run.leaf):
                return _unobservable(run, OBSERVE_NO_ADDRESS, epoch, lease, rotatable)
            try:
                panes = list(self.host.panes(run.workspace))
            except PaneHostError as exc:
                # An observation that raised is an observation that could not be made. It is
                # emphatically not evidence that the pane is gone: reporting it as absent is how a
                # live head loses its pane to a replacement opened beside it. Only the pane host's
                # own refusal is classified here — a session manager failing on some other terms is
                # not a fact about this head, and swallowing it would hide it from the caller that
                # does know what it means.
                return _unobservable(
                    run, OBSERVE_INVENTORY_UNREADABLE, epoch, lease, rotatable, evidence=str(exc),
                )
            pane = _pane_for(panes, run)
            if pane is None:
                return ObserveReceipt(
                    status=HEAD_GONE,
                    run=run,
                    reason=OBSERVE_PANE_ABSENT,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    leaf=run.leaf,
                )
            if not pane.connected:
                # A pane nothing can be typed into. Not a dead head, and not an observation either.
                return ObserveReceipt(
                    status=HEAD_ALIVE,
                    run=run,
                    reason=OBSERVE_PANE_DISCONNECTED,
                    epoch=self.activity.observed(run.run_id, output_at=pane.last_output_at),
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=pane.handle,
                    leaf=pane.leaf or run.leaf,
                    connected=False,
                    last_output_at=pane.last_output_at,
                )
            readiness = terminal_readiness(pane.handle, host=self.host)
            epoch = self.activity.observed(run.run_id, output_at=pane.last_output_at)
            if readiness == READINESS_UNKNOWN:
                # The probe failing is the probe failing. It is not a busy head and it is not an
                # idle one, so nothing about busyness is reported here.
                return ObserveReceipt(
                    status=HEAD_UNSUPPORTED,
                    run=run,
                    reason=OBSERVE_READINESS_UNKNOWN,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=pane.handle,
                    leaf=pane.leaf or run.leaf,
                    connected=True,
                    readiness=readiness,
                    last_output_at=pane.last_output_at,
                )
            if readiness == READINESS_READY and lease is not None:
                # The turn this runtime handed out has ended: the pane will take input again.
                # Closing the lease here is what keeps "a turn is running" a fact about now rather
                # than a fact about the last delivery — and it is where a drained head becomes
                # rotatable, because the last turn it was holding has just closed.
                self.activity.release(run.run_id)
                lease = None
                rotatable = self.activity.rotatable(run.run_id)
            return ObserveReceipt(
                status=HEAD_OK,
                run=run,
                epoch=epoch,
                lease=lease,
                rotation_ready=rotatable,
                handle=pane.handle,
                leaf=pane.leaf or run.leaf,
                connected=True,
                readiness=readiness,
                last_output_at=pane.last_output_at,
                busy=readiness == READINESS_BUSY or lease is not None,
            )

    def request_drain(self, run: HeadRun, initiator: StopInitiator) -> DrainReceipt:
        """Take this head out of service, as far as a session manager with no drain allows.

        The gate is real and local: `deliver` refuses for this head from here on. The head itself is
        never told anything, which is why the status is `unsupported` — a caller that needs a head to
        finish its turn before something else happens has not been given that here.

        What a drain closes is admission, not the turn. A head that is mid-turn keeps running it and
        keeps its lease; nothing is interrupted, cancelled or typed into its pane. When that last
        turn closes the head is done, and the receipts say so in `rotation_ready` rather than
        leaving a caller to infer it from a drain it remembers requesting.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a drain names who requested it")
        with self._lock:
            self.activity.close_admission(run.run_id)
            return DrainReceipt(
                status=HEAD_UNSUPPORTED,
                run=run,
                reason=DRAIN_UNSUPPORTED,
                draining=True,
                head_signalled=False,
                epoch=self.activity.epoch(run.run_id),
                lease=self.activity.lease(run.run_id),
                rotation_ready=self.activity.rotatable(run.run_id),
            )

    def stop(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        transport: HeadTransport | None = None,
        commit: Commit | None = None,
        preflight: LaunchPreflight | None = None,
        confirm_gone: Callable[[str], None] | None = None,
    ) -> StopReceipt:
        """End this head, through the operation that records who ended it before it acts.

        Unconditional, and that is the point of it existing beside `stop_if_quiescent`: a freeze, an
        operator taking a head down, a bring-up cleaning up after itself all mean "end this now",
        and they must not be refused because the head happens to be mid-turn. A stop that is only
        meant to happen while the head is quiet asks for that by name.
        """
        with self._lock:
            try:
                outcome = head_ops.stop(
                    run,
                    initiator,
                    host=self.host,
                    transport=transport,
                    commit=commit,
                    preflight=preflight,
                    confirm_gone=confirm_gone,
                )
            except head_ops.HeadStopFailed as exc:
                # A stop that could not be confirmed leaves a head the caller still owns, in
                # `finishing` and carrying its initiator. That is the whole point of the distinction.
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=getattr(exc, "run", None) or run,
                    reason=str(exc),
                    failure=exc,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            # The last thing that happened to this head, reported before the runtime forgets it: a
            # head that has ended has no epoch, no turn and no admission to keep.
            epoch = self.activity.acted(run.run_id)
            self.activity.forget(run.run_id)
            return StopReceipt(status=HEAD_OK, run=outcome.run, epoch=epoch)

    def attach(self, run: HeadRun) -> AttachReceipt:
        """Orca has no stream to join. The pane's address travels instead, and says which it is."""
        with self._lock:
            return AttachReceipt(
                status=HEAD_UNSUPPORTED,
                run=run,
                reason=ATTACH_UNSUPPORTED,
                handle=run.handle,
                leaf=run.leaf,
                epoch=self.activity.epoch(run.run_id),
                lease=self.activity.lease(run.run_id),
                rotation_ready=self.activity.rotatable(run.run_id),
            )

    def _turn_still_running(self, run: HeadRun) -> str:
        """Whether the turn this runtime is holding a lease for is still running, and how it knows.

        Orca cannot be asked about a head's process, so the honest question is about its pane: a
        pane that will take input again is a pane whose turn ended. The runtime asks rather than
        assuming, because a lease it granted three ticks ago and never saw close is stale knowledge,
        and refusing every later delivery on stale knowledge would strand the head.

        The other three answers all mean the turn has not been seen to end — the pane is working,
        it is held in a dialog, or it could not be probed at all — and a delivery on top of one of
        those is exactly what this refuses. "I could not tell" is not permission: it is a refusal
        the caller is told about and can make again, not a prompt typed over a running turn.

        A head whose lease is outstanding is asked once per delivery. It is the same probe `observe`
        makes, and it is only made when there is a lease to close.
        """
        if not run.handle:
            return "its pane can no longer be addressed to ask whether the turn ended"
        readiness = terminal_readiness(run.handle, host=self.host)
        if readiness == READINESS_READY:
            return ""
        if readiness == READINESS_UNKNOWN:
            return "its pane could not be probed, so the end of that turn was never seen"
        return f"its pane is {readiness}"

    # -- not a verb ---------------------------------------------------------------------------

    def stop_if_quiescent(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        expected_activity_epoch: int,
        head_process_alive: bool,
        teardown: Callable[[], None] | None = None,
        transport: HeadTransport | None = None,
        commit: Commit | None = None,
        preflight: LaunchPreflight | None = None,
        confirm_gone: Callable[[str], None] | None = None,
    ) -> StopReceipt:
        """End this head only while it is still quiet, with the check and the stop indivisible.

        This is the single place that decides whether a head may be taken down for a replacement.
        No caller keeps part of that decision and none goes around it; what a caller owes is the
        two facts it, and only it, can establish, and they travel in together.

        The order inside the one critical section, and the order is the contract:

          1. **The head's activity epoch against the caller's.** `expected_activity_epoch` is the
             caller's own reading, and it belongs to the moment the caller decided this head was
             finished — read in the argument list of this call it would say nothing, because
             whatever the head did between the decision and the stop would already be in it. It
             moved, so the decision has expired: `HEAD_ALIVE` with `STOP_ACTIVITY_SINCE`, and
             nothing is probed;
          2. **the head's process, by the fact the caller already established.** `head_process_alive`
             is the caller's pid-heartbeat evidence — the same evidence that made it decide to
             replace this head — and it is a required argument because there is no reading of it
             this runtime can make: Orca answers about ptys, not about processes. A process that is
             established *not* alive makes any outstanding lease stale by definition: the turn it
             names ended when the process did, so the lease is closed here and the pane is **not**
             probed;
          3. **only for a head whose process is alive, the end of the turn.** A pane that will take
             input again is a turn that ended: the lease is closed and the stop goes ahead. Working,
             held, or unprobeable is `HEAD_BUSY` — an active turn is never interrupted, which is the
             sprint's own definition of done;
          4. **admission closed, then the stop.**

        Why liveness outranks readiness, and it is not a preference. `terminal_readiness` answers
        about a pane, not about a turn: it says `busy` both for an agent TUI that is working and for
        the bare wrapper shell a head leaves behind when it dies, and `unknown` for a pane that has
        gone entirely. A signal that cannot tell "working" from "not there" cannot hold a veto over
        "not there". Letting it would make this refusal *permanent* rather than conservative:
        nothing else on a rotation moves this head's epoch or closes its lease — `deliver` and
        `observe` are only reached for a head the tick believes is live — so every later tick would
        get the same answer, and the sprint would keep a ghost pane and never get a new observer.

        Every refusal here names a condition some other path can change: a live head finishes its
        turn; activity is absorbed by the next tick's fresh judgement. A refusal whose condition
        nothing can change would be a defect, not caution.

        A refusal is typed and leaves nothing behind: nothing was stopped, and admission is exactly
        as it was — including when it was already closed by an earlier drain, which this must not
        silently re-open.

        `teardown` is for the callers whose stop is not `head_ops.stop`: the observer's is Orca's
        whole-worktree teardown, because a per-handle close reports a stop that worked as a stop
        that failed. It runs inside the same critical section and owns its own errors; letting it
        run outside would re-open the window this method exists to close. A teardown that raises
        puts admission back and the exception travels to its caller unchanged.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a stop names who ended the head")
        with self._lock:
            # The epoch is compared first, and it is the thing that actually guards this stop. The
            # lease below can be reclaimed from the backend, so it is not a barrier a caller can
            # rely on; the epoch is, because it moves for every prompt this runtime typed and every
            # line the pane printed since the caller formed its judgement. That is why the caller
            # owes an epoch read at the moment it decided this head was finished, not one read in
            # the argument list of this call: activity between the judgement and the stop has to
            # refuse the stop, and an epoch read here would have already absorbed it.
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
                # The caller established this head's process is gone, by the pid heartbeat it
                # already read to decide the head needed replacing. A lease outstanding on a
                # process that no longer exists is stale by definition — the turn it names ended
                # when the process did — so it is closed here and the pane is never asked.
                #
                # Not asking is the whole repair. The probe below says `busy` for the bare wrapper
                # shell a dead head leaves behind exactly as it does for a working agent, so asking
                # it here refused the rotation in precisely the state the rotation exists for, and
                # refused it forever: nothing on this path moves the epoch or closes the lease, so
                # the next tick got the same answer, and the one after that.
                self.activity.release(run.run_id)
                held = None
            if held is not None:
                # A head whose process is alive is the only head whose pane is worth asking about,
                # and here the question is a real one: a lease granted three ticks ago and never
                # seen to close is stale knowledge, and only `deliver`, `observe` and `stop` ever
                # close one. A pane that will take input again is a turn that has ended.
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
                if teardown is None:
                    receipt = self.stop(
                        run,
                        initiator,
                        transport=transport,
                        commit=commit,
                        preflight=preflight,
                        confirm_gone=confirm_gone,
                    )
                else:
                    # The epoch this stop ends on is taken before the teardown runs, not after: an
                    # observer's teardown reaches `forget_head` on its way out, so an `acted` after
                    # it would count up from nothing and put `1` on the receipt of a head that had
                    # been through many turns.
                    ended = self.activity.acted(run.run_id)
                    teardown()
                    self.activity.forget(run.run_id)
                    return StopReceipt(status=HEAD_OK, run=run, epoch=ended)
            except BaseException:
                if admitted:
                    self.activity.open_admission(run.run_id)
                raise
            if not receipt.ok and admitted:
                # Nothing was stopped, so nothing was taken out of service either.
                self.activity.open_admission(run.run_id)
            return receipt

    def stop_workspace(self, workspace: str) -> None:
        """Take down every pane of one worktree, which is the only stop Orca has at that scope.

        Not one of the six verbs and deliberately so: it names a container, not a head, and it makes
        no promise about any individual head's process. Callers that own heads in the workspace fence
        and confirm each of them around this call, exactly as they did when it was a bare session
        call. It lives on this object so that no pane call for a head's life is made outside it.

        It takes the lock anyway. For an observer the real stop *is* this call — its head owns the
        worktree — so leaving it outside would mean "delivery and stop run one at a time" held for
        every head except the one whose stop this product actually performs. Reentrant, so the
        conditional stop that reaches it through its own teardown is unaffected.
        """
        with self._lock:
            self.host.stop_workspace(workspace)

    def forget_head(self, run_id: str) -> None:
        """Drop what this runtime remembers about a head somebody else's stop has ended.

        The six verbs clean up after themselves; an observer's unconditional stop does not go
        through `stop`, because what ends an observer is Orca's worktree teardown. Without this its
        epoch, its output mark and its admission would stay in a runtime that lives as long as the
        production loop, one entry per head ever launched.
        """
        if not run_id:
            return
        with self._lock:
            self.activity.forget(run_id)


def _bring_up_status(exc: head_ops.HeadOperationError) -> str:
    """Which of the four things a refused bring-up left behind.

    The mapping is one-to-one with the operation's own types, and losing it is the failure mode this
    boundary was told to avoid: an aborted bring-up has a pane that may hold a live head, and
    treating it as a failed one is how live heads get killed.
    """
    if isinstance(exc, HeadSpawnAborted):
        return HEAD_ALIVE
    if isinstance(exc, HeadPaneBusy):
        return HEAD_BUSY
    return HEAD_GONE


def _delivery_status(exc: head_ops.HeadOperationError) -> str:
    """A refused delivery into a running head has not ended it.

    `nudge` closes nothing, so whatever it refused with, the head and its pane are still there for
    the caller to account for.
    """
    if isinstance(exc, HeadPaneBusy):
        return HEAD_BUSY
    return HEAD_ALIVE


def _unobservable(
    run: HeadRun, reason: str, epoch: int, lease: Any, rotatable: bool, *, evidence: Any = None,
) -> ObserveReceipt:
    """An observation Orca could not make, said as that and not as an answer about the head."""
    return ObserveReceipt(
        status=HEAD_UNSUPPORTED,
        run=run,
        reason=reason,
        evidence=evidence,
        epoch=epoch,
        lease=lease,
        rotation_ready=rotatable,
        handle=run.handle,
        leaf=run.leaf,
    )


def _pane_for(panes: list[Pane], run: HeadRun) -> Pane | None:
    """This head's pane in a workspace inventory: by stable leaf, and by handle only without one."""
    if run.leaf:
        return next((pane for pane in panes if pane.leaf == run.leaf), None)
    return next((pane for pane in panes if run.handle and pane.handle == run.handle), None)
