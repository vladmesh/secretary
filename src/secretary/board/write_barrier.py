"""The durable cutover write barrier shared by every board writer.

The fence is a property of the board, not of the controller that arms it: every
`TaskWriter`, `SprintWriter` and Product/Issue mutation consults it on
construction, on both backends.  It therefore lives with the board rather than
in `secretary.cutover`, which is the controller package and is retired with the
Kanboard cutover it performs.

The verdict is derived from one file, so it is remembered per file identity
rather than re-parsed per writer.  A frozen installation's state document is a
full phase transcript — megabytes on a host that has already cut over — and it
was being read and decoded once for every writer the process built.  The cache
is keyed by the file's `(st_mtime_ns, st_size, st_ino, st_dev)`, so any rewrite
of the document is seen on the very next call, and it holds only what the *file*
says: the controller-identity exception is a fact about this process and is
re-evaluated every time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NoReturn

CONTROLLER_ID_ENV = "SECRETARY_CUTOVER_CONTROLLER_ID"
IN_FLIGHT_STATUSES = frozenset(("applying", "failed-frozen"))
TERMINAL_STATUSES = frozenset(("resume-ready", "recovered-frozen"))

#: `state path -> (stat fingerprint, verdict)`.  The verdict is what the file
#: alone decides: `None` admits, `("refuse", message)` always refuses, and
#: `("fence", identity, controller_pid)` refuses everyone but the controller.
_VERDICTS: dict[Path, tuple[tuple[int, int, int, int], tuple[object, ...] | None]] = {}


def _refuse(message: str) -> NoReturn:
    from secretary.tasks import TaskError

    raise TaskError("cutover_frozen", message, 4)


def _read_state(state_path: Path) -> tuple[object, ...] | None:
    """Decode the durable state once and say what it alone decides."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The document was removed between the stat and the read.
        return None
    except (OSError, UnicodeError, ValueError):
        return ("refuse", "cutover state is unreadable; board writes are fenced")
    if not isinstance(state, dict):
        return ("refuse", "cutover state is invalid; board writes are fenced")
    status = state.get("status")
    # Terminal controller state is evidence only.  It must never re-arm during
    # a later unrelated pipeline freeze or backup.
    if status in TERMINAL_STATUSES:
        return None
    if status not in IN_FLIGHT_STATUSES:
        return None
    phase = state.get("phases", {}).get("global_freeze", {})
    if phase.get("status") not in {"running", "failed", "complete"}:
        return None
    return ("fence", str(state.get("identity") or ""), state.get("controller_pid"))


def require_board_write_allowed(data_dir: str | os.PathLike[str]) -> None:
    """Refuse writes while a cutover freeze is durable.

    The controller itself is the sole exception.  A child protocol probe must
    carry the same identity and have the recorded controller as its parent.
    """
    state_path = Path(data_dir).expanduser().resolve() / "cutover" / "postgres-v1.json"
    try:
        info = state_path.stat()
    except FileNotFoundError:
        _VERDICTS.pop(state_path, None)
        return
    except OSError:
        _refuse("cutover state is unreadable; board writes are fenced")
    fingerprint = (info.st_mtime_ns, info.st_size, info.st_ino, info.st_dev)
    remembered = _VERDICTS.get(state_path)
    if remembered is not None and remembered[0] == fingerprint:
        verdict = remembered[1]
    else:
        verdict = _read_state(state_path)
        _VERDICTS[state_path] = (fingerprint, verdict)
    if verdict is None:
        return
    if verdict[0] == "refuse":
        _refuse(str(verdict[1]))
    _, identity, controller_pid = verdict
    if (
        identity
        and os.environ.get(CONTROLLER_ID_ENV) == identity
        and controller_pid in {os.getpid(), os.getppid()}
    ):
        return
    _refuse("PostgreSQL cutover freeze is active; board writes are fenced")


def forget_board_write_verdicts() -> None:
    """Drop every remembered verdict, for a test that reuses one path deliberately.

    Product code never calls this: a rewritten document changes its own stat
    fingerprint, which is what makes the cache safe.  A test that writes several
    documents to one path inside a single timestamp granule is the one caller
    that needs the memory gone rather than merely stale.
    """
    _VERDICTS.clear()


__all__ = ["forget_board_write_verdicts", "require_board_write_allowed"]
