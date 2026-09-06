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

**Two facts, and they are not one field.** `ended` says the run is **over** -- the process it may
have held is provably gone, or none was ever spawned -- and `value` says **how** it ended. They
were one value until secretary-1563, because "terminal" was computed as `value in (finished,
process_failed)`: an ending that could only be named `source_unavailable` was therefore not an
ending at all, and every path that had to produce a terminal answer had to name a failure it had
no evidence for. Told apart, `source_unavailable` is an ending like any other -- the run is over,
what it did could not be established -- and nothing has to lie to close a run.

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
from secretary.webproto.runs import CLAIMED, UNRESOLVED, ProductRun

# Re-exported for the operation layer, which records the journal's path on every run it starts.
from triggered_agents.runtime.local_pty_head import JOURNAL_NAME as JOURNAL_NAME

# The substrate's own names for the record that carries a head's exit status and for the file it is
# written to, reached through the one backend that owns that package rather than around it, so this
# reader and that writer cannot disagree about either.
from triggered_agents.runtime.local_pty_head import RUN_EXITED, head_run_journal

#: The three values that *name an ending*. `running` is not one, and `unknown` is the absence of
#: one, so a run that is over while the evidence says either of those is recorded
#: `source_unavailable` by :meth:`secretary.webproto.lifecycle.RunLifecycle._ending` -- over, and
#: how not established. This is a partition of the same five values and not a sixth: whether a run
#: is over is the separate `ended` fact, and no reader decides it from this tuple.
ENDING_VALUES = (FINISHED, PROCESS_FAILED, SOURCE_UNAVAILABLE)

#: Read this module against :mod:`secretary.webproto.lifecycle`: a run's *phase* says where its
#: lifecycle is and a run's *state* says what its process is doing, and they are not the same
#: question. Only two phases answer the state question by themselves -- a settled run says its
#: recorded ending forever, and an unresolved run says `unknown` because at the moment that was
#: written nothing had established anything -- and every other phase is decided from the process
#: evidence. :func:`from_evidence` is that decision without either shortcut, for the one caller
#: that has just changed the world and must not read its own stale record back.


def observe(run: ProductRun, *, now: float) -> dict[str, Any]:
    """This run's state, its exit status and its result, from process evidence alone."""
    if run.ended:
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
            # Fact one, off the record and not off the value: a settled run is over whatever it
            # ended as, `source_unavailable` included. Deriving this from the value again is
            # exactly the seam this module was split on.
            ended=True,
            now=now,
            settled_at=run.settled_at,
        )
    if run.phase == UNRESOLVED:
        # A run whose cleanup was not confirmed. It is not `process_failed`: nothing established
        # that the process failed, and naming a failure here would be the same lie under a
        # different name. `unknown` is the read layer's own word for "no evidence establishes
        # this", and the fact that decides the card is the `ended: False` below -- the run stays
        # unsettled, which is what `admission.admit` already refuses a second run over. The
        # heartbeat and the journal still travel on `evidence` and `exit`, so an operator sees
        # what *is* known.
        return _document(
            UNKNOWN,
            run.unresolved_reason
            or "this run's head could not be confirmed stopped, so its ownership is unresolved",
            exit_status=_exit_status(_journal(run)[0]),
            result=_result(run),
            heartbeat=head_process_status(run.pid_file, expected=expected_identity(run)),
            # Fact one is false here, and that is the whole of the fence: a head may still be
            # alive under this run, so it is not over and no gate may treat it as over.
            ended=False,
            now=now,
        )
    if run.phase == CLAIMED:
        return _document(
            UNKNOWN,
            "this run holds a request and a workspace, and no head has been raised under it yet",
            exit_status=_empty_exit(),
            result=_result(run),
            heartbeat={"state": HEARTBEAT_NOT_YET_WRITTEN},
            # A claim is the phase before anything can exist: this run has not ended, it has not
            # begun. The lifecycle knows the other half -- that no process was ever spawned under
            # it -- and that is where a close of such a run establishes fact one.
            ended=False,
            now=now,
        )
    return from_evidence(run, now=now)


def from_evidence(run: ProductRun, *, now: float) -> dict[str, Any]:
    """This run's state from the process evidence alone, with no phase shortcut applied.

    :func:`observe` is the reader's answer and answers *two* phases out of the record instead --
    a settled run says its recorded ending forever, and an unresolved run says `unknown` because
    at the moment it was written nothing had established anything. This is the other question, and
    it is the one a caller asks once it has just changed the world: **what do the heartbeat, the
    journal and the result file say right now**.

    Keeping the two apart is not tidiness. A run whose cleanup could not be confirmed may have gone
    on to publish a result and end normally, and the read that finally confirms its stop then holds
    positive evidence of a *finished* run. Classifying that through `observe` would apply the
    stored `unresolved` shortcut and settle the run as `unknown` beside its own published result --
    a normal ending lost, permanently, and only ever on the recovery path. So a close classifies
    from here.
    """
    heartbeat = head_process_status(run.pid_file, expected=expected_identity(run))
    journal, journal_failure = _journal(run)
    exit_status = _exit_status(journal)
    result = _result(run)
    state, reason, ended = _classify(
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
        ended=ended,
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
) -> tuple[str, str, bool]:
    """The two facts this evidence establishes: how the run ended, why, and whether it is over.

    The third value is fact one as the *evidence* can see it -- the launch identity says there is
    no such process, or the supervisor recorded that it exited -- and it is deliberately not a
    function of the first: a run whose head is gone and whose journal cannot be read is over
    (`True`) while the only honest value for it is `source_unavailable`, and a run whose launch
    identity itself cannot be read is *not* over (`False`) under that same value. One value, two
    endedness answers, is precisely why these cannot be one field.

    A pid that belongs to another process stays `unknown` and not over, as it was: pid reuse
    proves nothing about this run's head either way, and a gate that freed the card on it would
    free it on a guess.
    """
    state = str(heartbeat.get("state") or "")
    if state == HEARTBEAT_LIVE_MATCH:
        return RUNNING, "a live process matches this run's launch identity", False
    if state == HEARTBEAT_UNREADABLE:
        return (
            SOURCE_UNAVAILABLE,
            (
                f"this run's launch identity could not be read ({heartbeat.get('reason') or 'unreadable'}), "
                "so nothing is proven about its process either way"
            ),
            False,
        )
    ended = state == HEARTBEAT_DEAD or bool(exit_status["recorded"])
    if not ended:
        if state == HEARTBEAT_IDENTITY_MISMATCH:
            return (
                UNKNOWN,
                (
                    "the pid in this run's launch identity belongs to another process, which proves "
                    "nothing about this run"
                ),
                False,
            )
        return UNKNOWN, "this run's head has not published a launch heartbeat yet", False
    if journal_failure and not exit_status["recorded"]:
        return (
            SOURCE_UNAVAILABLE,
            (
                f"this run's head is gone and its journal could not be read ({journal_failure}), so "
                "how it ended could not be established"
            ),
            True,
        )
    if result["present"]:
        return (
            FINISHED,
            "this run published its result and its head's process has ended" + _exit_tail(exit_status),
            True,
        )
    if exit_status["code"] == 0:
        return FINISHED, "this run's head process ended normally, and it published no result", True
    if exit_status["code"] is not None:
        return PROCESS_FAILED, f"this run's head process exited with status {exit_status['code']}", True
    if exit_status["signal"] is not None:
        return PROCESS_FAILED, f"this run's head process was ended by signal {exit_status['signal']}", True
    return (
        PROCESS_FAILED,
        "this run's head process is gone, it published no result, and nothing recorded how it ended",
        True,
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
    ended: bool,
    now: float,
    settled_at: float = 0.0,
) -> dict[str, Any]:
    """One run's state as its readers see it, carrying both facts and conflating neither.

    `ended` is passed in rather than derived here, and that is the contract: whether a run is over
    is established by the caller that holds the evidence for it -- the record, the launch identity,
    or the lifecycle that confirmed a stop -- and never re-derived from the value beside it.
    """
    available = state != SOURCE_UNAVAILABLE
    source = sources.available(now) if available else sources.unavailable(reason, now=now)
    return {
        "value": state,
        "reason": reason,
        "ended": bool(ended),
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
