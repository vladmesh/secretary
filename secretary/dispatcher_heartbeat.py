"""The durable identity carried by a dispatcher-launched head heartbeat.

The heartbeat file is deliberately an assertion about one *launch*, not merely an answer to
"does this integer still name a process?".  A PID can be reused after the head exits, and a file
survives a reboot, so every reader compares the producer's run, role and task binding as well as
the kernel's boot and process-start identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


HEARTBEAT_VERSION = 1


def heartbeat_role(role: str) -> str:
    """The stable role name written into a heartbeat record."""
    return "reviewer" if role == "review" else str(role or "")


def task_binding(task_ref: Mapping[str, Any] | None, fallback: str = "") -> str:
    """A compact durable binding for the task a run was launched to serve."""
    if isinstance(task_ref, Mapping):
        kind = str(task_ref.get("kind") or "")
        ref = str(task_ref.get("ref") or "")
        if kind and ref:
            return f"{kind}:{ref}"
    return fallback


def heartbeat_identity(
    *, run_id: str, role: str, task_ref: Mapping[str, Any] | None = None,
    task: str = "", leaf: str = "",
) -> dict[str, str]:
    """The non-kernel facts a heartbeat writer and reader must agree on."""
    return {
        "run_id": str(run_id or ""),
        "role": heartbeat_role(role),
        "task": task_binding(task_ref, task),
        "leaf": str(leaf or ""),
    }


def intent_heartbeat_identity(intent: Mapping[str, Any], *, task: str = "") -> dict[str, str]:
    """Expected identity while a launch is still represented only by its durable intent."""
    role = str(intent.get("role") or "")
    return heartbeat_identity(
        run_id=str(intent.get("run_id") or ""),
        role=role,
        task=task or str(intent.get("task") or ""),
        leaf=str(intent.get("leaf") or ""),
    )


def run_heartbeat_identity(
    run: Mapping[str, Any] | None, *, role: str, task: str = "", leaf: str = ""
) -> dict[str, str]:
    """Expected identity for a persisted ``triggered_agents.runtime.head.HeadRun``."""
    payload = run if isinstance(run, Mapping) else {}
    return heartbeat_identity(
        run_id=str(payload.get("run_id") or ""),
        role=role,
        task_ref=(payload.get("task_ref") if isinstance(payload.get("task_ref"), Mapping) else None),
        task=task,
        leaf=leaf or str(payload.get("leaf") or ""),
    )
