"""Read-only Phase 5 task protocol backed by the Pipeline Kanboard."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.board.card_transitions import CardTransitionForbidden, card_transition
from secretary.board.events import AnalyticsOutcomeConflict, BoardEventCanon, BoardEventPending
from secretary.board.host import MarkerComment, MutationResult, TransitionRequest
from secretary.board.models import (
    Actor,
    CardState,
    EntityKind,
    Event,
    EventKind,
    RelatedRefs,
)
from secretary.board.protocol_artifacts import (
    ArtifactOwnershipViolation,
    validate_rework_prerequisites,
)
from secretary.board.transitions import BoardProtocolError
from secretary.board_transport import (
    BoardTransport,
    BoardTransportError,
    resolve,
    transport_path,
)
from secretary.projects.integration_base import (
    integration_base_refusal,
    seed_ref_refusal,
)
from secretary.role_env import RUNTIME_ENV_FILE_ENVS, runtime_env_path
from triggered_agents.runtime.head import CODEX_LAUNCH_MODES
from triggered_agents.runtime.paths import instance_dir as normalize_instance_dir
from triggered_agents.runtime.redact import redact
from triggered_agents.runtime.references import (
    BoardRowsUnavailable,
    board_rows,
    next_reference,
    reference_allocation_lock,
)


class TaskError(Exception):
    """A task command failed without exposing backend credentials."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


class ArtifactOwnershipTaskError(TaskError):
    """The task-protocol form of a registry-backed ownership violation."""

    def __init__(self, violation: ArtifactOwnershipViolation) -> None:
        self.violation = violation
        super().__init__("artifact_ownership_violation", violation.message, 3)


class _CommittedWriteError(Exception):
    """A later step failed after a Kanboard mutation was committed."""


RUNTIME_TAILS = ("secretary-data",)


def durability_dirt(porcelain: str) -> list[str]:
    """Porcelain lines that count against durability."""
    dirt: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? "):
            path = line[3:].strip().strip('"').rstrip("/")
            if path in RUNTIME_TAILS or any(path.startswith(f"{tail}/") for tail in RUNTIME_TAILS):
                continue
        dirt.append(line)
    return dirt


def workspace_dirt(workspace: str | os.PathLike[str]) -> list[str]:
    """Uncommitted work in a checkout, or nothing when it is not a git checkout."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return durability_dirt(completed.stdout)


def _sprint_guard_denial_request_id(request_id: str) -> str:
    """Keep a denied guard check from consuming the operation's retry key."""
    return "sprint-guard-denied-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _sprint_guard_override_request_id(request_id: str) -> str:
    """Keep a granted override from consuming the operation's retry key."""
    return "sprint-guard-override-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _artifact_ownership_refusal_request_id(request_id: str) -> str:
    """Keep a refused instruction visible without consuming its decision retry key."""
    return "artifact-ownership-refusal-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _done_retention_request_id(task_id: int, date_moved: int) -> str:
    """One durable retry key for one card's one Done dwell episode."""
    identity = f"kanboard:{task_id}:done:{date_moved}"
    return "done-retention-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


_STATE_BY_COLUMN = {
    "Issues": "issues",
    "Ready": "ready",
    "In progress": "in_progress",
    "Validate": "validate",
    "Assessment": "assessment",
    "Blocked": "blocked",
    "Done": "done",
}
_KNOWN_METADATA = {
    "task_type",
    "project",
    "blocked_by",
    "claim",
    "slug",
    "base_branch",
    "seed_ref",
    "supersedes",
    "head",
    "resolved_head",
    "review_head",
    "resolved_review_head",
    "retry_same",
    "retry_switch",
    "retry_heads",
    "complexity",
    "family_preference",
    "routing_reason",
    "quota_snapshot_at",
    "codex_launch_mode",
    "sprint_ref",
}
_TASK_TYPES = {"code", "research"}
_COMPLEXITIES = {"cheap", "standard", "hard", "frontier"}
_FAMILY_PREFERENCES = {"auto", "claude", "codex"}
# Retired launch modes normalize away rather than silently changing a requested shape.
_CODEX_LAUNCH_MODES = CODEX_LAUNCH_MODES
_ROLES = {"po", "dispatcher", "worker", "reviewer", "steward", "retro", "observer"}
_COMMENT_ROLES = _ROLES
_CREATE_ROLES = {"po", "steward", "worker", "reviewer", "retro", "observer"}
# Agent roles that may not open an execution card: their only create is a proposal in the
# board's first column, which a PO later triages into Ready.
_PROPOSAL_CREATE_ROLES = {"worker", "reviewer", "retro"}
_EDIT_ROLES = {"po", "dispatcher", "observer"}
_EDITABLE_STATES = {"ready", "blocked"}
_READY_RESET_METADATA = {
    "claim": "",
    "resolved_head": "",
    "resolved_review_head": "",
    "retry_same": "",
    "retry_switch": "",
    "retry_heads": "",
}
_ROUTING_PHASES = {"worker", "review", "verdict"}
# Worker blocker classification is evidence for, not the observer's final verdict.
_BLOCK_CLASSIFICATIONS = ("external_fact", "wrong_task_definition")
# Persist a parked-card decision before effects; blocked remains the failure escape hatch.
_DECISION_TARGETS = {"release": "done", "rework": "in_progress", "reslice": "blocked"}
_DECISIONS = set(_DECISION_TARGETS)
_DECIDED_TARGETS = {"done", "in_progress"}
# Only the PO may use these Assessment exits; the dispatcher must record a decision.
_UNDECIDED_EXITS = {"ready", "validate", "issues"}
# Dispatcher Assessment moves require decisions; human escape-hatch moves do not.
_DECISION_BOUND_ROLES = {"dispatcher"}
# States in which a card holds a workspace, a suspended worker or a running head. `assessment`
# is one of them: the reviewer is gone, but the worker and its checkout are retained for a
# rework decision, so a second writer in the same project is as wrong there as in Validate.
ACTIVE_STATES = frozenset({"in_progress", "validate", "assessment"})
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")
# A Product or an Issue is not an execution task: it never takes a claim or a task transition,
# whatever column it currently sits in.
_TYPED_RECORD_TYPES = {"issue", "product"}

_MARKER_EVENT_ACTIONS = {
    EventKind.CARD_REPORTED.value: "reported",
    EventKind.CARD_VERDICTED.value: "verdict",
    EventKind.CARD_DECIDED.value: "decided",
}


def _event_action(event: dict[str, Any]) -> str:
    """The released control-plane action spelling for generic history readers."""
    return _MARKER_EVENT_ACTIONS.get(str(event.get("kind") or ""), str(event.get("kind") or ""))


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Read a legacy payload or the typed marker data without rewriting history."""
    if event.get("record_type") == TaskAudit._PROTOCOL_EVENT_RECORD_TYPE:
        data = event.get("data")
        return data if isinstance(data, dict) else {}
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _validate_outcome_round_context(data: dict[str, Any]) -> None:
    """Validate the compact durable identity hand-off used by outcome freezing."""
    expected = {
        "version",
        "phase",
        "attempt_id",
        "attempt",
        "report_generation",
        "request_ids",
        "assessment_visit",
        "source_event_id",
    }
    version = data.get("version")
    if version == 2:
        expected = expected | {"round_id", "specification_revision", "marker"}
    if set(data) != expected or version not in {1, 2}:
        raise TaskError("validation", "outcome round context has an unsupported field set", 2)
    phases = (
        {"worker", "review", "decision"}
        if version == 1
        else {"worker", "review", "decision", "report", "verdict"}
    )
    if data.get("phase") not in phases:
        raise TaskError("validation", "outcome round context has an unsupported phase", 2)
    if version == 2 and (not isinstance(data.get("round_id"), str) or not data["round_id"].strip()):
        raise TaskError("validation", "outcome round context needs a stable round id", 2)
    if version == 2:
        revision = data.get("specification_revision")
        if revision is not None and (not isinstance(revision, str) or not revision.strip()):
            raise TaskError(
                "validation", "outcome round context specification revision must be a string or null", 2
            )
        marker = data.get("marker")
        if not isinstance(marker, str):
            raise TaskError("validation", "outcome round context marker must be a string", 2)
        if data["phase"] in {"report", "verdict", "decision"} and not marker:
            raise TaskError("validation", "source outcome round context needs its marker", 2)
        if data["phase"] not in {"report", "verdict", "decision"} and marker:
            raise TaskError("validation", "only source outcome round context has a marker", 2)
    if not isinstance(data.get("attempt_id"), str) or not data["attempt_id"].strip():
        raise TaskError("validation", "outcome round context needs an attempt id", 2)
    for name in ("attempt", "report_generation"):
        value = data.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TaskError("validation", f"outcome round context needs a positive {name}", 2)
    request_ids = data.get("request_ids")
    if (
        not isinstance(request_ids, list)
        or not request_ids
        or any(not isinstance(value, str) or not value.strip() for value in request_ids)
        or len(set(request_ids)) != len(request_ids)
    ):
        raise TaskError("validation", "outcome round context needs unique request ids", 2)
    visit = data.get("assessment_visit")
    if not isinstance(visit, str):
        raise TaskError("validation", "outcome round context assessment visit must be a string", 2)
    if data["phase"] == "decision" and not visit:
        raise TaskError("validation", "decision outcome round context needs an Assessment visit", 2)
    if data["phase"] != "decision" and visit:
        raise TaskError("validation", "only decision outcome round context has an Assessment visit", 2)
    event_id = data.get("source_event_id")
    if not isinstance(event_id, str):
        raise TaskError("validation", "outcome round context source event id must be a string", 2)
    source_phases = {"decision"} if version == 1 else {"report", "verdict", "decision"}
    if data["phase"] in source_phases and not event_id:
        raise TaskError("validation", "source outcome round context needs its source event id", 2)
    if data["phase"] not in source_phases and event_id:
        raise TaskError("validation", "only source outcome round context has a source event id", 2)


def specification_revision(events: Iterable[dict[str, Any]], description: str) -> str:
    """Return the durable event id of the description the card currently exposes.

    A digest identifies content, but not an edit that returns a card to earlier text. The latest
    create/edit event which wrote the current digest is therefore the revision boundary. Legacy,
    malformed, or divergent history deliberately has no boundary: callers that would otherwise
    replay an instruction must fail closed.
    """
    current_digest = _digest(description)
    latest: dict[str, Any] | None = None
    for event in events:
        if str(event.get("kind") or "") not in {"created", "edited"}:
            continue
        payload = _event_payload(event)
        digest = payload.get("description_sha256")
        if digest is None:
            # A title/routing-only edit does not create a specification revision.
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return ""
        latest = event
    if latest is None:
        return ""
    payload = _event_payload(latest)
    if payload.get("description_sha256") != current_digest:
        return ""
    revision = latest.get("event_id")
    return revision if isinstance(revision, str) and revision else ""


def _check_execution_record(task: dict[str, Any]) -> None:
    """Reject a Product or an Issue on an execution-task path, before any write."""
    if task.get("record_type") in _TYPED_RECORD_TYPES:
        raise TaskError(
            "transition_forbidden",
            "Product issues and products cannot enter execution task columns",
            3,
        )


def _forbidden_move_message(role: str, source: str, target: str) -> str:
    """Why a role may not make this move, said in the terms of the role that asked."""
    if role == "observer" and source == "assessment":
        return (
            "the observer decides about a parked card and the dispatcher performs the decision: "
            "record it with `task decide` instead of moving the card"
        )
    return f"{role} may not move {source} to {target}"


def _transition_reason(reason: str, target: str) -> str:
    """The non-empty reason a typed Card transition event carries."""
    return reason if reason.strip() else f"Card transition to {target}"


def assessment_resolution(events: Iterable[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """The current Assessment visit and its one canonical decision."""
    ordered = list(events)
    latest_park = -1
    for index, event in enumerate(ordered):
        payload = _event_payload(event)
        lifecycle = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        if (event.get("kind") == "moved" and str(payload.get("to") or "") == "assessment") or (
            event.get("record_type") == TaskAudit._PROTOCOL_EVENT_RECORD_TYPE
            and str(lifecycle.get("target") or "") == "assessment"
        ):
            latest_park = index
    if latest_park < 0:
        return "", None
    visit = str(ordered[latest_park].get("event_id") or ordered[latest_park].get("request_id") or "")
    for event in ordered[latest_park + 1 :]:
        payload = _event_payload(event)
        if _event_action(event) != "decided" or not str(payload.get("decision") or ""):
            continue
        recorded_visit = str(payload.get("assessment_visit") or "")
        if not recorded_visit or recorded_visit == visit:
            return visit, event
    return visit, None


def standing_decision(events: Iterable[dict[str, Any]]) -> str:
    """The canonical decision for the current Assessment visit, or an empty string."""
    _visit, event = assessment_resolution(events)
    payload = _event_payload(event) if isinstance(event, dict) else {}
    return str(payload.get("decision") or "")


def is_significant_card_event(event: dict[str, Any], *, linked_refs: set[str]) -> bool:
    """Whether a card transition needs a new observer decision.

    Deliberately small, because it drives both wake delivery and resume freshness: a broad
    "successful event" rule turns every piece of machinery telemetry — the observer's own decision
    included — into another observer turn.
    """
    if str(event.get("ref") or "") not in linked_refs:
        return False
    typed = event.get("record_type") == TaskAudit._PROTOCOL_EVENT_RECORD_TYPE
    if not typed and str(event.get("outcome") or "") != "success":
        return False
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    if str(actor.get("role") or "") == "observer":
        return False
    if typed:
        payload = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        source = str(payload.get("source") or "")
        target = str(payload.get("target") or "")
    else:
        if str(event.get("kind") or "") != "moved":
            return False
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        source = str(payload.get("from") or "")
        target = str(payload.get("to") or "")
    # Assessment requires a decision; Blocked requires classification.
    if target in {"assessment", "blocked", "done"}:
        return True
    # Only human control-plane removal of work wakes the observer.
    return (
        str(actor.get("role") or "") in {"po", "steward"} and source in ACTIVE_STATES and target == "issues"
    )


def is_significant_observer_event(
    event: dict[str, Any],
    *,
    linked_refs: set[str],
    sprint_ref: str,
) -> bool:
    """Whether an audit event is a semantic wake for one sprint observer."""
    if is_significant_card_event(event, linked_refs=linked_refs):
        return True
    if str(event.get("ref") or "") != sprint_ref:
        return False
    if str(event.get("outcome") or "") != "success":
        return False
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    if str(actor.get("role") or "") == "observer":
        return False
    kind = str(event.get("kind") or "")
    if kind in {"budget_recorded", "budget_hard_stopped"}:
        return True
    # PO is the human control-plane role.  Dispatcher and role comments are routine telemetry.
    return kind == "commented" and str(actor.get("role") or "") == "po"


_BATCH_CHUNK = 200
_BATCH_COMMENT_CHUNK = 50
_BATCH_WRITE_CHUNK = 50
# Bound both dimensions of one JSON-RPC document.  Count protects Kanboard's
# dispatcher; bytes protect the web server and make a pathological comment fail
# before an unbounded request is allocated on the wire.
_BATCH_BYTES = 1_048_576


def _rpc_request(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params:
        request["params"] = params
    return request


class KanboardClient:
    """Small JSON-RPC client using local board transport configuration."""

    def __init__(self, transport: BoardTransport, instance_dir: Path) -> None:
        self.instance_dir = normalize_instance_dir(instance_dir).resolve()
        self.url = transport.url
        self._transport = transport

    @classmethod
    def for_instance(cls, instance: str | Path) -> KanboardClient:
        try:
            root = normalize_instance_dir(instance).resolve()
            return cls(resolve(root), root)
        except BoardTransportError:
            raise TaskError(
                "backend_unavailable", "Kanboard runtime configuration is unavailable", 1
            ) from None

    def call(self, method: str, **params: Any) -> Any:
        document = self._post(_rpc_request(1, method, params))
        if not isinstance(document, dict) or "error" in document:
            raise TaskError("backend_error", "Kanboard rejected the read request", 1)
        return document.get("result")

    def call_batch(self, calls: Iterable[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Run `[(method, params), ...]` in one request each chunk; return results in call order.

        Kanboard has no bulk read for per-task metadata or comments, so a view of many tasks
        otherwise pays one round trip per task. Requests go out in bounded chunks so a large board
        does not become one oversized request.
        """
        calls = list(calls)
        results: list[Any] = [None] * len(calls)
        start = 0
        while start < len(calls):
            requests: list[dict[str, Any]] = []
            request_bytes = 2  # JSON array brackets; commas are added below.
            comment_reads = 0
            comment_writes = 0
            while start + len(requests) < len(calls) and len(requests) < _BATCH_CHUNK:
                index = start + len(requests)
                method, params = calls[index]
                next_reads = comment_reads + (method == "getAllComments")
                next_writes = comment_writes + (method == "createComment")
                if next_reads > _BATCH_COMMENT_CHUNK or next_writes > _BATCH_WRITE_CHUNK:
                    break
                request = _rpc_request(index, method, params)
                encoded_size = len(json.dumps(request, separators=(",", ":")).encode("utf-8"))
                next_bytes = request_bytes + encoded_size + bool(requests)
                if next_bytes > _BATCH_BYTES:
                    if not requests:
                        raise TaskError("validation", "one Kanboard batch call exceeds the byte limit", 2)
                    break
                requests.append(request)
                request_bytes = next_bytes
                comment_reads = next_reads
                comment_writes = next_writes
            document = self._post(requests)
            if not isinstance(document, list):
                raise TaskError("backend_error", "Kanboard rejected the batch request", 1)
            answers: dict[int, Any] = {}
            expected = set(range(start, start + len(requests)))
            for entry in document:
                if (
                    not isinstance(entry, dict)
                    or entry.get("jsonrpc") != "2.0"
                    or not isinstance(entry.get("id"), int)
                    or isinstance(entry.get("id"), bool)
                    or entry.get("id") not in expected
                    or entry.get("id") in answers
                    or ("result" in entry) == ("error" in entry)
                    or "error" in entry
                ):
                    raise TaskError("backend_error", "Kanboard returned an invalid batch answer", 1)
                answers[entry["id"]] = entry["result"]
            if set(answers) != expected:
                raise TaskError("backend_error", "Kanboard returned an incomplete batch answer", 1)
            for index in expected:
                results[index] = answers[index]
            start += len(requests)
        return results

    def _post(self, payload: Any) -> Any:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": self._transport.authorization_header(),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1) from None


def all_project_cards(client: KanboardClient, project_id: int) -> list[dict[str, Any]]:
    """Return every Kanboard card of one board, open and archived alike."""
    try:
        return board_rows(client.call, project_id)
    except BoardRowsUnavailable:
        raise TaskError("backend_error", "Kanboard returned an invalid task list", 1) from None


def _task_metadata(answer: Any) -> dict[str, str]:
    """One task's metadata as the flat str->str map every reader works with."""
    if answer is not None and not isinstance(answer, dict):
        raise TaskError("backend_error", "Kanboard returned invalid task metadata", 1)
    return {str(key): _text(value) for key, value in (answer or {}).items()}


def project_card_by_reference(
    client: KanboardClient, project_id: int, reference: str
) -> dict[str, Any] | None:
    """Return the live card for a reference when an archived duplicate exists."""
    card = client.call("getTaskByReference", project_id=project_id, reference=reference)
    if not isinstance(card, dict) or _task_is_active(card):
        return card if isinstance(card, dict) else None
    active_cards = client.call("getAllTasks", project_id=project_id, status_id=1)
    if not isinstance(active_cards, list):
        raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
    for candidate in active_cards:
        if isinstance(candidate, dict) and candidate.get("reference") == reference:
            return candidate
    return card


def project_card_by_id(client: KanboardClient, project_id: int, task_id: int) -> dict[str, Any] | None:
    """Return the exact board row named by a recorded Kanboard task id."""
    for card in all_project_cards(client, project_id):
        if _positive_int(card.get("id")) == task_id:
            return card
    return None


def next_project_reference(client: KanboardClient, project_id: int, project: str) -> str:
    """Allocate the reference immediately after this project's board-wide high-water mark."""
    return next_reference(all_project_cards(client, project_id), f"{project}-")


@contextlib.contextmanager
def assessment_decision_lock(data_dir: Path, reference: str) -> Iterator[None]:
    """Serialize the complete decision transaction for one card, across observer processes."""
    directory = data_dir / "board" / "assessment-decisions"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(reference.encode("utf-8")).hexdigest() + ".lock"
    with (directory / name).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class TaskReader:
    def __init__(self, client: KanboardClient, board_name: str = "Pipeline") -> None:
        self.client = client
        self.board_name = board_name

    def list(
        self, *, states: set[str] | None = None, project: str | None = None, sprint: str | None = None
    ) -> list[dict[str, Any]]:
        project_id, columns, swimlanes = self._board()
        cards = self.client.call("getAllTasks", project_id=project_id, status_id=1) or []
        if not isinstance(cards, list):
            raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
        rows = [card for card in cards if isinstance(card, dict)]
        # One batched read for the whole listing: metadata is per task and Kanboard has no bulk
        # read for it, so asking row by row is what made a board-wide listing a per-row round trip.
        metadata = self._metadata_of(rows)
        result = []
        for card in rows:
            normalized = self._normalize(
                card, columns, swimlanes, metadata[_task_number(card)], comments=None
            )
            if states and normalized["state"] not in states:
                continue
            if project is not None and normalized["project"] != project:
                continue
            if sprint is not None and normalized["sprint"] != sprint:
                continue
            result.append(normalized)
        return sorted(result, key=lambda task: (task["state"], task["position"], task["ref"], task["id"]))

    def steward_reports_in_progress(self, project: str) -> list[dict[str, Any]]:
        """Return the small, durable report view a steward dispatch needs.

        This intentionally is not a second public Card list: callers get only the
        identity, freshness timestamp and report marker needed to decide whether a
        steward sweep is already running.  Kanboard exposes metadata per task, so
        all candidate metadata is fetched in one batch rather than one RPC per row.
        """
        project_id, columns, _ = self._board()
        in_progress_id = next(
            (identifier for identifier, title in columns.items() if title == "In progress"), None
        )
        if in_progress_id is None:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        raw = self.client.call("getAllTasks", project_id=project_id, status_id=1) or []
        if not isinstance(raw, list):
            raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
        cards = [
            card
            for card in raw
            if isinstance(card, dict) and _positive_int(card.get("column_id")) == in_progress_id
        ]
        metadata = self._metadata_of(cards)
        reports: list[dict[str, Any]] = []
        for card in cards:
            task_id = _task_number(card)
            meta = metadata[task_id]
            if meta.get("project") != project or meta.get("steward_report") != "1":
                continue
            reports.append(
                {
                    "reference": _text(card.get("reference")),
                    "date_moved": _positive_int(card.get("date_moved")),
                    "steward_report": "1",
                }
            )
        return reports

    def steward_signal_cards(
        self, *, states: set[str] | None = None, project: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the bounded operational card view used by steward anomaly reads.

        This is deliberately narrower than :meth:`list`: it exposes only the
        active-card fields a watchdog needs and keeps Kanboard rows private.
        Metadata is fetched once for the active board snapshot, never per card.
        """
        if states is not None and (unknown := states - set(_STATE_BY_COLUMN.values())):
            raise TaskError("validation", f"unknown task states: {sorted(unknown)}", 2)
        project_id, columns, _ = self._board()
        raw = self.client.call("getAllTasks", project_id=project_id, status_id=1)
        if not isinstance(raw, list) or any(not isinstance(card, dict) for card in raw):
            raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
        metadata = self._metadata_of(raw)
        cards: list[dict[str, Any]] = []
        for card in raw:
            task_id = _task_number(card)
            column = columns.get(_positive_int(card.get("column_id")) or -1)
            state = _STATE_BY_COLUMN.get(column or "")
            if state is None:
                raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
            meta = metadata[task_id]
            card_project = _text(meta.get("project"))
            if states is not None and state not in states:
                continue
            if project is not None and card_project != project:
                continue
            cards.append(
                {
                    "reference": _text(card.get("reference")),
                    "state": state,
                    "column": column,
                    "project": card_project,
                    "date_moved": _positive_int(card.get("date_moved")),
                    "steward_report": _text(meta.get("steward_report")),
                }
            )
        return cards

    def done_retention_candidates(self) -> list[dict[str, Any]]:
        """Return the deliberately small view used by Done-retention cleanup.

        The cleanup is allowed one active-board snapshot and one metadata batch.
        It must not infer an age for incomplete Kanboard rows, so an unusable
        ``date_moved`` is represented as ``None`` for the caller to skip.
        """
        project_id, columns, _ = self._board()
        done_id = next((identifier for identifier, title in columns.items() if title == "Done"), None)
        if done_id is None:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        raw = self.client.call("getAllTasks", project_id=project_id, status_id=1)
        if not isinstance(raw, list) or any(not isinstance(card, dict) for card in raw):
            raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
        done = [
            card for card in raw if _positive_int(card.get("column_id")) == done_id and _task_is_active(card)
        ]
        metadata = self._metadata_of(done)
        candidates: list[dict[str, Any]] = []
        for card in done:
            task_id = _task_number(card)
            # Product and Issue rows may be displayed in a task column, but they
            # are not execution-task retention candidates.
            if metadata[task_id].get("record_type") in _TYPED_RECORD_TYPES:
                continue
            candidates.append(
                {
                    "reference": _text(card.get("reference")),
                    "date_moved": _positive_int(card.get("date_moved")),
                }
            )
        return sorted(candidates, key=lambda candidate: str(candidate["reference"]))

    def export(self) -> list[dict[str, Any]]:
        """Return the complete legacy checkpoint projection in bounded board reads.

        Checkpoints retain the established board schema while reading it through the
        canonical Secretary transport.  Metadata and comments have no Kanboard bulk
        endpoint, so both are requested in one bounded JSON-RPC batch for the whole
        board rather than once per card.
        """
        # The installed head registry remains the authority for legacy effective-head values;
        # this is deliberately not a dependency on pipeline board operations or its export CLI.
        from triggered_agents.agents.pipeline.heads import default_head, reviewer_head

        project_id, columns, swimlanes = self._board()
        cards = all_project_cards(self.client, project_id)
        rows = [card for card in cards if isinstance(card, dict)]
        task_ids = [_task_number(card) for card in rows]
        answers = self.client.call_batch(
            (method, {"task_id": task_id})
            for task_id in task_ids
            for method in ("getTaskMetadata", "getAllComments")
        )
        result = []
        for index, card in enumerate(rows):
            meta = _task_metadata(answers[index * 2])
            raw_comments = answers[index * 2 + 1] or []
            if not isinstance(raw_comments, list):
                raise TaskError("backend_error", "Kanboard returned invalid task comments", 1)
            task_id = task_ids[index]
            head = _text(meta.get("head"))
            review = _text(meta.get("review_head"))
            result.append(
                {
                    "id": task_id,
                    "reference": _text(card.get("reference")),
                    "title": _text(card.get("title")),
                    "description": _text(card.get("description")),
                    "column": columns.get(_positive_int(card.get("column_id")) or -1, ""),
                    "swimlane": swimlanes.get(_positive_int(card.get("swimlane_id")) or -1, ""),
                    "position": _nonnegative_int(card.get("position")),
                    "date_moved": _positive_int(card.get("date_moved")),
                    "closed": not _task_is_active(card),
                    "metadata": meta,
                    "task_type": _text(meta.get("task_type")),
                    "project": _text(meta.get("project")),
                    "blocked_by": _text(meta.get("blocked_by")),
                    "head": head,
                    "effective_head": _text(meta.get("resolved_head")) or head or default_head(),
                    "review_head": review,
                    "effective_review_head": (
                        _text(meta.get("resolved_review_head")) or review or reviewer_head()
                    ),
                    "claim": _text(meta.get("claim")),
                    "slug": _text(meta.get("slug")),
                    "base_branch": _text(meta.get("base_branch")),
                    "seed_ref": _text(meta.get("seed_ref")),
                    "supersedes": _text(meta.get("supersedes")),
                    "comments": [
                        {
                            "ts": _text(comment.get("date_creation")),
                            "text": _text(comment.get("comment")),
                        }
                        for comment in raw_comments
                        if isinstance(comment, dict)
                    ],
                }
            )
        return result

    def restore_snapshot(self) -> dict[str, dict[str, Any]]:
        """Read every active or archived card as a normalized, authoritative snapshot.

        Recovery uses this after its setup writes and again for final parity.  The
        board rows are one read and metadata/comments share bounded JSON-RPC posts;
        unlike ``show`` this never grows a pair of HTTP reads per card.
        """
        project_id, columns, swimlanes = self._board()
        rows = [row for row in all_project_cards(self.client, project_id) if isinstance(row, dict)]
        task_ids = [_task_number(row) for row in rows]
        answers = self.client.call_batch(
            (method, {"task_id": task_id})
            for task_id in task_ids
            for method in ("getTaskMetadata", "getAllComments")
        )
        result: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            raw_comments = answers[index * 2 + 1] or []
            if not isinstance(raw_comments, list):
                raise TaskError("backend_error", "Kanboard returned invalid task comments", 1)
            card = self._normalize(
                row,
                columns,
                swimlanes,
                _task_metadata(answers[index * 2]),
                comments=[_normalize_comment(value) for value in raw_comments if isinstance(value, dict)],
            )
            previous = result.get(card["ref"])
            if previous is None or (not card.get("closed") and previous.get("closed")):
                result[card["ref"]] = card
        return result

    def show(self, reference: str) -> dict[str, Any]:
        project_id, columns, swimlanes = self._board()
        card = project_card_by_reference(self.client, project_id, reference)
        return self._show_card(card, columns, swimlanes)

    def show_id(self, task_id: int) -> dict[str, Any]:
        """Return one row by Kanboard id, without resolving a duplicate reference."""
        project_id, columns, swimlanes = self._board()
        card = project_card_by_id(self.client, project_id, task_id)
        return self._show_card(card, columns, swimlanes)

    def _show_card(
        self,
        card: dict[str, Any] | None,
        columns: dict[int, str],
        swimlanes: dict[int, str],
    ) -> dict[str, Any]:
        if not isinstance(card, dict):
            raise TaskError("not_found", "task was not found", 2)
        task_id = _positive_int(card.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
        raw_comments = self.client.call("getAllComments", task_id=task_id) or []
        comments = [_normalize_comment(comment) for comment in raw_comments if isinstance(comment, dict)]
        return self._normalize(
            card,
            columns,
            swimlanes,
            _task_metadata(self.client.call("getTaskMetadata", task_id=task_id)),
            comments=comments,
        )

    def _metadata_of(self, cards: list[dict[str, Any]]) -> dict[int, dict[str, str]]:
        """The task metadata of every given row, keyed by Kanboard task id, in one batched read."""
        task_ids = [_task_number(card) for card in cards]
        answers = self.client.call_batch(("getTaskMetadata", {"task_id": task_id}) for task_id in task_ids)
        return {task_id: _task_metadata(answer) for task_id, answer in zip(task_ids, answers, strict=True)}

    def _board(self) -> tuple[int, dict[int, str], dict[int, str]]:
        board = self.client.call("getProjectByName", name=self.board_name)
        if not isinstance(board, dict) or (project_id := _positive_int(board.get("id"))) is None:
            raise TaskError("backend_error", "Pipeline board is unavailable", 1)
        columns = {
            identifier: str(column.get("title") or "")
            for column in (self.client.call("getColumns", project_id=project_id) or [])
            if isinstance(column, dict) and (identifier := _positive_int(column.get("id"))) is not None
        }
        swimlanes = {
            identifier: str(swimlane.get("name") or "")
            for swimlane in (self.client.call("getActiveSwimlanes", project_id=project_id) or [])
            if isinstance(swimlane, dict) and (identifier := _positive_int(swimlane.get("id"))) is not None
        }
        return project_id, columns, swimlanes

    def _normalize(
        self,
        card: dict[str, Any],
        columns: dict[int, str],
        swimlanes: dict[int, str],
        meta: dict[str, str],
        *,
        comments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        task_id = _positive_int(card.get("id"))
        column = columns.get(_positive_int(card.get("column_id")) or -1)
        if task_id is None or column not in _STATE_BY_COLUMN:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        ref = _text(card.get("reference"))
        result: dict[str, Any] = {
            "id": f"task_kanboard_{task_id}",
            "ref": ref,
            "title": _text(card.get("title")),
            "description": _text(card.get("description")),
            "state": _STATE_BY_COLUMN[column],
            "closed": _nonnegative_int(card.get("is_active", card.get("status", 1))) == 0,
            "position": _nonnegative_int(card.get("position")),
            "project": _text(meta.get("project")),
            "type": _text(meta.get("task_type")),
            "blocked_by": _null_if_empty(meta.get("blocked_by")),
            "claim": {"worker": _null_if_empty(meta.get("claim")), "claimed_at": None},
            "routing": {
                "complexity": _enum_or_default(meta.get("complexity"), _COMPLEXITIES, "standard"),
                "family_preference": _enum_or_default(
                    meta.get("family_preference"), _FAMILY_PREFERENCES, "auto"
                ),
                "head_override": _null_if_empty(meta.get("head")),
                "review_head_override": _null_if_empty(meta.get("review_head")),
                "resolved_worker_family": None,
                "resolved_worker_head": _null_if_empty(meta.get("resolved_head")),
                "resolved_review_family": None,
                "resolved_review_head": _null_if_empty(meta.get("resolved_review_head")),
                "routing_reason": _null_if_empty(meta.get("routing_reason")),
                "quota_snapshot_at": _null_if_empty(meta.get("quota_snapshot_at")),
                "codex_launch_mode": _enum_or_none(meta.get("codex_launch_mode"), _CODEX_LAUNCH_MODES),
            },
            "workspace": {
                "slug": _null_if_empty(meta.get("slug")),
                "base_branch": _null_if_empty(meta.get("base_branch")),
                "seed_ref": _null_if_empty(meta.get("seed_ref")),
                "supersedes": _null_if_empty(meta.get("supersedes")),
            },
            "retry": {
                "same": _nonnegative_int(meta.get("retry_same")),
                "switched": _nonnegative_int(meta.get("retry_switch")),
                "heads": _split_heads(meta.get("retry_heads")),
            },
            "sprint": _null_if_empty(meta.get("sprint_ref")),
            "record_type": _null_if_empty(meta.get("record_type")),
            "audit": {
                "created_at": _rfc3339(card.get("date_creation")),
                "updated_at": _rfc3339(card.get("date_modification")),
                "backend": {"kind": "kanboard", "kanboard_task_id": task_id, "board": self.board_name},
            },
        }
        extensions = {key: value for key, value in meta.items() if key not in _KNOWN_METADATA}
        lane = swimlanes.get(_positive_int(card.get("swimlane_id")) or -1)
        if lane:
            extensions["swimlane"] = lane
        if extensions:
            result["extensions"] = {"kanboard": extensions}
        if comments is not None:
            result["comments"] = comments
        return result


class TaskAudit:
    """Durable, append-only audit log with retry-safe pending records."""

    # Recovery distinguishes protocol effects from generic audit records by this typed value.
    _PROTOCOL_EVENT_RECORD_TYPE = Event.RECORD_TYPE

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.board_dir = os.path.join(os.fspath(data_dir), "board")
        self.events_path = os.path.join(self.board_dir, "events.ndjson")
        self.pending_dir = os.path.join(self.board_dir, "pending-audit")
        self.lock_path = os.path.join(self.board_dir, ".audit.lock")
        # Incremental request index avoids rescanning the journal on every write.
        self._committed_offsets: dict[str, int] = {}
        # Event ownership is indexed under the same lock.
        self._committed_event_ids: dict[str, str] = {}
        self._committed_read = 0
        self._committed_ident: tuple[int, int] | None = None
        self._committed_anchor = b""
        self._marker_lock_depth = threading.local()

    def _pending_path(self, request_id: str) -> str:
        """Keep untrusted request ids out of the installation filesystem layout."""
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return os.path.join(self.pending_dir, f"v2-{digest}.json")

    def _require_v2_pending_layout(self) -> None:
        """Do not guess how a released generic transient record maps to v2."""
        if not os.path.isdir(self.pending_dir):
            return
        legacy = [
            name
            for name in os.listdir(self.pending_dir)
            if name.endswith(".json") and not name.startswith("v2-")
        ]
        if legacy:
            raise TaskError(
                "upgrade_required",
                "generic pending audit records use the pre-v2 filename layout; reconcile them with the previous Secretary version before upgrading",
                4,
            )

    @contextlib.contextmanager
    def _locked_audit(self, *, create_pending: bool = False) -> Iterator[None]:
        """Hold the one lock that owns journal and pending-record identity."""
        os.makedirs(self.board_dir, exist_ok=True)
        if create_pending:
            os.makedirs(self.pending_dir, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._require_v2_pending_layout()
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def marker_comment_lock(self, reference: str) -> Iterator[None]:
        """Serialize all internal Card comment effects for one marker identity.

        Generic restore replay re-enters this guard while finishing its own pending record, so the file
        lock stays process-wide while that same-thread contour is re-entrant; a second process still
        blocks at the flock until the outer effect completes.
        """
        from secretary.board.events import marker_comment_lock

        held = getattr(self._marker_lock_depth, "held", None)
        if held is None:
            held = self._marker_lock_depth.held = {}
        depth = held.get(reference, 0)
        if depth:
            held[reference] = depth + 1
            try:
                yield
            finally:
                held[reference] -= 1
            return
        with marker_comment_lock(Path(self.board_dir).parent, reference):
            held[reference] = 1
            try:
                yield
            finally:
                del held[reference]

    def pending_marker_owner(
        self, reference: str, content: str, *, request_id: str | None = None
    ) -> str | None:
        """Return a different pending owner of an indistinguishable marker.

        Runs under the audit lock while callers hold the per-Card marker lock, so no writer can put a
        matching row on Kanboard between the reservation check and its own effect.
        """
        from secretary.board.events import render_marker_comment

        with self._locked_audit():
            for record in self.pending_events():
                candidate = str(record.get("request_id") or "")
                if candidate == request_id:
                    continue
                if record.get("record_type") == self._PROTOCOL_EVENT_RECORD_TYPE:
                    try:
                        event = Event.from_record(record)
                        rendered = render_marker_comment(event)
                    except (TypeError, ValueError):
                        continue
                    if (
                        event.entity_kind is EntityKind.CARD
                        and event.ref == reference
                        and rendered == content
                    ):
                        return candidate
                    continue
                # Restore comment records are generic by design, but their
                # pending payload keeps the unreconciled body.  Treat that body
                # as the rendered marker identity until this exact owner either
                # proves and commits it or is safely discarded before effect.
                payload = record.get("payload")
                if (
                    record.get("kind") == "restored_comment"
                    and record.get("ref") == reference
                    and isinstance(payload, dict)
                    and payload.get("restore_body") == content
                ):
                    return candidate
        return None

    def pending_marker_owners(self, candidates: Iterable[tuple[str, str, str]]) -> dict[str, str]:
        """Resolve many per-Card marker reservations in one pending-journal scan.

        Callers hold every candidate's marker lock.  One restore wave can then
        retain the same cross-process exclusion without reopening every pending
        file once per occurrence.
        """
        from secretary.board.events import render_marker_comment

        wanted = {(reference, content): request_id for reference, content, request_id in candidates}
        owners: dict[str, str] = {}
        with self._locked_audit():
            for record in self.pending_events():
                candidate = str(record.get("request_id") or "")
                identity: tuple[str, str] | None = None
                if record.get("record_type") == self._PROTOCOL_EVENT_RECORD_TYPE:
                    try:
                        event = Event.from_record(record)
                        if event.entity_kind is EntityKind.CARD:
                            identity = (event.ref, render_marker_comment(event))
                    except (TypeError, ValueError):
                        continue
                else:
                    payload = record.get("payload")
                    if (
                        record.get("kind") == "restored_comment"
                        and isinstance(record.get("ref"), str)
                        and isinstance(payload, dict)
                        and isinstance(payload.get("restore_body"), str)
                    ):
                        identity = (record["ref"], payload["restore_body"])
                request_id = wanted.get(identity) if identity is not None else None
                if request_id is not None and candidate != request_id:
                    owners[request_id] = candidate
        return owners

    @classmethod
    def _is_protocol_event(cls, event: dict[str, Any]) -> bool:
        return event.get("record_type") == cls._PROTOCOL_EVENT_RECORD_TYPE

    def _pending_owner(
        self,
        request_id: str,
        event: dict[str, Any] | None,
        *,
        operation: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        """Resolve a request-id owner while ``.audit.lock`` is held.

        Returns ``(committed, pending, replace_generic_pending)``. A generic ``stage`` is the sole
        released operation allowed to replace a pending record, and only when both records are generic.
        Every other pending mismatch fails before a write, append, unlink or discard.
        """
        committed = self.committed_event(request_id)
        if committed is not None:
            if event is not None:
                self._require_same_event(committed, event)
            return committed, None, False

        pending = self.pending_event(request_id)
        if pending is None:
            return None, None, False
        # Only MutationEventTransaction can attest a protocol backend effect.
        if operation == "reconcile" and self._is_protocol_event(pending):
            self._require_same_event(pending, {})
        if event is not None and pending == event:
            return None, pending, False
        if (
            operation == "stage"
            and event is not None
            and not self._is_protocol_event(pending)
            and not self._is_protocol_event(event)
        ):
            return None, pending, True
        if operation == "discard" and event is None and not self._is_protocol_event(pending):
            return None, pending, False
        self._require_same_event(pending, event or {})
        raise AssertionError("unreachable")

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        with self._locked_audit(create_pending=True):
            committed, _pending, replace = self._pending_owner(
                request_id,
                event,
                operation="stage",
            )
            if committed is not None:
                return
            if self._product_issue_pending(request_id):
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            # A matching pending record is already the exact durable owner.  Do not replace it:
            # besides avoiding needless I/O, this preserves the evidence across a retry.
            if _pending is not None and not replace:
                return
            self._atomic_json(self._pending_path(request_id), event)

    def claim(
        self,
        request_id: str,
        event: dict[str, Any],
        *,
        verify: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        """Resolve request-id ownership and stage the record in one critical section."""
        with self._locked_audit(create_pending=True):
            committed, pending, _replace = self._pending_owner(
                request_id,
                event,
                operation="claim",
            )
            if committed is not None:
                return committed
            if pending is not None:
                return pending
            if self._product_issue_pending(request_id):
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            if verify is not None:
                verify(event)
            self._atomic_json(self._pending_path(request_id), event)
            return None

    def event_id_owner(self, event_id: str) -> str | None:
        """Which request id already published `event_id`, committed or pending."""
        self._refresh_committed_index()
        owner = self._committed_event_ids.get(event_id)
        if owner is not None:
            return owner
        for record in self.pending_events():
            if record.get("event_id") == event_id:
                candidate = record.get("request_id")
                if isinstance(candidate, str):
                    return candidate
        return None

    def append(self, request_id: str, event: dict[str, Any]) -> str:
        with self._locked_audit():
            return self._append_owned(request_id, event)

    def _append_owned(
        self,
        request_id: str,
        event: dict[str, Any],
        *,
        operation: str = "append",
    ) -> str:
        """Append one exact owner and clear only that owner's pending evidence.

        The caller holds ``.audit.lock``. Reconciliation shares this primitive so it cannot race a stage
        or recover a file another owner has replaced.
        """
        committed, pending, _replace = self._pending_owner(request_id, event, operation=operation)
        if operation == "reconcile" and committed is None and pending is None:
            raise TaskError("validation", "pending audit record disappeared", 2)
        if committed is None:
            with open(self.events_path, "a", encoding="utf-8") as events:
                events.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                events.flush()
                os.fsync(events.fileno())
        else:
            # A committed replay is already durable.  Look at pending only now, and only remove
            # an exact duplicate; a foreign pending file remains recovery evidence.
            pending = self.pending_event(request_id)
            if pending is not None and pending != event:
                return str(event["event_id"])
        if pending is not None:
            os.unlink(self._pending_path(request_id))
        return str(event["event_id"])

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        """Discard a generic pending retry, or the supplied exact protocol event."""
        with self._locked_audit():
            committed, pending, _replace = self._pending_owner(
                request_id,
                event,
                operation="discard",
            )
            if committed is not None or pending is None:
                return
            try:
                os.unlink(self._pending_path(request_id))
            except FileNotFoundError:
                pass

    def reconcile(self) -> tuple[int, int]:
        if not os.path.isdir(self.pending_dir):
            return 0, 0
        self._require_v2_pending_layout()
        repaired = 0
        unresolved = 0
        for name in sorted(os.listdir(self.pending_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.pending_dir, name)
            try:
                with open(path, encoding="utf-8") as source:
                    event = json.load(source)
                request_id = str(event["request_id"])
                # Retention's close has an ambiguous-response recovery rule:
                # only TaskWriter can re-read the exact Done episode and prove
                # it.  The generic journal repairer must leave that evidence.
                if event.get("kind") == "retired":
                    unresolved += 1
                    continue
                with self._locked_audit():
                    self._append_owned(request_id, event, operation="reconcile")
                repaired += 1
            except (OSError, TaskError, ValueError, KeyError, TypeError):
                unresolved += 1
        return repaired, unresolved

    def pending_events(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self.pending_dir):
            return []
        self._require_v2_pending_layout()
        result = []
        for name in sorted(os.listdir(self.pending_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.pending_dir, name), encoding="utf-8") as source:
                    result.append(json.load(source))
            except (OSError, ValueError):
                continue
        return result

    def _occurrence_projection_records(self) -> list[tuple[dict[str, Any], bool]]:
        """Read committed and pending audit records atomically for a fail-closed projection.

        Generic audit readers retain their released best-effort behaviour. The usage projection
        cannot skip an unreadable record, because that record may be the causal boundary a later
        phase must subtract.
        """
        result: list[tuple[dict[str, Any], bool]] = []
        with self._locked_audit():
            try:
                with open(self.events_path, encoding="utf-8") as events:
                    for line_number, line in enumerate(events, 1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError as exc:
                            raise ValueError(f"audit journal record {line_number} is unreadable") from exc
                        if not isinstance(record, dict):
                            raise TypeError(f"audit journal record {line_number} is not an object")
                        result.append((record, False))
            except FileNotFoundError:
                pass
            if not os.path.isdir(self.pending_dir):
                return result
            for name in sorted(os.listdir(self.pending_dir)):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(self.pending_dir, name)
                try:
                    with open(path, encoding="utf-8") as source:
                        record = json.load(source)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"pending audit record {name} is unreadable") from exc
                if not isinstance(record, dict):
                    raise TypeError(f"pending audit record {name} is not an object")
                result.append((record, True))
        return result

    def status(self) -> dict[str, int | bool]:
        self._require_v2_pending_layout()
        pending = 0
        if os.path.isdir(self.pending_dir):
            pending = sum(name.endswith(".json") for name in os.listdir(self.pending_dir))
        return {"ok": pending == 0, "pending": pending}

    def event(self, request_id: str) -> dict[str, Any] | None:
        committed = self.committed_event(request_id)
        if committed is not None:
            return committed
        return self.pending_event(request_id)

    def events(self, reference: str = "", *, kind: str = "") -> list[dict[str, Any]]:
        """Committed events in append order, optionally narrowed to one card and/or kind."""
        result: list[dict[str, Any]] = []
        try:
            with open(self.events_path, encoding="utf-8") as events:
                for line in events:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if reference and event.get("ref") != reference:
                        continue
                    if kind and event.get("kind") != kind and _event_action(event) != kind:
                        continue
                    result.append(event)
        except FileNotFoundError:
            return []
        return result

    def _anchor_intact(self) -> bool:
        """Лежит ли последняя прочитанная строка всё там же.

        Журнал по контракту только дописывается, но переписать его на месте может починка: inode тот же,
        размер не меньше, и одних stat-полей не хватает.
        """
        if not self._committed_anchor:
            return True
        start = self._committed_read - len(self._committed_anchor)
        if start < 0:
            return False
        try:
            with open(self.events_path, "rb") as events:
                events.seek(start)
                return events.read(len(self._committed_anchor)) == self._committed_anchor
        except OSError:
            return False

    def _refresh_committed_index(self) -> None:
        """Дочитать журнал с прошлой позиции, оставив недописанный хвост следующему разу."""
        try:
            stat = os.stat(self.events_path)
        except FileNotFoundError:
            self._committed_offsets = {}
            self._committed_event_ids = {}
            self._committed_read = 0
            self._committed_ident = None
            self._committed_anchor = b""
            return
        ident = (stat.st_dev, stat.st_ino)
        if ident != self._committed_ident or stat.st_size < self._committed_read or not self._anchor_intact():
            # журнал пересоздан, усечён или переписан — индекс больше не про этот файл
            self._committed_offsets = {}
            self._committed_event_ids = {}
            self._committed_read = 0
            self._committed_ident = ident
            self._committed_anchor = b""
        if stat.st_size == self._committed_read:
            return
        with open(self.events_path, "rb") as events:
            events.seek(self._committed_read)
            chunk = events.read()
        consumed = 0
        for raw in chunk.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break  # писатель не дописал строку; вернёмся к ней при следующем обновлении
            offset = self._committed_read + consumed
            consumed += len(raw)
            self._committed_anchor = raw
            if not raw.strip():
                continue
            try:
                candidate = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(candidate, dict):
                continue
            request_id = candidate.get("request_id")
            # первым побеждает самое раннее совпадение — так вёл себя скан сверху вниз
            if isinstance(request_id, str) and request_id not in self._committed_offsets:
                self._committed_offsets[request_id] = offset
            event_id = candidate.get("event_id")
            if (
                isinstance(event_id, str)
                and isinstance(request_id, str)
                and event_id not in self._committed_event_ids
            ):
                self._committed_event_ids[event_id] = request_id
        self._committed_read += consumed

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        self._refresh_committed_index()
        offset = self._committed_offsets.get(request_id)
        if offset is None:
            return None
        try:
            with open(self.events_path, "rb") as events:
                events.seek(offset)
                line = events.readline()
        except FileNotFoundError:
            return None
        try:
            candidate = json.loads(line)
        except ValueError:
            return None
        return candidate if isinstance(candidate, dict) else None

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        self._require_v2_pending_layout()
        pending = self._pending_path(request_id)
        try:
            with open(pending, encoding="utf-8") as source:
                return json.load(source)
        except FileNotFoundError:
            return None

    def _product_issue_pending(self, request_id: str) -> bool:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return os.path.exists(os.path.join(self.board_dir, "product-issue-transactions", f"v1-{digest}.json"))

    @staticmethod
    def _require_same_event(existing: dict[str, Any], event: dict[str, Any]) -> None:
        """A request id is an ownership claim, not merely an append de-duplication key."""
        if existing != event:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)

    @staticmethod
    def require_claim(
        existing: dict[str, Any],
        *,
        kind: str,
        reference: str | None,
        identity: dict[str, Any] | None,
    ) -> None:
        """Refuse a replay whose caller meant an operation other than the recorded one.

        The event kind, the card ref and the `identity` every write declares all have to match. The only
        fields left out are the ones a retry cannot recompute after the write went through, because they
        describe the state the write replaced: `moved`, `edited`'s digests, and `restored_comment`'s
        body once the comment is known to be on the card. Comparing those would turn a retry into a
        conflict.
        """
        if str(existing.get("kind") or "") != kind:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        if reference is not None and str(existing.get("ref") or "") != reference:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        if not identity:
            return
        payload = existing.get("payload")
        if not isinstance(payload, dict):
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        for key, value in identity.items():
            if payload.get(key) != value:
                raise TaskError("validation", "request id belongs to another operation or payload", 2)

    def require_pending_layout(self) -> None:
        """Run the released generic-pending upgrade gate before a new mutation starts."""
        self._require_v2_pending_layout()

    @staticmethod
    def _atomic_json(path: str, document: dict[str, Any]) -> None:
        directory = os.path.dirname(path)
        fd, temp = tempfile.mkstemp(prefix=".pending-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(document, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


class TaskWriter:
    """Protocol writes, role guards and normalized audit events."""

    def __init__(
        self,
        client: KanboardClient,
        *,
        data_dir: str | os.PathLike[str],
        workspace: str | os.PathLike[str] | None = None,
    ) -> None:
        self.client = client
        self.reader = TaskReader(client)
        self.data_dir = Path(data_dir)
        self.instance_dir = Path(client.instance_dir).expanduser().resolve()
        self.audit = TaskAudit(data_dir)
        # Importing the concrete adapter here keeps the protocol leaves usable
        # by the legacy task reader while giving migrated writes the same audit
        # owner as generic control-plane operations.
        from secretary.board.kanboard import KanboardBoardHost

        self.board_host = KanboardBoardHost(
            client,
            data_dir=os.fspath(data_dir),
            audit=self.audit,
        )
        self.workspace = Path(workspace) if workspace is not None else None
        self._redaction_cache: tuple[tuple[tuple[str, int, int, int], ...], tuple[str, ...]] | None = None

    def _redaction_values(self) -> tuple[str, ...]:
        """Open the catalog at most once while its on-disk inputs are unchanged."""
        root = self.instance_dir / "secrets"
        transport = transport_path(self.instance_dir)
        paths = [root / "catalog.yaml", root / "installation.key", transport]
        values_dir = root / "values"
        if values_dir.is_dir():
            paths.extend(sorted(path for path in values_dir.iterdir() if path.is_file()))
        fingerprint: list[tuple[str, int, int, int]] = []
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                fingerprint.append((str(path), -1, -1, -1))
            else:
                fingerprint.append((str(path), info.st_mtime_ns, info.st_size, info.st_mode))
        key = tuple(fingerprint)
        if self._redaction_cache is not None and self._redaction_cache[0] == key:
            return self._redaction_cache[1]
        from secretary.secret_store import SecretStoreError, redaction_values

        try:
            values = redaction_values(self.instance_dir)
        except SecretStoreError as exc:
            raise TaskError(
                "backend_unavailable", f"board redaction configuration is unavailable: {exc}", 1
            ) from None
        self._redaction_cache = (key, values)
        return values

    def _redact_for_board(self, text: str) -> str:
        """Remove credentials before they reach either board or audit history.

        An interrupted archive keeps its retry body locally and every board comment is exported into the
        checkpoint, so scrubbing happens at the protocol boundary and both copies receive the same safe
        text. An explicit role-env override selects its external file, so ``SECRETARY_RUNTIME_ENV_FILE``
        and ``TA_RUNTIME_ENV_FILE`` are scrubbed too.
        """
        # Delay this import to avoid the config/sprints import cycle.
        return redact(
            text,
            env_files=[
                runtime_env_path()
                if any(os.environ.get(name) for name in RUNTIME_ENV_FILE_ENVS)
                else self.instance_dir / "runtime.env"
            ],
            secret_values=self._redaction_values(),
        )

    def create(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        task_type: str,
        title: str,
        description: str = "",
        target: str = "ready",
        reference: str = "",
        blocked_by: str = "",
        head: str = "",
        review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        seed_ref: str = "",
        supersedes: str = "",
        complexity: str = "standard",
        family_preference: str = "auto",
        codex_launch_mode: str = "",
        sprint: str = "",
        priority: str = "",
        budget_event: str = "",
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        request_id: str | None = None,
        restoring: bool = False,
    ) -> dict[str, Any]:
        """Create an ordinary task through the released admission contract."""
        return self._create(
            role=role,
            actor=actor,
            project=project,
            task_type=task_type,
            title=title,
            description=description,
            target=target,
            reference=reference,
            blocked_by=blocked_by,
            head=head,
            review_head=review_head,
            slug=slug,
            base_branch=base_branch,
            seed_ref=seed_ref,
            supersedes=supersedes,
            complexity=complexity,
            family_preference=family_preference,
            codex_launch_mode=codex_launch_mode,
            sprint=sprint,
            priority=priority,
            budget_event=budget_event,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason,
            request_id=request_id,
            restoring=restoring,
            steward_report=False,
        )

    def _create(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        task_type: str,
        title: str,
        description: str = "",
        target: str = "ready",
        reference: str = "",
        blocked_by: str = "",
        head: str = "",
        review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        seed_ref: str = "",
        supersedes: str = "",
        complexity: str = "standard",
        family_preference: str = "auto",
        codex_launch_mode: str = "",
        sprint: str = "",
        priority: str = "",
        budget_event: str = "",
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        request_id: str | None = None,
        restoring: bool = False,
        steward_report: bool,
    ) -> dict[str, Any]:
        # Restore bypasses new-work admission only; all other guards still apply.
        self._role(role, _CREATE_ROLES)
        project = project.strip()
        task_type = task_type.strip()
        title = title.strip() if restoring else self._redact_for_board(title.strip())
        description = description if restoring else self._redact_for_board(description)
        target = target.strip()
        reference = reference.strip()
        blocked_by = blocked_by.strip()
        head = head.strip()
        review_head = review_head.strip()
        slug = slug.strip()
        base_branch = base_branch.strip()
        seed_ref = seed_ref.strip()
        supersedes = supersedes.strip()
        complexity = complexity.strip() or "standard"
        family_preference = family_preference.strip() or "auto"
        codex_launch_mode = codex_launch_mode.strip()
        sprint = sprint.strip()
        priority = priority.strip()
        budget_event = budget_event.strip()
        sprint_override_reason = (
            sprint_override_reason.strip()
            if restoring
            else self._redact_for_board(sprint_override_reason.strip())
        )
        if not project:
            raise TaskError("validation", "create requires a non-empty project", 2)
        if task_type not in _TASK_TYPES:
            known = ", ".join(sorted(_TASK_TYPES))
            raise TaskError("validation", f"unknown task type {task_type!r} (known: {known})", 2)
        if not title:
            raise TaskError("validation", "create requires a non-empty title", 2)
        if target not in {"ready", "issues", "in_progress"}:
            raise TaskError("validation", "create target must be ready, issues or in_progress", 2)
        if target == "in_progress" and not (role == "steward" and steward_report):
            raise TaskError("transition_forbidden", "only a steward report may be created In progress", 3)
        if steward_report and (role != "steward" or target != "in_progress"):
            raise TaskError("role_forbidden", "steward report creation requires steward In progress", 3)
        if steward_report and (task_type != "research" or not slug or reference or sprint):
            raise TaskError(
                "validation",
                "a steward report requires research, a slug, no explicit reference and no sprint",
                2,
            )
        if role in _PROPOSAL_CREATE_ROLES:
            if target != "issues":
                raise TaskError("role_forbidden", f"{role} may create only proposals in Issues", 3)
        elif target == "issues":
            raise TaskError("transition_forbidden", "execution tasks cannot be created in Issues", 3)
        if complexity not in _COMPLEXITIES:
            raise TaskError("validation", "complexity must be one of: " + ", ".join(sorted(_COMPLEXITIES)), 2)
        if family_preference not in _FAMILY_PREFERENCES:
            raise TaskError(
                "validation", "family preference must be one of: " + ", ".join(sorted(_FAMILY_PREFERENCES)), 2
            )
        if codex_launch_mode and codex_launch_mode not in _CODEX_LAUNCH_MODES:
            # Refuse unknown launch modes before any board call.
            known = ", ".join(sorted(_CODEX_LAUNCH_MODES))
            raise TaskError("validation", f"codex launch mode must be {known}", 2)
        if priority:
            raise TaskError("validation", "tasks do not accept product priority", 2)
        if slug and not _SLUG_RE.match(slug):
            raise TaskError("validation", "slug must match [a-z0-9-]{1,30}", 2)
        # Seed and integration base are two different refs (secretary-1541). A restore reproduces
        # cards that were admitted under the older single-field contract, so it carries whatever
        # they hold; the dispatcher refuses such a base fast and typed when it next runs the card.
        if not restoring:
            base_refusal = integration_base_refusal(base_branch) if base_branch else ""
            if base_refusal:
                raise TaskError("base_branch_not_integration_target", base_refusal, 2)
            seed_refusal = seed_ref_refusal(seed_ref) if seed_ref else ""
            if seed_refusal:
                raise TaskError("validation", seed_refusal, 2)
            if supersedes and not seed_ref:
                raise TaskError(
                    "validation", "supersedes names the predecessor of a seed; it requires --seed-ref", 2
                )
            if seed_ref and not supersedes:
                # A seed is inherited content, and whose content it is has to be readable off the
                # card: without it nobody can tell an intentional reslice from a stray ref.
                raise TaskError(
                    "validation",
                    "a seed requires --supersedes naming the predecessor card it inherits from",
                    2,
                )
        linked_sprint: dict[str, Any] | None = None
        if sprint:
            from secretary.sprints import SprintReader

            linked_sprint = SprintReader(self.client).show(sprint, include_cards=False)
            if linked_sprint["status"] != "open":
                raise TaskError("closed", "cannot link a new card to a closed or stopped sprint", 3)
            if project not in linked_sprint.get("reservations", []):
                raise TaskError(
                    "sprint_project_unreserved",
                    f"project {project!r} is not reserved by sprint {sprint}",
                    3,
                )
        if budget_event not in {"", "recreated_task", "hotfix"}:
            raise TaskError("validation", "budget event must be recreated_task or hotfix", 2)
        if budget_event and not sprint:
            raise TaskError("validation", "budget event requires a linked sprint", 2)

        request_id = request_id or str(uuid.uuid4())
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=project,
            card_sprint=sprint,
            linked_sprint=linked_sprint,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason,
            request_id=request_id,
            reference=reference,
        )
        # Admission follows ownership; Issues proposals and restores are not new work.
        if target == "ready" and not sprint and not restoring and not override_payload:
            raise TaskError("validation", "task creation requires an open sprint", 2)
        payload: dict[str, Any] = {
            "project": project,
            "task_type": task_type,
            "target": target,
            "reference": reference or None,
            "blocked_by": blocked_by or None,
            "head": head or None,
            "review_head": review_head or None,
            "slug": slug or None,
            "base_branch": base_branch or None,
            "seed_ref": seed_ref or None,
            "supersedes": supersedes or None,
            "complexity": complexity,
            "family_preference": family_preference,
            "codex_launch_mode": codex_launch_mode or None,
            "sprint": sprint or None,
            "budget_event": budget_event or None,
            **({"steward_report": True} if steward_report else {}),
            **override_payload,
            "title_sha256": _digest(title),
            "description_sha256": _digest(description),
        }
        # Allocate and stage automatic references under the board lock.
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind="created", reference=None, identity=payload)
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {
                "action": "created",
                "task": self.reader.show(str(committed["ref"])),
                "event_id": event_id,
                "replayed": True,
            }
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind="created", reference=None, identity=payload)
            try:
                self._finish_pending_cleanup(pending, None)
                task = self._pending_create_task(pending)
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {"action": "created", "task": task, "event_id": event_id, "replayed": True}

        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": "created",
            "outcome": "success",
            "task_id": "",
            "ref": reference,
            "backend": {
                "kind": "kanboard",
                "task_id": None,
                "revision": "pending",
                "reference_assignment": "atomic",
            },
            "request_id": request_id,
            "payload": payload,
        }
        self.audit.stage(request_id, event)
        try:
            created_ref = self._create_backend(
                project=project,
                task_type=task_type,
                title=title,
                description=description,
                target=target,
                reference=reference,
                blocked_by=blocked_by,
                head=head,
                review_head=review_head,
                slug=slug,
                base_branch=base_branch,
                seed_ref=seed_ref,
                supersedes=supersedes,
                complexity=complexity,
                family_preference=family_preference,
                codex_launch_mode=codex_launch_mode,
                sprint=sprint,
                steward_report=steward_report,
                event=event,
                request_id=request_id,
            )
        except _CommittedWriteError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        except Exception:
            self.audit.discard(request_id)
            raise
        try:
            task = self.reader.show(created_ref)
        except Exception:  # noqa: BLE001 - any post-create read failure is an ambiguous commit.
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        event["task_id"] = task["id"]
        event["ref"] = created_ref
        event["backend"]["revision"] = _revision(task)
        self.audit.stage(request_id, event)
        try:
            event_id = self.audit.append(request_id, event)
        except OSError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        return {"action": "created", "task": task, "event_id": event_id, "replayed": False}

    def create_steward_report(
        self,
        *,
        actor: str,
        project: str,
        title: str,
        slug: str,
        description: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create the steward's accounting artifact directly in In progress.

        The generic create transaction owns its reference reservation, staged
        identity and pending-audit recovery.  This narrow facade only supplies
        the invariant report shape; it never creates a temporary Ready card and
        deliberately does not require a sprint.
        """
        return self._create(
            role="steward",
            actor=actor,
            project=project,
            task_type="research",
            title=title,
            description=description,
            target="in_progress",
            slug=slug,
            steward_report=True,
            request_id=request_id,
        )

    def _create_backend(
        self,
        *,
        project: str,
        task_type: str,
        title: str,
        description: str,
        target: str,
        reference: str,
        blocked_by: str,
        head: str,
        review_head: str,
        slug: str,
        base_branch: str,
        seed_ref: str,
        supersedes: str,
        complexity: str,
        family_preference: str,
        codex_launch_mode: str,
        sprint: str,
        steward_report: bool,
        event: dict[str, Any],
        request_id: str,
    ) -> str:
        # The board accepts duplicate references, so holding this lock from the high-water
        # read through createTask prevents two local task-create processes assigning one ref.
        with reference_allocation_lock(self.data_dir):
            board_id, columns, swimlanes = self.reader._board()
            created_ref = reference or next_project_reference(self.client, board_id, project)
            # One question for both paths. A caller-supplied reference may name a card that
            # already exists, and an allocated one is only as free as the enumeration it was
            # counted from, so neither is written before the backend is asked about that exact
            # reference. Archived rows answer too: they hold their reference for good.
            if project_card_by_reference(self.client, board_id, created_ref):
                raise TaskError("validation", f"task reference {created_ref} is already claimed", 2)
            column_id = _target_column_id(columns, target)
            if column_id is None:
                raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
            swimlane_id = _matching_swimlane(swimlanes, project)
            # Persist the allocation before the atomic backend write. A process that dies
            # after createTask still leaves a recoverable, already-reserved reference.
            event["ref"] = created_ref
            self.audit.stage(request_id, event)
            task_id = _positive_int(
                self.client.call(
                    "createTask",
                    project_id=board_id,
                    title=title,
                    description=description,
                    column_id=column_id,
                    swimlane_id=swimlane_id or 0,
                    reference=created_ref,
                )
            )
            if task_id is None:
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            event["task_id"] = f"task_kanboard_{task_id}"
            event["backend"]["task_id"] = task_id
            try:
                self.audit.stage(request_id, event)
            except OSError as exc:
                raise _CommittedWriteError() from exc
            try:
                reference_persisted = self.reader.show_id(task_id)["ref"] == created_ref
            except Exception as exc:
                raise _CommittedWriteError() from exc
            if not reference_persisted:
                raise _CommittedWriteError()
            try:
                values = {
                    "record_type": "task",
                    "task_type": task_type,
                    "project": project,
                    "complexity": complexity,
                    "family_preference": family_preference,
                }
                if blocked_by:
                    values["blocked_by"] = blocked_by
                if head:
                    values["head"] = head
                if review_head:
                    values["review_head"] = review_head
                if slug:
                    values["slug"] = slug
                if base_branch:
                    values["base_branch"] = base_branch
                if seed_ref:
                    values["seed_ref"] = seed_ref
                if supersedes:
                    values["supersedes"] = supersedes
                if codex_launch_mode:
                    values["codex_launch_mode"] = codex_launch_mode
                if sprint:
                    values["sprint_ref"] = sprint
                if steward_report:
                    values.update({"record_type": "task", "claim": slug, "steward_report": "1"})
                self.client.call("saveTaskMetadata", task_id=task_id, values=values)
                if steward_report:
                    created = self.reader.show_id(task_id)
                    if not (
                        created["state"] == "in_progress"
                        and created["project"] == project
                        and created["type"] == "research"
                        and created.get("record_type") == "task"
                        and created["claim"]["worker"] == slug
                        and _is_steward_report(created)
                    ):
                        raise _CommittedWriteError()
            except Exception as exc:
                raise _CommittedWriteError() from exc
            return created_ref

    def comment(
        self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None
    ) -> dict[str, Any]:
        self._role(role, _COMMENT_ROLES)
        body = self._redact_for_board(body)
        payload = {"marker": role, "body_sha256": _digest(body)}
        return self._write(
            "commented",
            role,
            actor,
            reference,
            request_id,
            payload,
            lambda task: self.client.call(
                "createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{body}"
            ),
            identity=payload,
        )

    def _require_committed_workspace(self) -> None:
        """Refuse a done report from a dirty checkout.

        The worker runs the protocol from its own workspace, so failing here lets it commit and retry
        inside the same session instead of learning from the dispatcher that its card went to blocked.
        """
        if self.workspace is not None:
            workspace: Path = self.workspace
        else:
            try:
                workspace = Path.cwd()
            except OSError:
                return
        dirt = workspace_dirt(workspace)
        if not dirt:
            return
        shown = [line[3:].strip().strip('"') for line in dirt[:10]]
        files = ", ".join(shown)
        if len(dirt) > len(shown):
            files += f", +{len(dirt) - len(shown)} more"
        raise TaskError(
            "uncommitted", f"workspace has uncommitted changes: {files}; commit them and retry", 3
        )

    def report(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        kind: str,
        body: str,
        classification: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """A worker's report, and for a blocked one the kind of blocker it hit.

        The classification is required rather than offered: an external fact and a wrong task definition
        are repaired by different people. Two values and no free text, so repeated blocks from one head
        are countable. Its payload is staged as one typed Card occurrence which renders the
        `classification:` line, deliberately not card metadata that could disagree with the event.
        """
        self._role(role, {"worker"})
        body = self._redact_for_board(body)
        if kind not in {"done", "blocked"} or not body.strip():
            raise TaskError("validation", "reports require a non-empty body", 2)
        classification = classification.strip()
        if kind == "blocked" and classification not in _BLOCK_CLASSIFICATIONS:
            raise TaskError(
                "validation",
                "blocked reports require --classification, one of " + ", ".join(_BLOCK_CLASSIFICATIONS),
                2,
            )
        if kind == "done" and classification:
            raise TaskError("validation", "a done report carries no classification", 2)
        request_id = request_id or str(uuid.uuid4())
        # Resolve immutable ownership before either fresh admission or a card
        # read.  A replay must stay a pure replay, including when its worker
        # checkout has since become dirty or the card is no longer readable.
        legacy_owned = False
        try:
            owned = self.board_host.canon.event(request_id)
        except ValueError:
            owned = None
            legacy_owned = (
                self.audit.committed_event(request_id) is not None
                or self.audit.pending_event(request_id) is not None
            )
            if not legacy_owned:
                raise
        marker_data = owned.data if owned is not None else {}
        if owned is None and not legacy_owned:
            if kind == "done":
                self._require_committed_workspace()
            # This is the writer boundary for a worker report.  Bind the
            # report to the specification it actually answered now, rather
            # than asking a later terminal projection to guess from a mutable
            # card description.
            current = self.reader.show(reference)
            revision = specification_revision(self.audit.events(reference), current["description"])
            specification_data = {
                "description_sha256": _digest(current["description"]),
                "specification_revision": revision or None,
            }
        else:
            # Released marker records remain replayable without being
            # rewritten into the forward-lineage shape.
            specification_data = {
                name: marker_data[name]
                for name in ("description_sha256", "specification_revision")
                if name in marker_data
            }
        return self._marker_write(
            action="reported",
            event_kind=EventKind.CARD_REPORTED,
            role=role,
            actor=actor,
            reference=reference,
            reason=body,
            request_id=request_id,
            data={
                "marker": f"report:{kind}",
                "status": kind,
                "body": body,
                "body_sha256": _digest(body),
                **specification_data,
                "classification": classification or None,
            },
            fresh_admission=None,
        )

    def verdict(
        self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None
    ) -> dict[str, Any]:
        self._role(role, {"reviewer"})
        body = self._redact_for_board(body)
        if kind not in {"green", "red"} or not body.strip():
            raise TaskError("validation", "verdicts require a non-empty body", 2)
        current = self.reader.show(reference)
        revision = specification_revision(self.audit.events(reference), current["description"])
        return self._marker_write(
            action="verdict",
            event_kind=EventKind.CARD_VERDICTED,
            role=role,
            actor=actor,
            reference=reference,
            reason=body,
            request_id=request_id,
            data={
                "marker": f"review:{kind}",
                "status": kind,
                "body": body,
                "body_sha256": _digest(body),
                "description_sha256": _digest(current["description"]),
                "specification_revision": revision or None,
            },
        )

    def decide(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        kind: str,
        body: str,
        protocol_prerequisites: Iterable[str] = (),
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Record what to do with a parked card, apart from the move that does it.

        The decision and its effect are two facts. Recording the decision first is what makes it
        checkable, because the move out of Assessment refuses to carry one that is not on the card.

        The observer decides, and nobody else; a PO that has to intervene moves the card with
        `--sprint-override` and a reason. The same sprint reservation guard `move` carries applies here
        and answers about the caller as well as the card: the caller's sprint is the one the dispatcher
        launched its head for, carried in the head's environment, so the binding rather than the actor
        id distinguishes one sprint's observer from another's.
        """
        self._role(role, {"observer"})
        body = self._redact_for_board(body)
        if kind not in _DECISIONS:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(_DECISIONS))}", 2)
        if not body.strip():
            raise TaskError("validation", "a decision requires a non-empty reason", 2)
        declared_prerequisites = tuple(protocol_prerequisites)
        request_id = request_id or str(uuid.uuid4())
        with assessment_decision_lock(self.data_dir, reference):
            # Resolve immutable request ownership before mutable decision state.
            try:
                owned = self.board_host.canon.event(request_id)
            except ValueError as exc:
                message = str(exc)
                if "released generic audit record" in message:
                    message = "request id belongs to another operation or payload"
                raise TaskError("validation", message, 2) from None
            if owned is not None:
                # A retry must carry the immutable binding which the first decision committed.
                # Do not re-derive it from mutable board state or drop it from the marker identity.
                owned_data = owned.data if isinstance(owned.data, dict) else {}
                if tuple(owned_data.get("protocol_prerequisites") or ()) != declared_prerequisites:
                    raise TaskError("validation", "request id belongs to another operation or payload", 2)
                replay_data = {
                    "marker": f"decision:{kind}",
                    "decision": kind,
                    "body": body,
                    "body_sha256": _digest(body),
                }
                for field_name in ("description_sha256", "specification_revision", "protocol_prerequisites"):
                    if field_name in owned_data:
                        replay_data[field_name] = owned_data[field_name]
                return self._marker_write(
                    action="decided",
                    event_kind=EventKind.CARD_DECIDED,
                    role=role,
                    actor=actor,
                    reference=reference,
                    reason=body,
                    request_id=request_id,
                    data=replay_data,
                )
            current = self.reader.show(reference)
            # Authorization before anything about the card: which sprint holds the project is the
            # question of whether this observer may write here at all.
            self._guard_sprint_write(
                role=role,
                actor=actor,
                project=current["project"],
                card_sprint=str(current.get("sprint") or ""),
                linked_sprint=None,
                sprint_override=False,
                sprint_override_reason="",
                request_id=request_id,
                reference=reference,
            )
            if not self._sprint_holds_project(current["project"]):
                raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
            committed_events = self.audit.events(reference)
            pending_decisions = [
                event
                for event in self.audit.pending_events()
                if str(event.get("ref") or "") == reference and _event_action(event) == "decided"
            ]
            visit, existing = assessment_resolution([*committed_events, *pending_decisions])
            marker_data = {
                "marker": f"decision:{kind}",
                "decision": kind,
                "body": body,
                "body_sha256": _digest(body),
                "assessment_visit": visit or None,
                "description_sha256": _digest(current["description"]),
                "specification_revision": specification_revision(committed_events, current["description"])
                or None,
                "protocol_prerequisites": list(declared_prerequisites),
            }
            if current["state"] != "assessment":
                raise TaskError(
                    "transition_forbidden", "a decision is only recorded on a card in Assessment", 3
                )
            if existing is not None:
                existing_payload = _event_payload(existing)
                existing_kind = str(existing_payload.get("decision") or "")
                existing_request = str(existing.get("request_id") or "")
                if self.audit.pending_event(existing_request) is not None:
                    raise TaskError(
                        "decision_pending",
                        f"Assessment visit {visit} has an unfinished {existing_kind} decision; reconcile request {existing_request}",
                        4,
                    )
                if existing_kind != kind:
                    raise TaskError(
                        "decision_already_recorded",
                        f"Assessment visit {visit} already has a {existing_kind} decision",
                        3,
                    )
                return {
                    "action": "decided",
                    "task": current,
                    "event_id": str(existing.get("event_id") or existing.get("request_id") or ""),
                    "replayed": True,
                }
            if kind == "rework":
                try:
                    validate_rework_prerequisites(
                        declared_prerequisites,
                        specification_revision=marker_data["specification_revision"],
                    )
                except ValueError as exc:
                    raise TaskError("validation", str(exc), 2) from None
                except ArtifactOwnershipViolation as violation:
                    self._deny_rework_artifact_ownership(
                        violation=violation,
                        role=role,
                        actor=actor,
                        reference=reference,
                        request_id=request_id,
                        protocol_prerequisites=declared_prerequisites,
                    )
            elif declared_prerequisites:
                raise TaskError("validation", "protocol prerequisites are only supported for rework", 2)
            return self._marker_write(
                action="decided",
                event_kind=EventKind.CARD_DECIDED,
                role=role,
                actor=actor,
                reference=reference,
                reason=body,
                request_id=request_id,
                data=marker_data,
                require_assessment=True,
            )

    def routing(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one routing telemetry record for the card.

        Journal-only: the board holds no per-attempt routing history, so this write has no backend
        mutation. The event still goes through the normal pending/commit path, which makes it idempotent
        per request id and carries it into the recovery checkpoint.
        """
        self._role(role, {"dispatcher"})
        phase = _text(payload.get("phase"))
        if phase not in _ROUTING_PHASES:
            known = ", ".join(sorted(_ROUTING_PHASES))
            raise TaskError("validation", f"unknown routing phase {phase!r} (known: {known})", 2)
        heads = payload.get("heads")
        if not isinstance(heads, list) or not heads:
            raise TaskError("validation", "routing requires at least one head record", 2)
        return self._write(
            "routing",
            role,
            actor,
            reference,
            request_id,
            dict(payload),
            lambda task: None,
            identity=dict(payload),
        )

    def outcome_round_context(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Persist one exact forward source identity before its consumer runs.

        This is journal-only.  It is deliberately a typed dispatcher boundary
        rather than comment prose: recovery reads the request id that names
        this record and never reconstructs a worker, reviewer, or Assessment
        identity from card history.
        """
        self._role(role, {"dispatcher"})
        _validate_outcome_round_context(data)
        if not request_id.strip():
            raise TaskError("validation", "outcome round context needs the request id it owns", 2)
        return self._write(
            "outcome_round_context",
            role,
            actor,
            reference,
            request_id,
            dict(data),
            lambda task: None,
            identity=dict(data),
        )

    def attempt_usage(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: dict[str, Any],
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Append one durable ``attempt.usage`` occurrence for a finished worker or review phase.

        Journal-only, like routing telemetry: what a phase cost is not a board mutation, and the
        card carries no field it could disagree with. Unlike routing it is a typed protocol event,
        so the schema is checked at this boundary and the audit export exposes it without any
        marker prose to parse.

        The request id names one occurrence and owns it. A replay — a re-entered tick, a dispatcher
        recovering the same acceptance — commits the event that already owns the id rather than a
        freshly computed one, so a later read of a changed session file can neither add a second
        occurrence nor overwrite the first.

        Two durability steps, and the caller is told which one it reached. The exact occurrence is
        staged first, so a card cannot advance past a finished phase with nothing owed for it; the
        append then publishes it. An append that fails leaves the staged obligation, which is what
        ``finish_attempt_usage`` completes later — nothing is recomputed from a session file that has
        moved on. A stage that fails is an audit failure, and the caller has to treat it as one.
        """
        self._role(role, {"dispatcher"})
        if not request_id.strip():
            raise TaskError("validation", "an attempt usage event needs the request id it owns", 2)
        canon = self.board_host.canon
        if canon is None:
            raise TaskError("backend_unavailable", "board event canon is unavailable", 1)
        try:
            existing = canon.event(request_id)
        except (OSError, ValueError) as exc:
            raise TaskError(
                "audit_unavailable", f"attempt usage occurrence is unreadable: {exc}", 4
            ) from None
        if existing is not None:
            return self._commit_attempt_usage(canon, request_id, existing, replayed=True)
        try:
            event = Event(
                event_id="evt_" + uuid.uuid4().hex,
                kind=EventKind.ATTEMPT_USAGE,
                entity_kind=EntityKind.CARD,
                ref=reference,
                actor=Actor(role, actor),
                reason=reason,
                occurred_at=datetime.now(UTC),
                data=dict(data),
            )
            canon.stage(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError("audit_unavailable", "attempt usage occurrence could not be staged", 4) from None
        return self._commit_attempt_usage(canon, request_id, event, replayed=False)

    def _commit_attempt_usage(
        self,
        canon: BoardEventCanon,
        request_id: str,
        event: Event,
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        """Publish the staged occurrence, or report that its obligation is still owed."""
        try:
            canon.commit(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError(
                "audit_pending",
                "attempt usage occurrence is staged and awaits its journal append",
                4,
            ) from None
        return {"action": "attempt_usage", "event_id": event.event_id, "replayed": replayed}

    def finish_attempt_usage(self, *, role: str, reference: str = "") -> int:
        """Publish staged ``attempt.usage`` occurrences: this card's, or every card's.

        The recovery half of the durability order above. It finishes the exact staged record rather
        than a re-derived one, so a session file that has grown since cannot change what the phase
        was accounted for, and it is idempotent: a record already appended is simply gone from the
        pending set.

        Without a ``reference`` it takes the whole pending set. That is the form the production tick
        calls, because the card whose phase is owed an account may have gone Blocked or Done and be
        nowhere the tick would otherwise look. A record that cannot be published is left exactly
        where it is and stays owed.
        """
        self._role(role, {"dispatcher"})
        canon = self.board_host.canon
        if canon is None:
            return 0
        finished = 0
        for occurrence in canon.attempt_usage_occurrences(ref=reference):
            if not occurrence.pending:
                continue
            try:
                canon.commit(occurrence.request_id, occurrence.event)
            except (OSError, TypeError, ValueError, TaskError):
                # The obligation stays exactly where it is: still staged, still owed, still exact.
                continue
            finished += 1
        return finished

    def attempt_outcome(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: dict[str, Any],
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Stage then append one immutable observational terminal occurrence.

        This writer intentionally has no board effect.  Its caller is required
        to call it only after the lifecycle owner has confirmed the terminal
        move; a retry can only append the exact staged object.
        """
        self._role(role, {"dispatcher"})
        if not request_id.strip():
            raise TaskError("validation", "an attempt outcome needs the request id it owns", 2)
        canon = self.board_host.canon
        if canon is None:
            raise TaskError("backend_unavailable", "board event canon is unavailable", 1)
        try:
            occurrences = canon.attempt_outcome_occurrences(ref=reference)
            key = (reference, data.get("attempt_id"), data.get("report_generation"))
            existing_occurrence = next(
                (
                    occurrence
                    for occurrence in occurrences
                    if (
                        occurrence.event.ref,
                        occurrence.event.data.get("attempt_id"),
                        occurrence.event.data.get("report_generation"),
                    )
                    == key
                ),
                None,
            )
            if existing_occurrence is not None:
                if existing_occurrence.event.data != data:
                    raise AnalyticsOutcomeConflict(
                        f"attempt outcome natural key {key!r} has conflicting payloads"
                    )
                return self._commit_attempt_outcome(
                    canon, existing_occurrence.request_id, existing_occurrence.event, replayed=True
                )
            event = Event(
                event_id="evt_" + uuid.uuid4().hex,
                kind=EventKind.ATTEMPT_OUTCOME,
                entity_kind=EntityKind.CARD,
                ref=reference,
                actor=Actor(role, actor),
                reason=reason,
                occurred_at=datetime.now(UTC),
                data=dict(data),
            )
            canon.stage(request_id, event)
        except AnalyticsOutcomeConflict as exc:
            raise TaskError("analytics_outcome_conflict", str(exc), 3) from None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError("audit_unavailable", "attempt outcome could not be staged", 4) from None
        return self._commit_attempt_outcome(canon, request_id, event, replayed=False)

    def _commit_attempt_outcome(
        self, canon: BoardEventCanon, request_id: str, event: Event, *, replayed: bool
    ) -> dict[str, Any]:
        try:
            canon.commit(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError(
                "audit_pending", "attempt outcome is staged and awaits its journal append", 4
            ) from None
        return {"action": "attempt_outcome", "event_id": event.event_id, "replayed": replayed}

    def finish_attempt_outcomes(self, *, role: str, reference: str = "") -> int:
        """Append staged outcomes only; it never derives or changes lifecycle facts."""
        self._role(role, {"dispatcher"})
        canon = self.board_host.canon
        if canon is None:
            return 0
        finished = 0
        for occurrence in canon.attempt_outcome_occurrences(ref=reference):
            if not occurrence.pending:
                continue
            try:
                canon.commit(occurrence.request_id, occurrence.event)
            except (OSError, TypeError, ValueError, TaskError):
                continue
            finished += 1
        return finished

    def claim(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        worker: str,
        resolved_head: str = "",
        resolved_review_head: str = "",
        slug: str = "",
        base_branch: str = "",
        cap: int = 3,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._role(role, {"dispatcher"})
        worker = worker.strip()
        if not worker:
            raise TaskError("validation", "claim requires a non-empty worker id", 2)
        if cap < 1:
            raise TaskError("validation", "claim cap must be positive", 2)
        request_id = request_id or str(uuid.uuid4())
        if self._legacy_record(request_id) is not None:
            # Legacy generic claims retain their replay path and cannot move non-Cards.
            _check_execution_record(self.reader.show(reference))
            payload = {
                "worker": worker,
                "resolved_head": resolved_head or None,
                "resolved_review_head": resolved_review_head or None,
                "slug": slug or None,
                "base_branch": base_branch or None,
                "cap": cap,
            }
            return self._write(
                "claimed",
                role,
                actor,
                reference,
                request_id,
                payload,
                lambda task: None,
                identity=payload,
            )
        existing = self._typed_event(request_id)
        task = self.reader.show(reference)
        # Same guard as move: a product or an issue is not an execution task, so it never takes a
        # claim even if someone dragged it into Ready by hand.  It runs on the replay too, because
        # a card can have been retyped between the two attempts.
        _check_execution_record(task)
        if existing is None:
            # Admission is the fresh request's job alone, and every part of it binds: a claim is
            # admitted only from Ready, only when nobody holds the card, and only inside the
            # predecessor and capacity rules.  A retrying claimant does not get past them by
            # having tried before, because a failed attempt leaves no claim behind to recognize.
            if task["state"] != "ready":
                raise TaskError("claim_conflict", "claim requires a Ready task", 3)
            if task["claim"]["worker"] is not None:
                raise TaskError("claim_conflict", "task is already claimed", 3)
            blocked_by = task.get("blocked_by")
            if blocked_by:
                predecessor = self.reader.show(str(blocked_by))
                if predecessor["state"] != "done":
                    raise TaskError("predecessor_open", "blocked_by task is not Done", 3)
            for active in self.reader.list(states=set(ACTIVE_STATES)):
                if active["id"] == task["id"] or _is_steward_report(active):
                    continue
                if (
                    active["type"] == "code"
                    and task["type"] == "code"
                    and active["project"] == task["project"]
                ):
                    raise TaskError(
                        "capacity_reached", "one active code task per project is already claimed", 3
                    )
            active_count = sum(
                1
                for active in self.reader.list(states=set(ACTIVE_STATES))
                if active["id"] != task["id"] and not _is_steward_report(active)
            )
            if active_count >= cap:
                raise TaskError("capacity_reached", "active task capacity is reached", 3)

        values = {
            "claim": worker,
            "resolved_head": resolved_head or task["routing"]["head_override"] or "",
        }
        if resolved_review_head:
            values["resolved_review_head"] = resolved_review_head
        if slug:
            values["slug"] = slug
        if base_branch:
            values["base_branch"] = base_branch
        # Write claim metadata only after the transition effect is proven.
        result = self._transition_card(
            reference=reference,
            target=CardState.IN_PROGRESS,
            role=role,
            actor=actor,
            reason=f"claimed by {worker}",
            request_id=request_id,
            finish=lambda _card: self.client.call(
                "saveTaskMetadata",
                task_id=_task_number(task),
                values=values,
            ),
        )
        return {
            "action": "claimed",
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": existing is not None,
        }

    def move(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        decision: str = "",
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        request_id: str | None = None,
        outcome_owed: dict[str, Any] | None = None,
        terminal_taxonomy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._role(role, _ROLES)
        reason = self._redact_for_board(reason)
        sprint_override_reason = self._redact_for_board(sprint_override_reason)
        request_id = request_id or str(uuid.uuid4())
        if outcome_owed is not None and not isinstance(outcome_owed, dict):
            raise TaskError("validation", "attempt outcome obligation must be an object", 2)
        if terminal_taxonomy is not None and not isinstance(terminal_taxonomy, dict):
            raise TaskError("validation", "terminal taxonomy must be an object", 2)
        task = self.reader.show(reference)
        if (
            role == "steward"
            and (task["state"], target) == ("in_progress", "done")
            and not _is_steward_report(task)
        ):
            raise TaskError(
                "transition_forbidden",
                "steward may close In progress only for its own report card",
                3,
            )
        if self._legacy_record(request_id) is not None:
            # A move recorded before Card transitions migrated is a generic audit operation, and
            # it stays one: its retry replays that record and its pending form is finished by the
            # released cleanup, exactly as they were on the version that wrote it.
            return self._legacy_move(
                role=role,
                actor=actor,
                reference=reference,
                target=target,
                reason=reason,
                decision=decision,
                sprint_override=sprint_override,
                sprint_override_reason=sprint_override_reason,
                request_id=request_id,
                task=task,
            )
        existing = self._typed_event(request_id)
        # Resolve replays before guards: their request id already owns a typed event.
        if existing is not None:
            # A transition written before outcome obligations were introduced
            # remains a valid lifecycle replay. It cannot be rewritten to add
            # telemetry, but the caller still finishes its observation
            # best-effort after the confirmed effect.
            if outcome_owed is not None:
                stored_obligation = existing.data.get("attempt_outcome_owed")
                # The committed lifecycle fact owns the telemetry identity on
                # a replay. A later dispatcher record may already describe
                # the next generation, so re-deriving it here would turn an
                # exact effect retry into a conflicting payload.
                outcome_owed = dict(stored_obligation) if isinstance(stored_obligation, dict) else None
            if terminal_taxonomy is not None:
                stored_taxonomy = existing.data.get("terminal_taxonomy")
                terminal_taxonomy = dict(stored_taxonomy) if isinstance(stored_taxonomy, dict) else None
            try:
                target_state = CardState(target)
            except ValueError:
                raise TaskError(
                    "transition_forbidden",
                    _forbidden_move_message(role, task["state"], target),
                    3,
                ) from None
            # A replay repeats no admission check and no backend move, but it does finish the
            # idempotent board cleanup the first attempt may have lost. The comment is the one
            # follow-up it never repeats: it is not idempotent, and the released reconciliation
            # never recreated one either.
            result = self._transition_card(
                reference=reference,
                target=target_state,
                role=role,
                actor=actor,
                reason=_transition_reason(reason, target),
                request_id=request_id,
                outcome_owed=outcome_owed,
                terminal_taxonomy=terminal_taxonomy,
                finish=self._transition_cleanup(
                    task,
                    source=str(existing.source_state or ""),
                    target=target,
                    reason="",
                    role=role,
                ),
            )
            replay = {
                "action": "moved",
                "task": self.reader.show(reference),
                "event_id": result.event.event_id,
                "replayed": True,
            }
            if outcome_owed is not None:
                replay["outcome_owed"] = outcome_owed
            return replay
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=task["project"],
            card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(),
            request_id=request_id,
            reference=reference,
        )
        source = task["state"]
        _check_execution_record(task)
        if role == "observer" and not override_payload and not self._sprint_holds_project(task["project"]):
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
        try:
            target_state = CardState(target)
            card_transition(role, source, target_state)
        except (ValueError, CardTransitionForbidden):
            raise TaskError(
                "transition_forbidden", _forbidden_move_message(role, source, target), 3
            ) from None
        if (
            role == "steward"
            and (target == "blocked" or (source, target) == ("blocked", "done"))
            and not reason.strip()
        ):
            raise TaskError("validation", "this steward transition requires a non-empty reason", 2)
        # A Blocked exit records the observer's disposition of its classification.
        if role == "observer" and source == "blocked" and not reason.strip():
            raise TaskError("validation", "moving a card out of Blocked requires a non-empty reason", 2)
        self._check_decision(task, source, target, decision, role)
        result = self._transition_card(
            reference=reference,
            target=target_state,
            role=role,
            actor=actor,
            reason=_transition_reason(reason, target),
            request_id=request_id,
            outcome_owed=outcome_owed,
            terminal_taxonomy=terminal_taxonomy,
            finish=self._transition_cleanup(
                task,
                source=source,
                target=target,
                reason=reason,
                role=role,
            ),
        )
        moved = {
            "action": "moved",
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": False,
        }
        if outcome_owed is not None:
            moved["outcome_owed"] = outcome_owed
        return moved

    def _legacy_move(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        decision: str,
        sprint_override: bool,
        sprint_override_reason: str,
        request_id: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        """Replay a move this request id already recorded as a released generic operation."""
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=task["project"],
            card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(),
            request_id=request_id,
            reference=reference,
        )
        return self._write(
            "moved",
            role,
            actor,
            reference,
            request_id,
            lambda task: {
                "from": task["state"],
                "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                **({"decision": decision} if decision else {}),
                **override_payload,
            },
            lambda task: None,
            # `from` is the column the move already left, so it is the one field a retry cannot
            # recompute. Everything the caller asked for is compared.
            identity={
                "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                "decision": decision or None,
                "sprint_override_reason": override_payload.get("sprint_override_reason"),
            },
        )

    def _legacy_record(self, request_id: str) -> dict[str, Any] | None:
        """The released generic audit record this request id owns, if it owns one.

        The two representations are told apart by the record's own discriminator, never by guessing from
        a payload.
        """
        record = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if record is None or record.get("record_type") == TaskAudit._PROTOCOL_EVENT_RECORD_TYPE:
            return None
        return record

    def _transition_cleanup(
        self,
        task: dict[str, Any],
        *,
        source: str,
        target: str,
        reason: str,
        role: str,
    ) -> Callable[[Any], None]:
        """The board work a migrated Card state edge still owes once its column effect lands.

        Handed to the adapter so it runs inside the transition's transaction: it completes before the
        event commits, and an incomplete one leaves the exact pending typed record that both a retry and
        :meth:`reconcile` know how to finish.
        """

        def finish(_entity: Any) -> None:
            self._reset_transition_metadata(task, source=source, target=target)
            if reason.strip():
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[{role}]\n{reason}",
                )

        return finish

    def _reset_transition_metadata(self, task: dict[str, Any], *, source: str, target: str) -> None:
        """Apply the board metadata a Card state edge resets.

        Kept idempotent on purpose: a retry or :meth:`reconcile` may repeat it after the column effect
        and its typed event are durable.
        """
        if target in {"ready", "done"}:
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values=_READY_RESET_METADATA)
        elif source == "validate":
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={"resolved_review_head": ""}
            )

    def _transition_card(
        self,
        *,
        reference: str,
        target: CardState,
        role: str,
        actor: str,
        reason: str,
        request_id: str,
        outcome_owed: dict[str, Any] | None = None,
        terminal_taxonomy: dict[str, Any] | None = None,
        finish: Callable[[Any], None] | None = None,
    ) -> MutationResult:
        """Run one state edge through the typed adapter and its shared journal.

        The sprint the card belongs to is not passed here: the adapter reads the live card to authorize
        the edge anyway. `finish` carries this writer's remaining board work into the same transaction.
        """
        try:
            return self.board_host.transition(
                TransitionRequest(
                    EntityKind.CARD,
                    reference,
                    target,
                    Actor(role, actor),
                    reason,
                    RelatedRefs(()),
                    request_id,
                    data={
                        **({"attempt_outcome_owed": dict(outcome_owed)} if outcome_owed is not None else {}),
                        **(
                            {"terminal_taxonomy": dict(terminal_taxonomy)}
                            if terminal_taxonomy is not None
                            else {}
                        ),
                    },
                ),
                finish=finish,
            )
        except BoardEventPending:
            raise TaskError(
                "audit_pending",
                "backend write committed; audit repair is required",
                4,
            ) from None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except CardTransitionForbidden as exc:
            raise TaskError("transition_forbidden", str(exc), 3) from None
        except BoardProtocolError as exc:
            raise TaskError("backend_error", str(exc), 1) from None

    def _typed_event(self, request_id: str) -> Any | None:
        if self.board_host.canon is None:
            return None
        try:
            return self.board_host.canon.event(request_id)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None

    def _check_decision(
        self,
        task: dict[str, Any],
        source: str,
        target: str,
        decision: str,
        role: str,
    ) -> None:
        """A card leaves Assessment on a decision somebody recorded, or it does not leave.

        Two rules binding different callers. A supplied decision has to be real and has to agree with
        where the card is going, whoever passes it: each decision has exactly one destination. Needing a
        decision at all is the dispatcher's rule, because the dispatcher performs decisions; the PO's
        move is the escape hatch, already recorded as a sprint override.

        `blocked` without a decision stays open even for the dispatcher: the steward's stale escalation
        and the dispatcher's own failure paths reach it without anyone deciding, and a card that cannot
        be blocked is a card nothing can rescue. The observer has no exit from Assessment at all.
        """
        if decision and decision not in _DECISIONS:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(_DECISIONS))}", 2)
        if decision and source != "assessment":
            raise TaskError("validation", "a decision is only carried by a move out of Assessment", 2)
        if decision and _DECISION_TARGETS[decision] != target:
            raise TaskError(
                "decision_mismatch",
                f"a {decision} decision moves the card to {_DECISION_TARGETS[decision]}, not {target}",
                3,
            )
        if (
            source == "assessment"
            and target in _DECIDED_TARGETS
            and not decision
            and role in _DECISION_BOUND_ROLES
        ):
            raise TaskError(
                "decision_required",
                "a card leaves Assessment only on a recorded decision: record one with "
                "`task decide` and pass it as --decision",
                3,
            )
        if source == "assessment" and target in _UNDECIDED_EXITS and role in _DECISION_BOUND_ROLES:
            raise TaskError(
                "decision_required",
                f"{role} may not move a parked card to {target}: that leaves Assessment with "
                "nothing decided. Decide the card, or have the PO move it",
                3,
            )
        if decision and not self._decision_recorded(task["ref"], decision):
            raise TaskError(
                "decision_required",
                f"no {decision} decision is recorded on this card since it entered Assessment",
                3,
            )

    def _decision_recorded(self, reference: str, decision: str) -> bool:
        return standing_decision(self.audit.events(reference)) == decision

    def edit(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        title: str | None = None,
        description: str | None = None,
        head: str | None = None,
        review_head: str | None = None,
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Revise a card's spec in place instead of piling corrections into comments.

        The audit event chains old and new content digests. Cards with an active attempt (In progress /
        Validate) are not editable: the running head works from a TASK.md snapshot, so a mid-flight
        revision must go through preempt/requeue, not a silent spec swap.
        """
        self._role(role, _EDIT_ROLES)
        title = self._redact_for_board(title) if title is not None else None
        description = self._redact_for_board(description) if description is not None else None
        sprint_override_reason = self._redact_for_board(sprint_override_reason)
        request_id = request_id or str(uuid.uuid4())
        if title is not None and not title.strip():
            raise TaskError("validation", "edit title must be non-empty", 2)
        if title is None and description is None and head is None and review_head is None:
            raise TaskError("validation", "edit requires a new title, description, head or review head", 2)
        current = self.reader.show(reference)
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=current["project"],
            card_sprint=str(current.get("sprint") or ""),
            linked_sprint=None,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(),
            request_id=request_id,
            reference=reference,
        )
        if (
            role in {"observer", "dispatcher"}
            and not override_payload
            and not self._sprint_holds_project(current["project"])
        ):
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
        payload = {
            "title_sha256": _digest(title.strip()) if title is not None else None,
            "title_sha256_was": _digest(current["title"]) if title is not None else None,
            "description_sha256": _digest(description) if description is not None else None,
            "description_sha256_was": _digest(current["description"]) if description is not None else None,
            "head": head.strip() or None if head is not None else None,
            "head_was": current["routing"]["head_override"] if head is not None else None,
            "review_head": review_head.strip() or None if review_head is not None else None,
            "review_head_was": current["routing"]["review_head_override"]
            if review_head is not None
            else None,
            **override_payload,
        }

        def mutation(task: dict[str, Any]) -> Any:
            if task["state"] not in _EDITABLE_STATES:
                raise TaskError("edit_forbidden", "edit requires a Ready or Blocked card", 3)
            number = _task_number(task)
            update: dict[str, Any] = {}
            if title is not None:
                update["title"] = title.strip()
            if description is not None:
                update["description"] = description
            committed = False
            if update:
                if not self.client.call("updateTask", id=number, **update):
                    raise TaskError("backend_error", "Kanboard rejected the write", 1)
                committed = True
            values = {}
            if head is not None:
                values["head"] = head.strip()
            if review_head is not None:
                values["review_head"] = review_head.strip()
            if values:
                try:
                    self.client.call("saveTaskMetadata", task_id=number, values=values)
                except Exception as exc:
                    if committed:
                        raise _CommittedWriteError() from exc
                    raise

        # Replay compares both replaced-text digests and requested values.
        identity = {key: value for key, value in payload.items() if not key.endswith("_was")}
        return self._write("edited", role, actor, reference, request_id, payload, mutation, identity=identity)

    def _sprint_holds_project(self, project: str) -> bool:
        """Whether an open sprint reserves this card's project."""
        from secretary.sprints import active_sprint_projects

        return bool(active_sprint_projects(self.data_dir).get(project))

    def _guard_sprint_write(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        card_sprint: str,
        linked_sprint: dict[str, Any] | None,
        sprint_override: bool,
        sprint_override_reason: str,
        request_id: str,
        reference: str,
    ) -> dict[str, str]:
        """Authorize one create/move/edit against the caller and the open-sprint reservation index.

        Two questions, in this order. Who is writing: a caller of role `observer` names the sprint it
        was launched for, and a write about any other sprint's card is refused as the identity failure
        it is. Then what is being written: which open sprint reserves the card's project.

        The identity half is fail-closed. A head that carries no binding cannot prove which sprint it is
        the observer of, and an unprovable caller is refused rather than admitted.
        """
        self._guard_observer_identity(
            role=role,
            actor=actor,
            project=project,
            card_sprint=card_sprint,
            request_id=request_id,
            reference=reference,
        )
        from secretary.sprints import (
            SprintReader,
            active_sprint_projects,
            refresh_active_sprint_projects,
            sprint_guard_index_initialized,
            update_active_sprint_projects,
        )

        if not sprint_guard_index_initialized(self.data_dir):
            try:
                refresh_active_sprint_projects(self.data_dir, SprintReader(self.client))
            except TaskError as exc:
                self._deny_sprint_write(
                    code="sprint_guard_unavailable",
                    message=f"cannot verify open sprints for project {project}; write it through the sprint entity",
                    role=role,
                    actor=actor,
                    project=project,
                    sprint="",
                    request_id=request_id,
                    reference=reference,
                )
                raise AssertionError("unreachable") from exc

        refs = set(active_sprint_projects(self.data_dir).get(project, []))
        if linked_sprint is not None and project in linked_sprint.get("reservations", []):
            refs.add(str(linked_sprint["ref"]))
        held: list[str] = []
        for sprint_ref in sorted(refs):
            try:
                sprint = (
                    linked_sprint
                    if linked_sprint and sprint_ref == linked_sprint.get("ref")
                    else SprintReader(self.client).show(sprint_ref, include_cards=False)
                )
            except TaskError as exc:
                self._deny_sprint_write(
                    code="sprint_guard_unavailable",
                    message=f"cannot verify sprint {sprint_ref} reserving project {project}; write it through the sprint entity",
                    role=role,
                    actor=actor,
                    project=project,
                    sprint=sprint_ref,
                    request_id=request_id,
                    reference=reference,
                )
                raise AssertionError("unreachable") from exc
            update_active_sprint_projects(self.data_dir, sprint)
            if sprint.get("status") == "open" and project in sprint.get("reservations", []):
                held.append(sprint_ref)
        if not held:
            return {}
        sprint_ref = card_sprint if card_sprint in held else held[0]
        if role == "po" and sprint_override:
            if not sprint_override_reason:
                self._deny_sprint_write(
                    code="validation",
                    message="sprint override requires a non-empty reason",
                    role=role,
                    actor=actor,
                    project=project,
                    sprint=sprint_ref,
                    request_id=request_id,
                    reference=reference,
                    exit_code=2,
                )
            self._grant_sprint_override(
                role=role,
                actor=actor,
                project=project,
                sprint=sprint_ref,
                reason=sprint_override_reason,
                request_id=request_id,
                reference=reference,
            )
            return {"sprint_override_reason": sprint_override_reason}
        # The caller was already proven to be this card's sprint's observer above; what is left is
        # that the sprint holding the project is the one the card is linked to.
        if role == "observer" and card_sprint in held:
            return {}
        if role == "dispatcher":
            return {}
        self._deny_sprint_write(
            code="sprint_write_forbidden",
            message=f"project {project} is reserved by open sprint {sprint_ref}; write it through the sprint entity {sprint_ref}",
            role=role,
            actor=actor,
            project=project,
            sprint=sprint_ref,
            request_id=request_id,
            reference=reference,
        )
        raise AssertionError("unreachable")

    def _guard_observer_identity(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        card_sprint: str,
        request_id: str,
        reference: str,
    ) -> None:
        """Refuse a write of role `observer` that is not about the caller's own sprint.

        The binding is the launcher's, carried in the head's own environment: the reservation index says
        which sprint holds the card, and this says which sprint the caller is.

        The two refusals are separate codes because they are separate failures — a missing binding is a
        head nobody bound, a mismatch is a bound head reaching outside its sprint — and neither is
        `role_forbidden`. A card that names no sprint is left to the reservation guard below.
        """
        if role != "observer":
            return
        from secretary.role_env import declared_observer_sprint

        declared = declared_observer_sprint()
        if not declared:
            self._deny_sprint_write(
                code="observer_identity_unbound",
                message="this observer names no sprint, so its writes cannot be authenticated; "
                "it has to be launched by the dispatcher for one sprint",
                role=role,
                actor=actor,
                project=project,
                sprint="",
                request_id=request_id,
                reference=reference,
            )
        if card_sprint and card_sprint != declared:
            self._deny_sprint_write(
                code="observer_sprint_mismatch",
                message=f"this observer belongs to sprint {declared} and the card is linked to "
                f"{card_sprint}; write it as that sprint's observer",
                role=role,
                actor=actor,
                project=project,
                sprint=declared,
                request_id=request_id,
                reference=reference,
            )

    def _grant_sprint_override(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        sprint: str,
        reason: str,
        request_id: str,
        reference: str,
    ) -> None:
        """Record the granted single-writer override before the operation it authorizes runs.

        A generic control-plane record like the denial, not part of the Card's typed event: the event
        describes the lifecycle edge, this describes the authority the writer used. Written before the
        operation stages or effects anything, so an override that could not be recorded does not happen.
        The derived request id keeps it off the operation's own retry key.
        """
        override_request_id = _sprint_guard_override_request_id(request_id)
        if self.audit.committed_event(override_request_id) is not None:
            return
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": "sprint_guard_override",
            "outcome": "granted",
            "task_id": "",
            "ref": reference,
            "backend": {"kind": "kanboard", "task_id": None, "revision": "not_written"},
            "request_id": override_request_id,
            "payload": {
                "project": project,
                "sprint": sprint,
                "sprint_override_reason": reason,
                "operation_request_id": request_id,
            },
        }
        self.audit.stage(override_request_id, event)
        try:
            self.audit.append(override_request_id, event)
        except OSError:
            raise TaskError(
                "audit_pending",
                "sprint override was granted but audit repair is required",
                4,
            ) from None

    def _deny_sprint_write(
        self,
        *,
        code: str,
        message: str,
        role: str,
        actor: str,
        project: str,
        sprint: str,
        request_id: str,
        reference: str,
        exit_code: int = 3,
    ) -> None:
        denial_request_id = _sprint_guard_denial_request_id(request_id)
        event = self.audit.committed_event(denial_request_id)
        if event is None:
            event = {
                "event_id": "evt_" + uuid.uuid4().hex,
                "schema_version": 1,
                "occurred_at": _now(),
                "actor": {"role": role, "id": actor},
                "kind": "sprint_guard_denied",
                "outcome": "denied",
                "task_id": "",
                "ref": reference,
                "backend": {"kind": "kanboard", "task_id": None, "revision": "not_written"},
                "request_id": denial_request_id,
                "payload": {
                    "code": code,
                    "message": message,
                    "project": project,
                    "sprint": sprint,
                    "operation_request_id": request_id,
                },
            }
            self.audit.stage(denial_request_id, event)
            try:
                self.audit.append(denial_request_id, event)
            except OSError:
                raise TaskError(
                    "audit_pending", "sprint write was denied but audit repair is required", 4
                ) from None
        payload = event.get("payload") if isinstance(event, dict) else {}
        raise TaskError(str(payload.get("code") or code), str(payload.get("message") or message), exit_code)

    def _deny_rework_artifact_ownership(
        self,
        *,
        violation: ArtifactOwnershipViolation,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        protocol_prerequisites: tuple[str, ...],
    ) -> None:
        """Persist the denied rework without mutating its parked card.

        The derived audit key lets the observer correct the instruction and reuse the decision
        request identity: a refusal is evidence, not an authoritative decision or worker outcome.
        """
        refusal_request_id = _artifact_ownership_refusal_request_id(request_id)
        data = {
            "decision": "rework",
            "code": "artifact_ownership_violation",
            "artifact": violation.artifact.name,
            "artifact_owner": violation.artifact.owner.value,
            "requested_role": violation.requested_role.value,
            "specification_revision": violation.specification_revision,
            "protocol_prerequisites": list(protocol_prerequisites),
        }
        existing = self.board_host.canon.event(refusal_request_id)
        if existing is None:
            event = Event(
                "evt_artifact_ownership_" + hashlib.sha256(refusal_request_id.encode("utf-8")).hexdigest(),
                EventKind.CARD_DECISION_REFUSED,
                EntityKind.CARD,
                reference,
                Actor(role, actor),
                violation.message,
                datetime.now(UTC),
                data=data,
            )
        else:
            if (
                existing.kind is not EventKind.CARD_DECISION_REFUSED
                or existing.ref != reference
                or existing.data != data
            ):
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            event = existing
        try:
            self.board_host.canon.commit(refusal_request_id, event)
        except (OSError, ValueError) as exc:
            raise TaskError("audit_pending", "artifact ownership refusal requires audit repair", 4) from exc
        raise ArtifactOwnershipTaskError(violation)

    def archive(
        self, *, role: str, actor: str, reference: str, reason: str, request_id: str | None = None
    ) -> dict[str, Any]:
        self._role(role, {"po"})
        reason = self._redact_for_board(reason)
        if not reason.strip():
            raise TaskError("validation", "archive requires a non-empty reason", 2)

        def mutation(task: dict[str, Any]) -> Any:
            if task.get("record_type") in {"issue", "product"}:
                raise TaskError(
                    "transition_forbidden",
                    "Product issues must be closed with secretary issue close; products cannot be archived",
                    3,
                )
            self._check_archivable(task)
            self._check_dispatcher_archivable(reference)
            try:
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[archive]\n{reason}",
                )
                if not self.client.call("closeTask", task_id=_task_number(task)):
                    raise TaskError("backend_error", "Kanboard rejected the archive", 1)
            except Exception as exc:
                raise _CommittedWriteError() from exc

        return self._write(
            "archived",
            role,
            actor,
            reference,
            request_id,
            {"reason_sha256": _digest(reason)},
            mutation,
            retry_payload={"reason": reason},
            identity={"reason_sha256": _digest(reason)},
        )

    def retire_done(
        self,
        *,
        reference: str,
        expected_date_moved: int,
        cutoff: float,
        retention_days: int,
        actor: str = "retro-retention",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one proven-old Done episode without using the PO archive path.

        ``date_moved`` identifies the episode, rather than merely the card.  A
        reopen or a move away and back to Done therefore turns an old candidate
        into a harmless skip before a close can be sent.  Pending records retain
        that proof and can retry a lost reply only for the same episode.
        """
        expected_date_moved = _positive_int(expected_date_moved) or 0
        if not expected_date_moved:
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        try:
            cutoff_value = float(cutoff)
        except (TypeError, ValueError):
            raise TaskError("validation", "Done retention requires a numeric cutoff", 2) from None
        if retention_days < 0:
            raise TaskError("validation", "Done retention days cannot be negative", 2)

        initial = self._retention_card(reference, task_id=None)
        if initial is None:
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        task_id, raw, metadata, done_id = initial
        self._check_retention_record(metadata)
        request_id = request_id or _done_retention_request_id(task_id, expected_date_moved)
        identity = {
            "expected_date_moved": expected_date_moved,
            "cutoff": cutoff_value,
            "retention_days": retention_days,
            "task_id": task_id,
        }
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind="retired", reference=reference, identity=identity)
            return {"action": "retired", "reference": reference, "retired": True, "replayed": True}
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind="retired", reference=reference, identity=identity)
            try:
                self._finish_pending_retired(pending)
                self._prove_retired_closed(pending)
                self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError, ValueError):
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {"action": "retired", "reference": reference, "retired": True, "replayed": True}

        # Do not stage a successful-looking occurrence for a candidate that has
        # already aged out of eligibility between the list and this write.
        if not self._retention_matches(raw, metadata, expected_date_moved, cutoff_value, done_id):
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": "retro", "id": actor},
            "kind": "retired",
            "outcome": "success",
            "task_id": f"task_kanboard_{task_id}",
            "ref": reference,
            "backend": {"kind": "kanboard", "task_id": task_id, "revision": "pending"},
            "request_id": request_id,
            "payload": identity,
        }
        self.audit.stage(request_id, event)
        try:
            # This is the final guard immediately before the destructive call.
            guarded = self._retention_card(reference, task_id=task_id)
            if guarded is None:
                self.audit.discard(request_id, event)
                return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
            _guarded_id, latest, latest_metadata, latest_done_id = guarded
            self._check_retention_record(latest_metadata)
            if not self._retention_matches(
                latest, latest_metadata, expected_date_moved, cutoff_value, latest_done_id
            ):
                self.audit.discard(request_id, event)
                return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
            if not self.client.call("closeTask", task_id=task_id):
                raise TaskError("backend_error", "Kanboard rejected Done retention", 1)
        except _CommittedWriteError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        except TaskError as exc:
            # A transport error after close is ambiguous; leave its pending
            # evidence.  A definite local guard/validation failure is not.
            if exc.code == "backend_unavailable":
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            current = self.audit.pending_event(request_id)
            if current == event:
                try:
                    self.audit.discard(request_id, event)
                except (OSError, TaskError):
                    pass
            raise
        except Exception:  # noqa: BLE001 - an unknown close reply is deliberately ambiguous.
            # JSON-RPC transport failures can occur after Kanboard applied the
            # close, so reconciliation must prove or safely retry this episode.
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        try:
            self._finish_pending_retired(event)
            self._prove_retired_closed(event)
            self.audit.append(request_id, event)
        except (TaskError, OSError, KeyError, TypeError, ValueError):
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        return {"action": "retired", "reference": reference, "retired": True, "replayed": False}

    def restore_card(
        self,
        *,
        reference: str,
        metadata: dict[str, str],
        target: str,
        position: int | None = None,
        swimlane: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        from secretary.task_restore import restore_card

        return restore_card(self, reference, metadata, target, position, swimlane, request_id)

    def restore_comment(
        self, *, reference: str, body: str, occurrence: int, request_id: str | None = None
    ) -> dict[str, Any]:
        from secretary.task_restore import restore_comment

        return restore_comment(self, reference, body, occurrence, request_id)

    def _move_raw(self, task: dict[str, Any], target: str, *, position: int = 1, swimlane_id: int) -> None:
        board_id, columns, _ = self.reader._board()
        column_id = _target_column_id(columns, target)
        if column_id is None:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        ok = self.client.call(
            "moveTaskPosition",
            project_id=board_id,
            task_id=_task_number(task),
            column_id=column_id,
            position=position,
            swimlane_id=swimlane_id,
        )
        if not ok:
            raise TaskError("backend_error", "Kanboard rejected the write", 1)

    def _current_swimlane_id(self, task: dict[str, Any]) -> int:
        board_id, _, _ = self.reader._board()
        raw = project_card_by_reference(self.client, board_id, task["ref"])
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        return _positive_int(raw.get("swimlane_id")) or 0

    def _marker_write(
        self,
        *,
        action: str,
        event_kind: EventKind,
        role: str,
        actor: str,
        reference: str,
        reason: str,
        request_id: str | None,
        data: dict[str, Any],
        require_assessment: bool = False,
        fresh_admission: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Send a control-plane marker through the typed host transaction.

        The command supplies its semantic fields once; the host stages them as an immutable event and
        derives the board comment from that event, so a retry never has a second representation to
        drift from.
        """
        request_id = request_id or str(uuid.uuid4())
        if require_assessment:
            current = self.reader.show(reference)
            if current["state"] != "assessment":
                raise TaskError(
                    "transition_forbidden", "a decision is only recorded on a card in Assessment", 3
                )
        try:
            result = self.board_host.marker_comment(
                MarkerComment(
                    reference,
                    event_kind,
                    Actor(role, actor),
                    reason,
                    data,
                    request_id=request_id,
                    fresh_admission=fresh_admission,
                )
            )
        except BoardEventPending:
            raise TaskError(
                "audit_pending",
                "backend write committed; audit repair is required",
                4,
            ) from None
        except ValueError as exc:
            message = str(exc)
            if "released generic audit record" in message:
                message = "request id belongs to another operation or payload"
            raise TaskError("validation", message, 2) from None
        except BoardProtocolError as exc:
            raise TaskError("backend_error", str(exc), 1) from None
        return {
            "action": action,
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": result.replayed,
        }

    def _write(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None,
        payload: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
        mutation: Any,
        *,
        identity: dict[str, Any],
        retry_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Every request id explicitly declares the operation identity it owns.
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind=kind, reference=reference, identity=identity)
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {
                "action": kind,
                "task": self.reader.show(reference),
                "event_id": event_id,
                "replayed": True,
            }
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind=kind, reference=reference, identity=identity)
            try:
                self._finish_pending_cleanup(pending, retry_payload)
                task = self.reader.show(str(pending["ref"]))
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise TaskError(
                    "audit_pending", "backend write committed; audit repair is required", 4
                ) from None
            return {
                "action": kind,
                "task": self.reader.show(reference),
                "event_id": event_id,
                "replayed": True,
            }
        task = self.reader.show(reference)
        event_payload = payload(task) if callable(payload) else payload
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": kind,
            "outcome": "success",
            "task_id": task["id"],
            "ref": reference,
            "backend": {"kind": "kanboard", "task_id": _task_number(task), "revision": _revision(task)},
            "request_id": request_id,
            "payload": event_payload,
        }
        self.audit.stage(request_id, event)
        try:
            mutation(task)
        except _CommittedWriteError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        except Exception:
            self.audit.discard(request_id)
            raise
        try:
            task = self.reader.show(reference)
        except Exception:  # noqa: BLE001 - any post-write read failure is an ambiguous commit.
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        event["backend"]["revision"] = _revision(task)
        self.audit.stage(request_id, event)
        try:
            event_id = self.audit.append(request_id, event)
        except OSError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        return {"action": kind, "task": task, "event_id": event_id, "replayed": False}

    def reconcile(self, *, defer_restore_comments: bool = False) -> tuple[int, int]:
        repaired = 0
        unresolved = 0
        for event in self.audit.pending_events():
            try:
                if defer_restore_comments and event.get("kind") == "restored_comment":
                    continue
                if event.get("record_type") == TaskAudit._PROTOCOL_EVENT_RECORD_TYPE:
                    subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
                    if str(event.get("kind") or "") in _MARKER_EVENT_ACTIONS:
                        self.board_host.recover_marker_comment(str(event["request_id"]))
                        repaired += 1
                        continue
                    if subject.get("kind") == "sprint":
                        self.board_host.recover_sprint(str(event["request_id"]))
                        repaired += 1
                        continue
                    self._finish_pending_transition(event)
                    repaired += 1
                    continue
                if str(event.get("backend", {}).get("kind") or "") == "dispatcher":
                    # An observer lifecycle event describes a head, not a backend row: there is
                    # nothing to re-read and enrich, and it must repair even when the sprint it
                    # names has already left the board.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") in {
                    "sprint_guard_denied",
                    "sprint_guard_override",
                    "outcome_round_context",
                }:
                    # A guard decision records itself, not a backend row: there is nothing to
                    # re-read, and the decision it names was made whether or not the operation
                    # it authorized went on to succeed.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") == "reference_repaired":
                    from secretary.board.reference_repair import finish_pending_reference_repair

                    finish_pending_reference_repair(self, event)
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") in {"product_created", "issue_created", "issue_closed"}:
                    # Product/Issue writes have ordered backend cleanup.  Only their supported
                    # command, retried with the original request id, can prove that cleanup.
                    unresolved += 1
                    continue
                if event.get("kind") == "restored_comment":
                    from secretary.task_restore import finish_pending_restore_comment

                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    finish_pending_restore_comment(self, event, payload)
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if str(event.get("ref") or "").startswith("sprint:"):
                    from secretary.sprints import SprintWriter

                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if event.get("kind") == "budget_recorded" and payload.get("hard_limit_stop") is True:
                        SprintWriter(self.client, data_dir=self.data_dir).record_budget(
                            role=str(event.get("actor", {}).get("role") or ""),
                            actor=str(event.get("actor", {}).get("id") or ""),
                            reference=str(event["ref"]),
                            event_type=str(payload.get("event_type") or ""),
                            request_id=str(event["request_id"]),
                            source_event_id=str(payload.get("source_event_id") or ""),
                        )
                        repaired += 1
                        continue
                    SprintWriter(self.client, data_dir=self.data_dir)._pending(
                        str(event.get("kind") or "updated"), event
                    )
                    repaired += 1
                    continue
                if event.get("kind") == "retired":
                    self._finish_pending_retired(event)
                    self._prove_retired_closed(event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                self._finish_pending_cleanup(event, None)
                task = (
                    self._pending_create_task(event)
                    if event.get("kind") == "created"
                    else self.reader.show(str(event["ref"]))
                )
                event["task_id"] = task["id"]
                event["backend"]["revision"] = _revision(task)
                self.audit.stage(str(event["request_id"]), event)
                self.audit.append(str(event["request_id"]), event)
                repaired += 1
            except (TaskError, BoardProtocolError, OSError, KeyError, TypeError, ValueError):
                unresolved += 1
        return repaired, unresolved

    def _finish_pending_transition(self, event: dict[str, Any]) -> None:
        """Finish one typed pending Card transition: prove it, clean up, then commit it.

        A typed pending record is read as the transition it declares rather than guessed from a payload.
        The adapter proves the exact target on the board and never repeats a move; the metadata reset
        runs only once that target is live, so a transition whose effect was lost cannot strip a card's
        claim, and the event is published only after the reset is complete.
        """
        transition = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        target = str(transition.get("target") or "")
        ref = str(event["ref"])
        card = self.reader.show(ref)
        if card["state"] == target:
            self._reset_transition_metadata(
                card,
                source=str(transition.get("source") or ""),
                target=target,
            )
            if target in {"ready", "done"}:
                normalized = self.reader.show(ref)
                if (
                    normalized["claim"]["worker"] is not None
                    or normalized["routing"]["resolved_worker_head"] is not None
                    or normalized["routing"]["resolved_review_head"] is not None
                    or normalized["retry"] != {"same": 0, "switched": 0, "heads": []}
                ):
                    raise TaskError("backend_error", "pending Ready cleanup remains incomplete", 1)
        self.board_host.recover_transition(str(event["request_id"]))

    def _finish_pending_cleanup(
        self,
        event: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Complete idempotent backend cleanup before recording a pending event."""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event.get("kind") == "created":
            self._finish_pending_create(event, payload)
            return
        if event.get("kind") == "claimed":
            self._finish_pending_claim(event, payload)
            return
        if event.get("kind") == "restored":
            self._finish_pending_restore(event, payload)
            return
        if event.get("kind") == "archived":
            self._finish_pending_archive(event, retry_payload)
            return
        if event.get("kind") == "retired":
            self._finish_pending_retired(event)
            return
        if event.get("kind") == "decided":
            self._finish_pending_decided(event, payload, retry_payload)
            return
        if event.get("kind") == "restored_comment":
            from secretary.task_restore import finish_pending_restore_comment

            finish_pending_restore_comment(self, event, payload)
            return
        if event.get("kind") != "moved" or payload.get("to") != "ready":
            return
        task = self.reader.show(str(event["ref"]))
        if task["state"] != "ready":
            raise TaskError("backend_error", "pending move no longer matches task state", 1)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_READY_RESET_METADATA,
        )
        normalized = self.reader.show(str(event["ref"]))
        if (
            normalized["claim"]["worker"] is not None
            or normalized["routing"]["resolved_worker_head"] is not None
            or normalized["routing"]["resolved_review_head"] is not None
            or normalized["retry"] != {"same": 0, "switched": 0, "heads": []}
        ):
            raise TaskError("backend_error", "pending Ready cleanup remains incomplete", 1)

    def _retention_card(
        self, reference: str, *, task_id: int | None
    ) -> tuple[int, dict[str, Any], dict[str, str], int] | None:
        """Read an exact retention target, including archived rows when recovering."""
        board_id, columns, _swimlanes = self.reader._board()
        done_id = next((identifier for identifier, title in columns.items() if title == "Done"), None)
        if done_id is None:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        rows = all_project_cards(self.client, board_id)
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and _text(row.get("reference")) == reference
            and (task_id is None or _positive_int(row.get("id")) == task_id)
        ]
        if not matches:
            return None
        if task_id is None:
            active = [row for row in matches if _task_is_active(row)]
            if not active:
                return None
            matches = active
        if len(matches) != 1:
            raise TaskError("backend_error", "Done retention target is ambiguous", 1)
        raw = matches[0]
        number = _task_number(raw)
        return number, raw, _task_metadata(self.client.call("getTaskMetadata", task_id=number)), done_id

    @staticmethod
    def _check_retention_record(metadata: dict[str, str]) -> None:
        if metadata.get("record_type") in _TYPED_RECORD_TYPES:
            raise TaskError(
                "transition_forbidden", "Product issues and products cannot be retired as Done tasks", 3
            )

    def _retention_matches(
        self,
        raw: dict[str, Any],
        metadata: dict[str, str],
        expected_date_moved: int,
        cutoff: float,
        done_id: int,
    ) -> bool:
        return (
            _task_is_active(raw)
            and _positive_int(raw.get("column_id")) == done_id
            and _positive_int(raw.get("date_moved")) == expected_date_moved
            and expected_date_moved < cutoff
            and metadata.get("record_type") not in _TYPED_RECORD_TYPES
        )

    def _finish_pending_retired(self, event: dict[str, Any]) -> None:
        """Prove a retained close or repeat it only for its original Done episode."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        expected = _positive_int(payload.get("expected_date_moved"))
        task_id = _positive_int(payload.get("task_id"))
        try:
            cutoff = float(payload.get("cutoff"))
        except (TypeError, ValueError):
            cutoff = float("nan")
        ref = _text(event.get("ref"))
        if not ref or expected is None or task_id is None or cutoff != cutoff:
            raise TaskError("backend_error", "pending Done retention is incomplete", 1)
        target = self._retention_card(ref, task_id=task_id)
        if target is None:
            raise TaskError("backend_error", "pending Done retention target disappeared", 1)
        _number, raw, metadata, done_id = target
        self._check_retention_record(metadata)
        if _task_is_active(raw):
            if not self._retention_matches(raw, metadata, expected, cutoff, done_id):
                raise TaskError("backend_error", "pending Done retention no longer matches its episode", 1)
            if not self.client.call("closeTask", task_id=task_id):
                raise TaskError("backend_error", "pending Done retention remains incomplete", 1)
            target = self._retention_card(ref, task_id=task_id)
            if target is None:
                raise TaskError("backend_error", "pending Done retention target disappeared", 1)
            _number, raw, _metadata, _done_id = target
        if _task_is_active(raw):
            raise TaskError("backend_error", "pending Done retention remains incomplete", 1)

    def _prove_retired_closed(self, event: dict[str, Any]) -> None:
        """Last audit gate: a success event never names a live replacement episode."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = _positive_int(payload.get("task_id"))
        ref = _text(event.get("ref"))
        if task_id is None or not ref:
            raise TaskError("backend_error", "pending Done retention is incomplete", 1)
        target = self._retention_card(ref, task_id=task_id)
        if target is None or _task_is_active(target[1]):
            raise TaskError("backend_error", "pending Done retention is no longer closed", 1)

    def _finish_pending_claim(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        """Complete a claim whose metadata committed before the column move failed."""
        ref = str(event["ref"])
        worker = str(payload.get("worker") or "")
        if not worker:
            raise TaskError("backend_error", "pending claim is missing its worker id", 1)
        task = self.reader.show(ref)
        # A pending claim on a typed record can only come from before the claim guard existed (or
        # from a card typed after the claim). Finishing it would move a Product or an Issue into
        # In progress, so cleanup fails closed here as well and leaves the event for a PO.
        _check_execution_record(task)
        if task["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim no longer matches task claim", 1)
        if not _matches_optional(payload.get("resolved_head"), task["routing"]["resolved_worker_head"]):
            raise TaskError("backend_error", "pending claim worker head remains incomplete", 1)
        if not _matches_optional(
            payload.get("resolved_review_head"), task["routing"]["resolved_review_head"]
        ):
            raise TaskError("backend_error", "pending claim review head remains incomplete", 1)
        if task["state"] == "ready":
            self._move_raw(task, "in_progress", swimlane_id=self._current_swimlane_id(task))
        elif task["state"] != "in_progress":
            raise TaskError("backend_error", "pending claim no longer matches task state", 1)
        normalized = self.reader.show(ref)
        if normalized["state"] != "in_progress" or normalized["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim cleanup remains incomplete", 1)

    def _finish_pending_decided(
        self,
        event: dict[str, Any],
        payload: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Commit a decision only after its canonical marker and exact body exist on the card."""
        ref = str(event.get("ref") or "")
        marker = str(payload.get("marker") or "")
        expected = str(payload.get("body_sha256") or "")

        def matches(comment: dict[str, Any]) -> bool:
            if comment.get("marker") != marker:
                return False
            rendered = str(comment.get("body") or "")
            prefix = f"[{marker}]\n"
            body = rendered.removeprefix(prefix)
            return _digest(body) == expected

        task = self.reader.show(ref)
        matching = [comment for comment in task.get("comments", []) if matches(comment)]
        if matching:
            return
        body = str((retry_payload or {}).get("decision_body") or "")
        if not body or _digest(body) != expected:
            raise TaskError(
                "audit_pending",
                "pending decision has no verified board comment; retry its original request and body",
                4,
            )
        if task.get("state") != "assessment":
            raise TaskError("backend_error", "pending decision no longer matches Assessment", 1)
        self.client.call(
            "createComment",
            task_id=_task_number(task),
            user_id=0,
            content=f"[{marker}]\n{body}",
        )
        verified = self.reader.show(ref)
        if not any(matches(comment) for comment in verified.get("comments", [])):
            raise TaskError("backend_error", "pending decision comment could not be verified", 1)

    def _finish_pending_archive(
        self,
        event: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Complete an archive whose comment or close committed before the reply was lost."""
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending archive is missing its task ref", 1)
        event_payload = event.get("payload")
        expected_digest = _text(event_payload.get("reason_sha256")) if isinstance(event_payload, dict) else ""
        retry_reason = _text((retry_payload or {}).get("reason"))
        safe_retry_reason = self._redact_for_board(retry_reason)
        if retry_reason and safe_retry_reason != retry_reason:
            # The old pending record predates the board-boundary redactor.  Its
            # digest names the raw text, so silently replacing it would make
            # reconciliation believe a different archive reason was committed.
            # Leave it pending for an operator rather than publish a secret or
            # falsify the append-only history.
            raise TaskError(
                "audit_pending",
                "pending archive reason contains credential material; reissue the archive safely",
                4,
            )
        if retry_reason and _digest(retry_reason) != expected_digest:
            raise TaskError("validation", "archive retry reason does not match the pending request", 2)
        board_id, _, _ = self.reader._board()
        raw = project_card_by_reference(self.client, board_id, ref)
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        if _task_is_active(raw):
            task = self.reader.show(ref)
            self._check_archivable(task)
            self._check_dispatcher_archivable(ref)
            if not _has_archive_reason(task, expected_digest):
                if not retry_reason:
                    raise TaskError("backend_error", "pending archive reason comment is missing", 1)
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[archive]\n{retry_reason}",
                )
                task = self.reader.show(ref)
                if not _has_archive_reason(task, expected_digest):
                    raise TaskError("backend_error", "pending archive reason comment remains incomplete", 1)
            if not self.client.call("closeTask", task_id=_task_number(task)):
                raise TaskError("backend_error", "pending archive remains incomplete", 1)
        elif expected_digest:
            task_id = _positive_int(raw.get("id"))
            if task_id is None:
                raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
            raw_comments = self.client.call("getAllComments", task_id=task_id) or []
            comments = [_normalize_comment(comment) for comment in raw_comments if isinstance(comment, dict)]
            if not _has_archive_reason({"comments": comments}, expected_digest):
                raise TaskError("backend_error", "pending archive reason comment is missing", 1)
        raw = project_card_by_reference(self.client, board_id, ref)
        if isinstance(raw, dict) and _task_is_active(raw):
            raise TaskError("backend_error", "pending archive remains incomplete", 1)

    def _finish_pending_restore(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        from secretary.task_restore import finish_pending_restore

        finish_pending_restore(self, event, payload)

    def _finish_pending_create(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending create is missing its task ref", 1)
        task = self._pending_create_task(event)
        if task["ref"] != ref:
            backend = event.get("backend")
            if isinstance(backend, dict) and backend.get("reference_assignment") == "atomic":
                raise TaskError("backend_error", "pending atomic create reference remains incomplete", 1)
            # Repair a pre-atomic create only if no other row acquired its reference.
            if task["ref"]:
                raise TaskError("backend_error", "pending create task reference does not match", 1)
            board_id, _, _ = self.reader._board()
            current = project_card_by_reference(self.client, board_id, ref)
            if current is not None:
                raise TaskError("backend_error", "pending create reference belongs to another task", 1)
            if not self.client.call("updateTask", id=_task_number(task), reference=ref):
                raise TaskError("backend_error", "pending create reference remains incomplete", 1)
            task = self._pending_create_task(event)
            if task["ref"] != ref:
                raise TaskError("backend_error", "pending create reference remains incomplete", 1)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_create_metadata_values(payload),
        )
        normalized = self._pending_create_task(event)
        expected_mode = _text(payload.get("codex_launch_mode"))
        if expected_mode not in _CODEX_LAUNCH_MODES:
            expected_mode = ""
        if expected_mode and normalized["routing"]["codex_launch_mode"] != expected_mode:
            raise TaskError("backend_error", "pending create metadata remains incomplete", 1)
        if payload.get("steward_report") is True and not (
            normalized["state"] == "in_progress"
            and normalized.get("record_type") == "task"
            and normalized["type"] == "research"
            and normalized["claim"]["worker"] == _text(payload.get("slug"))
            and _is_steward_report(normalized)
        ):
            raise TaskError("backend_error", "pending steward report metadata remains incomplete", 1)

    def _pending_create_task(self, event: dict[str, Any]) -> dict[str, Any]:
        backend = event.get("backend")
        task_id = _positive_int(backend.get("task_id")) if isinstance(backend, dict) else None
        if task_id is not None:
            return self.reader.show_id(task_id)
        raise TaskError(
            "backend_error",
            "pending create is missing its backend task id; reconcile it manually",
            1,
        )

    @staticmethod
    def _role(role: str, allowed: set[str]) -> None:
        if role not in allowed:
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)

    @staticmethod
    def _check_archivable(task: dict[str, Any]) -> None:
        state = task["state"]
        if state in ACTIVE_STATES:
            raise TaskError(
                "live_work",
                "archive refuses a card with live worker or reviewer work, or one parked in "
                "Assessment holding a retained worker",
                3,
            )
        if task["claim"]["worker"] is not None:
            raise TaskError("live_work", "archive refuses a card with an active claim", 3)

    def _check_dispatcher_archivable(self, reference: str) -> None:
        state_path = self.data_dir / "dispatcher" / "production-state.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3) from None
        if not isinstance(payload, dict):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
        records = payload.get("records") or {}
        if not isinstance(records, dict):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
        record = records.get(reference)
        if not isinstance(record, dict):
            return
        if _dispatcher_record_has_live_work(record):
            raise TaskError("live_work", "archive refuses a card with live dispatcher work", 3)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _target_column_id(columns: dict[int, str], target: str) -> int | None:
    return next(
        (identifier for identifier, name in columns.items() if _STATE_BY_COLUMN.get(name) == target), None
    )


def _has_archive_reason(task: dict[str, Any], reason_sha256: str) -> bool:
    if not reason_sha256:
        return False
    comments = task.get("comments") or []
    if not isinstance(comments, list):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if comment.get("marker") != "archive":
            continue
        body = _text(comment.get("body"))
        reason = body.split("\n", 1)[1] if body.startswith("[archive]\n") else ""
        if _digest(reason) == reason_sha256:
            return True
    return False


def _dispatcher_record_has_live_work(record: dict[str, Any]) -> bool:
    return any(_text(record.get(key)) for key in ("workspace", "handle", "review_handle", "review_leaf"))


def _task_number(task: dict[str, Any]) -> int:
    value = _positive_int(str(task.get("id", "")).removeprefix("task_kanboard_"))
    if value is None:
        raise TaskError("backend_error", "Kanboard returned an invalid task", 1)
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _revision(task: dict[str, Any]) -> str:
    return "updated_at:" + str(task.get("audit", {}).get("updated_at") or "unknown")


def _null_if_empty(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _split_heads(value: Any) -> list[str]:
    return [head for head in _text(value).split(",") if head]


def _enum_or_default(value: Any, allowed: set[str], default: str) -> str:
    candidate = _text(value)
    return candidate if candidate in allowed else default


def _enum_or_none(value: Any, allowed: set[str]) -> str | None:
    candidate = _text(value)
    return candidate if candidate in allowed else None


def _matching_swimlane(swimlanes: dict[int, str], project: str) -> int | None:
    wanted = re.sub(r"[^a-z0-9]+", "", project.lower())
    for identifier, name in swimlanes.items():
        candidate = re.sub(r"[^a-z0-9]+", "", name.lower())
        if candidate == wanted:
            return identifier
    return None


def _is_steward_report(task: dict[str, Any]) -> bool:
    return task.get("extensions", {}).get("kanboard", {}).get("steward_report") == "1"


def _matches_optional(expected: Any, actual: Any) -> bool:
    expected_text = _text(expected)
    return not expected_text or actual == expected_text


def _task_is_active(task: dict[str, Any]) -> bool:
    active = task.get("is_active", task.get("status", 1))
    try:
        return int(active) != 0
    except (TypeError, ValueError):
        return True


def _create_metadata_values(payload: dict[str, Any]) -> dict[str, str]:
    values = {
        "task_type": _text(payload.get("task_type")),
        "project": _text(payload.get("project")),
        "complexity": _text(payload.get("complexity")) or "standard",
        "family_preference": _text(payload.get("family_preference")) or "auto",
    }
    for payload_key, metadata_key in (
        ("blocked_by", "blocked_by"),
        ("head", "head"),
        ("review_head", "review_head"),
        ("slug", "slug"),
        ("base_branch", "base_branch"),
        ("seed_ref", "seed_ref"),
        ("supersedes", "supersedes"),
        ("codex_launch_mode", "codex_launch_mode"),
        ("sprint", "sprint_ref"),
    ):
        value = _text(payload.get(payload_key))
        if metadata_key == "codex_launch_mode" and value not in _CODEX_LAUNCH_MODES:
            # Drop retired launch modes when repairing legacy creates.
            continue
        if value:
            values[metadata_key] = value
    if payload.get("steward_report") is True:
        slug = _text(payload.get("slug"))
        # A recovered report must prove the whole accounting identity, not merely
        # that its row exists.  Keep these in the one metadata write so a retry
        # repairs a partial backend write as one unit.
        values.update({"record_type": "task", "claim": slug, "steward_report": "1"})
    return values


def _rfc3339(value: Any) -> str | None:
    seconds = _positive_int(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    text = _text(comment.get("comment"))
    first_line = text.splitlines()[0] if text else ""
    marker = first_line[1:-1] if first_line.startswith("[") and first_line.endswith("]") else None
    return {"created_at": _rfc3339(comment.get("date_creation")), "body": text, "marker": marker}
