"""Time ceilings for the dispatcher's two open-ended waits.

`waiting-worker-report` and `waiting-review-verdict` watch the persisted terminal identity on
every tick. A missing pane is restarted immediately. A pane whose head process has exited while
the pane itself is still connected is caught the same tick via `head_process_status`. When Orca
supplies `lastOutputAt`, a head must also produce its first output shortly after launch; later
output renews the ordinary, generous silence ceiling. A runtime that cannot supply activity
timestamps, or cannot expose the pid heartbeat, uses that ceiling as its fallback.

Since S1-4 the wait tick's decision is the persisted vitality episode's verdict
(`dispatcher.DispatcherRuntime._decide_wait_by_verdict`), which replaced this module's
continuous-idle fence (`idle_outcome`) and its clock-only wait ladder (`wait_outcome`): the
fence fields the fence used to own are still cleared by `reset_idle`/`reset_wait`, but nothing
advances them any more. A confirmed pid answers whether the process runs, not whether it is
doing anything — that question now belongs to the reducer and the recovery policy.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from secretary.dispatcher_heartbeat import HEARTBEAT_VERSION, run_heartbeat_identity
from secretary.dispatcher_state import request_token
from secretary.infra.env import positive_int

# A missing pane is handled immediately. These ceilings remain deliberately generous for a head
# that has made progress and then goes quiet, and are the fallback for old Orca runtimes without
# `lastOutputAt`.
REVIEW_VERDICT_STALL_DEFAULT = 90 * 60
WORKER_REPORT_STALL_DEFAULT = 6 * 60 * 60
# A live head prints a prompt or launch output within a few dispatcher ticks. This only applies
# when an activity timestamp exists and has not advanced beyond the launch timestamp.
INITIAL_OUTPUT_STALL_DEFAULT = 3 * 60
# How long a head that is ready for input, with nothing delivered for the round the dispatcher is
# waiting on, is left alone before the watchdog acts (secretary-1063). A head between turns reads
# as ready for a moment — a delivered prompt whose turn has not started, a retained conversation
# just resumed — so this is a window rather than a single reading. It is short next to the silence
# ceilings above because it is not measuring silence: readiness says the head is not working.
IDLE_STALL_DEFAULT = 5 * 60
# How many bring-ups of one role's head are parked over a pane that is not ready for its launch
# prompt before the card is blocked over that pane (secretary-1163). A count rather than a window,
# because the retry is the dispatcher tick itself: the deferred launch is made again on the next
# one, so this is also how many ticks a head is given to get past whatever is holding its pane.
BRING_UP_DEFER_ATTEMPTS_DEFAULT = 5

# How many consecutive `review-launch-aborted` ticks a card is given before one operator
# escalation is emitted (issue:aa9a8ae4). Unlike a deferral this abort never blocks the card on its
# own, because the reviewer pane came up and its worker could not be confirmed gone, so the loop is
# otherwise silent to everyone but the steward's degraded-health line. The count is one tick each,
# so this is also how many ticks the recovery path is given to freeze or adopt before an operator
# is asked to look.
REVIEW_LAUNCH_ABORT_STUCK_DEFAULT = 10

# How many consecutive ticks may fail to bring a reviewer up over a green candidate before the card
# is blocked for an operator (secretary-1401). Each failure is one tick, so this is also how long
# the reviewer's runtime is given to come back. It is deliberately larger than the deferral bound:
# a deferred bring-up has a pane to point at and a head that may yet answer, while these are
# failures of the review stage's own machinery, and the alternative to waiting them out is a green
# candidate re-run from Ready.
REVIEW_INFRA_RETRY_ATTEMPTS_DEFAULT = 10


def stall_seconds(kind: str) -> int:
    """Ceiling for a wait, read per call. A typo in the env var falls back to the default rather
    than raising out of module import and taking the whole dispatcher down with it."""
    if kind == "review":
        name, default = "SECRETARY_REVIEW_VERDICT_STALL_SECONDS", REVIEW_VERDICT_STALL_DEFAULT
    else:
        name, default = "SECRETARY_WORKER_REPORT_STALL_SECONDS", WORKER_REPORT_STALL_DEFAULT
    return positive_int(name, default)


def initial_output_stall_seconds() -> int:
    """Short grace period for a pane whose activity has never passed its launch timestamp."""
    return positive_int("SECRETARY_INITIAL_OUTPUT_STALL_SECONDS", INITIAL_OUTPUT_STALL_DEFAULT)


def idle_stall_seconds() -> int:
    """How long an idle head with nothing delivered is given before the watchdog acts."""
    return positive_int("SECRETARY_HEAD_IDLE_STALL_SECONDS", IDLE_STALL_DEFAULT)


# How long a suspended head is given to answer its SIGCONT before the recovery policy
# escalates to the operator (S1-5). Deliberately minutes-scale: the whole rung exists so a
# stopped head reaches a human far below the six-hour ceilings, while a resuming head gets
# many tick cadences to show life. An unparseable value falls back rather than raising out of
# module import and taking the dispatcher with it, like every ceiling here.
SUSPENSION_RESPONSE_WINDOW_DEFAULT = 5 * 60


def suspension_response_window_seconds() -> int:
    """The SIGCONT response window for the recovery policy's second rung."""
    return positive_int(
        "SECRETARY_HEAD_SUSPENSION_RESPONSE_SECONDS", SUSPENSION_RESPONSE_WINDOW_DEFAULT,
    )


def bring_up_defer_attempts() -> int:
    """How many deferred bring-ups one role's head gets before its card is blocked."""
    return positive_int("SECRETARY_BRINGUP_DEFER_ATTEMPTS", BRING_UP_DEFER_ATTEMPTS_DEFAULT)


def review_launch_abort_stuck_ticks() -> int:
    """How many consecutive aborted reviewer launches pass before an operator is escalated to."""
    return positive_int("SECRETARY_REVIEW_LAUNCH_ABORT_STUCK", REVIEW_LAUNCH_ABORT_STUCK_DEFAULT)


def review_infra_retry_attempts() -> int:
    """How many reviewer bring-up failures a green candidate absorbs before the card is blocked."""
    return positive_int("SECRETARY_REVIEW_INFRA_RETRY_ATTEMPTS", REVIEW_INFRA_RETRY_ATTEMPTS_DEFAULT)


def reset_idle(record, kind: str) -> None:
    """Clear an idle episode when a replacement head takes over or a wait resets."""
    setattr(record, f"{kind}_idle_since", 0.0)
    setattr(record, f"{kind}_idle_confirmations", 0)


def pid_file_path(kind: str, reference: str) -> str:
    """Where a launched head's pid-heartbeat file lives.

    Outside the workspace, like the report and verdict bodies, so it never dirties `git status` for
    the done-report check. Keyed on kind and reference only: a respawn in the same workspace reuses
    the same path on purpose, since the dispatcher clears it before every fresh launch.
    """
    root = os.environ.get("SECRETARY_DISPATCHER_BODY_DIR", "/tmp").rstrip("/") or "/tmp"
    return f"{root}/secretary-{kind}-pid-{request_token(reference)}.pid"


HEARTBEAT_LIVE_MATCH = "live-match"
HEARTBEAT_DEAD = "dead"
HEARTBEAT_IDENTITY_MISMATCH = "identity-mismatch"
HEARTBEAT_NOT_YET_WRITTEN = "not-yet-written"
HEARTBEAT_UNREADABLE = "unreadable"


class HeadRunIdentityMismatch(RuntimeError):
    """A readable heartbeat names a live process other than the expected HeadRun."""


def _run_payload(run: Any) -> Mapping[str, Any]:
    """Accept a persisted HeadRun or the live operation value without importing head operations."""
    if isinstance(run, Mapping):
        return run
    to_json = getattr(run, "to_json", None)
    if callable(to_json):
        payload = to_json()
        if isinstance(payload, Mapping):
            return payload
    return {}


def head_run_process_status(
    pid_file: str,
    *,
    run: Any,
    role: str,
    task: str = "",
    leaf: str = "",
) -> dict[str, Any]:
    """Classify one pid file against the durable HeadRun expected to own it.

    The dispatcher boundary for every lifecycle and recovery consumer: it builds the expected
    identity from the recorded run rather than leaving each caller to combine a PID probe with an
    unrelated liveness boolean.
    """
    return head_process_status(
        pid_file,
        expected=run_heartbeat_identity(_run_payload(run), role=role, task=task, leaf=leaf),
    )


def guard_head_run_identity(
    pid_file: str,
    *,
    run: Any,
    role: str,
    task: str = "",
    leaf: str = "",
) -> dict[str, Any]:
    """Return the common classification or fence a readable foreign live process."""
    status = head_run_process_status(pid_file, run=run, role=role, task=task, leaf=leaf)
    if heartbeat_is_mismatch(status):
        raise HeadRunIdentityMismatch(pid_file)
    return status


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


def _heartbeat_handoff_path(pid_file: str) -> Path:
    """The launcher's durable leaf handoff beside its heartbeat.

    A terminal create may answer with its leaf before the shell running inside that terminal has
    reached the heartbeat writer, and the handoff covers that ordering.
    """
    return Path(f"{pid_file}.leaf")


def clear_head_heartbeat(pid_file: str) -> None:
    """Forget both halves of a completed launch identity before a fresh launch reuses its path."""
    for path in (Path(pid_file), _heartbeat_handoff_path(pid_file)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _replace_json(path: Path, payload: Mapping[str, Any]) -> bool:
    """Replace one small protocol record without exposing a partial JSON document."""
    temporary = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def bind_head_heartbeat(
    pid_file: str, *, expected: Mapping[str, Any], leaf: str
) -> bool:
    """Durably hand a pane leaf to the heartbeat writer and bind an existing record.

    The shell and terminal-create reply have no ordering guarantee. First write the handoff, so a
    writer that has not reached its base record yet incorporates the leaf itself; if its base record
    already exists, re-read and match it before the guarded second replace. The shell also rechecks
    the handoff after its base replace.
    """
    handoff = {
        "version": HEARTBEAT_VERSION,
        "expected": {
            name: str(expected.get(name) or "")
            for name in ("run_id", "role", "task")
        },
        "leaf": str(leaf or ""),
    }
    if not _replace_json(_heartbeat_handoff_path(pid_file), handoff):
        return False
    status = head_process_status(pid_file, expected=expected)
    if not heartbeat_is_live_match(status):
        # The handoff is the successful part in the writer-after-create ordering.  A missing or
        # unreadable base heartbeat remains inconclusive to readers until that writer publishes it.
        return True
    record = dict(status["record"])
    record["leaf"] = str(leaf or "")
    return _replace_json(Path(pid_file), record)


def reset_wait(record, kind: str) -> None:
    """Clear a wait's watchdog bookkeeping so the next wait of that kind starts fresh."""
    setattr(record, f"{kind}_waiting_since", 0.0)
    setattr(record, f"{kind}_respawns", 0)
    setattr(record, f"{kind}_progress_at", 0.0)
    reset_idle(record, kind)


def wait_cycle_token(record) -> str:
    """Per-cycle discriminator for every request-id the watchdog path can emit.

    attempt_id outlives the card and the record is dropped whenever the card lands in Blocked, so a
    bare attempt-scoped id repeats on the next stall. TaskWriter answers a repeated request-id with
    success and no mutation, so the tick would report "blocked" while the card stays put and the next
    tick re-adopts it. comment_baseline is re-read on adoption and every cycle leaves at least one
    comment behind, so it is stable within a cycle and distinct across them.
    """
    return f"{record.comment_baseline}-{record.worker_respawns}-{record.review_respawns}"
