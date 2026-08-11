"""The head: the thing a pipeline run is actually carried out by, as a type rather than a dict.

A head is one agent session — a Codex TUI, a Claude terminal — that the product brings up, points
at a task document, and eventually stops. Three operations describe its whole life, and they live
here next to the types they all take: `spawn(spec, workspace, task_ref)`, `nudge(run, pointer)` and
`stop(run, initiator)`. `HeadSpec` is the head they act on, `HeadRun` is the one that is running,
and `TaskRef` is what it was pointed at — a card, a sprint entity or a role's standing instruction,
because not every head serves a Pipeline card.

The neighbours are the other two halves of the same boundary: `prompt_document` owns what a head is
given, `pane_host` owns the pane it runs in — including, since these operations landed, the verbs
that open and close one. This package owns which head that is, and what happens to it.
"""
from __future__ import annotations

from .operations import (
    Deliver,
    HeadNudgeFailed,
    HeadOperationError,
    HeadOutcome,
    HeadPaneBusy,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadStopFailed,
    NudgePointer,
    nudge,
    spawn,
    stop,
)
from .run import (
    EXITED,
    FINISHING,
    LIFECYCLE,
    SPAWNED,
    WORKING,
    HeadRun,
    HeadRunError,
    StopInitiator,
    new_run_id,
)
from .spec import DEFAULT_EFFORT, HeadSpec, HeadSpecError, head_spec, load_head_specs
from .task_ref import (
    TASK_CARD,
    TASK_KINDS,
    TASK_SPRINT,
    TASK_STANDING,
    TaskRef,
    TaskRefError,
)

__all__ = [
    "DEFAULT_EFFORT",
    "Deliver",
    "EXITED",
    "FINISHING",
    "HeadNudgeFailed",
    "HeadOperationError",
    "HeadOutcome",
    "HeadPaneBusy",
    "HeadRun",
    "HeadRunError",
    "HeadSpawnAborted",
    "HeadSpawnFailed",
    "HeadSpec",
    "HeadSpecError",
    "HeadStopFailed",
    "LIFECYCLE",
    "NudgePointer",
    "SPAWNED",
    "StopInitiator",
    "TASK_CARD",
    "TASK_KINDS",
    "TASK_SPRINT",
    "TASK_STANDING",
    "TaskRef",
    "TaskRefError",
    "WORKING",
    "head_spec",
    "load_head_specs",
    "new_run_id",
    "nudge",
    "spawn",
    "stop",
]
