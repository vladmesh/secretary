"""Read-only Phase 5 task protocol backed by the Pipeline Kanboard."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


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
    """Porcelain lines that count against durability.

    Untracked runtime tails are dropped: they belong to the secretary installation,
    not to the worker's project, and a worker cannot commit its way out of them.
    """
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
_CODEX_LAUNCH_MODES = {"exec", "tui"}
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
_TRANSITIONS = {
    # PO is the human operator and may move a card between any two states.
    "po": {(source, target) for source in _STATES for target in _STATES if source != target},
    "dispatcher": {
        ("in_progress", "validate"), ("in_progress", "blocked"),
        ("in_progress", "ready"), ("validate", "in_progress"),
        ("validate", "blocked"), ("validate", "done"),
        ("validate", "assessment"), ("assessment", "in_progress"),
        ("assessment", "done"), ("assessment", "blocked"),
    },
    # The observer moves any card except one that is parked. `release`, `rework` and `reslice`
    # are effects the dispatcher performs, a merge, a rework round, a reslice, and a board move
    # that skipped them would put the card in Done with nothing merged. The observer's authority
    # over a parked card is `task decide`; the PO's override and the steward's Blocked escalation
    # are the two ways a card leaves Assessment without the dispatcher.
    "observer": {
        (source, target)
        for source in _STATES for target in _STATES
        if source != target and source != "assessment"
    },
    "worker": set(), "reviewer": set(), "retro": set(),
    "steward": {
        ("blocked", "ready"), ("blocked", "done"),
        ("in_progress", "done"), ("ready", "blocked"),
        ("in_progress", "blocked"), ("validate", "blocked"),
        ("assessment", "blocked"),
    },
}
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


def _check_execution_record(task: dict[str, Any]) -> None:
    """Reject a Product or an Issue on an execution-task path, before any write."""
    if task.get("record_type") in _TYPED_RECORD_TYPES:
        raise TaskError(
            "transition_forbidden",
            "Product issues and products cannot enter execution task columns",
            3,
        )


def _forbidden_move_message(role: str, source: str, target: str) -> str:
    """Why a role may not make this move, said in the terms of the role that asked.

    The observer out of Assessment is the one case worth its own sentence: the refusal is not
    that the card cannot go there, it is that the observer records the decision and the
    dispatcher performs it, so the answer is `task decide` rather than another move.
    """
    if role == "observer" and source == "assessment":
        return (
            "the observer decides about a parked card and the dispatcher performs the decision: "
            "record it with `task decide` instead of moving the card"
        )
    return f"{role} may not move {source} to {target}"


def standing_decision(events: Iterable[dict[str, Any]]) -> str:
    """The decision a card is holding since it last entered Assessment, or "" for none.

    Scoped to the current stay in the column on purpose: a decision from an earlier round is a
    decision about earlier work, and letting one release a later verdict is exactly the replay
    the seam exists to prevent. Both readers use this, the board writer refusing a decision-less
    move and the dispatcher deciding what to perform, so neither can drift from the other.
    """
    parked_at = -1
    ordered = list(events)
    for index, event in enumerate(ordered):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("kind") == "moved" and str(payload.get("to") or "") == "assessment":
            parked_at = index
    if parked_at < 0:
        return ""
    decision = ""
    for event in ordered[parked_at + 1:]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("kind") == "decided" and str(payload.get("decision") or ""):
            decision = str(payload["decision"])
    return decision


def is_significant_card_event(event: dict[str, Any], *, linked_refs: set[str]) -> bool:
    """Whether a committed card-audit line requires the sprint observer's attention."""
    return (
        str(event.get("ref") or "") in linked_refs
        and str(event.get("kind") or "") not in {"routing", "sprint_guard_denied"}
        and str(event.get("outcome") or "") == "success"
    )


class KanboardClient:
    """Small JSON-RPC client. Credentials are supplied only by runtime env."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self.url = env.get("KANBOARD_URL", "")
        self.user = env.get("KANBOARD_API_USER", "")
        self.token = env.get("KANBOARD_API_TOKEN", "")
        if not (self.url and self.user and self.token):
            raise TaskError("backend_unavailable", "Kanboard runtime configuration is unavailable", 1)

    def call(self, method: str, **params: Any) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            payload["params"] = params
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic " + base64.b64encode(
                    f"{self.user}:{self.token}".encode("utf-8")
                ).decode("ascii"),
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

    Kanboard 1.2.52 uses status 1 for open cards and 0 for closed cards. It does
    not support a complete-set status, so retain the first copy of each task id in
    case a backend returns a row in both responses.
    """
    cards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for status_id in (1, 0):
        response = client.call("getAllTasks", project_id=project_id, status_id=status_id) or []
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
        card = self.client.call("getTaskByReference", project_id=project_id, reference=reference)
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

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.board_dir = os.path.join(os.fspath(data_dir), "board")
        self.events_path = os.path.join(self.board_dir, "events.ndjson")
        self.pending_dir = os.path.join(self.board_dir, "pending-audit")
        self.lock_path = os.path.join(self.board_dir, ".audit.lock")

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

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        os.makedirs(self.pending_dir, exist_ok=True)
        self._require_v2_pending_layout()
        with open(self.lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                committed = self.committed_event(request_id)
                if committed is not None:
                    self._require_same_event(committed, event)
                    return
                if self._product_issue_pending(request_id):
                    raise TaskError("validation", "request id belongs to another operation or payload", 2)
                self._atomic_json(self._pending_path(request_id), event)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def append(self, request_id: str, event: dict[str, Any]) -> str:
        os.makedirs(self.board_dir, exist_ok=True)
        self._require_v2_pending_layout()
        with open(self.lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                committed = self.committed_event(request_id)
                if committed is None:
                    with open(self.events_path, "a", encoding="utf-8") as events:
                        events.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                        events.flush()
                        os.fsync(events.fileno())
                else:
                    self._require_same_event(committed, event)
                pending = self._pending_path(request_id)
                if os.path.exists(pending):
                    os.unlink(pending)
                return str(event["event_id"])
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def discard(self, request_id: str) -> None:
        try:
            self._require_v2_pending_layout()
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
                self.append(request_id, event)
                repaired += 1
            except (OSError, ValueError, KeyError, TypeError):
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
        """Committed events in append order, optionally narrowed to one card and/or kind.

        The card's own routing history lives here once it is Done: board metadata is reset on the
        way back to Ready and cleared on the way out of Validate.
        """
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
                    if kind and event.get("kind") != kind:
                        continue
                    result.append(event)
        except FileNotFoundError:
            return []
        return result

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        try:
            with open(self.events_path, encoding="utf-8") as events:
                for line in events:
                    if line.strip():
                        candidate = json.loads(line)
                        if candidate.get("request_id") == request_id:
                            return candidate
        except FileNotFoundError:
            pass
        return None

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        self._require_v2_pending_layout()
        pending = self._pending_path(request_id)
        try:
            with open(pending, encoding="utf-8") as source:
                return json.load(source)
        except FileNotFoundError:
            return None

    def _has_request(self, request_id: str) -> bool:
        try:
            with open(self.events_path, encoding="utf-8") as events:
                return any(json.loads(line).get("request_id") == request_id for line in events if line.strip())
        except FileNotFoundError:
            return False

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

        `_require_same_event` states the invariant but can only see events that are already
        equal: the replay paths hand the recorded event straight back to `append`. This
        compares the caller's intent instead, before anything is appended or answered.

        The event kind and the card ref always have to match, and so does the `identity` every
        write declares: what the caller asked for, which is the whole payload for most writes.
        The only fields left out are the ones a retry cannot recompute after the write went
        through, because they describe the state the write itself replaced: `moved` records the
        column the card left, `edited` records the digests of the text it overwrote, and
        `restored_comment` drops its body from the payload once the comment is known to be on
        the card. Comparing those would turn an ordinary retry into a conflict.
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
        self.audit = TaskAudit(data_dir)
        self.workspace = Path(workspace) if workspace is not None else None

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
        title = title.strip()
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
        sprint_override_reason = sprint_override_reason.strip()
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
            raise TaskError("validation", "codex launch mode must be exec or tui", 2)
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
        # A create claims its request id the same way every other write does. Its ref is not
        # compared: the backend assigns `PROJECT-N` when the caller passes no reference, so the
        # card this call means is named by `payload["reference"]` and by nothing else yet.
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
                task = self.reader.show(str(pending["ref"]))
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise TaskError("audit_pending", "backend write committed; audit repair is required", 4) from None
            return {"action": "created", "task": self.reader.show(str(pending["ref"])), "event_id": event_id, "replayed": True}

        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": "created",
            "outcome": "success",
            "task_id": "",
            "ref": reference,
            "backend": {"kind": "kanboard", "task_id": None, "revision": "pending"},
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
        board_id, columns, swimlanes = self.reader._board()
        if reference and self.client.call("getTaskByReference", project_id=board_id, reference=reference):
            raise TaskError("validation", "task reference already exists", 2)
        column_id = _target_column_id(columns, target)
        if column_id is None:
            raise TaskError("backend_error", "Kanboard board schema is invalid", 1)
        swimlane_id = _matching_swimlane(swimlanes, project)
        task_id = _positive_int(self.client.call(
            "createTask",
            project_id=board_id,
            title=title,
            description=description,
            column_id=column_id,
            swimlane_id=swimlane_id or 0,
        ))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard rejected the write", 1)
        created_ref = reference or f"{project}-{task_id}"
        event["ref"] = created_ref
        event["task_id"] = f"task_kanboard_{task_id}"
        event["backend"]["task_id"] = task_id
        self.audit.stage(request_id, event)
        try:
            ok = self.client.call("updateTask", id=task_id, reference=created_ref)
            if not ok:
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
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
        payload = {"marker": role, "body_sha256": _digest(body)}
        return self._write("commented", role, actor, reference, request_id, payload, lambda task: self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{body}"), identity=payload)

    def _require_committed_workspace(self) -> None:
        """Refuse a done report from a dirty checkout.

        The worker runs the protocol from its own workspace, so CWD is that checkout.
        Failing here lets the worker commit and retry inside the same session instead of
        learning from the dispatcher that its card went to blocked.
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

        The classification is required rather than offered: an external fact and a wrong task
        definition are repaired by different people in different places, and prose that leaves
        the observer to infer which one it is costs an analysis the worker had already done.
        Two values and no free text, so repeated blocks from one head are countable.

        It is durable in two places, both written by the single write this method already makes:
        the `reported` audit payload, which is the authoritative machine-readable copy, and a
        `classification:` line under the marker in the comment, which is what an observer reads
        on the card. It is deliberately not card metadata: a second backend write can fail on its
        own and leave a field that silently disagrees with the audit.
        """
        self._role(role, {"worker"})
        if kind not in {"done", "blocked"} or (kind == "blocked" and not body.strip()):
            raise TaskError("validation", "blocked reports require a non-empty body", 2)
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
            self._require_committed_workspace()
        marker = f"report:{kind}"
        payload: dict[str, Any] = {"marker": marker, "body_sha256": _digest(body)}
        content = f"[{marker}]\n{body}"
        if classification:
            payload["classification"] = classification
            content = f"[{marker}]\nclassification: {classification}\n\n{body}"
        # A done report carries no `classification` key at all, so the identity names it
        # explicitly as absent: reusing a blocked report's id for a done one is a different
        # operation even before the marker is compared.
        identity = {**payload, "classification": classification or None}
        return self._write("reported", role, actor, reference, request_id, payload, lambda task: self.client.call("createComment", task_id=_task_number(task), user_id=0, content=content), identity=identity)

    def verdict(self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"reviewer"})
        if kind not in {"green", "red"} or (kind == "red" and not body.strip()):
            raise TaskError("validation", "red verdicts require a non-empty body", 2)
        marker = f"review:{kind}"
        payload = {"marker": marker, "body_sha256": _digest(body)}
        return self._write("verdict", role, actor, reference, request_id, payload, lambda task: self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{marker}]\n{body}"), identity=payload)

    def decide(self, *, role: str, actor: str, reference: str, kind: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        """Record what to do with a parked card, apart from the move that does it.

        The decision and its effect are two facts. The effect, a merge or a rework round or a
        reslice, belongs to the dispatcher; recording it first is what makes the decision
        checkable, because the move out of Assessment refuses to carry one that is not on the
        card. An effect that fails takes the card to Blocked with its reason.

        The observer decides, and nobody else. One sprint has one observer and the decision is
        its judgement about a card it has been watching; a PO that has to intervene moves the
        card with `--sprint-override` and a reason, which reads in the audit as the override it
        is rather than as an unmarked decision.

        The same sprint reservation guard `move` carries applies here: a decision is refused on a
        card whose project no open sprint holds. That guard is positional, not an identity. It
        binds the card to a sprint and says nothing about the caller: every observer process runs
        as `--role observer --actor observer`, so nothing here distinguishes one sprint's observer
        from another's, and an observer that reaches this command can decide on any card whose
        project is held. Authenticating a caller to a sprint is deliberately not built here, and
        until it is, no rule elsewhere may treat an observer-authored event as self-authored.
        """
        self._role(role, {"observer"})
        if kind not in _DECISIONS:
            raise TaskError("validation", f"decision must be one of {', '.join(sorted(_DECISIONS))}", 2)
        if not body.strip():
            raise TaskError("validation", "a decision requires a non-empty reason", 2)
        request_id = request_id or str(uuid.uuid4())
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
        if current["state"] != "assessment":
            raise TaskError("transition_forbidden", "a decision is only recorded on a card in Assessment", 3)
        marker = f"decision:{kind}"

        def mutation(task: dict[str, Any]) -> Any:
            if task["state"] != "assessment":
                raise TaskError("transition_forbidden", "a decision is only recorded on a card in Assessment", 3)
            self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{marker}]\n{body}")

        payload = {"marker": marker, "decision": kind, "body_sha256": _digest(body)}
        return self._write(
            "decided", role, actor, reference, request_id, payload, mutation, identity=payload,
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

        Journal-only: the board holds no per-attempt routing history (Ready resets it, leaving
        Validate clears the reviewer head), so this write has no backend mutation. The event still
        goes through the normal pending/commit path, which makes it idempotent per request id and
        carries it into the recovery checkpoint with the rest of `events.ndjson`.
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
        # Same guard as move: a product or an issue is not an execution task, so it never takes a
        # claim even if someone dragged it into Ready by hand. It runs before _write, not only in
        # the mutation, because a retry with a pending or committed claim request id replays
        # through _finish_pending_claim and never reaches the mutation at all.
        _check_execution_record(self.reader.show(reference))

        def mutation(task: dict[str, Any]) -> Any:
            _check_execution_record(task)
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
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values=values)
            try:
                self._move_raw(task, "in_progress", swimlane_id=self._current_swimlane_id(task))
            except Exception as exc:
                raise _CommittedWriteError() from exc

        payload = {
            "worker": worker,
            "resolved_head": resolved_head or None,
            "resolved_review_head": resolved_review_head or None,
            "slug": slug or None,
            "base_branch": base_branch or None,
            "cap": cap,
        }
        return self._write(
            "claimed", role, actor, reference, request_id, payload, mutation, identity=payload,
        )

    def move(
        self, *, role: str, actor: str, reference: str, target: str, reason: str,
        decision: str = "", sprint_override: bool = False, sprint_override_reason: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._role(role, _ROLES)
        request_id = request_id or str(uuid.uuid4())
        task = self.reader.show(reference)
        override_payload = self._guard_sprint_write(
            role=role, actor=actor, project=task["project"], card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None, sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(), request_id=request_id, reference=reference,
        )
        def mutation(task: dict[str, Any]) -> Any:
            source = task["state"]
            if task.get("record_type") in {"issue", "product"}:
                raise TaskError("transition_forbidden", "Product issues and products cannot enter execution task columns", 3)
            if role == "observer" and not override_payload and not self._sprint_holds_project(task["project"]):
                raise TaskError("role_forbidden", "role is not permitted for this operation", 3)
            if (source, target) not in _TRANSITIONS[role]:
                raise TaskError("transition_forbidden", _forbidden_move_message(role, source, target), 3)
            if role == "steward" and (target == "blocked" or (source, target) == ("blocked", "done")) and not reason.strip():
                raise TaskError("validation", "this steward transition requires a non-empty reason", 2)
            # The observer's disposition of a Blocked card is the other half of the worker's
            # classification: the card says why it stopped, and the move out says what was done
            # about it. Without this a card leaves Blocked with nothing recorded, and a head that
            # blocks without cause repeatedly is invisible.
            if role == "observer" and source == "blocked" and not reason.strip():
                raise TaskError("validation", "moving a card out of Blocked requires a non-empty reason", 2)
            self._check_decision(task, source, target, decision, role)
            self._move_raw(task, target, swimlane_id=self._current_swimlane_id(task))
            try:
                if target in {"ready", "done"}:
                    self.client.call("saveTaskMetadata", task_id=_task_number(task), values=_READY_RESET_METADATA)
                elif source == "validate":
                    self.client.call("saveTaskMetadata", task_id=_task_number(task), values={"resolved_review_head": ""})
                if reason.strip():
                    self.client.call("createComment", task_id=_task_number(task), user_id=0, content=f"[{role}]\n{reason}")
            except Exception as exc:
                raise _CommittedWriteError() from exc
        return self._write(
            "moved", role, actor, reference, request_id,
            lambda task: {
                "from": task["state"], "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                **({"decision": decision} if decision else {}),
                **override_payload,
            },
            mutation,
            # `from` is the column the move already left, so it is the one field a retry cannot
            # recompute. Everything the caller asked for is compared.
            identity={
                "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                "decision": decision or None,
                "sprint_override_reason": override_payload.get("sprint_override_reason"),
            },
        )

    def _check_decision(
        self, task: dict[str, Any], source: str, target: str, decision: str, role: str,
    ) -> None:
        """A card leaves Assessment on a decision somebody recorded, or it does not leave.

        Two rules, and they bind different callers. A decision that is supplied has to be real and
        has to agree with where the card is going, whoever passes it: each decision has exactly one
        destination, so a `release` paired with a move back to In progress is a rework nobody
        decided. Needing a decision at all is the dispatcher's rule, because the dispatcher is what
        performs decisions; the PO is the human operator and its move is the escape hatch, already
        recorded as a sprint override on a reserved project. Holding the PO to a decision would
        close the only way past a seam that is stuck.

        `blocked` without a decision is left open on purpose even for the dispatcher: the steward's
        stale escalation and the dispatcher's own failure paths reach it without anyone having
        decided anything, and a card that cannot be blocked is a card nothing can rescue. The three
        remaining exits are closed to the dispatcher, because each of them leaves the column with
        the decision still unmade. The observer reaches none of this: the authority matrix gives it
        no exit from Assessment at all, because performing a decision is the dispatcher's part of
        the seam.
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

        The audit event chains old and new content digests; full text history is
        recoverable from the Git checkpoint of `state/board/cards.ndjson`. Cards with
        an active attempt (In progress / Validate) are not editable: the running head
        works from a TASK.md snapshot, so a mid-flight revision must go through
        preempt/requeue, not a silent spec swap.
        """
        self._role(role, _EDIT_ROLES)
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
        """Whether an open sprint reserves this card's project.

        Both callers run after `_guard_sprint_write`, which initializes the index and
        refreshes the entries of the project it was asked about.
        """
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
        """Authorize one create/move/edit against the open-sprint reservation index."""
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
            return {"sprint_override_reason": sprint_override_reason}
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
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=task["ref"])
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        return _positive_int(raw.get("swimlane_id")) or 0

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
                if str(event.get("backend", {}).get("kind") or "") == "dispatcher":
                    # An observer lifecycle event describes a head, not a backend row: there is
                    # nothing to re-read and enrich, and it must repair even when the sprint it
                    # names has already left the board.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") == "sprint_guard_denied":
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

                    SprintWriter(self.client, data_dir=self.data_dir)._pending(
                        str(event.get("kind") or "updated"), event
                    )
                    repaired += 1
                    continue
                self._finish_pending_cleanup(event, None)
                task = self.reader.show(str(event["ref"]))
                event["task_id"] = task["id"]
                event["backend"]["revision"] = _revision(task)
                self.audit.stage(str(event["request_id"]), event)
                self.audit.append(str(event["request_id"]), event)
                repaired += 1
            except (TaskError, OSError, KeyError, TypeError):
                unresolved += 1
        return repaired, unresolved

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
        if retry_reason and _digest(retry_reason) != expected_digest:
            raise TaskError("validation", "archive retry reason does not match the pending request", 2)
        board_id, _, _ = self.reader._board()
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=ref)
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
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=ref)
        if isinstance(raw, dict) and _task_is_active(raw):
            raise TaskError("backend_error", "pending archive remains incomplete", 1)

    def _finish_pending_restore(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        from secretary.task_restore import finish_pending_restore

        finish_pending_restore(self, event, payload)

    def _finish_pending_create(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending create is missing its task ref", 1)
        try:
            task = self.reader.show(ref)
        except TaskError as exc:
            if exc.code != "not_found":
                raise
            backend = event.get("backend")
            task_id = _positive_int(backend.get("task_id")) if isinstance(backend, dict) else None
            if task_id is None:
                raise
            if not self.client.call("updateTask", id=task_id, reference=ref):
                raise TaskError("backend_error", "pending create reference remains incomplete", 1)
            task = self.reader.show(ref)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_create_metadata_values(payload),
        )
        normalized = self.reader.show(ref)
        expected_mode = _text(payload.get("codex_launch_mode"))
        if expected_mode and normalized["routing"]["codex_launch_mode"] != expected_mode:
            raise TaskError("backend_error", "pending create metadata remains incomplete", 1)

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
        for state_path in (
            self.data_dir / "dispatcher" / "pilot-state.json",
            self.data_dir / "dispatcher" / "production-state.json",
        ):
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError, UnicodeError):
                raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3) from None
            if not isinstance(payload, dict):
                raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
            records = payload.get("records") or {}
            if not isinstance(records, dict):
                raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
            record = records.get(reference)
            if not isinstance(record, dict):
                continue
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
