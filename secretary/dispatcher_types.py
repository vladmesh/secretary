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
        workspace: str = "",
        pid_file: str = "",
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.workspace = workspace
        self.pid_file = pid_file


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


def review_pane_label(reference: str) -> str:
    """Stable human-readable label for the reviewer pane. Carries the card reference and the role
    so an operator can tell the two panes of one worktree apart in the Orca client. Lifecycle
    checks key off the persisted handle, not this label: a head overwrites the terminal title with
    its own OSC sequence seconds after launch, and a title-only check would then read the reviewer
    as gone (or as the worker)."""
    return f"{reference} reviewer"


def legacy_review_pane_label(reference: str) -> str:
    """Pre-651 reviewer title. Still matched when re-finding an orphaned pane so a card that was
    already in review when the dispatcher upgraded does not get a second reviewer."""
    return f"{reference} review"
