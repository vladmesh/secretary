"""State helpers for the pilot dispatcher."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from secretary._fsutil import write_json


class DispatcherStateError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


@dataclass
class DispatcherRecord:
    worker: str
    workspace: str
    handle: str
    head: str
    review_head: str
    attempt_id: str
    comment_baseline: int
    review_baseline: int
    state: str
    claimed_at: float
    # Mechanical validation gate (secretary-633): "" until the gate is green for the current code
    # state, then "green". Reset to "" on every fresh entry to validate so a reworked card re-runs
    # the gate instead of coasting on a stale pass. gate_pending_since stamps when a github CI
    # rollup first went non-terminal, driving the pending watchdog.
    gate_state: str = ""
    gate_pending_since: float = 0.0
    # Last checkout rejected by a mechanical gate or red review in this attempt. A worker that
    # reports done again at this exact SHA has not produced a new result, so the dispatcher can
    # return it to rework once and then escalate instead of looping forever.
    rejected_sha: str = ""
    rejected_done_reports: int = 0
    # Reviewer pane (secretary-651). The reviewer runs in its own split pane inside the worker's
    # worktree, so its terminal handle must be tracked apart from `handle` (the worker's) or
    # stopping one takes down the other and recovery cannot tell them apart. review_leaf is the
    # pane's leafId: `terminal list` can hand back a different handle alias for the same pty, so
    # the leaf is the stable token to re-find the pane by. review_commit pins the checkout the
    # reviewer was pointed at; the merge gate refuses a verdict once HEAD has moved off it.
    review_handle: str = ""
    review_leaf: str = ""
    review_commit: str = ""
    # The worker pane has the same handle-alias problem as the reviewer pane.  Keep its leafId
    # too, so an inventory alias cannot turn a live worker into a missing-terminal respawn.
    worker_leaf: str = ""
    # Wait watchdogs (secretary-654): when the current wait for a worker report / review
    # verdict started, and how many times that wait has already respawned its head. Both
    # reset whenever the card enters a fresh wait of that kind.
    worker_waiting_since: float = 0.0
    worker_respawns: int = 0
    # Most recent output from the tracked head pane.  This is deliberately pane-scoped: output
    # from an unrelated shell in the same worktree must not keep a broken head alive.
    worker_started_at: float = 0.0
    worker_progress_at: float = 0.0
    review_waiting_since: float = 0.0
    review_respawns: int = 0
    review_started_at: float = 0.0
    review_progress_at: float = 0.0
    # Pause (secretary-731): when a freeze stopped this card's worker / reviewer head, 0.0 when it
    # did not. A head with an empty handle is otherwise indistinguishable from one that died, so
    # these are what let the tick log and pause-status say "stopped on purpose". Cleared on resume,
    # by the relaunch or by the decision not to relaunch.
    paused_worker_at: float = 0.0
    paused_reviewer_at: float = 0.0
    # Routing telemetry (secretary-716). attempt_round counts the card's worker rounds: claim opens
    # round 1, every rework bounce (red verdict, red gate) opens the next one. worker_run/review_run
    # are the launch snapshots of the heads currently serving that round, kept here so the verdict
    # record reports the configuration the heads actually started with rather than re-reading a
    # `heads.toml` that may have been edited since. Canon is the journal; this is the live copy.
    attempt_round: int = 0
    worker_run: dict[str, Any] = field(default_factory=dict)
    review_run: dict[str, Any] = field(default_factory=dict)
    # Durable launch intent (secretary-820): the bring-up this record is in the middle of, written
    # before the host is asked for a head and cleared once the host has answered. Empty at rest.
    # `dispatcher_launch` owns its shape and its recovery; nothing else reads inside it.
    launch_intent: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "claimed_at": self.claimed_at,
            "comment_baseline": self.comment_baseline,
            "gate_pending_since": self.gate_pending_since,
            "gate_state": self.gate_state,
            "handle": self.handle,
            "head": self.head,
            "attempt_id": self.attempt_id,
            "attempt_round": self.attempt_round,
            "paused_reviewer_at": self.paused_reviewer_at,
            "paused_worker_at": self.paused_worker_at,
            "review_baseline": self.review_baseline,
            "review_commit": self.review_commit,
            "review_handle": self.review_handle,
            "review_head": self.review_head,
            "review_leaf": self.review_leaf,
            "review_progress_at": self.review_progress_at,
            "review_respawns": self.review_respawns,
            "review_started_at": self.review_started_at,
            "review_waiting_since": self.review_waiting_since,
            "rejected_done_reports": self.rejected_done_reports,
            "rejected_sha": self.rejected_sha,
            "state": self.state,
            "worker": self.worker,
            "worker_leaf": self.worker_leaf,
            "worker_progress_at": self.worker_progress_at,
            "worker_respawns": self.worker_respawns,
            "worker_started_at": self.worker_started_at,
            "worker_run": self.worker_run,
            "review_run": self.review_run,
            "launch_intent": dict(self.launch_intent),
            "worker_waiting_since": self.worker_waiting_since,
            "workspace": self.workspace,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "DispatcherRecord":
        return cls(
            worker=str(payload.get("worker") or ""),
            workspace=str(payload.get("workspace") or ""),
            handle=str(payload.get("handle") or ""),
            head=str(payload.get("head") or ""),
            review_head=str(payload.get("review_head") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            attempt_round=int(payload.get("attempt_round") or 0),
            worker_run=_run_snapshot(payload.get("worker_run")),
            review_run=_run_snapshot(payload.get("review_run")),
            launch_intent=_run_snapshot(payload.get("launch_intent")),
            comment_baseline=int(payload.get("comment_baseline") or 0),
            review_baseline=int(payload.get("review_baseline") or 0),
            state=str(payload.get("state") or "claimed"),
            claimed_at=float(payload.get("claimed_at") or time.time()),
            gate_state=str(payload.get("gate_state") or ""),
            gate_pending_since=float(payload.get("gate_pending_since") or 0.0),
            rejected_sha=str(payload.get("rejected_sha") or ""),
            rejected_done_reports=int(payload.get("rejected_done_reports") or 0),
            review_handle=str(payload.get("review_handle") or ""),
            review_leaf=str(payload.get("review_leaf") or ""),
            review_commit=str(payload.get("review_commit") or ""),
            worker_leaf=str(payload.get("worker_leaf") or ""),
            worker_waiting_since=float(payload.get("worker_waiting_since") or 0.0),
            worker_respawns=int(payload.get("worker_respawns") or 0),
            worker_started_at=float(payload.get("worker_started_at") or 0.0),
            worker_progress_at=float(payload.get("worker_progress_at") or 0.0),
            review_waiting_since=float(payload.get("review_waiting_since") or 0.0),
            review_respawns=int(payload.get("review_respawns") or 0),
            review_started_at=float(payload.get("review_started_at") or 0.0),
            review_progress_at=float(payload.get("review_progress_at") or 0.0),
            paused_worker_at=float(payload.get("paused_worker_at") or 0.0),
            paused_reviewer_at=float(payload.get("paused_reviewer_at") or 0.0),
        )


def _run_snapshot(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class CutoverState:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "dispatcher"
        self.path = self.root / "pilot-state.json"
        self.tick_lock = self.root / "pilot-tick.lock"

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "phase": "new"}
        except (OSError, ValueError, UnicodeError):
            raise DispatcherStateError("state_unavailable", "dispatcher state is unreadable", 2) from None
        if not isinstance(payload, dict):
            raise DispatcherStateError("state_unavailable", "dispatcher state has an unsupported shape", 2)
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        write_json(self.path, payload)

    def records(self, payload: dict[str, Any]) -> dict[str, DispatcherRecord]:
        raw = payload.get("records") or {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(ref): DispatcherRecord.from_json(record)
            for ref, record in raw.items()
            if isinstance(record, dict)
        }

    def put_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        payload["records"] = {ref: record.to_json() for ref, record in sorted(records.items())}


def now_rfc3339() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_attempt_id() -> str:
    return f"attempt-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:12]}"


def ensure_attempt(payload: dict[str, Any], reference: str, actor: str, owner: str) -> str:
    attempt_id = str(payload.get("attempt_id") or "")
    if attempt_id:
        return attempt_id
    attempt_id = new_attempt_id()
    payload["attempt_id"] = attempt_id
    record_attempt(payload, attempt_id, reference, actor, owner)
    return attempt_id


def record_attempt(
    payload: dict[str, Any],
    attempt_id: str,
    reference: str,
    actor: str,
    owner: str,
) -> None:
    attempts = payload.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        payload["attempts"] = attempts
    if any(isinstance(attempt, dict) and attempt.get("attempt_id") == attempt_id for attempt in attempts):
        return
    attempts.append({
        "attempt_id": attempt_id,
        "pilot_ref": reference,
        "owner": owner,
        "started_at": now_rfc3339(),
        "started_by": actor,
    })


def mark_attempt_rolled_back(payload: dict[str, Any], actor: str, reason: str) -> None:
    attempt_id = str(payload.get("attempt_id") or "")
    attempts = payload.get("attempts")
    if not attempt_id or not isinstance(attempts, list):
        return
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and attempt.get("attempt_id") == attempt_id:
            attempt["rolled_back_at"] = now_rfc3339()
            attempt["rolled_back_by"] = actor
            attempt["rollback_reason"] = reason
            return


def attempt_request_id(attempt_id: str, action: str, reference: str, suffix: str = "") -> str:
    parts = ["dispatcher", request_token(attempt_id or "attempt-missing"), action, reference]
    if suffix:
        parts.append(suffix)
    return "-".join(request_token(part) for part in parts)


def request_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return token or "empty"


def claim_mismatch(
    task: dict[str, Any],
    worker: str,
    resolved_head: str,
    resolved_review_head: str,
) -> list[str]:
    mismatches = []
    if task.get("state") != "in_progress":
        mismatches.append("state")
    if task.get("claim", {}).get("worker") != worker:
        mismatches.append("worker")
    routing = task.get("routing", {})
    if routing.get("resolved_worker_head") != resolved_head:
        mismatches.append("resolved_head")
    if routing.get("resolved_review_head") != resolved_review_head:
        mismatches.append("resolved_review_head")
    return mismatches


def claim_actual(task: dict[str, Any]) -> dict[str, Any]:
    routing = task.get("routing", {})
    return {
        "state": task.get("state"),
        "worker": task.get("claim", {}).get("worker"),
        "resolved_head": routing.get("resolved_worker_head"),
        "resolved_review_head": routing.get("resolved_review_head"),
    }


def record_divergence(
    payload: dict[str, Any],
    attempt_id: str,
    reference: str,
    step: str,
    reason: str,
    *,
    expected: dict[str, Any],
    actual: dict[str, Any],
    details: list[str],
) -> dict[str, Any]:
    divergences = payload.setdefault("controlled_divergences", [])
    if not isinstance(divergences, list):
        divergences = []
        payload["controlled_divergences"] = divergences
    divergence = {
        "id": f"div_{uuid.uuid4().hex[:16]}",
        "at": now_rfc3339(),
        "attempt_id": attempt_id,
        "pilot_ref": reference,
        "step": step,
        "reason": reason,
        "expected": expected,
        "actual": actual,
        "details": details,
        # Opening rule: every divergence starts open. Closing rule lives with the
        # production tick (see `_reconcile_production` in dispatcher_production.py):
        # a divergence closes once its card leaves the active dispatcher cycle
        # (in_progress/validate), whatever state it lands in. A divergence with no
        # "status" is a pre-existing record from before this field existed and is
        # treated as open.
        "status": "open",
    }
    divergences.append(divergence)
    return divergence


def divergence_is_open(divergence: dict[str, Any]) -> bool:
    return divergence.get("status") != "closed"


def close_divergence(divergence: dict[str, Any], reason: str) -> None:
    divergence["status"] = "closed"
    divergence["closed_at"] = now_rfc3339()
    divergence["closed_reason"] = reason
