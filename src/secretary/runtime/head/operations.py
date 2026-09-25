"""What a head operation answers with: its refusals, the pointer it delivers, and the run it hands back.

`spawn`, `nudge` and `stop` are verbs of the head backend (`HeadRuntime`, implemented by
`local_pty_head`). What stays here is what every backend and every caller shares about them:

  * **a failure says what it left running.** `HeadSpawnAborted` means a head may still be alive,
    so the caller keeps its launch intent; `HeadSpawnFailed` means nothing of the bring-up survived.
    Treating the first as the second is how live heads get killed;
  * **a head is pointed at a document, not handed its text.** `NudgePointer` is the one bounded
    line a head is sent (`prompt_document`);
  * **a delivery cannot rewrite a launch.** `post_delivery_run` is the one merge of the run a
    pre-send callback persisted with the address facts the operation itself proved.

The Orca pane versions of the three verbs, and the session-manager seam they reached through, were
removed in secretary-1725.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from secretary.runtime.prompt_document import nudge_for

# `HeadSpec`, `TaskRef` and `StopInitiator` are imported for the callers that name the head types
# through this module (`dispatch.launch` and the dispatcher fixtures).
from .run import HeadRun, StopInitiator
from .spec import HeadSpec
from .task_ref import TaskRef

__all__ = [
    "HeadNudgeFailed",
    "HeadOperationError",
    "HeadRun",
    "HeadSpawnAborted",
    "HeadSpawnFailed",
    "HeadSpec",
    "HeadStopFailed",
    "NudgePointer",
    "StopInitiator",
    "TaskRef",
    "post_delivery_run",
]


class HeadOperationError(RuntimeError):
    """Any refusal from one of the three operations, with the delivery evidence there was."""

    def __init__(
        self,
        message: str,
        *,
        evidence: Any = None,
        run: HeadRun | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence = evidence
        # A delivery callback can durably refine the run after the head was started, before the
        # prompt is sent.  Failures after that boundary still have to hand the caller that exact
        # run, or a launch-intent recovery would overwrite the source it just persisted.
        self.run = run


class HeadSpawnFailed(HeadOperationError):
    """A bring-up that left nothing running.

    The caller may treat it as a launch that did not happen. That is only safe because whatever
    the bring-up started was confirmed gone.
    """


class HeadSpawnAborted(HeadOperationError):
    """A bring-up that may have left a live head behind.

    The run travels with it — identity, address and pid heartbeat — because that is what the caller
    needs to either adopt or stop this head on a later tick.
    """

    def __init__(self, message: str, *, run: HeadRun, evidence: Any = None) -> None:
        super().__init__(message, evidence=evidence, run=run)


class HeadNudgeFailed(HeadOperationError):
    """A prompt into a live head that did not reach its confirmation."""


class HeadStopFailed(HeadOperationError):
    """A stop that could not be confirmed. The run travels in `finishing`, with its initiator."""

    def __init__(self, message: str, *, run: HeadRun) -> None:
        super().__init__(message)
        self.run = run


@dataclass(frozen=True)
class NudgePointer:
    """What a head is sent: one bounded line, and the document it points at when there is one.

    A pointer at a document is the protocol's rule (`prompt_document`): the task never enters the
    terminal, only its path does. A pointer without a document is the other legitimate case, where
    the line *is* the message.
    """

    text: str
    document: str = ""

    @classmethod
    def at_document(cls, path: str, note: str = "") -> NudgePointer:
        """The nudge for a task document already written to disk.

        `note` is the discriminating tail a caller may need in the delivered line itself, for a head
        whose scrollback holds an earlier round's instructions. It is built into the same line through
        `nudge_for`, so the path stays absolute and the ceiling is checked over path and note together;
        a caller assembling its own text beside the pointer would deliver a line nothing validated.
        """
        return cls(text=nudge_for(path, note), document=str(path))

    @classmethod
    def line(cls, text: str) -> NudgePointer:
        """A short instruction that carries its own content."""
        return cls(text=text)


def post_delivery_run(before: HeadRun, after: HeadRun) -> HeadRun:
    """Merge the exact pre-send handoff with address facts this operation has proved.

    A provider callback owns its newly persisted source; `spawn` and `nudge` own only the
    address they just used and the lifecycle transition they can prove. Keeping those writers
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
    # the backend supplied its stable leaf; it cannot change any provider source field.
    return after.rebound(before.handle, leaf=before.leaf)
