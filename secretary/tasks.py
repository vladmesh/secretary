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
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from secretary.board.card_transitions import CardTransitionForbidden, card_transition
from secretary.board.events import BoardEventPending
from secretary.board.host import MarkerComment, MutationResult, TransitionRequest
from secretary.board.models import Actor, CardState, EntityKind, Event, EventKind, RelatedRefs
from secretary.board.transitions import BoardProtocolError
from secretary.board_transport import (
    BoardTransport, BoardTransportError, resolve, transport_path,
)
from secretary.role_env import RUNTIME_ENV_FILE_ENVS, runtime_env_path
from triggered_agents.runtime.head import CODEX_LAUNCH_MODES
from triggered_agents.runtime.paths import instance_dir as normalize_instance_dir
from triggered_agents.runtime.redact import redact


class TaskError(Exception):
    """A task command failed without exposing backend credentials."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


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
    "task_type", "project", "blocked_by", "claim", "slug", "base_branch",
    "head", "resolved_head", "review_head", "resolved_review_head", "retry_same",
    "retry_switch", "retry_heads", "complexity", "family_preference", "routing_reason",
    "quota_snapshot_at", "codex_launch_mode",
    "sprint_ref",
}
_TASK_TYPES = {"code", "research"}
_COMPLEXITIES = {"cheap", "standard", "hard", "frontier"}
_FAMILY_PREFERENCES = {"auto", "claude", "codex"}
# The launch modes a card may carry. It is the registry's own set, so a card can never ask for a
# mode no head can be launched in. Cards written before Codex became TUI-only still hold the
# retired `exec`; it is outside this set, so `_normalize` reads such a card as carrying no mode at
# all rather than as a card that asked for a launch shape the product no longer has.
_CODEX_LAUNCH_MODES = CODEX_LAUNCH_MODES
_ROLES = {"po", "dispatcher", "worker", "reviewer", "steward", "retro", "observer"}
_COMMENT_ROLES = _ROLES
_CREATE_ROLES = {"po", "steward", "worker", "reviewer", "retro", "observer"}
# Agent roles that may not open an execution card: their only create is a proposal in the
# board's first column, which a PO later triages into Ready.
_PROPOSAL_CREATE_ROLES = {"worker", "reviewer", "retro"}
_EDIT_ROLES = {"po", "dispatcher", "observer"}
_EDITABLE_STATES = {"ready", "blocked"}
# `assessment` is a durable wait: a card parked there has no running head and no mechanical
# gate left to run, and it leaves only when the sprint's observer decides release, rework or
# reslice. A substantive reviewer verdict parks the card here; the dispatcher then performs the
# recorded decision, so the effect of a verdict is never the verdict's own tick.
_STATES = ("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done")
_READY_RESET_METADATA = {
    "claim": "",
    "resolved_head": "",
    "resolved_review_head": "",
    "retry_same": "",
    "retry_switch": "",
    "retry_heads": "",
}
_ROUTING_PHASES = {"worker", "review", "verdict"}
# What kind of blocker a worker ran into, in the worker's own view. Two values and no more:
# an external fact is repaired outside the card, a wrong task definition is repaired by
# rewriting or reslicing the card, and the observer's next move differs between the two. The
# worker's view is not the verdict, it is the cheapest evidence the observer has to start from.
_BLOCK_CLASSIFICATIONS = ("external_fact", "wrong_task_definition")
# What the observer may decide about a parked card, and where each decision sends it. The
# decision is recorded on the card before anything acts on it: the effect belongs to the
# dispatcher and can fail, and a failed effect blocks the card rather than half releasing it.
# `blocked` takes no decision requirement: it is the escape hatch the steward's stale
# escalation and every dispatcher failure path already use, and refusing it would strand cards.
_DECISION_TARGETS = {"release": "done", "rework": "in_progress", "reslice": "blocked"}
_DECISIONS = set(_DECISION_TARGETS)
_DECIDED_TARGETS = {"done", "in_progress"}
# The other three ways out of Assessment. Each of them leaves the column with nothing decided,
# and Ready additionally clears the claim and lets a second worker start on the reviewed
# checkout, so the dispatcher does not take them. The PO still can: it is the human operator,
# and on a reserved project that move is a recorded sprint override. The observer needs no entry
# here: it leaves Assessment by no exit at all.
_UNDECIDED_EXITS = {"ready", "validate", "issues"}
# Who the decision rules bind: the dispatcher, and only it. It is what performs a decision, so a
# move it makes out of Assessment either carries one or is not its move to make. The PO's move is
# the escape hatch out of a seam that is stuck, and a hatch that needs the thing it is escaping is
# not one. A decision that is passed is still checked against the card and its destination for
# every role.
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
        if (
            event.get("kind") == "moved" and str(payload.get("to") or "") == "assessment"
        ) or (
            event.get("record_type") == TaskAudit._PROTOCOL_EVENT_RECORD_TYPE
            and str(lifecycle.get("target") or "") == "assessment"
        ):
            latest_park = index
    if latest_park < 0:
        return "", None
    visit = str(ordered[latest_park].get("event_id") or ordered[latest_park].get("request_id") or "")
    for event in ordered[latest_park + 1:]:
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
    # Assessment needs an observer decision; Blocked needs classification; Done is the semantic
    # post-release edge where the observer can choose the next cut or close the sprint.
    if target in {"assessment", "blocked", "done"}:
        return True
    # A human control-plane move which removes a live or parked card back to Issues also changes
    # the observer's next-cut decision.  Do not turn every Issues transition into a wake: routine
    # dispatcher/routing moves are deliberately excluded, and the old state must have held work.
    # `steward` has no such transition today, but keeping both human control-plane roles here
    # makes a future permitted equivalent retain the same semantic contract.
    return (
        str(actor.get("role") or "") in {"po", "steward"}
        and source in ACTIVE_STATES
        and target == "issues"
    )


def is_significant_observer_event(
    event: dict[str, Any], *, linked_refs: set[str], sprint_ref: str,
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


class KanboardClient:
    """Small JSON-RPC client using local board transport configuration."""

    def __init__(self, transport: BoardTransport, instance_dir: Path) -> None:
        self.instance_dir = normalize_instance_dir(instance_dir).resolve()
        self.url = transport.url
        self._transport = transport

    @classmethod
    def for_instance(cls, instance: str | Path) -> "KanboardClient":
        try:
            root = normalize_instance_dir(instance).resolve()
            return cls(resolve(root), root)
        except BoardTransportError:
            raise TaskError("backend_unavailable", "Kanboard runtime configuration is unavailable", 1) from None

    def call(self, method: str, **params: Any) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            payload["params"] = params
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
                document = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1) from None
        if not isinstance(document, dict) or "error" in document:
            raise TaskError("backend_error", "Kanboard rejected the read request", 1)
        return document.get("result")


def all_project_cards(client: KanboardClient, project_id: int) -> list[dict[str, Any]]:
    """Return every Kanboard card by combining its open and closed status sets.

    Kanboard 1.2.52 uses status 1 for open cards and 0 for closed, and has no complete-set status,
    so the first copy of each task id is retained in case a backend returns a row in both responses.
    """
    cards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for status_id in (1, 0):
        response = client.call("getAllTasks", project_id=project_id, status_id=status_id)
        if not isinstance(response, list):
            raise TaskError("backend_error", "Kanboard returned an invalid task list", 1)
        for card in response:
            if not isinstance(card, dict):
                continue
            identifier = card.get("id")
            if identifier is not None:
                key = str(identifier)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            cards.append(card)
    return cards


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


def project_card_by_id(
    client: KanboardClient, project_id: int, task_id: int
) -> dict[str, Any] | None:
    """Return the exact board row named by a recorded Kanboard task id."""
    for card in all_project_cards(client, project_id):
        if _positive_int(card.get("id")) == task_id:
            return card
    return None


def next_project_reference(client: KanboardClient, project_id: int, project: str) -> str:
    """Allocate the reference immediately after this project's board-wide high-water mark."""
    pattern = re.compile(rf"^{re.escape(project)}-(\d+)$")
    highest = 0
    for card in all_project_cards(client, project_id):
        match = pattern.fullmatch(str(card.get("reference") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{project}-{highest + 1}"


@contextlib.contextmanager
def project_reference_allocation_lock(data_dir: Path) -> Iterator[None]:
    """Serialize allocation and assignment across task-create processes."""
    board_dir = data_dir / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    with (board_dir / ".create.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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
        result = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            normalized = self._normalize(card, columns, swimlanes, comments=None)
            if states and normalized["state"] not in states:
                continue
            if project is not None and normalized["project"] != project:
                continue
            if sprint is not None and normalized["sprint"] != sprint:
                continue
            result.append(normalized)
        return sorted(result, key=lambda task: (task["state"], task["position"], task["ref"], task["id"]))

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
        comments = [
            _normalize_comment(comment)
            for comment in raw_comments
            if isinstance(comment, dict)
        ]
        return self._normalize(card, columns, swimlanes, comments=comments)

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
        *,
        comments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        task_id = _positive_int(card.get("id"))
        column = columns.get(_positive_int(card.get("column_id")) or -1)
        if task_id is None or column not in _STATE_BY_COLUMN:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        metadata = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(metadata, dict):
            raise TaskError("backend_error", "Kanboard returned invalid task metadata", 1)
        meta = {str(key): _text(value) for key, value in metadata.items()}
        ref = _text(card.get("reference"))
        result: dict[str, Any] = {
            "id": f"task_kanboard_{task_id}", "ref": ref, "title": _text(card.get("title")),
            "description": _text(card.get("description")), "state": _STATE_BY_COLUMN[column],
            "closed": _nonnegative_int(card.get("is_active", card.get("status", 1))) == 0,
            "position": _nonnegative_int(card.get("position")), "project": _text(meta.get("project")),
            "type": _text(meta.get("task_type")), "blocked_by": _null_if_empty(meta.get("blocked_by")),
            "claim": {"worker": _null_if_empty(meta.get("claim")), "claimed_at": None},
            "routing": {
                "complexity": _enum_or_default(
                    meta.get("complexity"), _COMPLEXITIES, "standard"
                ),
                "family_preference": _enum_or_default(
                    meta.get("family_preference"), _FAMILY_PREFERENCES, "auto"
                ),
                "head_override": _null_if_empty(meta.get("head")), "review_head_override": _null_if_empty(meta.get("review_head")),
                "resolved_worker_family": None, "resolved_worker_head": _null_if_empty(meta.get("resolved_head")),
                "resolved_review_family": None, "resolved_review_head": _null_if_empty(meta.get("resolved_review_head")),
                "routing_reason": _null_if_empty(meta.get("routing_reason")), "quota_snapshot_at": _null_if_empty(meta.get("quota_snapshot_at")),
                "codex_launch_mode": _enum_or_none(meta.get("codex_launch_mode"), _CODEX_LAUNCH_MODES),
            },
            "workspace": {"slug": _null_if_empty(meta.get("slug")), "base_branch": _null_if_empty(meta.get("base_branch"))},
            "retry": {"same": _nonnegative_int(meta.get("retry_same")), "switched": _nonnegative_int(meta.get("retry_switch")), "heads": _split_heads(meta.get("retry_heads"))},
            "sprint": _null_if_empty(meta.get("sprint_ref")),
            "record_type": _null_if_empty(meta.get("record_type")),
            "audit": {"created_at": _rfc3339(card.get("date_creation")), "updated_at": _rfc3339(card.get("date_modification")), "backend": {"kind": "kanboard", "kanboard_task_id": task_id, "board": self.board_name}},
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

    # Generic journal records intentionally have no discriminator.  This explicit value is
    # the only distinction the released audit substrate needs to make: a protocol pending
    # record may describe an effect that has already reached the backend, while a generic
    # record retains its released same-writer restaging behaviour.  It is taken from the
    # typed record itself: recovery now routes on this discriminator, so the two spellings
    # must not be able to drift apart.
    _PROTOCOL_EVENT_RECORD_TYPE = Event.RECORD_TYPE

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.board_dir = os.path.join(os.fspath(data_dir), "board")
        self.events_path = os.path.join(self.board_dir, "events.ndjson")
        self.pending_dir = os.path.join(self.board_dir, "pending-audit")
        self.lock_path = os.path.join(self.board_dir, ".audit.lock")
        # Индекс request_id -> смещение строки в журнале. Дочитывается инкрементально,
        # только новые байты. Без него committed_event разбирал весь events.ndjson на
        # каждой записи, а append/stage зовут его на каждое событие: прогон получался
        # квадратичным по числу событий.
        self._committed_offsets: dict[str, int] = {}
        # event_id -> request_id, заполняется тем же проходом: владельца идентификатора
        # надо уметь спросить под замком, не разбирая журнал заново.
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
        legacy = [name for name in os.listdir(self.pending_dir) if name.endswith(".json") and not name.startswith("v2-")]
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

    def pending_marker_owner(self, reference: str, content: str, *, request_id: str | None = None) -> str | None:
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
        # TaskAudit's released reconciler can repair generic rows, but it cannot attest a
        # protocol backend effect.  That recovery belongs to MutationEventTransaction, which
        # supplies the exact typed event through BoardEventCanon.commit().
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
                request_id, event, operation="stage",
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
                request_id, event, operation="claim",
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
                request_id, event, operation="discard",
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
            if isinstance(event_id, str) and isinstance(request_id, str) and event_id not in self._committed_event_ids:
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
            client, data_dir=os.fspath(data_dir), audit=self.audit,
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
            raise TaskError("backend_unavailable", f"board redaction configuration is unavailable: {exc}", 1) from None
        self._redaction_cache = (key, values)
        return values

    def _redact_for_board(self, text: str) -> str:
        """Remove credentials before they reach either board or audit history.

        An interrupted archive keeps its retry body locally and every board comment is exported into the
        checkpoint, so scrubbing happens at the protocol boundary and both copies receive the same safe
        text. An explicit role-env override selects its external file, so ``SECRETARY_RUNTIME_ENV_FILE``
        and ``TA_RUNTIME_ENV_FILE`` are scrubbed too.
        """
        # Keep TaskWriter importable while config is loading sprints.  The
        # store depends on that same config module and is needed only at a real
        # protocol write, long after startup imports have settled.
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
        # `restoring` is the restore path recreating a card that already existed: sprint
        # admission decides what new work may start, and history is not new work.  Every
        # other guard here still applies to it.
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
        complexity = complexity.strip() or "standard"
        family_preference = family_preference.strip() or "auto"
        codex_launch_mode = codex_launch_mode.strip()
        sprint = sprint.strip()
        priority = priority.strip()
        budget_event = budget_event.strip()
        sprint_override_reason = sprint_override_reason.strip() if restoring else self._redact_for_board(sprint_override_reason.strip())
        if not project:
            raise TaskError("validation", "create requires a non-empty project", 2)
        if task_type not in _TASK_TYPES:
            known = ", ".join(sorted(_TASK_TYPES))
            raise TaskError("validation", f"unknown task type {task_type!r} (known: {known})", 2)
        if not title:
            raise TaskError("validation", "create requires a non-empty title", 2)
        if target not in {"ready", "issues"}:
            raise TaskError("validation", "create target must be ready or issues", 2)
        if role in _PROPOSAL_CREATE_ROLES:
            if target != "issues":
                raise TaskError("role_forbidden", f"{role} may create only proposals in Issues", 3)
        elif target == "issues":
            raise TaskError("transition_forbidden", "execution tasks cannot be created in Issues", 3)
        if complexity not in _COMPLEXITIES:
            raise TaskError("validation", "complexity must be one of: " + ", ".join(sorted(_COMPLEXITIES)), 2)
        if family_preference not in _FAMILY_PREFERENCES:
            raise TaskError("validation", "family preference must be one of: " + ", ".join(sorted(_FAMILY_PREFERENCES)), 2)
        if codex_launch_mode and codex_launch_mode not in _CODEX_LAUNCH_MODES:
            # Refused here, before any board call: a card must not be able to carry a launch mode
            # the product cannot launch. `exec` is the one this rejects in practice, and it is
            # rejected rather than downgraded, so nobody is left believing the card asked for
            # something that then quietly ran as something else.
            known = ", ".join(sorted(_CODEX_LAUNCH_MODES))
            raise TaskError("validation", f"codex launch mode must be {known}", 2)
        if priority:
            raise TaskError("validation", "tasks do not accept product priority", 2)
        if slug and not _SLUG_RE.match(slug):
            raise TaskError("validation", "slug must match [a-z0-9-]{1,30}", 2)
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
            role=role, actor=actor, project=project, card_sprint=sprint,
            linked_sprint=linked_sprint, sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason, request_id=request_id,
            reference=reference,
        )
        # After the ownership guard: a project another sprint holds is refused by that sprint,
        # not by the admission rule.  A proposal in Issues is not yet admitted work, so only a
        # Ready card needs a sprint of its own; an audited PO override is the hotfix path around
        # it, and restore recreates cards that were admitted once already.
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
            "complexity": complexity,
            "family_preference": family_preference,
            "codex_launch_mode": codex_launch_mode or None,
            "sprint": sprint or None,
            "budget_event": budget_event or None,
            **override_payload,
            "title_sha256": _digest(title),
            "description_sha256": _digest(description),
        }
        # A create claims its request id the same way every other write does. Its reference is
        # allocated under the board lock for automatic creates, and only becomes part of the
        # staged event immediately before the atomic backend write.
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind="created", reference=None, identity=payload)
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": "created", "task": self.reader.show(str(committed["ref"])), "event_id": event_id, "replayed": True}
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
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
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
                "kind": "kanboard", "task_id": None, "revision": "pending",
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
                complexity=complexity,
                family_preference=family_preference,
                codex_launch_mode=codex_launch_mode,
                sprint=sprint,
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
        except Exception:
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
        complexity: str,
        family_preference: str,
        codex_launch_mode: str,
        sprint: str,
        event: dict[str, Any],
        request_id: str,
    ) -> str:
        # The board accepts duplicate references, so holding this lock from the high-water
        # read through createTask prevents two local task-create processes assigning one ref.
        with project_reference_allocation_lock(self.data_dir):
            board_id, columns, swimlanes = self.reader._board()
            if reference:
                if project_card_by_reference(self.client, board_id, reference):
                    raise TaskError("validation", "task reference already exists", 2)
                created_ref = reference
            else:
                created_ref = next_project_reference(self.client, board_id, project)
            column_id = _target_column_id(columns, target)
            if column_id is None:
                raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
            swimlane_id = _matching_swimlane(swimlanes, project)
            # Persist the allocation before the atomic backend write. A process that dies
            # after createTask still leaves a recoverable, already-reserved reference.
            event["ref"] = created_ref
            self.audit.stage(request_id, event)
            task_id = _positive_int(self.client.call(
                "createTask",
                project_id=board_id,
                title=title,
                description=description,
                column_id=column_id,
                swimlane_id=swimlane_id or 0,
                reference=created_ref,
            ))
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
                if codex_launch_mode:
                    values["codex_launch_mode"] = codex_launch_mode
                if sprint:
                    values["sprint_ref"] = sprint
                self.client.call("saveTaskMetadata", task_id=task_id, values=values)
            except Exception as exc:
                raise _CommittedWriteError() from exc
            return created_ref

    def comment(self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, _COMMENT_ROLES)
        body = self._redact_for_board(body)
        payload = {"marker": role, "body_sha256": _digest(body)}
        return self._write("commented", role, actor, reference, request_id, payload, lambda task: self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{body}"), identity=payload)

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
        raise TaskError("uncommitted", f"workspace has uncommitted changes: {files}; commit them and retry", 3)

    def report(
        self, *, role: str, actor: str, reference: str, kind: str, body: str,
        classification: str = "", request_id: str | None = None,
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
                "blocked reports require --classification, one of "
                + ", ".join(_BLOCK_CLASSIFICATIONS),
                2,
            )
        if kind == "done":
            if classification:
                raise TaskError("validation", "a done report carries no classification", 2)
        return self._marker_write(
            action="reported", event_kind=EventKind.CARD_REPORTED, role=role, actor=actor,
            reference=reference, reason=body, request_id=request_id,
            data={
                "marker": f"report:{kind}", "status": kind, "body": body,
                "body_sha256": _digest(body), "classification": classification or None,
            },
            fresh_admission=self._require_committed_workspace if kind == "done" else None,
        )

    def verdict(self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"reviewer"})
        body = self._redact_for_board(body)
        if kind not in {"green", "red"} or not body.strip():
            raise TaskError("validation", "verdicts require a non-empty body", 2)
        return self._marker_write(
            action="verdict", event_kind=EventKind.CARD_VERDICTED, role=role, actor=actor,
            reference=reference, reason=body, request_id=request_id,
            data={"marker": f"review:{kind}", "status": kind, "body": body, "body_sha256": _digest(body)},
        )

    def decide(self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None) -> dict[str, Any]:
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
        request_id = request_id or str(uuid.uuid4())
        with assessment_decision_lock(self.data_dir, reference):
            # Resolve immutable request ownership while holding the complete
            # decision lock, before inspecting a mutable Assessment visit or
            # admitting another decision.  A concurrent same-id mismatch must
            # therefore reach the host comparison rather than borrow the
            # current visit's canonical-decision shortcut.
            try:
                owned = self.board_host.canon.event(request_id)
            except ValueError as exc:
                message = str(exc)
                if "released generic audit record" in message:
                    message = "request id belongs to another operation or payload"
                raise TaskError("validation", message, 2) from None
            if owned is not None:
                return self._marker_write(
                    action="decided", event_kind=EventKind.CARD_DECIDED, role=role, actor=actor,
                    reference=reference, reason=body, request_id=request_id,
                    data={
                        "marker": f"decision:{kind}", "decision": kind, "body": body,
                        "body_sha256": _digest(body),
                    },
                )
            current = self.reader.show(reference)
            # Authorization before anything about the card: which sprint holds the project is the
            # question of whether this observer may write here at all.
            self._guard_sprint_write(
                role=role, actor=actor, project=current["project"],
                card_sprint=str(current.get("sprint") or ""), linked_sprint=None,
                sprint_override=False, sprint_override_reason="", request_id=request_id,
                reference=reference,
            )
            if not self._sprint_holds_project(current["project"]):
                raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
            committed_events = self.audit.events(reference)
            pending_decisions = [
                event for event in self.audit.pending_events()
                if str(event.get("ref") or "") == reference and _event_action(event) == "decided"
            ]
            visit, existing = assessment_resolution([*committed_events, *pending_decisions])
            marker_data = {
                "marker": f"decision:{kind}", "decision": kind, "body": body,
                "body_sha256": _digest(body), "assessment_visit": visit or None,
            }
            if current["state"] != "assessment":
                raise TaskError("transition_forbidden", "a decision is only recorded on a card in Assessment", 3)
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
                    raise TaskError("decision_already_recorded", f"Assessment visit {visit} already has a {existing_kind} decision", 3)
                return {"action": "decided", "task": current, "event_id": str(existing.get("event_id") or existing.get("request_id") or ""), "replayed": True}
            return self._marker_write(
                action="decided", event_kind=EventKind.CARD_DECIDED, role=role, actor=actor,
                reference=reference, reason=body, request_id=request_id,
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
            "routing", role, actor, reference, request_id, dict(payload), lambda task: None,
            identity=dict(payload),
        )

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
            # Claims written before Card transitions migrated remain generic audit
            # operations.  They retain their released replay/reconciliation path;
            # in particular, do not reinterpret a legacy pending record as a new
            # typed transition or let it move a record that has since become a
            # Product or Issue.
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
                "claimed", role, actor, reference, request_id, payload, lambda task: None,
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
                if active["type"] == "code" and task["type"] == "code" and active["project"] == task["project"]:
                    raise TaskError("capacity_reached", "one active code task per project is already claimed", 3)
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
        # The claim metadata is written inside the transition, once the column effect is proven.
        # A refused or failed move therefore leaves no claimed-but-Ready card behind, and a
        # metadata write that does not land keeps the pending event instead of a clean journal.
        result = self._transition_card(
            reference=reference, target=CardState.IN_PROGRESS, role=role, actor=actor,
            reason=f"claimed by {worker}", request_id=request_id,
            finish=lambda _card: self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values=values,
            ),
        )
        return {
            "action": "claimed", "task": self.reader.show(reference),
            "event_id": result.event.event_id, "replayed": existing is not None,
        }

    def move(
        self, *, role: str, actor: str, reference: str, target: str, reason: str,
        decision: str = "", sprint_override: bool = False, sprint_override_reason: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._role(role, _ROLES)
        reason = self._redact_for_board(reason)
        sprint_override_reason = self._redact_for_board(sprint_override_reason)
        request_id = request_id or str(uuid.uuid4())
        task = self.reader.show(reference)
        if self._legacy_record(request_id) is not None:
            # A move recorded before Card transitions migrated is a generic audit operation, and
            # it stays one: its retry replays that record and its pending form is finished by the
            # released cleanup, exactly as they were on the version that wrote it.
            return self._legacy_move(
                role=role, actor=actor, reference=reference, target=target, reason=reason,
                decision=decision, sprint_override=sprint_override,
                sprint_override_reason=sprint_override_reason, request_id=request_id, task=task,
            )
        existing = self._typed_event(request_id)
        # The replay is answered before the sprint guard, not after it: this request id already
        # owns a typed event, and a denial would have to be written under the same id. A retry of
        # a transition that already happened cannot be turned into a guard record.
        if existing is not None:
            try:
                target_state = CardState(target)
            except ValueError:
                raise TaskError(
                    "transition_forbidden", _forbidden_move_message(role, task["state"], target), 3,
                ) from None
            # A replay repeats no admission check and no backend move, but it does finish the
            # idempotent board cleanup the first attempt may have lost. The comment is the one
            # follow-up it never repeats: it is not idempotent, and the released reconciliation
            # never recreated one either.
            result = self._transition_card(
                reference=reference, target=target_state, role=role, actor=actor,
                reason=_transition_reason(reason, target), request_id=request_id,
                finish=self._transition_cleanup(
                    task, source=str(existing.source_state or ""), target=target, reason="",
                    role=role,
                ),
            )
            return {
                "action": "moved", "task": self.reader.show(reference),
                "event_id": result.event.event_id, "replayed": True,
            }
        override_payload = self._guard_sprint_write(
            role=role, actor=actor, project=task["project"], card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None, sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(), request_id=request_id, reference=reference,
        )
        source = task["state"]
        _check_execution_record(task)
        if role == "observer" and not override_payload and not self._sprint_holds_project(task["project"]):
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
        try:
            target_state = CardState(target)
            card_transition(role, source, target_state)
        except (ValueError, CardTransitionForbidden):
            raise TaskError("transition_forbidden", _forbidden_move_message(role, source, target), 3) from None
        if role == "steward" and (target == "blocked" or (source, target) == ("blocked", "done")) and not reason.strip():
            raise TaskError("validation", "this steward transition requires a non-empty reason", 2)
        # The observer's disposition of a Blocked card is the other half of the worker's
        # classification: the card says why it stopped, and the move out says what was done
        # about it. Without this a card leaves Blocked with nothing recorded, and a head that
        # blocks without cause repeatedly is invisible.
        if role == "observer" and source == "blocked" and not reason.strip():
            raise TaskError("validation", "moving a card out of Blocked requires a non-empty reason", 2)
        self._check_decision(task, source, target, decision, role)
        result = self._transition_card(
            reference=reference, target=target_state, role=role, actor=actor,
            reason=_transition_reason(reason, target), request_id=request_id,
            finish=self._transition_cleanup(
                task, source=source, target=target, reason=reason, role=role,
            ),
        )
        return {
            "action": "moved", "task": self.reader.show(reference),
            "event_id": result.event.event_id, "replayed": False,
        }

    def _legacy_move(
        self, *, role: str, actor: str, reference: str, target: str, reason: str, decision: str,
        sprint_override: bool, sprint_override_reason: str, request_id: str, task: dict[str, Any],
    ) -> dict[str, Any]:
        """Replay a move this request id already recorded as a released generic operation."""
        override_payload = self._guard_sprint_write(
            role=role, actor=actor, project=task["project"], card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None, sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(), request_id=request_id,
            reference=reference,
        )
        return self._write(
            "moved", role, actor, reference, request_id,
            lambda task: {
                "from": task["state"], "to": target,
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
        self, task: dict[str, Any], *, source: str, target: str, reason: str, role: str,
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
                    "createComment", task_id=_task_number(task), user_id=0,
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
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values={"resolved_review_head": ""})

    def _transition_card(
        self,
        *,
        reference: str,
        target: CardState,
        role: str,
        actor: str,
        reason: str,
        request_id: str,
        finish: Callable[[Any], None] | None = None,
    ) -> MutationResult:
        """Run one state edge through the typed adapter and its shared journal.

        The sprint the card belongs to is not passed here: the adapter reads the live card to authorize
        the edge anyway. `finish` carries this writer's remaining board work into the same transaction.
        """
        try:
            return self.board_host.transition(
                TransitionRequest(
                    EntityKind.CARD, reference, target, Actor(role, actor), reason,
                    RelatedRefs(()), request_id,
                ),
                finish=finish,
            )
        except BoardEventPending:
            raise TaskError(
                "audit_pending", "backend write committed; audit repair is required", 4,
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
        self, task: dict[str, Any], source: str, target: str, decision: str, role: str,
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
            source == "assessment" and target in _DECIDED_TARGETS
            and not decision and role in _DECISION_BOUND_ROLES
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
            role=role, actor=actor, project=current["project"], card_sprint=str(current.get("sprint") or ""),
            linked_sprint=None, sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(), request_id=request_id, reference=reference,
        )
        if role in {"observer", "dispatcher"} and not override_payload and not self._sprint_holds_project(current["project"]):
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
        payload = {
            "title_sha256": _digest(title.strip()) if title is not None else None,
            "title_sha256_was": _digest(current["title"]) if title is not None else None,
            "description_sha256": _digest(description) if description is not None else None,
            "description_sha256_was": _digest(current["description"]) if description is not None else None,
            "head": head.strip() or None if head is not None else None,
            "head_was": current["routing"]["head_override"] if head is not None else None,
            "review_head": review_head.strip() or None if review_head is not None else None,
            "review_head_was": current["routing"]["review_head_override"] if review_head is not None else None,
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

        # The `_was` digests describe the text this edit replaced, which a retry after the write
        # can no longer read off the card. The new spec, the heads and the override reason are
        # what the caller asked for, and they are compared.
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
            role=role, actor=actor, project=project, card_sprint=card_sprint,
            request_id=request_id, reference=reference,
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
                    role=role, actor=actor, project=project, sprint="", request_id=request_id, reference=reference,
                )
                raise AssertionError("unreachable") from exc

        refs = set(active_sprint_projects(self.data_dir).get(project, []))
        if linked_sprint is not None and project in linked_sprint.get("reservations", []):
            refs.add(str(linked_sprint["ref"]))
        held: list[str] = []
        for sprint_ref in sorted(refs):
            try:
                sprint = linked_sprint if linked_sprint and sprint_ref == linked_sprint.get("ref") else SprintReader(self.client).show(sprint_ref, include_cards=False)
            except TaskError as exc:
                self._deny_sprint_write(
                    code="sprint_guard_unavailable",
                    message=f"cannot verify sprint {sprint_ref} reserving project {project}; write it through the sprint entity",
                    role=role, actor=actor, project=project, sprint=sprint_ref, request_id=request_id, reference=reference,
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
                    code="validation", message="sprint override requires a non-empty reason",
                    role=role, actor=actor, project=project, sprint=sprint_ref,
                    request_id=request_id, reference=reference, exit_code=2,
                )
            self._grant_sprint_override(
                role=role, actor=actor, project=project, sprint=sprint_ref,
                reason=sprint_override_reason, request_id=request_id, reference=reference,
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
            role=role, actor=actor, project=project, sprint=sprint_ref, request_id=request_id, reference=reference,
        )
        raise AssertionError("unreachable")

    def _guard_observer_identity(
        self, *, role: str, actor: str, project: str, card_sprint: str,
        request_id: str, reference: str,
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
                role=role, actor=actor, project=project, sprint="", request_id=request_id,
                reference=reference,
            )
        if card_sprint and card_sprint != declared:
            self._deny_sprint_write(
                code="observer_sprint_mismatch",
                message=f"this observer belongs to sprint {declared} and the card is linked to "
                        f"{card_sprint}; write it as that sprint's observer",
                role=role, actor=actor, project=project, sprint=declared,
                request_id=request_id, reference=reference,
            )

    def _grant_sprint_override(
        self, *, role: str, actor: str, project: str, sprint: str, reason: str,
        request_id: str, reference: str,
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
            "event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(),
            "actor": {"role": role, "id": actor}, "kind": "sprint_guard_override",
            "outcome": "granted", "task_id": "", "ref": reference,
            "backend": {"kind": "kanboard", "task_id": None, "revision": "not_written"},
            "request_id": override_request_id,
            "payload": {
                "project": project, "sprint": sprint, "sprint_override_reason": reason,
                "operation_request_id": request_id,
            },
        }
        self.audit.stage(override_request_id, event)
        try:
            self.audit.append(override_request_id, event)
        except OSError:
            raise TaskError(
                "audit_pending", "sprint override was granted but audit repair is required", 4,
            ) from None

    def _deny_sprint_write(
        self, *, code: str, message: str, role: str, actor: str, project: str,
        sprint: str, request_id: str, reference: str, exit_code: int = 3,
    ) -> None:
        denial_request_id = _sprint_guard_denial_request_id(request_id)
        event = self.audit.committed_event(denial_request_id)
        if event is None:
            event = {
                "event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(),
                "actor": {"role": role, "id": actor}, "kind": "sprint_guard_denied", "outcome": "denied",
                "task_id": "", "ref": reference, "backend": {"kind": "kanboard", "task_id": None, "revision": "not_written"},
                "request_id": denial_request_id,
                "payload": {
                    "code": code, "message": message, "project": project, "sprint": sprint,
                    "operation_request_id": request_id,
                },
            }
            self.audit.stage(denial_request_id, event)
            try:
                self.audit.append(denial_request_id, event)
            except OSError:
                raise TaskError("audit_pending", "sprint write was denied but audit repair is required", 4) from None
        payload = event.get("payload") if isinstance(event, dict) else {}
        raise TaskError(str(payload.get("code") or code), str(payload.get("message") or message), exit_code)

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

    def _move_raw(
        self, task: dict[str, Any], target: str, *, position: int = 1, swimlane_id: int
    ) -> None:
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
                raise TaskError("transition_forbidden", "a decision is only recorded on a card in Assessment", 3)
        try:
            result = self.board_host.marker_comment(MarkerComment(
                reference, event_kind, Actor(role, actor), reason, data,
                request_id=request_id, fresh_admission=fresh_admission,
            ))
        except BoardEventPending:
            raise TaskError(
                "audit_pending", "backend write committed; audit repair is required", 4,
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
        # `identity` has no default: what a request id claims is part of declaring an operation,
        # and a write that never says it would replay another caller's payload as its own.
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind=kind, reference=reference, identity=identity)
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": kind, "task": self.reader.show(reference), "event_id": event_id, "replayed": True}
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
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": kind, "task": self.reader.show(reference), "event_id": event_id, "replayed": True}
        task = self.reader.show(reference)
        event_payload = payload(task) if callable(payload) else payload
        event = {"event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(), "actor": {"role": role, "id": actor}, "kind": kind, "outcome": "success", "task_id": task["id"], "ref": reference, "backend": {"kind": "kanboard", "task_id": _task_number(task), "revision": _revision(task)}, "request_id": request_id, "payload": event_payload}
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
        except Exception:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        event["backend"]["revision"] = _revision(task)
        self.audit.stage(request_id, event)
        try:
            event_id = self.audit.append(request_id, event)
        except OSError:
            raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
        return {"action": kind, "task": task, "event_id": event_id, "replayed": False}

    def reconcile(self) -> tuple[int, int]:
        repaired = 0
        unresolved = 0
        for event in self.audit.pending_events():
            try:
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
                if event.get("kind") in {"sprint_guard_denied", "sprint_guard_override"}:
                    # A guard decision records itself, not a backend row: there is nothing to
                    # re-read, and the decision it names was made whether or not the operation
                    # it authorized went on to succeed.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") in {"product_created", "issue_created", "issue_closed"}:
                    # Product/Issue writes have ordered backend cleanup.  Only their supported
                    # command, retried with the original request id, can prove that cleanup.
                    unresolved += 1
                    continue
                if str(event.get("ref") or "").startswith("sprint:"):
                    from secretary.sprints import SprintWriter

                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if event.get("kind") == "budget_recorded" and payload.get("hard_limit_stop") is True:
                        SprintWriter(self.client, data_dir=self.data_dir).record_budget(
                            role=str(event.get("actor", {}).get("role") or ""),
                            actor=str(event.get("actor", {}).get("id") or ""),
                            reference=str(event["ref"]), event_type=str(payload.get("event_type") or ""),
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
                self._finish_pending_cleanup(event, None)
                task = self._pending_create_task(event) if event.get("kind") == "created" else self.reader.show(str(event["ref"]))
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
                card, source=str(transition.get("source") or ""), target=target,
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
        if not _matches_optional(payload.get("resolved_review_head"), task["routing"]["resolved_review_head"]):
            raise TaskError("backend_error", "pending claim review head remains incomplete", 1)
        if task["state"] == "ready":
            self._move_raw(task, "in_progress", swimlane_id=self._current_swimlane_id(task))
        elif task["state"] != "in_progress":
            raise TaskError("backend_error", "pending claim no longer matches task state", 1)
        normalized = self.reader.show(ref)
        if normalized["state"] != "in_progress" or normalized["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim cleanup remains incomplete", 1)

    def _finish_pending_decided(
        self, event: dict[str, Any], payload: dict[str, Any], retry_payload: dict[str, Any] | None,
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
            body = rendered[len(prefix):] if rendered.startswith(prefix) else rendered
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
            "createComment", task_id=_task_number(task), user_id=0, content=f"[{marker}]\n{body}",
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
            comments = [
                _normalize_comment(comment)
                for comment in raw_comments
                if isinstance(comment, dict)
            ]
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
            # A pending event written by the pre-atomic create path can still name a row
            # without its reference. Repair that one recorded row only when no other row
            # acquired the reference while the writer was down.
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
    return next((identifier for identifier, name in columns.items() if _STATE_BY_COLUMN.get(name) == target), None)


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
    return any(
        _text(record.get(key))
        for key in ("workspace", "handle", "review_handle", "review_leaf")
    )


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
        ("codex_launch_mode", "codex_launch_mode"),
        ("sprint", "sprint_ref"),
    ):
        value = _text(payload.get(payload_key))
        if metadata_key == "codex_launch_mode" and value not in _CODEX_LAUNCH_MODES:
            # A create recorded before Codex became TUI-only can still be replayed by the repair
            # path. Its retired launch mode is dropped rather than written back: the reader no
            # longer accepts it, so writing it would leave the repair unable to confirm the
            # metadata it just wrote, and the card is launched interactively either way.
            continue
        if value:
            values[metadata_key] = value
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
