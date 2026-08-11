"""Shared dispatcher exceptions and selectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DispatcherError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


class HostError(Exception):
    pass


class GateTransportError(HostError):
    """The gate could not reach its backend, so no verdict was received at all.

    A question that never got an answer is not a red gate (secretary-1164). A TLS handshake
    timeout, a DNS failure, a dropped connection or a 5xx from GitHub itself says nothing about
    the code under validation, and treating the absence of an answer as a negative one blocked a
    card whose required check was in fact green. The dispatcher keeps such a card exactly where it
    is and asks again on the next tick, bounded, instead of deciding on silence.
    """


class HeadLaunchAborted(HostError):
    """A worker or reviewer bring-up that failed after its terminal was already created.

    The same ambiguity `ObserverLaunchAborted` covers for the observer, and it is answered the same
    way. The bring-up did not finish, but something of it may still be running, so the failure
    carries the pane it opened and the heartbeat that head writes. The caller keeps the launch
    intent instead of blocking the card on it: the next tick reads the heartbeat and either adopts
    the head or stops what is left of it. Clearing the intent and dropping the record here would
    leave a live head with nothing pointing at it, which is the second head this contour exists to
    prevent.
    """

    def __init__(
        self,
        message: str,
        *,
        handle: str = "",
        leaf: str = "",
        workspace: str = "",
        pid_file: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.leaf = leaf
        self.workspace = workspace
        self.pid_file = pid_file
        # What the shared delivery boundary saw, when this bring-up failed delivering a prompt.
        # It travels with the ambiguity rather than being read off a pane nobody may touch: the
        # caller persists it before it decides anything about the head that may still be running.
        self.evidence = dict(evidence or {})


class HeadPaneNotReady(HostError):
    """A bring-up that left nothing running because its head pane would not take the prompt.

    Orca answers the readiness question in three states, and two of them are this one: a pane that
    is working, and a pane held in a dialog its head cannot leave on its own — the codex update
    prompt that ate two bring-ups in 33 minutes on `sprint:1200` (secretary-1163). Neither is a
    failed round. The head never received its prompt and the pane was closed behind it, so the same
    launch is worth making again on the next tick, the way the observer's lifecycle already defers
    its own.

    The state travels with the failure because it is what the card is eventually blocked over: an
    operator reading "bring-up failed" goes looking for a broken head or a broken host, and the
    answer is a dialog nobody answered.
    """

    def __init__(
        self,
        message: str,
        *,
        readiness: str,
        pane: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.readiness = readiness
        self.pane = pane
        # What the shared delivery boundary saw of the prompt this pane would not take. The pane
        # is closed behind this failure, so nothing can be asked of it afterwards: whatever is not
        # carried here is gone, and the caller's durable telemetry is the only place left to put it.
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class ReviewLaunch:
    """What a reviewer bring-up hands back to the runtime: the pane the reviewer runs in and the
    commit its checkout was pinned at once the worker head was shut down."""

    handle: str
    leaf: str = ""
    commit: str = ""
    # The launch configuration of the reviewer head this bring-up started, snapshotted by the
    # launcher itself (secretary-716). The runtime writes it to the routing journal as-is.
    run: dict[str, Any] = field(default_factory=dict)
    # The reviewer's own head run, as the three head operations keep it (secretary-1414). Distinct
    # from `run` above, which is the routing snapshot of the configuration this head launched with:
    # this is the state of the head itself — the identity a later stop addresses, and the lifecycle
    # that stop moves. The caller writes it onto the record, which is where it becomes durable.
    head_run: dict[str, Any] = field(default_factory=dict)
    delivery_evidence: dict[str, Any] = field(default_factory=dict)


def review_pane_label(reference: str) -> str:
    """Stable human-readable label for the reviewer pane. Carries the card reference and the role
    so an operator can tell the two panes of one worktree apart in the Orca client. Lifecycle
    checks key off the persisted handle, not this label: a head overwrites the terminal title with
    its own OSC sequence seconds after launch, and a title-only check would then read the reviewer
    as gone (or as the worker)."""
    return f"{reference} reviewer"


# Who a head's stop was initiated by, as the head run records it (secretary-1412). These live here
# rather than beside the runtime because every module that performs a stop has to name one, and the
# runtime imports those modules. A stop with no initiator is impossible at the operation's
# signature; naming them here is what keeps the names from drifting per call site.
STOPPED_BY_DISPATCHER = "dispatcher"
STOPPED_BY_REVIEW_FREEZE = "review-freeze"
STOPPED_BY_REPLACEMENT = "replacement"
STOPPED_BY_OPERATOR = "operator"
STOPPED_BY_RECONCILIATION = "reconciliation"
STOPPED_BY_LAUNCH_RECOVERY = "launch-recovery"
# The reviewer's round is over: a red verdict handing the checkout back, a green one parking it,
# a parked round released for rework. All of them are the verdict ending that head, which is the
# distinction an operator reads this field for — a reviewer that finished is not a reviewer that
# was killed (secretary-1414).
STOPPED_BY_REVIEW_VERDICT = "review-verdict"
# The wait watchdog ending a head that stopped answering: the respawn of a silent reviewer and the
# escalation that follows the second stall. The head may well still be running, which is exactly
# why the record has to name who decided it should not be.
STOPPED_BY_WATCHDOG = "watchdog"
