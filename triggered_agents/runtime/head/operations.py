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
  * **what to run and how to confirm a delivery stay with the caller.** `command` is rendered by
    whoever owns the registry (`render_command` is untouched by this package), and `confirm` is the
    criterion the caller already has for "this head's turn started". Injecting both is what keeps
    the operations free of the dispatcher, rather than the dispatcher free of them;
  * **a failure says what it left running.** `HeadSpawnAborted` means a pane exists and may be
    holding a live head, so the caller keeps its launch intent; `HeadSpawnFailed` means nothing of
    the bring-up survived and the caller may treat it as a launch that did not happen. That
    distinction is the whole reason a bring-up failure is not one exception: the product has killed
    live heads by treating the first as the second.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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

# What one delivery into a live pane looks like from here: the text, and what it is a pointer at.
Deliver = Callable[..., DeliveryOutcome]
# The caller's own proof that its head began a turn, as `tui_delivery` takes it.
Confirm = Callable[[float], bool]


class HeadOperationError(RuntimeError):
    """Any refusal from one of the three operations, with the delivery evidence there was."""

    def __init__(self, message: str, *, evidence: Any = None) -> None:
        super().__init__(message)
        self.evidence = evidence


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
        super().__init__(message, evidence=evidence)
        self.run = run


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
    def at_document(cls, path: str) -> "NudgePointer":
        """The nudge for a task document already written to disk."""
        return cls(text=nudge_for(path), document=str(path))

    @classmethod
    def line(cls, text: str) -> "NudgePointer":
        """A short instruction that carries its own content."""
        return cls(text=text)


@dataclass(frozen=True)
class HeadOutcome:
    """What one operation leaves behind: the run as it now is, and what the pane answered."""

    run: HeadRun
    delivery: DeliveryOutcome | None = None


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
    deliver: Deliver | None = None,
    confirm: Confirm | None = None,
    close: Callable[[str, str], None] | None = None,
    subject: str = "",
) -> HeadOutcome:
    """Bring one head up in its own pane and, when a pointer is given, point it at its task.

    `command` is what the pane runs; this package renders nothing, because what a Claude or a Codex
    invocation looks like is the registry's business and re-deriving it here would be the second
    opinion `HeadSpec` exists to prevent.

    `close` is how the caller's own cleanup proves a pane it opened left no head behind — a pid
    heartbeat, in the production dispatcher. Without one the pane is simply closed through the host,
    which is all a session manager can promise on its own.
    """
    run = HeadRun(
        run_id=run_id or new_run_id(),
        spec=spec,
        workspace=workspace,
        task_ref=task_ref,
        pid_file=pid_file,
    )
    pane = _open_pane(
        host, workspace, title, command, split_from=split_from, pid_file=pid_file, close=close,
        run=run,
    )
    run = run.rebound(pane.handle, leaf=pane.leaf)
    if pointer is None:
        # A head whose prompt went on its own command line has already been given its task; there
        # is nothing to deliver and nothing to confirm. Which of the two shapes this head is in is
        # the caller's to say, because it is the rendered command that decides it, not the spec:
        # a raw command override runs an adapter's binary in a shape the profile never described.
        return HeadOutcome(run)
    try:
        outcome = _deliver(
            deliver, host, run, pointer, confirm=confirm, subject=subject or "head-launch"
        )
    except Exception as exc:  # noqa: BLE001 — classified by what it left running, not by type
        raise _spawn_delivery_failure(host, run, pointer, exc, close=close) from None
    return HeadOutcome(run.working(), outcome)


def nudge(
    run: HeadRun,
    pointer: NudgePointer,
    *,
    host: SessionHost,
    deliver: Deliver | None = None,
    confirm: Confirm | None = None,
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
    outcome = _deliver(
        deliver, host, live, pointer, confirm=confirm, subject=subject or "head-nudge"
    )
    return HeadOutcome(live.working(), outcome)


def stop(
    run: HeadRun,
    initiator: StopInitiator,
    *,
    host: SessionHost,
    close: Callable[[str, str], None] | None = None,
    confirm_gone: Callable[[str], None] | None = None,
) -> HeadOutcome:
    """End one head and record who ended it.

    The initiator is positional and typed, so there is no way to call this without one — the record
    of a stopped head names its initiator by construction rather than by a check inside a body that
    a later caller could route around. It is written onto the run *before* the pane is touched, so
    a stop that is refused, or one whose dispatcher dies mid-way, still leaves the run saying who
    was ending this head.

    The pane is re-found by leaf before it is closed: closing a stale handle is closing whatever
    the session manager has since put there. A run with neither pane nor heartbeat cannot be
    promised gone, and says so.
    """
    live = run.finishing(initiator)
    if live.lifecycle == EXITED:
        return HeadOutcome(live)
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
            (close or _host_close(host))(handle, live.pid_file)
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
    pid_file: str,
    close: Callable[[str, str], None] | None,
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
            raise _cleanup(host, run.rebound(pane.handle), pid_file, close, str(exc)) from None
        if not pane.leaf:
            raise _cleanup(
                host, run.rebound(pane.handle), pid_file, close,
                "the session manager exposed no stable leaf for the split pane",
            )
    try:
        host.rename_pane(pane.handle, title)
    except Exception as exc:  # noqa: BLE001
        raise _cleanup(
            host, run.rebound(pane.handle, leaf=pane.leaf), pid_file, close, str(exc)
        ) from None
    return pane


def _cleanup(
    host: SessionHost,
    run: HeadRun,
    pid_file: str,
    close: Callable[[str, str], None] | None,
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
        (close or _host_close(host))(run.handle, pid_file)
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
    close: Callable[[str, str], None] | None,
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
    evidence = getattr(exc, "evidence", None)
    if pointer.document:
        return HeadSpawnAborted(
            f"the launch nudge was not confirmed delivered, and the pane may have taken it "
            f"anyway: {exc}",
            run=run,
            evidence=evidence,
        )
    readiness = terminal_readiness(run.handle, host=host)
    failure = _cleanup(host, run, run.pid_file, close, str(exc), evidence=evidence)
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
    deliver: Deliver | None,
    host: SessionHost,
    run: HeadRun,
    pointer: NudgePointer,
    *,
    confirm: Confirm | None,
    subject: str,
) -> DeliveryOutcome:
    """One delivery into this head's pane, through the caller's transport or the host's own."""
    if deliver is not None:
        return deliver(
            run.handle,
            pointer.text,
            adapter=run.spec.adapter,
            document_path=pointer.document,
            subject=subject,
        )
    try:
        return deliver_interactive_prompt(
            run.handle,
            pointer.text,
            host=host,
            adapter=run.spec.adapter,
            confirm=confirm,
            # Without a caller's criterion the delivery is acknowledged out of band: the pane
            # having gone to work is the last thing this package can observe by itself, and
            # pretending to a confirmation it did not make would be the weaker report, not the
            # stronger one.
            ack_out_of_band=confirm is None,
            subject=subject,
            document_path=pointer.document,
        )
    except TuiDeliveryError as exc:
        raise HeadNudgeFailed(str(exc), evidence=getattr(exc, "evidence", None)) from None


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


def _host_close(host: SessionHost) -> Callable[[str, str], None]:
    def close(handle: str, _pid_file: str) -> None:
        host.close_pane(handle)

    return close
