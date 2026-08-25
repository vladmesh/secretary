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

`stop_workspace` sits beside the six verbs and is deliberately not one of them: it is the container
teardown Orca offers, one call for every pane of a worktree, and it names no head. It lives here so
that no session-manager call for a head's life is made anywhere else.
"""
from __future__ import annotations

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
        self._draining: set[str] = set()

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
        """
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
            return StartReceipt(
                status=_bring_up_status(exc),
                run=getattr(exc, "run", None),
                reason=str(exc),
                failure=exc,
                evidence=getattr(exc, "evidence", None),
                epoch=self.activity.epoch,
            )
        epoch = self.activity.observed(outcome.run.run_id)
        lease = None
        if pointer is not None:
            # A pointer that was delivered is a turn this head is now running. A head whose prompt
            # went on its own command line was given no turn *here*, and this runtime does not claim
            # one it did not hand out.
            lease = self.activity.renew(outcome.run.run_id, subject or "head-launch")
        return StartReceipt(
            status=HEAD_OK,
            run=outcome.run,
            delivery=outcome.delivery,
            fallback_reason=outcome.fallback_reason,
            epoch=epoch,
            lease=lease,
        )

    def deliver(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        *,
        subject: str = "",
        transport: HeadTransport | None = None,
    ) -> DeliverReceipt:
        """Put one prompt in front of a running head.

        A head this runtime has been asked to drain is refused here and nowhere else: the refusal is
        local to the runtime, because Orca has no drain of its own to consult. Note what this does
        *not* do — it does not refuse a delivery because a turn lease is outstanding. Serialising
        delivery against the turn it would interrupt is a real change of behaviour and belongs to the
        card that makes delivery, drain and stop atomic; this one only moves the boundary.
        """
        if run.run_id in self._draining:
            return DeliverReceipt(
                status=HEAD_DRAINING,
                run=run,
                reason=f"a drain was requested for this head: {DRAIN_UNSUPPORTED}",
                epoch=self.activity.epoch,
                lease=self.activity.lease(run.run_id),
            )
        try:
            outcome = head_ops.nudge(
                run,
                pointer,
                host=self.host,
                transport=transport,
                subject=subject,
            )
        except head_ops.HeadOperationError as exc:
            return DeliverReceipt(
                status=_delivery_status(exc),
                run=getattr(exc, "run", None) or run,
                reason=str(exc),
                failure=exc,
                evidence=getattr(exc, "evidence", None),
                epoch=self.activity.epoch,
                lease=self.activity.lease(run.run_id),
            )
        epoch = self.activity.observed(outcome.run.run_id)
        lease = self.activity.renew(outcome.run.run_id, subject or "head-nudge")
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
        epoch = self.activity.epoch
        lease = self.activity.lease(run.run_id)
        if not run.workspace or not (run.handle or run.leaf):
            return _unobservable(run, OBSERVE_NO_ADDRESS, epoch, lease)
        try:
            panes = list(self.host.panes(run.workspace))
        except Exception as exc:  # noqa: BLE001 -- whatever the session manager called its refusal
            # An observation that raised is an observation that could not be made. It is emphatically
            # not evidence that the pane is gone: reporting it as absent is how a live head loses its
            # pane to a replacement opened beside it.
            return _unobservable(
                run, OBSERVE_INVENTORY_UNREADABLE, epoch, lease, evidence=str(exc),
            )
        pane = _pane_for(panes, run)
        if pane is None:
            return ObserveReceipt(
                status=HEAD_GONE,
                run=run,
                reason=OBSERVE_PANE_ABSENT,
                epoch=epoch,
                lease=lease,
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
                handle=pane.handle,
                leaf=pane.leaf or run.leaf,
                connected=False,
                last_output_at=pane.last_output_at,
            )
        readiness = terminal_readiness(pane.handle, host=self.host)
        epoch = self.activity.observed(run.run_id, output_at=pane.last_output_at)
        if readiness == READINESS_UNKNOWN:
            # The probe failing is the probe failing. It is not a busy head and it is not an idle
            # one, so nothing about busyness is reported here.
            return ObserveReceipt(
                status=HEAD_UNSUPPORTED,
                run=run,
                reason=OBSERVE_READINESS_UNKNOWN,
                epoch=epoch,
                lease=lease,
                handle=pane.handle,
                leaf=pane.leaf or run.leaf,
                connected=True,
                readiness=readiness,
                last_output_at=pane.last_output_at,
            )
        if readiness == READINESS_READY and lease is not None:
            # The turn this runtime handed out has ended: the pane will take input again. Closing
            # the lease here is what keeps "a turn is running" a fact about now rather than a fact
            # about the last delivery.
            self.activity.release(run.run_id)
            lease = None
        return ObserveReceipt(
            status=HEAD_OK,
            run=run,
            epoch=epoch,
            lease=lease,
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
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a drain names who requested it")
        self._draining.add(run.run_id)
        return DrainReceipt(
            status=HEAD_UNSUPPORTED,
            run=run,
            reason=DRAIN_UNSUPPORTED,
            draining=True,
            head_signalled=False,
            epoch=self.activity.epoch,
            lease=self.activity.lease(run.run_id),
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
        """End this head, through the operation that records who ended it before it acts."""
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
            # A stop that could not be confirmed leaves a head the caller still owns, in `finishing`
            # and carrying its initiator. That is the whole point of the distinction.
            return StopReceipt(
                status=HEAD_ALIVE,
                run=getattr(exc, "run", None) or run,
                reason=str(exc),
                failure=exc,
                epoch=self.activity.epoch,
                lease=self.activity.lease(run.run_id),
            )
        self._draining.discard(run.run_id)
        self.activity.forget(run.run_id)
        return StopReceipt(
            status=HEAD_OK, run=outcome.run, epoch=self.activity.observed(run.run_id)
        )

    def attach(self, run: HeadRun) -> AttachReceipt:
        """Orca has no stream to join. The pane's address travels instead, and says which it is."""
        return AttachReceipt(
            status=HEAD_UNSUPPORTED,
            run=run,
            reason=ATTACH_UNSUPPORTED,
            handle=run.handle,
            leaf=run.leaf,
            epoch=self.activity.epoch,
            lease=self.activity.lease(run.run_id),
        )

    # -- not a verb ---------------------------------------------------------------------------

    def stop_workspace(self, workspace: str) -> None:
        """Take down every pane of one worktree, which is the only stop Orca has at that scope.

        Not one of the six verbs and deliberately so: it names a container, not a head, and it makes
        no promise about any individual head's process. Callers that own heads in the workspace fence
        and confirm each of them around this call, exactly as they did when it was a bare session
        call. It lives on this object so that no pane call for a head's life is made outside it.
        """
        self.host.stop_workspace(workspace)


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
    run: HeadRun, reason: str, epoch: int, lease: Any, *, evidence: Any = None,
) -> ObserveReceipt:
    """An observation Orca could not make, said as that and not as an answer about the head."""
    return ObserveReceipt(
        status=HEAD_UNSUPPORTED,
        run=run,
        reason=reason,
        evidence=evidence,
        epoch=epoch,
        lease=lease,
        handle=run.handle,
        leaf=run.leaf,
    )


def _pane_for(panes: list[Pane], run: HeadRun) -> Pane | None:
    """This head's pane in a workspace inventory: by stable leaf, and by handle only without one."""
    if run.leaf:
        return next((pane for pane in panes if pane.leaf == run.leaf), None)
    return next((pane for pane in panes if run.handle and pane.handle == run.handle), None)
