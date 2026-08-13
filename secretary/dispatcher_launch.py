"""Durable launch intent for the dispatcher-launched worker and reviewer heads.

Same contour as the observer's (`dispatcher_observer`) and for the same reason. A head is a real
process from the moment the host is asked for one, but `DispatcherRecord` only learns its workspace,
its pane and its routing from the `save_records` at the end of the launch path. A state write that
refuses in that window leaves a running head with nothing pointing at it, and the next tick reads
the card as headless and opens a second one.

So the launch is fixed on disk first. The intent names the role, the round and attempt it belongs
to, the workspace the head runs in and the pid file it writes its heartbeat to. Workspace and pid
file are path arithmetic over the card reference and the worker id, so both are known before the
host answers, which is exactly what a tick that dies mid-launch never gets to see. With them the
next tick can settle the only question that matters, "is the head of that launch alive", without
the terminal handle the lost tick never persisted. The round is the round the new head belongs to,
which for a rework is the next one: it is reserved here, before the host call, so an adoption
resumes the rework rather than the round the red result closed.

Resolution runs before anything else the tick would do with the card:

  live pid    -> adopt it. The record becomes what the launch would have written, and no second
                 head is launched.
  no pid yet  -> leave the intent alone. A head that has just started has not written its
                 heartbeat, and that is not evidence of death, so it waits out the same grace
                 window every fresh head gets.
  dead pid    -> the launch left nothing running. Whatever it may have left in the workspace is
                 stopped, the intent is dropped, and the ordinary path relaunches — into the round
                 the intent reserved, since a rework's round is over whether or not its head lived.

The intent is held for the whole bring-up, not only up to the host call. Once the host answers, the
pane it opened and the configuration it launched go into the intent, and only when the record has
everything — pane identity, routing event, its own save — is the intent spent. Everything in that
tail runs over a process that already exists, so a failure there is ambiguous rather than a launch
that did not happen, and an adoption reads the launch's own snapshot instead of asking a registry
that may have been edited since.

State that cannot be written is a launch that does not happen: the caller answers a failed intent
write by not touching the host at all. A failing data plane then costs the card a tick instead of
giving it two heads. A bring-up that fails once its terminal is already up is not a headless card
either: the host reports it as `HeadLaunchAborted` with the pane it opened, the intent stays on
disk carrying that handle, and the same resolution settles it a tick later.

A head adopted this way usually has no pane handle, which is why `command_terminal_status` falls
back to the pid heartbeat when a record carries no pane identity for its role. Without that the
wait watchdog would read the adopted head as a missing terminal and respawn it: the double launch
this module exists to prevent, one tick later. For the same reason the heartbeat stays on the
record as that role's identity (`worker_pid_file` / `review_pid_file`): every stop the lifecycle
makes afterwards — the freeze before review, a respawn, a red verdict, a pipeline freeze — goes
through it when there is no pane to close, and one that quietly stopped nothing would put a second
process on the checkout just as surely.

Nothing here assumes a stop worked. A head the host will not confirm gone keeps its record and its
intent, and the tick that wanted to replace it returns `*-stop-unconfirmed` instead: an
unconfirmed stop followed by a launch is the same two heads by another route.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from secretary.dispatcher_helpers import scrub_host_output
from secretary.dispatcher_heartbeat import intent_heartbeat_identity
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_tui import READINESS_BLOCKED, READINESS_BUSY
from secretary.dispatcher_types import (
    STOPPED_BY_LAUNCH_RECOVERY,
    HeadLaunchAborted,
    HeadPaneNotReady,
    HostError,
)
from secretary.dispatcher_watchdog import (
    bring_up_defer_attempts,
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)
from triggered_agents.runtime.head import operations as head_ops

WORKER_ROLE = "worker"
REVIEW_ROLE = "review"
# What each readiness state reads as in a line an operator has to act on. Orca's own words for the
# two are `busy` and `blocked`, and "blocked" is already the name of a board column here.
PANE_STATE_LABELS = {READINESS_BUSY: "busy", READINESS_BLOCKED: "held in a dialog"}

# A review launch whose document nudge met a working pane is not a failed launch.  The existing
# reviewer is still the exact run the intent names, so retry the nudge through that run rather than
# adopting it as though it had already received its task.  The delay is capped to keep the durable
# schedule finite even when a reviewer spends a long time in a real turn.
REVIEW_BUSY_RETRY_INITIAL_SECONDS = 30
REVIEW_BUSY_RETRY_MAX_SECONDS = 5 * 60

# What "the data plane refused this write" looks like. Deliberately not bare `Exception`: a launch
# that cannot be recorded is answered by not launching, and anything that is not a storage failure
# (a bug in the record, the health probe's own abort signal) has to keep travelling instead of
# being reported to the operator as a full disk.
STORAGE_ERRORS = (OSError, ValueError, TypeError, UnicodeError)


def launch_pid_file(role: str, reference: str) -> str:
    """The heartbeat file the head of this role writes. Known before the head exists."""
    return pid_file_path(role, reference)


def launch_intent(record: DispatcherRecord) -> dict[str, Any]:
    intent = getattr(record, "launch_intent", None)
    return intent if isinstance(intent, dict) and intent.get("role") else {}


def busy_launch_delivery(intent: dict[str, Any]) -> dict[str, Any]:
    """Return a durable, still-undelivered busy launch nudge, if this intent has one.

    Older launch intents have no delivery member.  They are deliberately not inferred to be busy:
    their missing evidence remains unknown and follows the historical recovery route.
    """
    delivery = intent.get("delivery") if isinstance(intent, dict) else None
    if not isinstance(delivery, dict):
        return {}
    if delivery.get("state") != READINESS_BUSY:
        return {}
    return dict(delivery)


def defer_busy_launch_delivery(
    record: DispatcherRecord,
    evidence: dict[str, Any],
    *,
    now: float | None = None,
) -> int:
    """Persist the next capped retry for a pre-send reviewer document nudge.

    The intent is the only durable owner of a reviewer which has not crossed launch delivery
    confirmation.  Keeping the retry there prevents a live heartbeat from being mistaken for a
    successful reviewer launch on the next dispatcher tick.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return 0
    previous = busy_launch_delivery(intent)
    attempts = int(previous.get("attempts") or 0) + 1
    delay = min(
        REVIEW_BUSY_RETRY_INITIAL_SECONDS * (2 ** min(attempts - 1, 4)),
        REVIEW_BUSY_RETRY_MAX_SECONDS,
    )
    intent["delivery"] = {
        "state": READINESS_BUSY,
        "attempts": attempts,
        "next_at": (time.time() if now is None else now) + delay,
        "evidence": dict(evidence),
    }
    record.launch_intent = intent
    return delay


def write_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    *,
    role: str,
    action: str,
    head: str,
    workspace: str,
    round_number: int | None = None,
) -> str | None:
    """Fix one bring-up on disk before the host is called. Returns the failure, or None.

    The record is left exactly as it was when the write fails, so a caller that answers with "no
    host call this tick" leaves nothing behind for the next one to misread.

    `round_number` is the round the head being launched will belong to. A rework opens a new one,
    and it has to be reserved here rather than after the host call: the round the intent carries is
    the round an adoption resumes, and a rework recovered on the round its red verdict ended would
    merge two rounds and their routing into one.
    """
    previous = dict(getattr(record, "launch_intent", None) or {})
    previous_workspace_settled = record.workspace_settled
    reserved = record.attempt_round if round_number is None else round_number
    run_id = uuid.uuid4().hex
    # The production host can attest a fully-shaped HeadRun before the intent is saved and before
    # its pane exists.  Test doubles intentionally do not implement this hook: they model a host
    # which has already crossed the boundary, while this module's real host path remains the only
    # path allowed to open a terminal.
    preflight_run: dict[str, Any] | None = None
    attest = getattr(getattr(runtime, "host", None), "preflight_codex_run", None)
    if callable(attest):
        document_name = "REVIEW.md" if role == REVIEW_ROLE else "TASK.md"
        try:
            candidate = attest(
                head,
                role="reviewer" if role == REVIEW_ROLE else role,
                workspace=workspace,
                task_ref=head_ops.TaskRef.card(ref, document=str(Path(workspace) / document_name)),
                pid_file=launch_pid_file(role, ref),
                run_id=run_id,
            )
        except Exception as exc:
            return f"codex-fanout-policy: {type(exc).__name__}: {exc}"
        preflight_run = candidate.to_json()
    record.workspace_settled = False
    record.launch_intent = {
        "role": role,
        "action": action,
        "head": head,
        "workspace": workspace,
        "pid_file": launch_pid_file(role, ref),
        # The run id is fixed before the host is touched.  A dispatcher that dies while Orca is
        # creating the pane can therefore still prove that a heartbeat belongs to this intent,
        # rather than adopting whichever later process happened to reuse its pid path.
        "run_id": run_id,
        "task": f"card:{ref}",
        "attempt_id": record.attempt_id,
        "round": reserved,
        "opens_round": bool(reserved) and reserved != record.attempt_round,
        "respawns": int(getattr(record, f"{role}_respawns", 0) or 0),
        "at": time.time(),
    }
    if preflight_run is not None:
        record.launch_intent["head_run"] = preflight_run
        _remember_head_run(record, role, preflight_run)
    records[ref] = record
    try:
        runtime.save_records(payload, records)
    except STORAGE_ERRORS as exc:
        record.launch_intent = previous
        record.workspace_settled = previous_workspace_settled
        return f"{type(exc).__name__}: {exc}"
    return None


def confirm_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    *,
    handle: str = "",
    leaf: str = "",
    run: dict[str, Any] | None = None,
    head_run: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
) -> None:
    """Put what the finished host call knows about the head into its intent, on disk.

    The pane and the launch snapshot exist only once the host has answered, and everything the tick
    still owes that head afterwards — its pane leaf, its routing record, its own save — runs against
    a process that is already up and can refuse. Writing them into the intent here is what lets a
    recovery adopt that head with the configuration it actually launched on, instead of inventing
    one from a registry that may have been edited since.

    `head_run` is the head's own run — its identity and its lifecycle, as the three head operations
    keep it — and it is why this is the single place a launched head is written down (secretary-1414).
    Every caller used to assign it to the record on the line after this call, which put a durable
    save between the two: a tick that died in that window left a head whose run nothing recorded,
    and the next tick reconstructed a fresh identity for the same process, so a stop already begun
    stopped being a continuation of itself and its initiator was lost. Written here, into the intent
    and onto the record in one save, there is no window to die in, and the adoption below brings
    that same run back.

    A refused write is not a problem: the pre-launch intent is already on disk and names the same
    head, so recovery still finds it and only its routing snapshot falls back to the registry.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return
    if handle:
        intent["handle"] = handle
    if leaf:
        intent["leaf"] = leaf
    if run:
        intent["run"] = dict(run)
    if head_run is not None:
        # `None` is a caller with nothing to say about the run; an empty dict is a bring-up that
        # answered without one — noop mode, or a host seam that reports none — and that answer
        # replaces whatever run the previous head of this role left on the record.
        if head_run:
            # The pre-send ingress can have durably bound a provider source before this caller
            # receives its launcher result.  Reconcile every copy that belongs to this launch
            # before writing: pane/lifecycle evidence may move forward, but a stale result cannot
            # write an unbound or conflicting source back over the handoff recovery will read.
            canonical = _canonical_launch_head_run(record, str(intent.get("role") or ""), intent, head_run)
            intent["head_run"] = canonical
            # The launcher writes this id into its heartbeat before Orca opens the pane. Keeping
            # the same value in the recovered intent makes the HeadRun, intent and file one
            # identity even if the ordinary record save lands later.
            intent["run_id"] = str(canonical.get("run_id") or intent.get("run_id") or "")
            head_run = canonical
        _remember_head_run(record, str(intent.get("role") or ""), head_run)
    if delivery is not None:
        # Delivery confirmation shares the intent write with the run it confirms.  A recovery
        # therefore either sees a pending busy nudge and retries it, or sees this exact run with
        # a confirmed nudge and may finish adoption; it never invents success from a heartbeat.
        intent["delivery"] = dict(delivery)
    intent["launched"] = True
    record.launch_intent = intent
    records[ref] = record
    _persist_quietly(runtime, payload, records)


def _remember_head_run(
    record: DispatcherRecord, role: str, head_run: dict[str, Any] | None
) -> None:
    """Put one role's head run on the record. The caller's save is what makes it durable."""
    if head_run is None or not role:
        return
    setattr(record, role_field(role, "head_run"), dict(head_run))


def _canonical_launch_head_run(
    record: DispatcherRecord,
    role: str,
    intent: dict[str, Any],
    reported: dict[str, Any],
) -> dict[str, Any]:
    """Return the one post-delivery HeadRun a launch intent is permitted to write.

    The ingress writer and the pane operation own different facts.  A callback can advance the
    provider source while the pane operation adds its returned handle and `working` lifecycle.
    Both must describe the exact same launch identity.  A foreign or damaged persistent copy is a
    fence, never a prompt to reconstruct or attribute a different head.
    """
    values: list[dict[str, Any]] = []
    stored = intent.get("head_run")
    if isinstance(stored, dict) and stored.get("run_id"):
        values.append(stored)
    record_run = getattr(record, role_field(role, "head_run"), {}) if role else {}
    if isinstance(record_run, dict) and record_run.get("run_id"):
        values.append(record_run)
    values.append(reported)
    # The ordinary adapter paths predate provider source binding and intentionally retain their
    # old launcher-result behavior.  The canonical merge is the Codex source-bearing contour;
    # applying it to a generic snapshot would turn harmless fixture/registry differences into a
    # new lifecycle fence without any source to protect.
    if not any(_has_provider_source(value) for value in values):
        return dict(reported)
    try:
        current = head_ops.HeadRun.from_json(values[0])
        for value in values[1:]:
            current = _merge_launch_head_runs(current, head_ops.HeadRun.from_json(value))
    except HostError:
        raise
    except (TypeError, ValueError) as exc:
        raise HostError(f"launch HeadRun handoff is unreadable or identity-mismatched: {exc}") from None
    return current.to_json()


def _has_provider_source(value: dict[str, Any]) -> bool:
    policy = value.get("fanout_policy") if isinstance(value, dict) else None
    return isinstance(policy, dict) and isinstance(policy.get("provider_source"), dict)


def _merge_launch_head_runs(current: head_ops.HeadRun, later: head_ops.HeadRun) -> head_ops.HeadRun:
    """Merge verified pane/lifecycle evidence without ever discarding a bound source."""
    if (
        not current.same_run(later)
        or current.spec != later.spec
        or current.workspace != later.workspace
        or current.task_ref != later.task_ref
        or current.role != later.role
        or current.pid_file != later.pid_file
    ):
        raise HostError("launch HeadRun identity mismatch")
    policy = _newer_provider_policy(current.fanout_policy, later.fanout_policy)
    lifecycle_rank = {"spawned": 0, "working": 1, "finishing": 2, "exited": 3}
    if lifecycle_rank.get(later.lifecycle, -1) < lifecycle_rank.get(current.lifecycle, -1):
        lifecycle = current.lifecycle
        stopped_by = current.stopped_by
    else:
        lifecycle = later.lifecycle
        stopped_by = later.stopped_by
    # The latest writer may know a newly returned pane identity; its empty fields never erase a
    # retained address, because that address is also what a fenced adoption must use after a crash.
    return replace(
        later,
        handle=later.handle or current.handle,
        leaf=later.leaf or current.leaf,
        lifecycle=lifecycle,
        stopped_by=stopped_by,
        fanout_policy=policy,
    )


def merge_launch_head_run(current: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    """Merge two source-bearing writers for one run, fencing foreign identity at the boundary."""
    try:
        return _merge_launch_head_runs(
            head_ops.HeadRun.from_json(current), head_ops.HeadRun.from_json(later),
        ).to_json()
    except HostError:
        raise
    except (TypeError, ValueError) as exc:
        raise HostError(f"launch HeadRun handoff is unreadable: {exc}") from None


def _newer_provider_policy(current: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    """Choose a compatible provider source, preferring a bound cursor over stale launch state."""
    current_source = current.get("provider_source") if isinstance(current, dict) else None
    later_source = later.get("provider_source") if isinstance(later, dict) else None
    if not isinstance(current_source, dict):
        if isinstance(current, dict) and current.get("provider_source_required") is True:
            raise HostError("persisted provider source is incomplete")
        return dict(later)
    if not isinstance(later_source, dict):
        # A pane/lifecycle writer that knows nothing about a source has no authority to erase
        # either the preflight baseline or the source binding it was handed.
        return dict(current)
    current_state = str(current_source.get("state") or "")
    later_state = str(later_source.get("state") or "")
    if current_state == "unbound":
        preflight_keys = (
            "version", "kind", "run_id", "head_run_fingerprint", "workspace", "role", "task_ref",
            "root", "baseline",
        )
        if any(current_source.get(key) != later_source.get(key) for key in preflight_keys):
            raise HostError("preflight provider source conflicts for one launch HeadRun")
        if later_state in ("unbound", "bound"):
            return dict(later)
        return dict(current)
    if current_state != "bound":
        # This is a typed unavailable/identity-mismatch record.  It is intentionally not healed
        # by a later local result which happens to carry a well-formed source.
        raise HostError("persisted provider source is unavailable")
    if later_state != "bound":
        return dict(current)
    identity_keys = (
        "version", "kind", "run_id", "head_run_fingerprint", "workspace", "role", "task_ref",
        "root", "baseline", "path", "session_id", "parent_thread_id", "initial_range",
    )
    if any(current_source.get(key) != later_source.get(key) for key in identity_keys):
        raise HostError("bound provider sources conflict for one launch HeadRun")
    current_cursor = current_source.get("cursor") if isinstance(current_source.get("cursor"), dict) else {}
    later_cursor = later_source.get("cursor") if isinstance(later_source.get("cursor"), dict) else {}
    current_line = current_cursor.get("line")
    later_line = later_cursor.get("line")
    if not isinstance(current_line, int) or not isinstance(later_line, int):
        raise HostError("bound provider source cursor is malformed")
    if current_line == later_line:
        if current_cursor.get("digest") != later_cursor.get("digest"):
            raise HostError("bound provider source cursor conflicts for one launch HeadRun")
        return dict(later)
    return dict(later if later_line > current_line else current)


def launch_left_a_head(record: DispatcherRecord) -> bool:
    """Does this launch's heartbeat prove a head exists, whatever the failure claimed?

    A host reporting an ordinary failure is claiming that no head of this bring-up is running. The
    heartbeat is the one piece of evidence that can contradict it, and where it does, the intent has
    to survive: a record dropped over a live process is the second head this contour prevents.
    """
    intent = launch_intent(record)
    if not intent:
        return False
    status = head_process_status(
        str(intent.get("pid_file") or ""), expected=intent_heartbeat_identity(intent)
    )
    return bool(status.get("match"))


def role_field(role: str, suffix: str) -> str:
    return f"{'review' if role == REVIEW_ROLE else 'worker'}_{suffix}"


def clear_launch_intent(record: DispatcherRecord) -> None:
    """Take back an intent whose host call has answered. The caller's own save persists it.

    The heartbeat the intent named stays on the record as that role's head identity. It is what
    every later stop falls back on when the pane handle is missing — an adopted head never had one
    — and without it a freeze, a respawn or a red-verdict bounce would quietly stop nothing and
    open a second process beside the one still running.
    """
    intent = launch_intent(record)
    role = str(intent.get("role") or "")
    pid_file = str(intent.get("pid_file") or "")
    if role and pid_file:
        setattr(record, role_field(role, "pid_file"), pid_file)
    record.launch_intent = {}


def forget_role_head(record: DispatcherRecord, role: str) -> None:
    """Drop every pointer to one role's head, once it is confirmed gone."""
    if role == REVIEW_ROLE:
        record.review_handle = ""
        record.review_leaf = ""
        record.review_pid_file = ""
        return
    record.handle = ""
    record.worker_leaf = ""
    record.worker_pid_file = ""
    # Nothing is left to resume, but a red transition already opened over that head still owes the
    # card its replacement and outlives the pointers.
    record.worker_continuation.drop_session()


def mark_launch_aborted(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    exc: HeadLaunchAborted,
) -> None:
    """Keep the intent of a bring-up that failed with a pane already open.

    The host could not promise that nothing of that head is running, so the intent survives with
    whatever identity the failure carried. The next tick reads the heartbeat and ordinarily adopts
    the head — pane included, so its lifecycle is whole — or stops what is left of it. A reviewer
    whose pre-send document nudge recorded busy is the narrow exception: it retries that exact
    nudge before either adoption side effect. A persist that refuses here is not a problem: the
    pre-launch intent is already on disk and names the same pid file, which is the identity
    recovery actually settles the question with.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return
    if exc.handle:
        intent["handle"] = exc.handle
    if exc.leaf:
        intent["leaf"] = exc.leaf
    if exc.head_run:
        # A bring-up that spawned its head and then failed over it knows that head's run, and the
        # pane it left is live. Carrying it here is what lets the adoption continue that run rather
        # than reconstruct one for a head this dispatcher did in fact start (secretary-1414).
        intent["head_run"] = dict(exc.head_run)
        _remember_head_run(record, str(intent.get("role") or ""), exc.head_run)
    intent["aborted"] = True
    record.launch_intent = intent
    records[ref] = record
    _persist_quietly(runtime, payload, records)


def launch_aborted(
    *, step: str, ref: str, attempt_id: str, role: str, reason: str
) -> dict[str, Any]:
    """The outcome of a bring-up that may have left a head running and could not confirm it."""
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-launch-aborted",
        "reason": f"launch may have left a head running: {reason}",
    }


def role_label(role: str) -> str:
    """What this role's head is called in a line an operator reads."""
    return "reviewer" if role == REVIEW_ROLE else "worker"


def pane_state_label(readiness: str) -> str:
    return PANE_STATE_LABELS.get(readiness, readiness or "not ready")


def launch_attempts(record: DispatcherRecord, role: str) -> int:
    return int(getattr(record, role_field(role, "launch_attempts"), 0) or 0)


def reset_launch_attempts(record: DispatcherRecord, role: str) -> None:
    """This role's head came up. The deferrals before it belong to an episode that is over."""
    setattr(record, role_field(role, "launch_attempts"), 0)


def launch_deferred(
    record: DispatcherRecord,
    exc: Exception,
    *,
    step: str,
    ref: str,
    attempt_id: str,
    role: str,
) -> dict[str, Any] | None:
    """Park a bring-up whose head pane would not take its prompt. None when it cannot be parked.

    None means the ordinary failure path owns this failure: either it is not a pane that was busy
    or held in a dialog — a probe nobody answered is deliberately not one, since a pane that cannot
    be asked is not a pane anything can wait for — or this role has spent its attempts and the card
    is blocked over that pane.

    A parked launch changes nothing else on the record. The state it is in is what the next tick
    relaunches from, exactly as it would have without this failure, and only the counter moves. The
    caller persists it: a deferral nobody wrote down is an unbounded retry.
    """
    if not isinstance(exc, HeadPaneNotReady):
        return None
    limit = bring_up_defer_attempts()
    attempts = launch_attempts(record, role) + 1
    if attempts > limit:
        return None
    setattr(record, role_field(role, "launch_attempts"), attempts)
    return {
        "status": "skipped",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-launch-deferred",
        "readiness": exc.readiness,
        "attempts": attempts,
        # Every deferred attempt says which one it is, so an operator reading the tick can tell a
        # head that is still coming up from a head that has not come up for ten minutes.
        "reason": (
            f"the {role_label(role)} head pane is {pane_state_label(exc.readiness)}; bring-up "
            f"attempt {attempts} of {limit} is deferred to the next tick: "
            f"{scrub_host_output(str(exc))}"
        ),
    }


def bring_up_blocked_reason(
    default: str, exc: Exception, record: DispatcherRecord, role: str
) -> str:
    """The card's Blocked reason for a bring-up that will not be retried.

    A pane that never became ready is named, with the state it stayed in and how many bring-ups it
    cost. `default` is the caller's own wording for every other failure, which is what "bring-up
    failed" means and all it should ever have meant.
    """
    if not isinstance(exc, HeadPaneNotReady):
        return f"{default}: {scrub_host_output(str(exc))}"
    return (
        f"the {role_label(role)} head pane was {pane_state_label(exc.readiness)} on all "
        f"{launch_attempts(record, role) + 1} bring-up attempts and never took its launch prompt: "
        f"{scrub_host_output(str(exc))}"
    )


def head_stop_unconfirmed(
    *, step: str, ref: str, attempt_id: str, role: str, reason: str
) -> dict[str, Any]:
    """The outcome of a tick that refused to launch because a stop was not confirmed.

    A head the host would not promise is gone is a head that may still be editing the checkout, so
    nothing takes its place this tick. The card keeps its record and the next tick retries the stop.
    """
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-stop-unconfirmed",
        "reason": f"the previous head could not be confirmed stopped: {reason}",
    }


def launch_intent_unwritable(
    *, step: str, ref: str, attempt_id: str, role: str, reason: str
) -> dict[str, Any]:
    """The outcome of a tick that refused to launch because it could not record the launch."""
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-launch-intent-unwritable",
        "reason": f"launch intent could not be persisted: {reason}",
    }


def launch_intent_liveness(intent: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Whether the head this intent was written for is running, and on what evidence.

    `pid_known: False` means the heartbeat is not readable yet. A head launched seconds ago has not
    written one, so that reads as alive until the grace window the watchdog already uses for a pane
    that has produced no output at all has passed.
    """
    now = time.time() if now is None else now
    status = head_process_status(
        str(intent.get("pid_file") or ""), expected=intent_heartbeat_identity(intent)
    )
    if status.get("state") == "identity-mismatch":
        return {"alive": False, "pid_known": True, "identity_mismatch": True}
    if status.get("known"):
        return {"alive": bool(status.get("match")), "pid_known": True}
    started_at = float(intent.get("at") or 0.0)
    if started_at and now - started_at <= initial_output_stall_seconds():
        return {"alive": True, "pid_known": False}
    return {"alive": False, "pid_known": False}


def resolve_launch_intent(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Settle an unresolved bring-up before the tick acts on this card.

    Returns the tick's outcome when the intent takes the tick (adopted, or still coming up), and
    None when there is nothing pending or the launch left nothing running, in which case the
    ordinary path relaunches from a record that no longer claims a head.
    """
    ref = task["ref"]
    record = records.get(ref)
    if record is None:
        return None
    intent = launch_intent(record)
    if not intent:
        return None
    role = str(intent.get("role"))
    step = "review" if role == REVIEW_ROLE else "advance"
    liveness = launch_intent_liveness(intent)
    if liveness.get("identity_mismatch"):
        # The pid is a living process, but not this launch.  It cannot be signalled or attributed
        # to this HeadRun, and a replacement over the still-owned pane would be just as unsafe.
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-heartbeat-identity-mismatch",
            "head": str(intent.get("head") or ""),
            "reason": "launch heartbeat names a live process with a mismatching launch identity",
        }
    if liveness["alive"] and not liveness["pid_known"]:
        # Left exactly as it is: the next tick asks again, once the grace window has either
        # produced a heartbeat or run out. Killing a head that is still starting would cost the
        # card its session for no evidence at all.
        return {
            "status": "skipped",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-launch-pending",
            "head": str(intent.get("head") or ""),
            "reason": "a launch intent is still within its grace window and has written no pid yet",
        }
    if not liveness["alive"]:
        failure = stop_launch_intent(runtime, record, intent, role)
        if failure is None:
            keep_reserved_round(runtime, record, intent)
        _persist_quietly(runtime, payload, records)
        if failure is not None:
            return head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id,
                role=role,
                reason=failure,
            )
        return None
    if role == REVIEW_ROLE and busy_launch_delivery(intent):
        # `dispatcher_review` owns the review document and its confirmation rule.  Importing it
        # only here avoids its normal dependency on this launch-intent module becoming a cycle at
        # import time.  A busy result takes the tick; a confirmed retry falls through to the same
        # adoption boundary every completed reviewer launch uses.
        from secretary.dispatcher_review import retry_busy_reviewer_launch_delivery

        deferred = retry_busy_reviewer_launch_delivery(
            runtime, task, records, payload, record, intent, step
        )
        if deferred is not None:
            return deferred
        intent = launch_intent(record)
    return _adopt_launch_intent(runtime, task, records, payload, record, intent, role, step)


def keep_reserved_round(runtime: Any, record: DispatcherRecord, intent: dict[str, Any]) -> None:
    """Carry the round a dropped worker intent reserved onto the record it was written over.

    A rework reserves the next round before the host call, while the record still carries the round
    the red result closed. Dropping such an intent — the launch left nothing running, or a freeze
    stopped what it left — is not the same as giving the reservation back: the round is over either
    way, and the relaunch that follows belongs to the new one. Without this the respawn runs the
    rework inside the round that rejected it, and its routing and its verdict land there too.

    Only for an intent that opens a round. A claim or a respawn continues the round the record
    already names, and its state is the ordinary path's to decide.
    """
    if str(intent.get("role") or "") == REVIEW_ROLE:
        return
    reserved = int(intent.get("round") or 0)
    if not intent.get("opens_round") or not reserved or record.attempt_round == reserved:
        return
    runtime.open_worker_round(record, round_number=reserved)
    # The state the rework bring-up would have written. The head is not up, so the wait watchdog
    # owns the relaunch from here, and it does it inside the round opened above.
    record.state = "claimed"


def stop_launch_intent(
    runtime: Any, record: DispatcherRecord, intent: dict[str, Any], role: str
) -> str | None:
    """End whatever a launch left behind and take its intent back. Returns the failure, or None.

    Called both for a launch whose head is not there — the pane can outlive the process, and the
    relaunch reuses this workspace — and wherever a card leaves the cycle with an intent still on
    it. Either way the intent survives an unconfirmed stop: a head the host will not promise is
    gone must keep a pointer, or a later requeue starts a second process in the same checkout.

    The identity is whatever the launch got as far as recording: the pane the failure handed back,
    the heartbeat the head wrote for itself, and failing both the workspace itself. That last case
    is a head nothing can name individually — a raw `SECRETARY_DISPATCHER_*_COMMAND` override runs
    without the heartbeat wrapper — and stopping the workspace is all that can still reach it.
    """
    _remember_launch_identity(record, intent, role)
    handle = getattr(record, "review_handle" if role == REVIEW_ROLE else "handle", "")
    leaf = getattr(record, "review_leaf" if role == REVIEW_ROLE else "worker_leaf", "")
    pid_file = getattr(record, role_field(role, "pid_file"), "")
    # The path is known before the head exists, so it says nothing on its own; a heartbeat that can
    # be read is what proves this launch left something a role-scoped stop can address.
    status = head_process_status(pid_file, expected=intent_heartbeat_identity(intent))
    if status.get("state") == "identity-mismatch":
        return "launch heartbeat names a live process with a mismatching launch identity"
    named = bool(handle or leaf) or bool(status.get("known"))
    try:
        if role == REVIEW_ROLE and named:
            # Imported here rather than at module scope: `dispatcher_review` writes the reviewer's
            # intent through this module, and a top-level import either way would be a cycle.
            from secretary.dispatcher_review import end_review_pane

            end_review_pane(runtime.host, record, STOPPED_BY_LAUNCH_RECOVERY)
        elif named:
            runtime.host.stop_head(record, WORKER_ROLE, STOPPED_BY_LAUNCH_RECOVERY)
            forget_role_head(record, WORKER_ROLE)
        else:
            # A legacy launch without a role identity can only be settled by its workspace.  A
            # worker or reviewer that has a leaf is always stopped through that exact pane above.
            runtime.host.stop_workspace(record)
            forget_role_head(record, WORKER_ROLE)
            forget_role_head(record, REVIEW_ROLE)
    except HostError as exc:
        return f"{type(exc).__name__}: {exc}"
    record.launch_intent = {}
    return None


def _remember_launch_identity(
    record: DispatcherRecord, intent: dict[str, Any], role: str
) -> None:
    """Put the launch's own identity on the record, so the stop paths can reach its head."""
    pid_file = str(intent.get("pid_file") or "")
    handle = str(intent.get("handle") or "")
    leaf = str(intent.get("leaf") or "")
    # A first claim writes its intent before the record knows where the head runs, so the
    # workspace comes back off the intent here: without it the stop has no worktree to address.
    record.workspace = record.workspace or str(intent.get("workspace") or "")
    if pid_file:
        setattr(record, role_field(role, "pid_file"), pid_file)
    if role == REVIEW_ROLE:
        record.review_handle = record.review_handle or handle
        record.review_leaf = record.review_leaf or leaf
    else:
        record.handle = record.handle or handle
        record.worker_leaf = record.worker_leaf or leaf
    stored_run = intent.get("head_run")
    if not isinstance(stored_run, dict) or not stored_run.get("run_id"):
        # An interrupted create can leave only the pre-launch intent and a heartbeat.  Rebuild the
        # same HeadRun identity from that intent before routing it through a fenced stop; minting
        # a new run here would turn our own live head into an apparent foreign process.
        run_id = str(intent.get("run_id") or "")
        task = str(intent.get("task") or "")
        task_kind, separator, task_ref = task.partition(":")
        if run_id and separator and task_kind == "card" and task_ref:
            stored_run = head_ops.HeadRun(
                run_id=run_id,
                spec=head_ops.HeadSpec(
                    profile_id=str(intent.get("head") or "unknown"), adapter="unknown"
                ),
                workspace=record.workspace,
                task_ref=head_ops.TaskRef.card(task_ref),
                handle=handle,
                leaf=leaf,
                pid_file=pid_file,
            ).to_json()
    _remember_head_run(record, role, stored_run if isinstance(stored_run, dict) else None)


def _adopt_launch_intent(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
) -> dict[str, Any]:
    """Take the head of a launch whose tick did not survive as this card's head for that role.

    The run comes back first, and the pane pointers below are re-addressed on top of it: the head
    being adopted is the head that launch started, so its identity, its lifecycle and any initiator
    a stop already wrote are the launch's, while the handle, the leaf and the heartbeat are only
    where that same head is currently reachable. Nothing after this reconstructs a run over it —
    `worker_lifecycle_run`/`review_lifecycle_run` reconstruct only for a record that carries none,
    and after this one does (secretary-1414).
    """
    ref = task["ref"]
    launched_at = float(intent.get("at") or 0.0) or time.time()
    record.workspace = record.workspace or str(intent.get("workspace") or "")
    handle = str(intent.get("handle") or "")
    leaf = str(intent.get("leaf") or "")
    stored_run = intent.get("head_run")
    if not isinstance(stored_run, dict) or not stored_run.get("run_id"):
        # A launch can fail after its process wrote the pre-pane heartbeat but before the host can
        # return a HeadRun.  The intent's run id was fixed before that process existed, so it is
        # enough to recover the same durable run rather than minting a new identity over it.
        run_id = str(intent.get("run_id") or "")
        if run_id:
            stored_run = head_ops.HeadRun(
                run_id=run_id,
                spec=head_ops.HeadSpec(
                    profile_id=str(intent.get("head") or "unknown"), adapter="unknown"
                ),
                workspace=record.workspace,
                task_ref=head_ops.TaskRef.card(ref),
                handle=handle,
                leaf=leaf,
                pid_file=str(intent.get("pid_file") or ""),
            ).to_json()
    _remember_head_run(record, role, stored_run if isinstance(stored_run, dict) else None)
    if role == REVIEW_ROLE:
        # A reviewer bring-up shuts the worker head down before it hands the pane back, and an
        # adopted one has to do the same: a worker still editing the checkout would leave the
        # verdict describing a tree that no longer exists. It goes through the same confirmed stop,
        # so a worker that will not die keeps the intent instead of being assumed gone.
        record.review_handle = handle
        record.review_leaf = leaf
        record.review_pid_file = str(intent.get("pid_file") or "")
        try:
            runtime.host.freeze_worker(record)
        except HostError as exc:
            _persist_quietly(runtime, payload, records)
            return head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id,
                role=WORKER_ROLE,
                reason=f"{type(exc).__name__}: {exc}",
            )
        forget_role_head(record, WORKER_ROLE)
        record.state = "reviewing"
        record.review_started_at = record.review_progress_at = launched_at
        if not record.review_commit:
            # The worker is down and the reviewer writes no commits, so the checkout still sits
            # where the launch pinned it. The merge gate needs that sha to accept the verdict.
            record.review_commit = runtime.host.head_commit(record)
        deferred = _record_adopted_routing(
            runtime, task, records, payload, record, intent, role, step
        )
        if deferred is not None:
            return deferred
        clear_launch_intent(record)
    else:
        record.state = "claimed"
        reserved = int(intent.get("round") or 0)
        if intent.get("opens_round") and reserved:
            # The launch was a rework: it reserved the next round before calling the host, and the
            # adopted head belongs to that round. Opening it here is what keeps the rework's
            # routing and its verdict apart from the round the red result closed.
            runtime.open_worker_round(record, round_number=reserved)
        # Whatever pane identity the launch got as far as reporting. Usually none: the tick died
        # before the host answered. The heartbeat `clear_launch_intent` keeps is what the freeze,
        # the respawn and the red-verdict bounce then stop this head by.
        record.handle = handle
        record.worker_leaf = leaf
        record.worker_started_at = record.worker_progress_at = launched_at
        deferred = _record_adopted_routing(
            runtime, task, records, payload, record, intent, role, step
        )
        if deferred is not None:
            return deferred
        clear_launch_intent(record)
    records[ref] = record
    _persist_quietly(runtime, payload, records)
    return {
        "status": "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": f"{role}-launch-adopted",
        "head": str(intent.get("head") or ""),
        "reason": "a launch intent was left by a tick that did not finish; its head is alive",
    }


def _record_adopted_routing(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
) -> dict[str, Any] | None:
    """Give the adopted head its routing record. Returns the tick's outcome when that write fails.

    The head an interrupted tick launched is a head that ran, so the round owes it the same routing
    event every other bring-up writes: without it the verdict names only the other role, and the
    history reads as a round nobody worked. The snapshot is the launch's own where the intent got as
    far as carrying it, and the registry as it stands now otherwise.

    A journal that refuses keeps the intent: the head is adopted again next tick and the routing is
    retried then, which is one head with late telemetry instead of a head with none.
    """
    ref = task["ref"]
    run = intent.get("run") if isinstance(intent.get("run"), dict) else None
    try:
        if role == REVIEW_ROLE:
            runtime.record_review_routing(task, record, run)
        else:
            runtime.record_worker_routing(task, record, run)
    except Exception as exc:  # noqa: BLE001 — any journal refusal, whatever the plane called it
        records[ref] = record
        _persist_quietly(runtime, payload, records)
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-launch-adopt-deferred",
            "head": str(intent.get("head") or ""),
            "reason": f"the adopted head could not be recorded in the routing journal: {exc}",
        }
    return None


def _persist_quietly(
    runtime: Any, payload: dict[str, Any], records: dict[str, DispatcherRecord]
) -> bool:
    """Flush the records mid-tick. False means this tick's own save is what carries them.

    Never raised at the caller: an adoption that is not persisted is repeated by the next tick
    from the same intent, which is the same answer rather than a second head.
    """
    try:
        runtime.save_records(payload, records)
    except STORAGE_ERRORS:
        return False
    return True
