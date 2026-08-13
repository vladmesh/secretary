"""The three operations a head's whole life is made of: `spawn`, `nudge`, `stop`.

The package docstring has promised these since `HeadSpec` landed. What they are is small — open a
pane and point the head at its task, point a running head at something new, end it — and what
matters is where they reach:

  * **they reach the session manager only through `SessionHost`.** There is no `subprocess` here and
    no `orca` argument vector; opening, addressing, re-finding and closing a pane are all methods on
    the host that is passed in. That is not tidiness: it is what makes a contract suite for these
    three operations runnable on a fake host with no Orca installed, which is the first piece of the
    backend-independent suite Milestone 5 needs. The verbs themselves were not invented here
    either — they moved out of the dispatcher's `_create_terminal` / `_split_pane` /
    terminal-inventory methods into `pane_host`, so there is one set of pane commands, not two;
  * **the pane is not the head.** `spawn` returns a `HeadRun` whose identity outlives the handle,
    and `stop` re-finds the pane by its stable leaf before closing anything, because a session
    manager that aliased a handle must not be able to make a stop close somebody else's pane;
  * **what to run is passed in, and how a prompt is delivered stays behind the host.** `command`
    is a string these operations never build: the head that is being spawned decided what it runs
    before anything was opened, and an operation that re-derived it could open a pane running
    something other than what its caller recorded. It comes from `command.render_head_command`,
    which is in this package but beside these operations rather than inside them — one renderer for
    the dispatcher, a tick and an operator shell alike, fed a registry profile as data by whoever
    owns the registry. Delivery and pane-closing semantics a product has of its own — the provider
    transcript that proves a turn started, a close whose refusal only matters when no heartbeat
    can be read — arrive as a `HeadTransport`, which is handed the host and reaches the session
    manager through it. That is deliberately not a callback the caller closes over its own backend
    runner with: an operation whose delivery went around the host would put the product's only
    real path back outside the seam these three exist to hold, while the contract suite went on
    passing on a fake;
  * **a failure says what it left running.** `HeadSpawnAborted` means a pane exists and may be
    holding a live head, so the caller keeps its launch intent; `HeadSpawnFailed` means nothing of
    the bring-up survived and the caller may treat it as a launch that did not happen. That
    distinction is the whole reason a bring-up failure is not one exception: the product has killed
    live heads by treating the first as the second.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..pane_host import Pane, SessionHost
from ..prompt_document import nudge_for
from ..tui_delivery import (
    DeliveryOutcome,
    READINESS_BLOCKED,
    READINESS_BUSY,
    TuiDeliveryError,
    deliver_interactive_prompt,
    terminal_readiness,
)
from .run import EXITED, HeadRun, StopInitiator, new_run_id
from .spec import HeadSpec
from .task_ref import TaskRef

# The caller's own proof that its head began a turn, as `tui_delivery` takes it. A criterion, not
# a transport: it is handed a send boundary and answers yes or no, and nothing about a session
# manager reaches it.
Confirm = Callable[[float], bool]
# Somewhere durable to put a run before the operation acts on it, for a caller whose record has to
# survive the operation dying half-way. Called with the run as it will be, never after the fact.
Commit = Callable[["HeadRun"], None]
# The product's launch-identity proof. It receives the exact run it would otherwise attribute to
# the stop, and therefore runs before that attribution is made durable.
# A launch preflight returns the exact run it attested.  Keeping that immutable value rather than
# accepting side metadata is what prevents a pane from being created under a different identity
# than the policy record persisted before it.
LaunchPreflight = Callable[["HeadRun"], "HeadRun"]


class HeadOperationError(RuntimeError):
    """Any refusal from one of the three operations, with the delivery evidence there was."""

    def __init__(
        self, message: str, *, evidence: Any = None, run: "HeadRun | None" = None,
    ) -> None:
        super().__init__(message)
        self.evidence = evidence
        # A delivery callback can durably refine the run after the pane was opened, before the
        # prompt is sent.  Failures after that boundary still have to hand the caller that exact
        # run, or a launch-intent recovery would overwrite the source it just persisted.
        self.run = run


class HeadSpawnFailed(HeadOperationError):
    """A bring-up that left nothing running.

    The caller may treat it as a launch that did not happen: block the work, drop the record, open
    a replacement. That is only safe because the pane was closed and the close was confirmed.
    """


class HeadSpawnAborted(HeadOperationError):
    """A bring-up whose pane exists and may be holding a live head.

    The run travels with it — identity, pane and pid heartbeat — because that is what the caller
    needs to either adopt or stop this head on a later tick. Blocking the work and forgetting the
    record here is how a live head ends up on a checkout nothing points at.
    """

    def __init__(self, message: str, *, run: HeadRun, evidence: Any = None) -> None:
        super().__init__(message, evidence=evidence, run=run)


class HeadPaneBusy(HeadOperationError):
    """The pane was working, or held in a dialog, and never took its prompt.

    Not a failed bring-up: it is a bring-up worth making again, and the caller defers rather than
    blocking the work. The pane it names has already been closed.
    """

    def __init__(self, message: str, *, readiness: str, pane: str, evidence: Any = None) -> None:
        super().__init__(message, evidence=evidence)
        self.readiness = readiness
        self.pane = pane


class HeadNudgeFailed(HeadOperationError):
    """A prompt into a live head that did not reach its confirmation."""


class HeadStopFailed(HeadOperationError):
    """A stop that could not be confirmed. The run travels in `finishing`, with its initiator."""

    def __init__(self, message: str, *, run: HeadRun) -> None:
        super().__init__(message)
        self.run = run


@dataclass(frozen=True)
class NudgePointer:
    """What a pane is sent: one bounded line, and the document it points at when there is one.

    A pointer at a document is the protocol's rule (`prompt_document`): the task never enters the
    terminal, only its path does, and the evidence then records the mode and the document rather
    than looking like a task that shrank to one line. A pointer without a document is the other
    legitimate case — "report now", "here is your continuation" — where the line *is* the message.
    """

    text: str
    document: str = ""

    @classmethod
    def at_document(cls, path: str, note: str = "") -> "NudgePointer":
        """The nudge for a task document already written to disk.

        `note` is the discriminating tail a caller may need in the delivered line itself — which
        round this document is, what inside it outranks what — for a head whose scrollback holds
        an earlier round's instructions and could otherwise act on them. It is built into the same
        line through `nudge_for`, so the path is still absolute and the ceiling is still checked
        over path and note together; a caller that assembled its own text beside the pointer would
        be delivering a line nothing validated, which is how a pointer stopped carrying its
        document at all (secretary-1413).
        """
        return cls(text=nudge_for(path, note), document=str(path))

    @classmethod
    def line(cls, text: str) -> "NudgePointer":
        """A short instruction that carries its own content."""
        return cls(text=text)


@dataclass(frozen=True)
class HeadOutcome:
    """What one operation leaves behind: the run as it now is, and what the pane answered."""

    run: HeadRun
    delivery: DeliveryOutcome | None = None


@dataclass(frozen=True)
class HeadDelivery:
    """The transport receipt together with the run authoritative after pre-send work.

    Delivery can persist a provider-source binding after the pane was created but immediately
    before the prompt is sent.  The operation cannot keep returning its earlier local copy in
    that case: every following launcher and launch intent must receive this handoff value.
    """

    run: HeadRun
    outcome: DeliveryOutcome


class HeadTransport(Protocol):
    """A product's own delivery and pane-close semantics, performed against the host it is given.

    The two verbs an operation cannot decide for a product: what counts as a prompt this head
    received, and what a refused close means over this head's heartbeat. Both are policy, and both
    are given the `SessionHost` the operation is running on rather than reaching a session manager
    of their own. That is the difference between a seam and a hole — an implementation that
    ignored `host` and called a backend directly would be exactly the bypass this shape exists to
    prevent, and the contract suite pins it by running the production transport against a fake.
    """

    def deliver(
        self, run: HeadRun, pointer: NudgePointer, *, host: SessionHost, subject: str
    ) -> HeadDelivery:
        """Put one prompt into this head's pane and return its post-delivery run."""

    def close(self, run: HeadRun, *, host: SessionHost) -> None:
        """Close this head's pane, or refuse in whatever terms this product's evidence allows."""


@dataclass(frozen=True)
class HostTransport:
    """Delivery and close as a session manager alone can promise them.

    The default, and what a caller with no confirmation criterion of its own gets: the shared
    interactive delivery path through the host, and a plain pane close. `confirm` is the caller's
    proof that a turn started when it has one; without it the send is acknowledged out of band,
    because the last thing this package can observe by itself is the pane having gone to work.
    """

    confirm: Confirm | None = None

    def deliver(
        self, run: HeadRun, pointer: NudgePointer, *, host: SessionHost, subject: str
    ) -> HeadDelivery:
        return HeadDelivery(
            run=run,
            outcome=deliver_interactive_prompt(
                run.handle,
                pointer.text,
                host=host,
                adapter=run.spec.adapter,
                confirm=self.confirm,
                # Pretending to a confirmation it did not make would be the weaker report, not the
                # stronger one.
                ack_out_of_band=self.confirm is None,
                subject=subject,
                document_path=pointer.document,
            ),
        )

    def close(self, run: HeadRun, *, host: SessionHost) -> None:
        host.close_pane(run.handle)


def spawn(
    spec: HeadSpec,
    workspace: str,
    task_ref: TaskRef,
    *,
    host: SessionHost,
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
) -> HeadOutcome:
    """Bring one head up in its own pane and, when a pointer is given, point it at its task.

    `command` is what the pane runs, and it is decided before this is called — by
    `command.render_head_command`, from the profile the caller resolved. This operation renders
    nothing itself: a bring-up that re-derived its own command could open a pane running something
    other than what its caller recorded, which is the second opinion `HeadSpec` exists to prevent.

    `transport` is how this product delivers a prompt and how its cleanup proves a pane it opened
    left no head behind — a pid heartbeat, in the production dispatcher. It performs both through
    the same host this operation was given; without one the pane is written into and closed through
    the host directly, which is all a session manager can promise on its own.
    """
    transport = transport or HostTransport()
    if run is None:
        run = HeadRun(
            run_id=run_id or new_run_id(),
            spec=spec,
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            pid_file=pid_file,
        )
    elif (
        run.spec.profile_id != spec.profile_id
        or run.spec.adapter != spec.adapter
        or run.workspace != workspace
        or run.task_ref != task_ref
        or (run_id and run.run_id != run_id)
        or (pid_file and run.pid_file != pid_file)
        or (role and run.role != role)
    ):
        raise HeadSpawnFailed("launch preflight run does not match the pane being opened")
    if preflight is not None:
        run = preflight(run)
    if commit is not None:
        # A policy attestation must hit durable state before the host is asked to create anything.
        # This is also useful to the old launch-intent callers, which commit the still-handleless
        # run before a process can write its heartbeat.
        commit(run)
    pane = _open_pane(
        host, workspace, title, command, split_from=split_from, transport=transport, run=run,
    )
    run = run.rebound(pane.handle, leaf=pane.leaf)
    if commit is not None:
        commit(run)
    if pointer is None:
        # A head whose prompt went on its own command line has already been given its task; there
        # is nothing to deliver and nothing to confirm. Which of the two shapes this head is in is
        # the caller's to say, because it is the rendered command that decides it, not the spec:
        # a raw command override runs an adapter's binary in a shape the profile never described.
        return HeadOutcome(run)
    try:
        delivery = _deliver(transport, host, run, pointer, subject=subject or "head-launch")
    except Exception as exc:  # noqa: BLE001 — classified by what it left running, not by type
        raise _spawn_delivery_failure(host, run, pointer, exc, transport=transport) from None
    return HeadOutcome(delivery.run.working(), delivery.outcome)


def nudge(
    run: HeadRun,
    pointer: NudgePointer,
    *,
    host: SessionHost,
    transport: HeadTransport | None = None,
    subject: str = "",
) -> HeadOutcome:
    """Point a running head at something: its task document, or one line of instruction.

    The pane is re-found by the run's own leaf first, so a head whose handle the session manager
    aliased is nudged rather than reported missing. A run that has been asked to stop is refused:
    handing more work to a head somebody is ending is how a stop turns into a race.
    """
    if not run.running:
        raise HeadNudgeFailed(f"a head in {run.lifecycle} is not nudged")
    live = _relocated(host, run)
    if not live.handle:
        raise HeadNudgeFailed("the head's pane can no longer be addressed")
    delivery = _deliver(
        transport or HostTransport(), host, live, pointer, subject=subject or "head-nudge"
    )
    return HeadOutcome(delivery.run.working(), delivery.outcome)


def stop(
    run: HeadRun,
    initiator: StopInitiator,
    *,
    host: SessionHost,
    transport: HeadTransport | None = None,
    commit: Commit | None = None,
    preflight: Preflight | None = None,
    confirm_gone: Callable[[str], None] | None = None,
) -> HeadOutcome:
    """End one head and record who ended it.

    The initiator is positional and typed, so there is no way to call this without one — the record
    of a stopped head names its initiator by construction rather than by a check inside a body that
    a later caller could route around.

    The order is the invariant, and this is the one place that holds it: the run enters `finishing`
    with its initiator and is handed to `commit` *before* a single host call is made. A stop is the
    operation most likely to be interrupted — the pane refuses, the heartbeat will not confirm, the
    dispatcher is restarted mid-way — and every one of those interruptions has to leave behind a
    durable run that says which head is being ended and by whom. A caller that commits afterwards
    is a caller whose crash loses both, and whose next tick then opens a second stop of a head it
    can no longer name. `finishing` is idempotent by initiator, so a retry through another path
    re-commits the same run rather than renaming the actor that began this.

    The pane is re-found by leaf before it is closed: closing a stale handle is closing whatever
    the session manager has since put there. A run with neither pane nor heartbeat cannot be
    promised gone, and says so.
    """
    if run.lifecycle == EXITED:
        return HeadOutcome(run)
    # A readable mismatch is not a stop attempt of this run.  In particular it must not create a
    # durable ``finishing``/``stopped_by`` attribution before the product has proved the process
    # behind the pid file is the expected HeadRun.
    if preflight is not None:
        try:
            preflight(run)
        except Exception as exc:  # noqa: BLE001
            raise HeadStopFailed(f"the head failed its stop identity fence: {exc}", run=run) from None
    live = run.finishing(initiator)
    if commit is not None:
        commit(live)
    transport = transport or HostTransport()
    handle = live.handle
    if live.leaf and live.workspace:
        # The inventory read is part of the proof here, unlike the lenient relocation a nudge does:
        # an inventory that cannot be read must not pass for a pane that is gone, or the next tick
        # puts a replacement beside a head this stop failed to address.
        try:
            handle = _handle_for_leaf(host, live.workspace, live.leaf)
        except Exception as exc:  # noqa: BLE001
            raise HeadStopFailed(
                f"the head's pane could not be located before the stop: {exc}", run=live
            ) from None
        live = live.rebound(handle, leaf=live.leaf) if handle else live
    elif not handle and not live.pid_file:
        raise HeadStopFailed(
            "the head has neither a pane handle nor a pid heartbeat (and no pane leaf), so "
            "nothing here can promise it is gone",
            run=live,
        )
    if handle:
        try:
            transport.close(live, host=host)
        except Exception as exc:  # noqa: BLE001 — whatever the host called its refusal
            raise HeadStopFailed(f"the head's pane would not close: {exc}", run=live) from None
    if confirm_gone is not None:
        try:
            confirm_gone(live.pid_file)
        except Exception as exc:  # noqa: BLE001
            raise HeadStopFailed(f"the head was not confirmed gone: {exc}", run=live) from None
    return HeadOutcome(live.exited())


def _open_pane(
    host: SessionHost,
    workspace: str,
    title: str,
    command: str,
    *,
    split_from: str,
    transport: HeadTransport,
    run: HeadRun,
) -> Pane:
    """The pane this head will live in, split off a sibling when the caller named one.

    A split is labelled afterwards because the session manager's split takes no title, and a label
    that will not stick is a failed bring-up rather than an unlabelled pane: an operator has no
    other way to tell two panes of one worktree apart. The head is already running by then, so the
    cleanup decides which kind of failure it is — see `_cleanup`.
    """
    if not split_from:
        return host.open_pane(workspace, title, command)
    pane = host.split_pane(split_from, command)
    if not pane.leaf:
        # A split reply that names the pane by handle alone can still be found in a fresh
        # inventory, which yields the same stable leaf `open_pane` returns directly. A new head is
        # never left leafless: that is only for records written before pane identity existed.
        try:
            pane = Pane(handle=pane.handle, leaf=_leaf_for(host, workspace, pane.handle))
        except Exception as exc:  # noqa: BLE001
            raise _cleanup(host, run.rebound(pane.handle), transport, str(exc)) from None
        if not pane.leaf:
            raise _cleanup(
                host, run.rebound(pane.handle), transport,
                "the session manager exposed no stable leaf for the split pane",
            )
    try:
        host.rename_pane(pane.handle, title)
    except Exception as exc:  # noqa: BLE001
        raise _cleanup(
            host, run.rebound(pane.handle, leaf=pane.leaf), transport, str(exc)
        ) from None
    return pane


def _cleanup(
    host: SessionHost,
    run: HeadRun,
    transport: HeadTransport,
    reason: str,
    *,
    evidence: Any = None,
) -> HeadOperationError:
    """Close a pane this bring-up opened, and say which kind of failure that leaves.

    A close the caller can confirm leaves nothing of the bring-up: the failure is ordinary and the
    caller may act as though no head exists. A close that is refused says nothing about the head,
    so it goes back as `HeadSpawnAborted` carrying the pane — the caller keeps its intent and the
    next tick settles that head instead of opening a second one beside it.
    """
    try:
        transport.close(run, host=host)
    except Exception as exc:  # noqa: BLE001
        return HeadSpawnAborted(f"{reason}; the head's pane would not close: {exc}", run=run,
                                evidence=evidence)
    return HeadSpawnFailed(reason, evidence=evidence)


def _spawn_delivery_failure(
    host: SessionHost,
    run: HeadRun,
    pointer: NudgePointer,
    exc: Exception,
    *,
    transport: HeadTransport,
) -> HeadOperationError:
    """What an unconfirmed launch prompt means, which depends on what was sent.

    A *nudge at a document* that was not confirmed says nothing about the head: the line is short
    enough that no provider has been seen to lose one, the task is on disk either way, and the
    classification that would decide otherwise is the one that killed six live Claude heads in
    eight minutes on the worker path (2026-08-11). So the pane is not closed over it — the bring-up
    hands it back as the ambiguity it is.

    A prompt that carried its own content is different: without it the head has nothing at all, so
    the pane's state is asked before anything is closed, because afterwards there is nothing left to
    ask. A pane that is working or held in a dialog is a bring-up worth making again; a pane nothing
    can ask about is not a busy pane, it is a pane nothing can wait for, and it takes the ordinary
    failure path.
    """
    # `HeadDelivery` makes a successful pre-send mutation authoritative on the success path.
    # A later transport refusal takes the same handoff: an aborted launch must retain the bound
    # source for recovery rather than returning the pre-delivery local copy.
    post_delivery = getattr(exc, "run", None)
    if isinstance(post_delivery, HeadRun):
        run = post_delivery
    evidence = getattr(exc, "evidence", None)
    if pointer.document:
        return HeadSpawnAborted(
            f"the launch nudge was not confirmed delivered, and the pane may have taken it "
            f"anyway: {exc}",
            run=run,
            evidence=evidence,
        )
    readiness = terminal_readiness(run.handle, host=host)
    failure = _cleanup(host, run, transport, str(exc), evidence=evidence)
    if isinstance(failure, HeadSpawnAborted):
        return failure
    if readiness in (READINESS_BUSY, READINESS_BLOCKED):
        return HeadPaneBusy(
            f"the head pane was {readiness} and never took its launch prompt: {exc}",
            readiness=readiness,
            pane=run.handle,
            evidence=evidence,
        )
    return failure


def _deliver(
    transport: HeadTransport,
    host: SessionHost,
    run: HeadRun,
    pointer: NudgePointer,
    *,
    subject: str,
) -> HeadDelivery:
    """One delivery into this head's pane, performed by the transport against this host.

    A delivery boundary failure is re-raised as this package's own, evidence and all, whichever
    transport produced it: what the caller reads is the same refusal whether the product's
    confirmation criterion or the plain host path made it.
    """
    try:
        delivery = transport.deliver(run, pointer, host=host, subject=subject)
    except TuiDeliveryError as exc:
        raise HeadNudgeFailed(
            str(exc), evidence=getattr(exc, "evidence", None), run=getattr(exc, "head_run", run),
        ) from None
    return HeadDelivery(post_delivery_run(run, delivery.run), delivery.outcome)


def post_delivery_run(before: HeadRun, after: HeadRun) -> HeadRun:
    """Merge the exact pre-send handoff with pane facts this operation has proved.

    A provider callback owns its newly persisted source.  `spawn` and `nudge` own only the pane
    address they just used and the lifecycle transition they can prove.  Keeping those writers
    separate makes a stale launch result unable to erase a newer source binding.
    """
    if not isinstance(after, HeadRun) or not before.same_run(after):
        raise HeadNudgeFailed("post-delivery HeadRun does not match the launched head", run=before)
    if (
        before.spec != after.spec
        or before.workspace != after.workspace
        or before.task_ref != after.task_ref
        or before.role != after.role
        or before.pid_file != after.pid_file
    ):
        raise HeadNudgeFailed("post-delivery HeadRun changed its launch identity", run=before)
    if after.lifecycle != before.lifecycle or after.stopped_by != before.stopped_by:
        raise HeadNudgeFailed("pre-send delivery callback changed HeadRun lifecycle", run=before)
    # The callback receives the rebound run in the normal launch path.  Reapplying these exact
    # operation-owned facts also covers a retained callback that read the same run just before
    # the session manager supplied its stable leaf; it cannot change any provider source field.
    return after.rebound(before.handle, leaf=before.leaf)


def _relocated(host: SessionHost, run: HeadRun) -> HeadRun:
    """The run addressed at the pane it is in now.

    The leaf is the stable token; the handle is not. An inventory that cannot be read leaves the
    run exactly as it was rather than blanking a handle a caller could still have used: an
    unreadable inventory is not evidence that a pane is gone.
    """
    if not run.leaf or not run.workspace:
        return run
    try:
        current = _handle_for_leaf(host, run.workspace, run.leaf)
    except Exception:  # noqa: BLE001
        return run
    if not current or current == run.handle:
        return run
    return run.rebound(current, leaf=run.leaf)


def _handle_for_leaf(host: SessionHost, workspace: str, leaf: str) -> str:
    for pane in host.panes(workspace):
        if pane.leaf == leaf:
            return pane.handle
    return ""


def _leaf_for(host: SessionHost, workspace: str, handle: str) -> str:
    for pane in host.panes(workspace):
        if pane.handle == handle:
            return pane.leaf
    return ""
