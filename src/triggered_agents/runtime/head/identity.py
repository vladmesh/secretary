"""The product's one reader of a head's launch identity, beside the one writer of it.

`command.with_pid_heartbeat` is what puts the record on disk: the head's own shell writes `pid`,
`boot_id`, `proc_starttime_ticks` beside the run, role and task it was launched for, and then
`exec`s, so the pid stays the head's own for its whole life. This module is the other half of that
one scheme — the classification of such a record into "this launch is running", "it ended" and
"this pid is somebody else's now".

It lives here rather than in the control plane that grew it because both of the things that need
it live under this package: `local_pty_head.LocalPtyHeadRuntime` and `orca_legacy_head` are handed
this reader by whoever builds them, and the mechanical-role driver in `runtime/dispatch.py` builds
one too. `secretary.dispatcher_watchdog` re-exports every name below, so the control plane keeps
the spelling it has always used and there is still exactly one implementation.

A record survives a reboot and a pid can be handed out again, which is why a bare "does this
integer name a process?" is not the question any of these answer: `boot_id` and
`proc_starttime_ticks` are what make a stale record read as the dead head it describes, and
`expected` is what makes a live process that is not this launch read as a mismatch rather than as
a match. Missing, half-written, malformed and legacy pid-only files keep their own inconclusive
states: a reader that cannot tell must never be read as one that said no.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The record layout `with_pid_heartbeat` writes and this module reads. A record of any other
#: version is inconclusive rather than dead: it was written by a scheme this reader does not know.
HEARTBEAT_VERSION = 1

HEARTBEAT_LIVE_MATCH = "live-match"
HEARTBEAT_DEAD = "dead"
HEARTBEAT_IDENTITY_MISMATCH = "identity-mismatch"
HEARTBEAT_NOT_YET_WRITTEN = "not-yet-written"
HEARTBEAT_UNREADABLE = "unreadable"


def _proc_starttime_ticks(pid: int) -> str:
    """Linux's process-creation discriminator for a live PID.

    ``comm`` may contain spaces and parentheses, so splitting the complete ``stat`` line on
    whitespace is not safe. The final closing parenthesis ends it; field 22 is then token 19 of the
    remaining fields (which begin at field 3).
    """
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    close = stat.rfind(")")
    fields = stat[close + 2:].split()
    if close < 0 or len(fields) <= 19:
        raise ValueError("/proc stat has no process start time")
    return fields[19]


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def _is_zombie(pid: int) -> bool:
    """A process the kernel has not reaped yet still answers `kill(pid, 0)`, so a check right at
    exit can read one tick stale as still alive. Reading its own `/proc` status closes that gap."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return "Z" in line
    return False


def _is_stopped(pid: int) -> bool:
    """Whether a live process is suspended with SIGSTOP/SIGTSTP."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return "T" in line
    return False


def _unreadable(reason: str) -> dict[str, Any]:
    return {"known": False, "alive": False, "match": False, "state": HEARTBEAT_UNREADABLE,
            "reason": reason}


def _record_matches_expected(record: Mapping[str, Any], expected: Mapping[str, Any] | None) -> bool:
    if expected is None:
        return True
    # An empty expected run is deliberately not a wildcard.  A caller that has no durable HeadRun
    # cannot prove a process belongs to it, even when its pid file happens to be well formed.
    for name in ("run_id", "role", "task"):
        value = str(expected.get(name) or "")
        if not value or str(record.get(name) or "") != value:
            return False
    leaf = str(expected.get("leaf") or "")
    return not leaf or str(record.get("leaf") or "") == leaf


def _read_record(pid_file: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        raw = Path(pid_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, {"known": False, "alive": False, "match": False,
                      "state": HEARTBEAT_NOT_YET_WRITTEN}
    except OSError as exc:
        return None, _unreadable(type(exc).__name__)
    try:
        record = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, _unreadable("malformed-json")
    if not isinstance(record, dict):
        return None, _unreadable("record-is-not-an-object")
    if record.get("version") != HEARTBEAT_VERSION:
        return None, _unreadable("unknown-version")
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return None, _unreadable("invalid-pid")
    required = ("boot_id", "proc_starttime_ticks", "run_id", "role", "task")
    if pid <= 0 or any(not str(record.get(name) or "") for name in required):
        return None, _unreadable("missing-identity")
    record["pid"] = pid
    record["leaf"] = str(record.get("leaf") or "")
    return record, None


def head_process_status(
    pid_file: str, *, expected: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Classify a launch-identity heartbeat without trusting PID reuse.

    A readable record has one of ``live-match``, ``dead`` or ``identity-mismatch``. Missing,
    partially written, malformed and legacy PID-only files retain their distinct inconclusive states.
    """
    record, failure = _read_record(pid_file)
    if failure is not None:
        return failure
    assert record is not None
    pid = int(record["pid"])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"known": True, "alive": False, "match": False, "state": HEARTBEAT_DEAD,
                "pid": pid, "record": record}
    except PermissionError:
        # A normal dispatcher head is owned by us.  Treat an uninspectable process as inconclusive:
        # a weak permission answer cannot authorize a signal or a replacement.
        return _unreadable("process-not-inspectable")
    except OSError as exc:
        return _unreadable(type(exc).__name__)
    try:
        boot_matches = str(record["boot_id"]) == _boot_id()
        start_matches = str(record["proc_starttime_ticks"]) == _proc_starttime_ticks(pid)
    except (OSError, ValueError) as exc:
        return _unreadable(type(exc).__name__)
    alive = not _is_zombie(pid)
    if not alive:
        return {"known": True, "alive": False, "match": False, "state": HEARTBEAT_DEAD,
                "pid": pid, "record": record}
    if not boot_matches or not start_matches or not _record_matches_expected(record, expected):
        return {
            "known": True,
            "alive": True,
            "match": False,
            "state": HEARTBEAT_IDENTITY_MISMATCH,
            "pid": pid,
            "record": record,
            "stopped": _is_stopped(pid),
        }
    return {
        "known": True,
        "alive": True,
        "match": True,
        "state": HEARTBEAT_LIVE_MATCH,
        "pid": pid,
        "record": record,
        "stopped": _is_stopped(pid),
    }


def heartbeat_is_live_match(status: Mapping[str, Any]) -> bool:
    return str(status.get("state") or "") == HEARTBEAT_LIVE_MATCH


def heartbeat_is_dead(status: Mapping[str, Any]) -> bool:
    return str(status.get("state") or "") == HEARTBEAT_DEAD


def heartbeat_is_mismatch(status: Mapping[str, Any]) -> bool:
    return str(status.get("state") or "") == HEARTBEAT_IDENTITY_MISMATCH
