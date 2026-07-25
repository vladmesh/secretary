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


@dataclass(frozen=True)
class ReviewLaunch:
    """What a reviewer bring-up hands back to the runtime: the pane the reviewer runs in and the
    commit its checkout was pinned at once the worker head was shut down."""

    handle: str
    leaf: str = ""
    commit: str = ""
    # The reviewer profile that actually went up (a fallback makes it differ from the head the card
    # was claimed with) and its launch configuration, snapshotted by the launcher.
    head: str = ""
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


@dataclass(frozen=True)
class PilotSelector:
    reference: str

    @classmethod
    def exact(cls, reference: str | None) -> "PilotSelector":
        value = (reference or "").strip()
        if not value:
            raise DispatcherError("pilot_selector_required", "dispatcher requires an exact pilot ref")
        return cls(value)

    def accepts(self, task: dict[str, Any]) -> bool:
        return task.get("ref") == self.reference
