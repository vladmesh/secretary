"""A product run's two events, written into the history the read layer already reads.

Criterion 6 of secretary-1562 is a prohibition before it is a requirement: the run's launch and its
outcome must be visible through the `task_events` and `task_snapshot` that already exist, *without*
a second history. So there is no run journal, no run event store and no per-run ndjson file
anywhere in this package. What a run publishes it publishes into `<data>/board/events.ndjson`
through `secretary.tasks.TaskAudit` — the same append-only journal, under the same lock, in the
same generic record shape the control plane's own records (`sprint_guard_denied`,
`sprint_guard_override`) already use — and :class:`secretary.webproto.journal.EventJournal` reads it
back with no change at all, cursor included.

Two events per run, and there are two because a run has exactly two things worth a place in a
card's history:

``product_run.started``   a head was raised for this card: which run, which role, which profile,
                          the pid, the workspace, the run directory and the log
``product_run.finished``  that run reached a terminal state: which state, the exit status, whether
                          a result was published and the verdict on it when there is one

Both are idempotent, and by the journal's own mechanism rather than by a check here: the request id
is derived from the run id, and `TaskAudit` refuses to append a second record under a request id it
already owns. Every field of the record is derived from the run — including `occurred_at`, which is
the run's own start or settle time and not the clock at the moment of the call — so a replay builds
a byte-identical event and the journal recognises it as the one it already holds instead of
refusing it as a different payload under a taken id.

The records are deliberately *generic* audit records rather than typed board protocol events. A
typed event is a Card lifecycle transition, and a product run is not one: it moves no card, and
`is_significant_card_event` must go on reading it as machinery telemetry rather than waking a
sprint observer for it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from secretary.tasks import TaskAudit, TaskError
from secretary.webproto.errors import RuntimeUnavailable
from secretary.webproto.runs import ProductRun

STARTED = "product_run.started"
FINISHED = "product_run.finished"

#: Who writes these. Deliberately not one of the pipeline's roles: a product run is not a worker, a
#: reviewer or the dispatcher, and a history that said it was would be read as an attempt.
ACTOR = {"role": "product-runtime", "id": "secretary.webproto"}


def publish_started(audit: TaskAudit, run: ProductRun) -> dict[str, Any]:
    """Record that this run's head was raised, on the card's own history."""
    return _publish(
        audit,
        run,
        kind=STARTED,
        outcome="success",
        occurred_at=_isoformat(run.started_at),
        payload={
            "run_id": run.run_id,
            "role": run.role,
            "profile": run.profile,
            "adapter": run.adapter,
            "runtime": run.runtime,
            "parent_run_id": run.parent_run_id,
            "project": run.project,
            "workspace": run.workspace,
            "run_dir": run.run_dir,
            "pid_file": run.pid_file,
            "journal": run.journal_path,
            "log": run.log_path,
            "result_path": run.result_path,
            "head_pid": run.head_pid,
            "supervisor_pid": run.supervisor_pid,
        },
    )


def publish_finished(audit: TaskAudit, run: ProductRun, state: dict[str, Any]) -> dict[str, Any]:
    """Record how this run ended, once, on the same history its start is on.

    `outcome` is the journal's own two-valued field and is not a third name for the run's state: it
    says whether the run reached its ending having done its work, and `payload.state` carries the
    exact value out of the read layer's vocabulary that this ending is.
    """
    result = state.get("result") if isinstance(state.get("result"), dict) else {}
    return _publish(
        audit,
        run,
        kind=FINISHED,
        outcome="success" if run.settled_state == "finished" else "failure",
        occurred_at=_isoformat(run.settled_at),
        payload={
            "run_id": run.run_id,
            "role": run.role,
            "profile": run.profile,
            "parent_run_id": run.parent_run_id,
            "project": run.project,
            "state": run.settled_state,
            "reason": run.settled_reason,
            "exit": state.get("exit"),
            "result_present": bool(result.get("present")),
            "result": result.get("value"),
            "verdict": result.get("verdict"),
            "workspace": run.workspace,
            "run_dir": run.run_dir,
            "journal": run.journal_path,
        },
    )


def request_id_for(run_id: str, kind: str) -> str:
    """The one request id a given run's given event is ever written under."""
    return f"product-run:{run_id}:{kind}"


def _publish(
    audit: TaskAudit,
    run: ProductRun,
    *,
    kind: str,
    outcome: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request_id = request_id_for(run.run_id, kind)
    event = {
        "event_id": "evt_" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32],
        "schema_version": 1,
        "occurred_at": occurred_at,
        "actor": dict(ACTOR),
        "kind": kind,
        "outcome": outcome,
        "task_id": "",
        "ref": run.ref,
        "backend": {"kind": "kanboard", "task_id": None, "revision": "not_written"},
        "request_id": request_id,
        "payload": payload,
    }
    try:
        audit.stage(request_id, event)
        audit.append(request_id, event)
    except TaskError as exc:
        raise RuntimeUnavailable(
            f"this run's {kind} event could not be written to the card's history: {exc.message}"
        ) from None
    except OSError as exc:
        raise RuntimeUnavailable(
            f"this run's {kind} event could not be written to the card's history: {exc}"
        ) from None
    return event


def _isoformat(moment: float) -> str:
    """The journal's own UTC spelling, so a replay of the same run builds the same record."""
    return datetime.fromtimestamp(moment, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
