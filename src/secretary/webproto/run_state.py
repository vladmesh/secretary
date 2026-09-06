"""What became of one product run, told apart into the values the read layer already introduced.

Criterion 5 of secretary-1562 asks for distinguishable outcomes rather than one "it did not work",
and names five: a normal ending, a non-zero exit, a process that died, a source that could not say,
and not knowing. It also says the values are the ones the read layer already introduced and that no
second dictionary is invented. Both hold here, and they hold together because the two are not one
field:

* **`state` is** :data:`secretary.webproto.agents.AGENT_STATES` **, unchanged** — `running`,
  `finished`, `process_failed`, `source_unavailable`, `unknown`. A dashboard that renders an agent
  row renders a run row with the same five words and the same meanings;
* **`exit` is the process's own exit status**, `{"code": ..., "signal": ...}`, taken from the
  supervisor's journal. It is not a vocabulary, so it adds none: a normal ending is `finished` with
  code `0`, an ending the head chose and failed at is `process_failed` with a code, and a process
  that was killed is `process_failed` with a signal. Those are the two the card names separately,
  and they are separate here without a sixth word.

The evidence is three files this product owns, and nothing else — no pane, no window, no session
manager, and no inventory of any kind:

  1. the head's **launch identity**, read through the product's one reader for it
     (`secretary.dispatcher_watchdog.head_process_status`, the same door
     :mod:`secretary.webproto.agents` reads liveness through). The expectation is the shape the
     local-pty backend itself writes and compares — run id, role and the task string the supervisor
     was given — so this asks the head the same question its own backend asks;
  2. the supervisor's **journal** in the run directory, for `run.exited` and the exit status on it;
  3. this run's **result file**, which the head is told the path of and writes itself.

The order of the decision is the point of the module. A result that was published and a process
that has since ended is a run that finished — however that process ended, including a stop this
product asked for once the result was in, because the product owns the process and ending a head
whose work is done is what owning it means. Only after that do the exit status and the bare absence
of a process get to speak, and each of them says something different from the others.

Nothing here writes. Settling a run — recording that ending once, so its one terminal event can be
published without a second history — is :meth:`secretary.webproto.runs.RunStore.settle`, and the
operation layer is what calls it with what this module observed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from secretary.dispatcher_watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_IDENTITY_MISMATCH,
    HEARTBEAT_LIVE_MATCH,
    HEARTBEAT_NOT_YET_WRITTEN,
    HEARTBEAT_UNREADABLE,
    head_process_status,
)
from secretary.webproto import sources
from secretary.webproto.agents import (
    FINISHED,
    PROCESS_FAILED,
    RUNNING,
    SOURCE_UNAVAILABLE,
    UNKNOWN,
)
from secretary.webproto.runs import ProductRun

# Re-exported for the operation layer, which records the journal's path on every run it starts.
from triggered_agents.runtime.local_pty_head import JOURNAL_NAME as JOURNAL_NAME

# The substrate's own names for the record that carries a head's exit status and for the file it is
# written to, reached through the one backend that owns that package rather than around it, so this
# reader and that writer cannot disagree about either.
from triggered_agents.runtime.local_pty_head import RUN_EXITED, head_run_journal

#: The states from which a run never moves again. What `settle` records and what a client polling
#: a run stops polling on.
TERMINAL_STATES = (FINISHED, PROCESS_FAILED)


def observe(run: ProductRun, *, now: float) -> dict[str, Any]:
    """This run's state, its exit status and its result, from process evidence alone."""
    if run.settled:
        # A settled run is history and says the same thing forever. Re-deriving it would let a run
        # that was `finished` at noon read as `source_unavailable` at midnight because its run
        # directory had been swept -- and the exit status and result are part of "the same thing",
        # so they come from the record the settle wrote and not from files that may be gone. That
        # is also what makes this run's terminal event deterministic: the document below is a pure
        # function of the record, so republishing the event after a journal failure rebuilds the
        # identical record rather than a second, differing one.
        return _document(
            run.settled_state,
            run.settled_reason,
            exit_status=settled_exit(run),
            result=settled_result(run),
            heartbeat={"state": "settled"},
            now=now,
            settled_at=run.settled_at,
        )
    if not run.raised:
        return _document(
            UNKNOWN,
            "this run holds a request and a workspace, and no head has been raised under it yet",
            exit_status=_empty_exit(),
            result=_result(run),
            heartbeat={"state": HEARTBEAT_NOT_YET_WRITTEN},
            now=now,
        )

    heartbeat = head_process_status(run.pid_file, expected=expected_identity(run))
    journal, journal_failure = _journal(run)
    exit_status = _exit_status(journal)
    result = _result(run)
    state, reason = _classify(
        heartbeat=heartbeat,
        exit_status=exit_status,
        result=result,
        journal_failure=journal_failure,
    )
    return _document(
        state,
        reason,
        exit_status=exit_status,
        result=result,
        heartbeat=heartbeat,
        now=now,
    )


def terminal_evidence(run: ProductRun) -> tuple[dict[str, Any], dict[str, Any]]:
    """The exit status and result behind an ending, in the shape a settle records them.

    Read once, at the moment the ending is settled, because that is the last moment they are
    guaranteed to be there: after it they are the record's, not the filesystem's.
    """
    return _exit_status(_journal(run)[0]), _result(run)


def settled_exit(run: ProductRun) -> dict[str, Any]:
    """The exit status recorded with this run's ending, or the empty one for an ending without."""
    recorded = run.settled_exit
    return dict(recorded) if isinstance(recorded, dict) and recorded else _empty_exit()


def settled_result(run: ProductRun) -> dict[str, Any]:
    """The result recorded with this run's ending, or "there was none"."""
    recorded = run.settled_result
    if isinstance(recorded, dict) and recorded:
        return dict(recorded)
    return {"present": False, "value": None, "reason": None}


def expected_identity(run: ProductRun) -> dict[str, str]:
    """The identity this run's head wrote, in the shape its own backend writes and compares.

    `LocalPtyHeadRuntime._process_alive` builds exactly this — run id, role and the raw task string
    the supervisor was handed — and the supervisor's `with_pid_heartbeat` wrapper writes exactly
    these three. Asking with any other shape would read a live head as somebody else's process.
    """
    return {"run_id": run.run_id, "role": run.role, "task": run.ref}


def _classify(
    *,
    heartbeat: dict[str, Any],
    exit_status: dict[str, Any],
    result: dict[str, Any],
    journal_failure: str,
) -> tuple[str, str]:
    state = str(heartbeat.get("state") or "")
    if state == HEARTBEAT_LIVE_MATCH:
        return RUNNING, "a live process matches this run's launch identity"
    if state == HEARTBEAT_UNREADABLE:
        return SOURCE_UNAVAILABLE, (
            f"this run's launch identity could not be read ({heartbeat.get('reason') or 'unreadable'}), "
            "so nothing is proven about its process either way"
        )
    ended = state == HEARTBEAT_DEAD or exit_status["recorded"]
    if not ended:
        if state == HEARTBEAT_IDENTITY_MISMATCH:
            return UNKNOWN, (
                "the pid in this run's launch identity belongs to another process, which proves "
                "nothing about this run"
            )
        return UNKNOWN, "this run's head has not published a launch heartbeat yet"
    if journal_failure and not exit_status["recorded"]:
        return SOURCE_UNAVAILABLE, (
            f"this run's head is gone and its journal could not be read ({journal_failure}), so "
            "how it ended could not be established"
        )
    if result["present"]:
        return FINISHED, (
            "this run published its result and its head's process has ended" + _exit_tail(exit_status)
        )
    if exit_status["code"] == 0:
        return FINISHED, "this run's head process ended normally, and it published no result"
    if exit_status["code"] is not None:
        return PROCESS_FAILED, f"this run's head process exited with status {exit_status['code']}"
    if exit_status["signal"] is not None:
        return PROCESS_FAILED, f"this run's head process was ended by signal {exit_status['signal']}"
    return PROCESS_FAILED, (
        "this run's head process is gone, it published no result, and nothing recorded how it ended"
    )


def _exit_tail(exit_status: dict[str, Any]) -> str:
    if exit_status["code"] is not None:
        return f" (exit status {exit_status['code']})"
    if exit_status["signal"] is not None:
        return f" (ended by signal {exit_status['signal']})"
    return ""


def _journal(run: ProductRun) -> tuple[tuple[dict[str, Any], ...], str]:
    """This run's supervisor journal, and the reason it could not be read when it could not."""
    run_dir = Path(run.journal_path).parent if run.journal_path else Path(run.run_dir or ".")
    try:
        return head_run_journal(run_dir), ""
    except OSError as exc:
        return (), f"{type(exc).__name__}: {exc}"


def _exit_status(events: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("kind") != RUN_EXITED:
            continue
        code = event.get("exit_code")
        signal = event.get("signal")
        return {
            "recorded": True,
            "code": code if isinstance(code, int) else None,
            "signal": signal if isinstance(signal, int) else None,
            "at": event.get("at"),
        }
    return _empty_exit()


def _empty_exit() -> dict[str, Any]:
    return {"recorded": False, "code": None, "signal": None, "at": None}


def _result(run: ProductRun) -> dict[str, Any]:
    """The head's own result document, when it wrote one.

    A result that is there but is not JSON is reported as present with a null value and the reason:
    the head did publish something, and telling that from "it published nothing" is the difference
    between a run that finished badly and one that never got there.
    """
    if not run.result_path:
        return {"present": False, "value": None, "reason": "this run declares no result path"}
    path = Path(run.result_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"present": False, "value": None, "reason": None}
    except OSError as exc:
        return {"present": False, "value": None, "reason": f"the result file could not be read: {exc}"}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        return {"present": True, "value": None, "reason": f"the result file is not JSON: {exc}"}
    return {"present": True, "value": value if isinstance(value, dict) else {"value": value}, "reason": None}


def verdict_of(result: dict[str, Any]) -> str | None:
    """The reviewer's verdict, when the result carries one. Free text is not a verdict."""
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    verdict = value.get("verdict")
    return verdict if isinstance(verdict, str) and verdict else None


def _document(
    state: str,
    reason: str,
    *,
    exit_status: dict[str, Any],
    result: dict[str, Any],
    heartbeat: dict[str, Any],
    now: float,
    settled_at: float = 0.0,
) -> dict[str, Any]:
    available = state != SOURCE_UNAVAILABLE
    source = sources.available(now) if available else sources.unavailable(reason, now=now)
    return {
        "value": state,
        "reason": reason,
        "terminal": state in TERMINAL_STATES,
        "settled_at": sources.isoformat(settled_at) if settled_at else None,
        "source": source.to_json(),
        "exit": {"code": exit_status["code"], "signal": exit_status["signal"], "at": exit_status["at"]},
        "result": {
            "present": bool(result["present"]),
            "value": result["value"],
            "reason": result["reason"],
            "verdict": verdict_of(result),
        },
        "evidence": {
            "kind": "process_heartbeat",
            "heartbeat_state": str(heartbeat.get("state") or ""),
            "pid": heartbeat.get("pid") if isinstance(heartbeat.get("pid"), int) else None,
            "detail": str(heartbeat.get("reason") or "") or None,
        },
    }
