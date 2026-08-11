"""The head: the thing a pipeline run is actually carried out by, as a type rather than a dict.

A head is one agent session — a Codex TUI, a Claude terminal — that the product brings up, points
at a task document, and eventually stops. Three operations describe its whole life, and they live
here next to the types they all take: `spawn(spec, workspace, task_ref)`, `nudge(run, pointer)` and
`stop(run, initiator)`. `HeadSpec` is the head they act on, `HeadRun` is the one that is running,
and `TaskRef` is what it was pointed at — a card, a sprint entity or a role's standing instruction,
because not every head serves a Pipeline card.

`command` is the fourth thing they all need and the one that used to live everywhere else: the
shell command a head's pane runs, rendered once here from a registry profile the caller hands over
as data.

The neighbours are the other two halves of the same boundary: `prompt_document` owns what a head is
given, `pane_host` owns the pane it runs in — including, since these operations landed, the verbs
that open and close one. This package owns which head that is, what it runs, and what happens to it.
"""
from __future__ import annotations

from .command import (
    CLAUDE_EFFORTS,
    CODEX_EFFORTS,
    CODEX_LAUNCH_MODES,
    CODEX_TUI_MODE,
    PROMPT_AFTER_START_ADAPTERS,
    PYTHON_SAFE_PATH_FLAG,
    RUNTIME_ROLE_ENV,
    SECRETARY_ROLE_ENV,
    HeadCommand,
    HeadCommandError,
    render_head_command,
    validate_launch_shape,
    with_pid_heartbeat,
    wrap_role_command,
)
from .operations import (
    Commit,
    Confirm,
    HeadNudgeFailed,
    HeadOperationError,
    HeadOutcome,
    HeadPaneBusy,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadStopFailed,
    HeadTransport,
    HostTransport,
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
    "CLAUDE_EFFORTS",
    "CODEX_EFFORTS",
    "CODEX_LAUNCH_MODES",
    "CODEX_TUI_MODE",
    "Commit",
    "Confirm",
    "DEFAULT_EFFORT",
    "EXITED",
    "FINISHING",
    "HeadCommand",
    "HeadCommandError",
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
    "HeadTransport",
    "HostTransport",
    "LIFECYCLE",
    "NudgePointer",
    "PROMPT_AFTER_START_ADAPTERS",
    "PYTHON_SAFE_PATH_FLAG",
    "RUNTIME_ROLE_ENV",
    "SECRETARY_ROLE_ENV",
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
    "render_head_command",
    "spawn",
    "stop",
    "validate_launch_shape",
    "with_pid_heartbeat",
    "wrap_role_command",
]
