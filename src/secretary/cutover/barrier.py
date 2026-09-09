"""The durable cutover write barrier shared by every board writer."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _refuse(message: str) -> None:
    from secretary.tasks import TaskError

    raise TaskError("cutover_frozen", message, 4)


def require_board_write_allowed(data_dir: str | os.PathLike[str]) -> None:
    """Refuse writes while a cutover freeze is durable.

    The controller itself is the sole exception.  A child protocol probe must
    carry the same identity and have the recorded controller as its parent.
    """
    state_path = Path(data_dir).expanduser().resolve() / "cutover" / "postgres-v1.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, ValueError):
        _refuse("cutover state is unreadable; board writes are fenced")
    if not isinstance(state, dict):
        _refuse("cutover state is invalid; board writes are fenced")
    phase = state.get("phases", {}).get("global_freeze", {})
    if phase.get("status") not in {"running", "failed", "complete"}:
        return
    identity = str(state.get("identity") or "")
    controller_pid = state.get("controller_pid")
    if (
        identity
        and os.environ.get("SECRETARY_CUTOVER_CONTROLLER_ID") == identity
        and controller_pid in {os.getpid(), os.getppid()}
    ):
        return
    # A completed/recovered cutover stays fenced until the supported pause is
    # explicitly lifted.  After that explicit action, the durable cutover record
    # remains evidence rather than a permanent write prohibition.
    if state.get("status") in {"resume-ready", "recovered-frozen"}:
        from secretary.dispatcher_pause import ProductionPause

        if not ProductionPause(Path(data_dir)).summary().get("paused"):
            return
    _refuse("PostgreSQL cutover freeze is active; board writes are fenced")


__all__ = ["require_board_write_allowed"]
