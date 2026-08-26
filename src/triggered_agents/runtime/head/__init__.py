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

`runtime` is where those three operations became a boundary rather than a namespace: `HeadRuntime`
is one head backend as everything above it may see one -- six verbs (`start`, `deliver`, `observe`,
`request_drain`, `stop`, `attach`), each answering with a typed receipt. The backends that wear them
live outside this package, because this one names no session manager: the existing path is
`runtime.orca_legacy_head`, beside the Orca argument vectors it is built on. `runtime` also owns the
values that used to be squeezed into a lifecycle state, all of them per head: the `TurnLease` a head
runs one turn under, the activity epoch that moves when the backend sees *that* head do something,
and whether the head still admits work at all. A backend serialises its own verbs around them, so
nothing above the boundary has to hold a lock to get one head's delivery, drain and stop in order.

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
    HeadDelivery,
    HeadNudgeFailed,
    HeadOperationError,
    HeadOutcome,
    HeadPaneBusy,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadStopFailed,
    HeadTransport,
    HostTransport,
    LaunchPreflight,
    NudgePointer,
    nudge,
    post_delivery_run,
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
from .runtime import (
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
    RECEIPT_STATUSES,
    AttachReceipt,
    DeliverReceipt,
    DrainReceipt,
    HeadActivity,
    HeadReceipt,
    HeadRuntime,
    ObserveReceipt,
    StartReceipt,
    StopReceipt,
    TurnLease,
    TurnLeaseError,
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
    "DEFAULT_EFFORT",
    "EXITED",
    "FINISHING",
    "HEAD_ALIVE",
    "HEAD_BUSY",
    "HEAD_DRAINING",
    "HEAD_GONE",
    "HEAD_OK",
    "HEAD_UNSUPPORTED",
    "LIFECYCLE",
    "OBSERVE_INVENTORY_UNREADABLE",
    "OBSERVE_NO_ADDRESS",
    "OBSERVE_PANE_ABSENT",
    "OBSERVE_PANE_DISCONNECTED",
    "OBSERVE_READINESS_UNKNOWN",
    "PROMPT_AFTER_START_ADAPTERS",
    "PYTHON_SAFE_PATH_FLAG",
    "RECEIPT_STATUSES",
    "RUNTIME_ROLE_ENV",
    "SECRETARY_ROLE_ENV",
    "SPAWNED",
    "TASK_CARD",
    "TASK_KINDS",
    "TASK_SPRINT",
    "TASK_STANDING",
    "WORKING",
    "AttachReceipt",
    "Commit",
    "Confirm",
    "DeliverReceipt",
    "DrainReceipt",
    "HeadActivity",
    "HeadCommand",
    "HeadCommandError",
    "HeadDelivery",
    "HeadNudgeFailed",
    "HeadOperationError",
    "HeadOutcome",
    "HeadPaneBusy",
    "HeadReceipt",
    "HeadRun",
    "HeadRunError",
    "HeadRuntime",
    "HeadSpawnAborted",
    "HeadSpawnFailed",
    "HeadSpec",
    "HeadSpecError",
    "HeadStopFailed",
    "HeadTransport",
    "HostTransport",
    "LaunchPreflight",
    "NudgePointer",
    "ObserveReceipt",
    "StartReceipt",
    "StopInitiator",
    "StopReceipt",
    "TaskRef",
    "TaskRefError",
    "TurnLease",
    "TurnLeaseError",
    "head_spec",
    "load_head_specs",
    "new_run_id",
    "nudge",
    "post_delivery_run",
    "render_head_command",
    "spawn",
    "stop",
    "validate_launch_shape",
    "with_pid_heartbeat",
    "wrap_role_command",
]
