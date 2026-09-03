"""Sprint entities stored as tasks on a dedicated Kanboard board."""

from __future__ import annotations

import fcntl
import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from secretary.sprint_observer import (
    KIND_HEAD,
    OBSERVER_FIELD,
    ObserverMetadataError,
    check_observer_profile,
    encode_observer,
    installed_observer_profiles,
    is_executable,
    parse_observer,
)
from secretary.tasks import (
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    _digest,
    _now,
    _positive_int,
    _rfc3339,
    _text,
    is_significant_observer_event,
    reference_allocation_lock,
)
from triggered_agents.runtime.references import BoardRowsUnavailable, board_rows, next_reference

SPRINT_BOARD_NAME = "Secretary sprints"
SPRINT_REFERENCE_PREFIX = "sprint:"
SPRINT_METADATA = {
    "sprint_goal",
    "sprint_definition_of_done",
    "sprint_repositories",
    "sprint_product",
    "sprint_issues",
    "sprint_reservations",
    "sprint_status",
    "sprint_budget",
    "sprint_budget_uncharged",
    "sprint_current_task",
    "sprint_resume",
    "sprint_source_audit",
    "sprint_observer",
}
SOURCE_AUDIT_FIELDS = ("created_at", "updated_at", "board")
# Charged restart types contribute to total and thresholds.
BUDGET_EVENT_TYPES = (
    "red_review",
    "blocked",
    "red_ci",
    "preempt",
    "recreated_task",
    "hotfix",
)
# Infrastructure bring-up failures are visible but never spend restart budget.
BUDGET_UNCHARGED_INFRASTRUCTURE = "infrastructure_blocked"
BUDGET_UNCHARGED_EVENT_TYPES = (BUDGET_UNCHARGED_INFRASTRUCTURE,)
BUDGET_RECORDED_EVENT_TYPES = BUDGET_EVENT_TYPES + BUDGET_UNCHARGED_EVENT_TYPES
# Uncharged counts stay outside computed sprint_budget metadata.
BUDGET_UNCHARGED_FIELD = "sprint_budget_uncharged"
DEFAULT_BUDGET_SIGNAL = 3
DEFAULT_BUDGET_HARD = 6
DEFAULT_OPEN_SPRINT_LIMIT = 1
MAX_OPEN_SPRINT_LIMIT = 2
# Observer freshness is based on card transitions, not status-read time.
RESUME_FRESHNESS_GRACE_SECONDS = 5 * 60
RESUME_FIELDS = (
    "selected_step",
    "selected_why",
    "rejected_alternatives",
    "current_task",
    "dod_state",
    "next_safe_step",
)
_GUARD_INDEX = "sprints/active-repositories.json"
# Version 1 indexes are rebuilt: guards key by project, not repository path.
_GUARD_INDEX_VERSION = 2
_ADMISSION_LOCK = "sprints/admission.lock"
SPRINT_CREATED = "created"
SPRINT_REOPENED = "reopened"
SPRINT_CLOSED = "closed"
SPRINT_STATUSES = {"open", "closed", "stopped"}
# Terminal sprint states reject semantic writes, so their resume freshness is stable.
SPRINT_TERMINAL_STATUSES = {"closed", "stopped"}
# A compensated refused create holds nothing and needs no pending repair.
_ADMISSION_REFUSALS = {"sprint_conflict", "resource_conflict"}


def active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """Return the local index of projects reserved by open sprints."""
    return _read_guard_index(Path(data_dir) / _GUARD_INDEX) or {}


def _read_guard_index(path: Path) -> dict[str, list[str]] | None:
    """Return the index, or None when it is absent, unreadable or of an older version."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _GUARD_INDEX_VERSION:
        return None
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        return None
    return {
        str(project): sorted({str(ref) for ref in refs if str(ref)})
        for project, refs in projects.items()
        if isinstance(refs, list) and str(project)
    }


def sprint_guard_index_initialized(data_dir: str | Path) -> bool:
    return _read_guard_index(Path(data_dir) / _GUARD_INDEX) is not None


def refresh_active_sprint_projects(data_dir: str | Path, reader: Any) -> None:
    """Seed the index from the live board without racing a sprint mutation."""
    with _sprint_guard_index_lock(data_dir):
        _replace_active_sprint_projects(data_dir, reader.list(statuses={"open"}, create=False))


def _replace_active_sprint_projects(data_dir: str | Path, sprints: list[dict[str, Any]]) -> None:
    projects: dict[str, list[str]] = {}
    for sprint in sprints:
        if sprint.get("status") != "open":
            continue
        reference = str(sprint.get("ref") or "")
        if not reference:
            continue
        for project in sprint.get("reservations") or []:
            name = str(project).strip()
            if name:
                projects[name] = sorted(set(projects.get(name, []) + [reference]))
    _write_guard_index(data_dir, projects)


def update_active_sprint_projects(data_dir: str | Path, sprint: dict[str, Any]) -> None:
    """Update one sprint's entries in the local reserved-project index."""
    with _sprint_guard_index_lock(data_dir):
        path = Path(data_dir) / _GUARD_INDEX
        projects = _read_guard_index(path)
        if projects is None and path.exists():
            # Rebuild stale index key spaces from the board.
            path.unlink()
            return
        projects = projects or {}
        reference = str(sprint.get("ref") or "")
        for project in list(projects):
            refs = [ref for ref in projects[project] if ref != reference]
            if refs:
                projects[project] = refs
            else:
                projects.pop(project)
        if reference and sprint.get("status") == "open":
            for project in sprint.get("reservations") or []:
                name = str(project).strip()
                if name:
                    projects[name] = sorted(set(projects.get(name, []) + [reference]))
        _write_guard_index(data_dir, projects)


@contextmanager
def _sprint_guard_index_lock(data_dir: str | Path):
    with _exclusive_lock((Path(data_dir) / _GUARD_INDEX).with_suffix(".lock")):
        yield


@contextmanager
def sprint_admission_lock(data_dir: str | Path):
    """Serialize every admission of an open sprint on this installation.

    The rules for opening a sprint are reads of live state, so two writers that both check before
    either writes would each see no open sprint and both create one. Held across the check and the
    backend write, and nothing after it.
    """
    with _exclusive_lock(Path(data_dir) / _ADMISSION_LOCK):
        yield


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_guard_index(data_dir: str | Path, projects: dict[str, list[str]]) -> None:
    path = Path(data_dir) / _GUARD_INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"version": _GUARD_INDEX_VERSION, "projects": projects},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def budget_thresholds(config: dict[str, Any] | None = None) -> dict[str, int]:
    """Read installation budget limits, retaining safe defaults for old installations."""
    raw = (config or {}).get("sprint_budget") if isinstance(config, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    signal = _positive_int(raw.get("signal")) or DEFAULT_BUDGET_SIGNAL
    hard = _positive_int(raw.get("hard")) or DEFAULT_BUDGET_HARD
    if hard < signal:
        raise TaskError("validation", "sprint budget hard threshold must not be below signal threshold", 2)
    return {"signal": signal, "hard": hard}


def open_sprint_limit(config: dict[str, Any] | None = None) -> int:
    """How many sprints this installation may hold open, refusing to widen on bad input.

    An absent, unreadable or malformed setting all answer one. This answers rather than raises, so
    a malformed value cannot stop admission either; `open_sprint_limit_invalid` reports the refusal.
    """
    raw = config.get("open_sprint_limit") if isinstance(config, dict) else None
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_OPEN_SPRINT_LIMIT
    if not DEFAULT_OPEN_SPRINT_LIMIT <= raw <= MAX_OPEN_SPRINT_LIMIT:
        return DEFAULT_OPEN_SPRINT_LIMIT
    return raw


def open_sprint_limit_invalid(config: dict[str, Any] | None = None) -> bool:
    """Whether the setting is present and holds something this installation cannot honour."""
    if not isinstance(config, dict) or "open_sprint_limit" not in config:
        return False
    raw = config["open_sprint_limit"]
    # `True` is an int to Python and would compare equal to the limit it falls back to,
    # so the type is judged before the value.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return True
    return not DEFAULT_OPEN_SPRINT_LIMIT <= raw <= MAX_OPEN_SPRINT_LIMIT


def instance_open_sprint_limit(instance: Path | None) -> int:
    """The limit of one named installation, read at the moment it is asked for.

    Nothing here can widen the limit, which is what makes a malformed setting harmless.
    """
    if instance is None:
        return DEFAULT_OPEN_SPRINT_LIMIT
    from secretary.config import ConfigError, load_config

    path = instance / "instance.yaml" if instance.is_dir() else instance
    try:
        config = load_config(path)
    except ConfigError:
        return DEFAULT_OPEN_SPRINT_LIMIT
    return open_sprint_limit(config if isinstance(config, dict) else None)


def open_sprint_admission_error(rows: list[dict[str, Any]], *, limit: int) -> str | None:
    """Why this set of open sprints could not have been admitted, or None if it could.

    A whole set is judged by admitting it one row at a time, in reference order, against the rows
    already accepted: an export must not be a way to arrive at a pair `create` would have refused.
    """
    admitted: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda row: str(row.get("ref") or "")):
        try:
            _refuse_open_sprint(row, admitted, limit=limit)
        except TaskError as exc:
            return f"{row.get('ref') or '?'}: {exc.message}"
        admitted.append(row)
    return None


def _refuse_open_sprint(candidate: dict[str, Any], others: list[dict[str, Any]], *, limit: int) -> None:
    """Refuse a sprint this installation has no room, or no disjoint room, for.

    Every collision the caller can act on is reported before the generic count refusal, because a
    resource refusal names the sprint holding the resource while the count refusal distinguishes
    none of them.
    """
    saturated = len(others) >= limit
    _refuse_shared_reservations(
        [str(project) for project in candidate.get("reservations") or []],
        others,
    )
    if limit > DEFAULT_OPEN_SPRINT_LIMIT:
        _refuse_shared_resources(candidate, others)
    if saturated:
        raise _open_sprint_count_error(others, limit)


def _open_sprint_count_error(others: list[dict[str, Any]], limit: int) -> TaskError:
    refs = ", ".join(sorted(str(sprint["ref"]) for sprint in others))
    if limit == DEFAULT_OPEN_SPRINT_LIMIT:
        return TaskError(
            "sprint_conflict",
            f"installation already has an open sprint: {refs}; close it before opening another",
            2,
        )
    return TaskError(
        "sprint_conflict",
        f"installation already holds its limit of {limit} open sprints: {refs}; "
        "close one before opening another",
        2,
    )


def _refuse_shared_reservations(reservations: list[str], others: list[dict[str, Any]]) -> None:
    """Refuse a project another open sprint already reserves, naming both."""
    held: dict[str, str] = {}
    for sprint in others:
        for project in sprint.get("reservations") or []:
            held.setdefault(str(project), str(sprint["ref"]))
    clashes = [(project, held[project]) for project in reservations if project in held]
    if clashes:
        raise TaskError(
            "resource_conflict",
            "project(s) already reserved by an open sprint: "
            + ", ".join(f"{project} held by {ref}" for project, ref in sorted(clashes)),
            2,
        )


def _refuse_shared_resources(candidate: dict[str, Any], others: list[dict[str, Any]]) -> None:
    """The invariants that make a second open sprint safe, above the reservations.

    Two open sprints may only exist while nothing they work on is shared: a different product, no
    shared project reservation, and no repository tree either contains. Overlap includes nesting
    and is judged on canonical paths; a stored root that is not already absolute is refused rather
    than resolved here, because the tree it names would depend on the resolving process's cwd.

    The candidate's own roots are judged before any pairwise comparison and whether or not another
    sprint is open.
    """
    product = str(candidate.get("product") or "").strip()
    ordered = sorted(others, key=lambda row: str(row["ref"]))
    # Judge both sides so disjointness does not depend on iteration order.
    if ordered and not product:
        raise TaskError(
            "resource_conflict",
            "this sprint declares no product, so it cannot be proven disjoint from "
            f"open sprint {ordered[0]['ref']!s}",
            2,
        )
    roots = _scanned_roots(
        candidate.get("repositories") or [],
        refusal=lambda text, why: TaskError(
            "resource_conflict",
            f"this sprint declares repository root {text!r}, which {why}, so it cannot be "
            "proven disjoint from another open sprint",
            2,
        ),
    )
    for sprint in ordered:
        reference = str(sprint["ref"])
        other_product = str(sprint.get("product") or "").strip()
        if not other_product:
            raise TaskError(
                "resource_conflict",
                f"open sprint {reference} declares no product, so a second open sprint "
                "cannot be proven disjoint from it",
                2,
            )
        if other_product == product:
            raise TaskError(
                "resource_conflict",
                f"product {product} is already the product of open sprint {reference}; "
                "a second open sprint needs a different product",
                2,
            )
        held_roots = _scanned_roots(
            sprint.get("repositories") or [],
            # `reference` is bound here rather than closed over: the callee calls this back
            # inside the same iteration, but a refusal that named the wrong sprint would be a
            # silent lie, and the binding costs nothing.
            refusal=lambda text, why, reference=reference: TaskError(
                "resource_conflict",
                f"open sprint {reference} declares repository root {text!r}, which {why}, "
                "so a second open sprint cannot be proven disjoint from it",
                2,
            ),
        )
        for held in held_roots:
            clash = next((root for root in roots if _roots_overlap(root, held)), None)
            if clash is not None:
                raise TaskError(
                    "resource_conflict",
                    f"repository root {clash} overlaps {held}, held by open sprint {reference}",
                    2,
                )


def ensure_sprint_board(client: KanboardClient) -> int:
    """Return the dedicated sprint board, creating it once when absent."""
    board_id = _sprint_board(client, create=True)
    if board_id is None:
        raise TaskError("backend_error", "Kanboard did not create the sprint board", 1)
    return board_id


def _sprint_board(client: KanboardClient, *, create: bool) -> int | None:
    board = client.call("getProjectByName", name=SPRINT_BOARD_NAME)
    board_id = _positive_int(board.get("id")) if isinstance(board, dict) else None
    if board_id is None and create:
        board_id = _positive_int(client.call("createProject", name=SPRINT_BOARD_NAME))
    if board_id is None:
        return None
    return board_id


class _AuditOnce:
    """One committed-audit traversal shared by the sprint summaries of a single operation."""

    def __init__(self, data_dir: Path | None) -> None:
        self._data_dir = data_dir
        self._events: list[dict[str, Any]] | None = None

    def events(self) -> list[dict[str, Any]]:
        if self._data_dir is None:
            return []
        if self._events is None:
            self._events = TaskAudit(self._data_dir).events()
        return self._events


def _task_id(raw: dict[str, Any]) -> int:
    """The Kanboard identifier of a sprint row, which every read of it needs."""
    task_id = _positive_int(raw.get("id"))
    if task_id is None:
        raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
    return task_id


def _sprint_metadata(answer: Any) -> dict[str, str]:
    """One sprint row's metadata, narrowed to the contract fields a sprint is made of."""
    if answer is not None and not isinstance(answer, dict):
        raise TaskError("backend_error", "Kanboard returned invalid sprint metadata", 1)
    return {str(key): _text(value) for key, value in (answer or {}).items() if str(key) in SPRINT_METADATA}


class SprintReader:
    def __init__(
        self,
        client: KanboardClient,
        *,
        data_dir: str | Path | None = None,
        thresholds: dict[str, int] | None = None,
    ) -> None:
        self.client = client
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.thresholds = (
            budget_thresholds({"sprint_budget": thresholds}) if thresholds else budget_thresholds()
        )

    def _sprint_rows(self, board_id: int) -> list[dict[str, Any]]:
        """Every row of the sprint board that carries a sprint reference."""
        rows = self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
        if not isinstance(rows, list):
            raise TaskError("backend_error", "Kanboard returned an invalid sprint list", 1)
        return [raw for raw in rows if _is_sprint_row(raw)]

    def _metadata_of(self, rows: list[dict[str, Any]]) -> dict[int, dict[str, str]]:
        """The metadata of every given sprint row, keyed by task id, in one batched read."""
        task_ids = [_task_id(raw) for raw in rows]
        answers = self.client.call_batch(("getTaskMetadata", {"task_id": task_id}) for task_id in task_ids)
        return {task_id: _sprint_metadata(answer) for task_id, answer in zip(task_ids, answers, strict=True)}

    def list(self, *, statuses: set[str] | None = None, create: bool = True) -> list[dict[str, Any]]:
        board_id = _sprint_board(self.client, create=create)
        if board_id is None:
            return []
        rows = self._sprint_rows(board_id)
        metadata = self._metadata_of(rows)
        result = []
        for raw in rows:
            sprint = self._normalize(
                raw,
                metadata[_task_id(raw)],
                comments=None,
                include_resume_freshness=False,
            )
            # Without live cards this value would claim freshness based on incomplete data.  `show`
            # and `status` populate it after reading the linked cards instead.
            sprint.pop("resume_freshness", None)
            if statuses and sprint["status"] not in statuses:
                continue
            result.append(sprint)
        return sorted(result, key=lambda sprint: (sprint["status"], sprint["ref"], sprint["id"]))

    def export(self) -> list[dict[str, Any]]:
        """Every sprint entity with its records, in a deterministic order."""
        board_id = _sprint_board(self.client, create=False)
        if board_id is None:
            return []
        rows = self._sprint_rows(board_id)
        metadata = self._metadata_of(rows)
        all_comments = self.client.call_batch(("getAllComments", {"task_id": _task_id(raw)}) for raw in rows)
        result = []
        for raw, comments_raw in zip(rows, all_comments, strict=True):
            comments = [
                {"created_at": _rfc3339(comment.get("date_creation")), "body": _text(comment.get("comment"))}
                for comment in comments_raw or []
                if isinstance(comment, dict)
            ]
            sprint = self._normalize(
                raw,
                metadata[_task_id(raw)],
                comments=comments,
                include_resume_freshness=False,
            )
            # Freshness needs the linked cards this view deliberately does not read.
            sprint.pop("resume_freshness", None)
            result.append(sprint)
        return sorted(result, key=lambda sprint: (sprint["ref"], sprint["id"]))

    def show(
        self,
        reference: str,
        *,
        include_cards: bool = True,
        include_resume_freshness: bool = True,
        audit: _AuditOnce | None = None,
    ) -> dict[str, Any]:
        board_id = ensure_sprint_board(self.client)
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
        if not isinstance(raw, dict):
            raise TaskError("not_found", "sprint was not found", 2)
        task_id = _task_id(raw)
        comments = None
        if include_cards:
            comments_raw = self.client.call("getAllComments", task_id=task_id) or []
            comments = [
                {"created_at": _rfc3339(comment.get("date_creation")), "body": _text(comment.get("comment"))}
                for comment in comments_raw
                if isinstance(comment, dict)
            ]
        # Freshness requires linked cards, loaded after normalization.
        sprint = self._normalize(
            raw,
            _sprint_metadata(self.client.call("getTaskMetadata", task_id=task_id)),
            comments=comments,
            include_resume_freshness=False,
        )
        if include_cards:
            sprint["cards"] = TaskReader(self.client).list(sprint=reference)
        if include_resume_freshness:
            sprint["resume_freshness"] = self._resume_freshness(sprint, sprint.get("resume"), audit=audit)
        return sprint

    def _normalize(
        self,
        raw: dict[str, Any],
        meta: dict[str, str],
        *,
        comments: list[dict[str, Any]] | None,
        include_resume_freshness: bool = True,
    ) -> dict[str, Any]:
        task_id = _task_id(raw)
        repositories = _json_list(meta.get("sprint_repositories"))
        budget = _budget(meta.get("sprint_budget"), self.thresholds, meta.get(BUDGET_UNCHARGED_FIELD))
        result: dict[str, Any] = {
            "id": f"sprint_kanboard_{task_id}",
            "ref": _text(raw.get("reference")),
            "goal": meta.get("sprint_goal", ""),
            "definition_of_done": meta.get("sprint_definition_of_done", ""),
            "repositories": repositories,
            **_ownership(meta),
            **_observer(meta),
            "status": meta.get("sprint_status") if meta.get("sprint_status") in SPRINT_STATUSES else "open",
            "budget": budget,
            "current_task": meta.get("sprint_current_task") or None,
            "audit": {
                "created_at": _rfc3339(raw.get("date_creation")),
                "updated_at": _rfc3339(raw.get("date_modification")),
                "backend": {"kind": "kanboard", "kanboard_task_id": task_id, "board": SPRINT_BOARD_NAME},
                # A restored sprint sits on a fresh Kanboard row, so its own dates
                # describe the recovery, not the sprint. The dates it was restored
                # from stay readable here.
                "source": _source_audit(meta.get("sprint_source_audit")),
            },
        }
        if comments is not None:
            result["comments"] = comments
        resume = _resume(meta.get("sprint_resume"))
        result["resume"] = resume
        if include_resume_freshness:
            result["resume_freshness"] = self._resume_freshness(result, resume)
        return result

    def statuses(
        self,
        *,
        observers: dict[str, dict[str, Any]] | None = None,
        headless: dict[str, dict[str, Any]] | None = None,
        create: bool = False,
    ) -> list[dict[str, Any]]:
        """Every sprint's status, reading each part of the board once for the whole call.

        Asking `status` per sprint read the same things over and over: the sprint's metadata, its
        comments, and the entire Pipeline listing with the metadata of every card on it - once per
        sprint. That is what kept `secretary status` around a minute on a live board with 72
        sprints and 1119 cards. Nothing here is per sprint but the assembling: the sprint rows and
        their metadata are one read each, the cards are one listing shared by every sprint, and the
        committed audit is consumed at most once.
        """
        observers = observers or {}
        audit = _AuditOnce(self.data_dir)
        sprints = self.list(create=create)
        linked: dict[str, list[dict[str, Any]]] = {}
        for card in TaskReader(self.client).list():
            linked.setdefault(str(card.get("sprint") or ""), []).append(card)
        result = []
        for listed in sprints:
            sprint = {**listed, "cards": linked.get(listed["ref"], [])}
            sprint["resume_freshness"] = self._resume_freshness(
                sprint,
                sprint.get("resume"),
                audit=audit,
            )
            result.append(self._status(sprint, observers.get(sprint["ref"]), headless or {}))
        return result

    def status(
        self,
        reference: str,
        *,
        observer: dict[str, Any] | None = None,
        headless: dict[str, dict[str, Any]] | None = None,
        audit: _AuditOnce | None = None,
    ) -> dict[str, Any]:
        return self._status(self.show(reference, audit=audit), observer, headless or {})

    def _status(
        self,
        sprint: dict[str, Any],
        observer: dict[str, Any] | None,
        headless: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """One sprint's status view over a sprint that has already been read."""
        cards = sprint.get("cards") or []
        states: dict[str, list[str]] = {}
        for card in cards:
            if isinstance(card, dict):
                states.setdefault(str(card.get("state") or "unknown"), []).append(str(card.get("ref") or ""))
        return {
            "ref": sprint["ref"],
            "goal": sprint["goal"],
            "status": sprint["status"],
            **{field: sprint[field] for field in ("product", "issues", "reservations") if field in sprint},
            "current_task": sprint["current_task"],
            "cards": {key: sorted(value) for key, value in sorted(states.items())},
            "budget": sprint["budget"],
            "resume_freshness": sprint["resume_freshness"],
            "stop_reason": "budget_hard_limit" if sprint["status"] == "stopped" else None,
            "observer": observer or {"state": "unknown"},
            # This sprint's own cards that owe a worker no dispatcher record can name
            # (secretary-1544). Any column of this sprint, not only In progress: the column is what
            # cannot say it, and a sprint whose only visible signal is "3 in progress" reads as
            # moving while nothing is.
            "degraded_cards": {
                reference: detail
                for reference, detail in sorted((headless or {}).items())
                if any(reference in refs for refs in states.values())
            },
        }

    def _resume_freshness(
        self,
        sprint: dict[str, Any],
        resume: dict[str, Any] | None,
        *,
        audit: _AuditOnce | None = None,
    ) -> dict[str, Any]:
        """The freshness of a sprint's resume, read here and nowhere else.

        An open sprint is judged against the significant linked-card events of the committed audit; a
        closed or stopped sprint against its own record and nothing else, since that record is frozen
        at the terminal transition. The returned shape is the same in every case.
        """
        if not resume:
            return {
                "fresh": False,
                "error": "resume_missing",
                "recorded_at": None,
                "last_event_at": None,
                "lag_seconds": None,
                "threshold_seconds": RESUME_FRESHNESS_GRACE_SECONDS,
            }
        last_event = ""
        terminal = str(sprint.get("status") or "") in SPRINT_TERMINAL_STATUSES
        if self.data_dir is not None and not terminal:
            refs = {
                str(card.get("ref") or "")
                for card in sprint.get("cards") or []
                if isinstance(card, dict) and str(card.get("ref") or "")
            }
            for event in (audit if audit is not None else _AuditOnce(self.data_dir)).events():
                if is_significant_observer_event(event, linked_refs=refs, sprint_ref=sprint["ref"]):
                    last_event = max(last_event, str(event.get("occurred_at") or ""))
        recorded_at = str(resume.get("recorded_at") or "")
        lag_seconds = _resume_lag_seconds(recorded_at, last_event)
        invalid_recorded_at = _timestamp(recorded_at) is None
        stale = invalid_recorded_at or bool(
            last_event and (lag_seconds is None or lag_seconds > RESUME_FRESHNESS_GRACE_SECONDS)
        )
        return {
            "fresh": not stale,
            "error": "resume_stale" if stale else None,
            "recorded_at": recorded_at or None,
            "last_event_at": last_event or None,
            "lag_seconds": lag_seconds,
            "threshold_seconds": RESUME_FRESHNESS_GRACE_SECONDS,
        }


class SprintWriter:
    """Sprint mutations with the task protocol's durable audit semantics."""

    def __init__(
        self,
        client: KanboardClient,
        *,
        data_dir: str | Path,
        thresholds: dict[str, int] | None = None,
        instance: str | Path | None = None,
    ) -> None:
        from secretary.product_issues import ProductIssueTransaction

        self.client = client
        self.thresholds = (
            budget_thresholds({"sprint_budget": thresholds}) if thresholds else budget_thresholds()
        )
        self.reader = SprintReader(client, data_dir=data_dir, thresholds=self.thresholds)
        self.audit = TaskAudit(data_dir)
        # Reuse the Product/Issue transaction's staged-intent semantics.
        self.transactions = ProductIssueTransaction(data_dir, self.audit)
        self.data_dir = Path(data_dir)
        self.instance = Path(instance) if instance is not None else None

    def _host(self):
        """Construct the normalized host lazily to keep reader imports acyclic."""
        from secretary.board.kanboard import KanboardBoardHost

        return KanboardBoardHost(
            self.client,
            data_dir=str(self.data_dir),
            instance=str(self.instance) if self.instance else None,
            audit=self.audit,
        )

    @staticmethod
    def _host_error(exc: Exception) -> TaskError:
        from secretary.board.events import BoardEventPending
        from secretary.board.transitions import BoardProtocolError

        if isinstance(exc, BoardEventPending):
            return TaskError(
                "audit_pending", "Sprint lifecycle write is pending repair; retry with the same request id", 4
            )
        if isinstance(exc, BoardProtocolError) and ("rejected" in str(exc) or "refused" in str(exc)):
            return TaskError("backend_error", str(exc), 1)
        if isinstance(exc, TaskError):
            return exc
        return TaskError("validation", str(exc), 2)

    def _transition_host(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        request_id: str,
        observer: str | None = None,
        budget_by_type: tuple[tuple[str, int], ...] = (),
    ) -> None:
        from secretary.board import (
            Actor,
            EntityKind,
            RelatedRefs,
            SprintState,
            SprintSupplement,
            TransitionRequest,
        )

        try:
            host = self._host()
            supplement = SprintSupplement(observer=observer, budget_by_type=budget_by_type)
            host.transition(
                TransitionRequest(
                    EntityKind.SPRINT,
                    reference,
                    SprintState(target),
                    Actor(role, actor),
                    reason,
                    RelatedRefs(),
                    request_id,
                    supplement if (observer is not None or budget_by_type) else None,
                )
            )
        except Exception as exc:
            raise self._host_error(exc) from None

    def create(
        self,
        *,
        role: str,
        actor: str,
        goal: str,
        definition_of_done: str = "",
        repositories: list[str] | None = None,
        product: str = "",
        issues: list[str] | None = None,
        projects: list[str] | None = None,
        reference: str = "",
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._role(role, {"po", "steward"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = self._create_intent(
            role=role,
            actor=actor,
            goal=goal,
            definition_of_done=definition_of_done,
            repositories=repositories or [],
            product=product,
            issues=issues or [],
            reservations=projects or [],
            reference=reference,
            observer=observer,
        )
        # Lock admission with row creation; resolve request ownership first.
        with sprint_admission_lock(self.data_dir):
            document, committed = self.transactions.existing(request_id, kind=SPRINT_CREATED, intent=intent)
            if committed is not None:
                return self._committed_result(SPRINT_CREATED, committed)
            if document is None:
                self._check_ownership(intent["product"], intent["issues"], intent["reservations"])
                self._check_conflicts(intent, excluding="")
                document, committed = self._begin_create(request_id, intent)
                if committed is not None:
                    return self._committed_result(SPRINT_CREATED, committed)
            return self._run_create(document, admitted=True)

    def restore_create(
        self,
        *,
        reference: str,
        goal: str,
        definition_of_done: str = "",
        repositories: list[str] | None = None,
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
        status: str = "open",
    ) -> dict[str, Any]:
        """Recreate one exported sprint row, without the rules for opening a sprint.

        Not an admission: `restore` writes the real fields immediately after, and several restored
        entities are written one after another, so this must not check or invent ownership.

        Status and observer are written before the reference: the row becomes visible the moment its
        reference lands, and between the two writes a reader would otherwise see an open sprint with no
        observer — the one state the strict reader calls corrupt.
        """
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = self._create_intent(
            role="steward",
            actor="restore",
            goal=goal,
            definition_of_done=definition_of_done,
            repositories=repositories or [],
            product="",
            issues=[],
            reservations=[],
            reference=reference,
            require_goal=False,
            observer=observer,
            status=status,
            require_executable_observer=False,
            canonical_repositories=False,
        )
        document, committed = self.transactions.existing(request_id, kind=SPRINT_CREATED, intent=intent)
        if committed is not None:
            return self._committed_result(SPRINT_CREATED, committed)
        if document is None:
            document, committed = self._begin_create(request_id, intent)
            if committed is not None:
                return self._committed_result(SPRINT_CREATED, committed)
        return self._run_create(document, admitted=False)

    def _create_intent(
        self,
        *,
        role: str,
        actor: str,
        goal: str,
        definition_of_done: str,
        repositories: list[str],
        product: str,
        issues: list[str],
        reservations: list[str],
        reference: str,
        require_goal: bool = True,
        observer: dict[str, Any] | None = None,
        status: str = "open",
        require_executable_observer: bool = True,
        canonical_repositories: bool = True,
    ) -> dict[str, Any]:
        """The normalized request, which is both the replay key and the repair recipe.

        A repeat of the same request id carrying a different intent is another operation and is refused
        before any side effect. The observer is part of the staged intent for the same reason the
        reservations are: a repair has to write the value the caller chose, not one it picks up later.

        Repository roots are canonicalized here, where the operator declaring them is, so the absolute
        root is what the row persists and a root this host cannot resolve is refused before any board
        row, staged intent, metadata or audit event exists. Recovery canonicalizes nothing.
        """
        goal = goal.strip()
        reference = reference.strip()
        if require_goal and not goal:
            raise TaskError("validation", "create requires a non-empty goal", 2)
        if reference and not reference.startswith(SPRINT_REFERENCE_PREFIX):
            raise TaskError("validation", f"sprint reference must start with {SPRINT_REFERENCE_PREFIX}", 2)
        if status not in SPRINT_STATUSES:
            raise TaskError("validation", f"unknown sprint status {status!r}", 2)
        return {
            "role": role,
            "actor": actor,
            "goal": goal,
            "definition_of_done": definition_of_done,
            "repositories": (
                canonical_repository_roots(repositories)
                if canonical_repositories
                else _unique_strings(repositories)
            ),
            "product": product.strip(),
            "issues": _unique_strings(issues),
            "reservations": _unique_strings(reservations),
            "reference": reference,
            "status": status,
            "observer": self._observer_intent(
                observer,
                executable=require_executable_observer,
            ),
        }

    def _observer_intent(
        self,
        observer: dict[str, Any] | None,
        *,
        executable: bool,
    ) -> dict[str, Any] | None:
        """The observer value a create writes, refused here rather than at the backend.

        An operator opening a sprint has to state one: absent, null, empty, `default` and `inherited`
        are not interpretations this model has. Recovery reproduces whatever its export carried,
        including the migration provenance of a closed row, which is not executable.

        A declared profile is resolved against the installation's head registry: a sprint may not be
        opened on a head that does not exist, or the fence would stop its projects on the first tick.
        """
        if observer is None:
            if executable:
                raise TaskError(
                    "validation",
                    "sprint requires an explicit observer; pass --observer <profile> or --observer none",
                    2,
                )
            return None
        value = parse_observer(observer)
        if value is None:
            raise TaskError("validation", "sprint observer is not a valid observer value", 2)
        if not executable:
            return value
        if not is_executable(value):
            raise TaskError(
                "validation",
                "sprint observer must be a concrete head profile or none; migration provenance "
                "is a record of what ran and can never be declared",
                2,
            )
        if value["kind"] == KIND_HEAD:
            # Only a concrete head needs the registry. `none` declares no profile, so a sprint
            # that runs without an observer is not held up by a registry it never asks about.
            try:
                check_observer_profile(
                    value,
                    installed_observer_profiles(self.instance),
                    subject="sprint",
                )
            except ObserverMetadataError as exc:
                raise TaskError("validation", exc.message, 2) from None
        return value

    def _begin_create(
        self, request_id: str, intent: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Claim the request id after the mutable preconditions have passed."""
        reference = str(intent["reference"])
        if reference:
            board_id = _sprint_board(self.client, create=False)
            if board_id is not None and self.client.call(
                "getTaskByReference", project_id=board_id, reference=reference
            ):
                raise TaskError("validation", "sprint reference already exists", 2)
            try:
                TaskReader(self.client).show(reference)
            except TaskError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise TaskError("validation", "sprint reference already belongs to a Pipeline card", 2)
        event = self._event(
            SPRINT_CREATED,
            str(intent["role"]),
            str(intent["actor"]),
            reference,
            request_id,
            {"intent": intent},
        )
        document, committed = self.transactions.begin(
            request_id, kind=SPRINT_CREATED, intent=intent, event=event
        )
        if document is None and committed is None:
            raise TaskError("audit_pending", "sprint transaction claim is unavailable", 4)
        return document, committed  # type: ignore[return-value]

    def _run_create(self, document: dict[str, Any], *, admitted: bool) -> dict[str, Any]:
        """Drive the staged create to its single audit event, or leave it repairable."""
        try:
            reference = self._finish_create(document, admitted=admitted)
            sprint = self.reader.show(reference)
            event = document["event"]
            event["task_id"] = sprint["id"]
            event["backend"]["revision"] = "updated_at:" + str(sprint["audit"]["updated_at"] or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint)
            return {"action": SPRINT_CREATED, "sprint": sprint, "event_id": str(event["event_id"])}
        except TaskError as exc:
            answer = exc.code in _ADMISSION_REFUSALS or (
                exc.code in {"validation", "role_forbidden"} and not document.get("progress")
            )
            # Refuse only after compensation proves the request holds no row.
            clean = self._compensate_create(document)
            if answer and clean:
                self.transactions.discard(document)
                raise
            raise TaskError(
                "audit_pending",
                "sprint create is pending repair; retry with the same request id",
                4,
            ) from None
        except (OSError, KeyError, TypeError):
            self._compensate_create(document)
            raise TaskError(
                "audit_pending",
                "sprint create is pending repair; retry with the same request id",
                4,
            ) from None

    def _compensate_create(self, document: dict[str, Any]) -> bool:
        """Take back the row of a create that never got as far as its reference.

        The staged intent stays here either way: it is what a repeat resumes, and only the caller of a
        refusal it will never repeat discards it. Returns whether the request now holds nothing on the
        backend, which is what makes discarding its intent safe.
        """
        progress = document.get("progress") or {}
        task_id = progress.get("task_id")
        if progress.get("reference_done"):
            return False
        if not isinstance(task_id, int):
            return not progress
        try:
            if self.client.call("removeTask", task_id=task_id) is not True:
                return False
            document["progress"] = {}
            self.transactions.save(document)
            return True
        except (TaskError, OSError, KeyError, TypeError):
            return False

    def _finish_create(self, document: dict[str, Any], *, admitted: bool) -> str:
        """Apply every backend sub-step, recognising the ones an earlier attempt did."""
        intent = document["intent"]
        event = document["event"]
        progress = document.setdefault("progress", {})
        board_id = ensure_sprint_board(self.client)
        created_ref = self._reference(document, board_id, admitted=admitted)
        # Recheck admission before a resumed create publishes a sprint.
        if admitted and not progress.get("reference_done"):
            staged = progress.get("task_id")
            staged_id = staged if isinstance(staged, int) else None
            self._check_reference_claim(created_ref, staged_id)
            self._check_conflicts(intent, excluding_id=staged_id)
        row = self._create_row(document, board_id, created_ref, admitted=admitted)
        task_id = _positive_int(row.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
        event.update({"ref": created_ref, "task_id": f"sprint_kanboard_{task_id}"})
        event["backend"]["task_id"] = task_id
        progress["task_id"] = task_id
        self.transactions.save(document)
        self._ensure_metadata(document, task_id, self._create_values(intent), step="fields")
        # Write the reference last to publish an atomically admitted sprint.
        if str(row.get("reference") or "") != created_ref:
            progress["reference_started"] = True
            self.transactions.save(document)
            if not self.client.call(
                "updateTask",
                id=task_id,
                reference=created_ref,
                description="",
            ):
                raise TaskError("backend_error", "Kanboard rejected the sprint reference", 1)
            row = self._create_row(document, board_id, created_ref, admitted=admitted)
            if str(row.get("reference") or "") != created_ref:
                raise TaskError("backend_error", "sprint reference remains incomplete", 1)
        progress["reference_done"] = True
        self.transactions.save(document)
        return created_ref

    def _reference(self, document: dict[str, Any], board_id: int, *, admitted: bool) -> str:
        """This request's sprint reference, allocated once and then remembered.

        The reference is what publishes a sprint, so a create that stalled and is being repeated
        writes the one it was already going to write rather than a second one allocated meanwhile.
        A repeat that took its row back holds nothing, including this record, and allocates afresh.

        An automatic reference used to be the row's own Kanboard id, which is not a record of what
        the board handed out. Row ids trail the references by hundreds here, so on 2026-08-06 a new
        sprint took `sprint:804` from a sprint closed in July and became unaddressable behind it.

        Only an admitted create allocates. A restore is the one create that adopts a row it finds
        under its reference, because that row is the one it exported and is putting back; a
        reference invented here would let it adopt a row it has never seen. Restoring a sprint
        therefore means naming it, and a restore without a reference is refused rather than given
        somebody else's row.
        """
        recorded = str(document["intent"].get("reference") or document.get("reference") or "")
        if recorded:
            return recorded
        if not admitted:
            raise TaskError("validation", "a restored sprint must name its own reference", 2)
        with reference_allocation_lock(self.data_dir):
            try:
                rows = board_rows(self.client.call, board_id)
            except BoardRowsUnavailable:
                raise TaskError("backend_error", "Kanboard returned an invalid sprint list", 1) from None
            # Kept beside the staged intent rather than in `progress`, which records what the
            # request holds on the backend: an allocation holds nothing, and a create discarded
            # before it wrote a row hands the number straight back.
            document["reference"] = next_reference(rows, SPRINT_REFERENCE_PREFIX)
            self.transactions.save(document)
        return str(document["reference"])

    def _create_row(
        self,
        document: dict[str, Any],
        board_id: int,
        reference: str,
        *,
        admitted: bool,
    ) -> dict[str, Any]:
        """The row this request created, creating it once when it has none yet.

        A row counts as this request's own only when the staged progress names its task id, or, for
        recovery, when nothing has been written for this reference yet. An admitted create never adopts
        a row it merely shares a reference with.
        """
        rows = [
            row
            for row in self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
            if isinstance(row, dict)
        ]
        staged = (document.get("progress") or {}).get("task_id")
        if isinstance(staged, int):
            row = next((row for row in rows if _positive_int(row.get("id")) == staged), None)
            if row is None:
                raise TaskError("backend_error", "the sprint row of this request was not found", 1)
            return row
        if not admitted:
            row = next((row for row in rows if str(row.get("reference") or "") == reference), None)
            if row is not None:
                return row
        # Unpublished create rows are found by request id, not reference.
        marker = _create_marker(str(document["request_id"]))
        matches = [row for row in rows if str(row.get("description") or "") == marker]
        if len(matches) > 1:
            raise TaskError("backend_error", "pending sprint create correlation is ambiguous", 1)
        if matches:
            return matches[0]
        columns = self.client.call("getColumns", project_id=board_id) or []
        column_id = next(
            (_positive_int(column.get("id")) for column in columns if isinstance(column, dict)), None
        )
        if column_id is None:
            raise TaskError("backend_error", "sprint board has no column", 1)
        document.setdefault("progress", {})["create_started"] = True
        self.transactions.save(document)
        created = _positive_int(
            self.client.call(
                "createTask",
                project_id=board_id,
                title=str(document["intent"]["goal"]),
                description=marker,
                column_id=column_id,
            )
        )
        if created is None:
            raise TaskError("backend_error", "Kanboard rejected the sprint write", 1)
        document["progress"]["task_id"] = created
        self.transactions.save(document)
        rows = [
            row
            for row in self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
            if isinstance(row, dict)
        ]
        row = next((row for row in rows if _positive_int(row.get("id")) == created), None)
        if row is None:
            raise TaskError("backend_error", "the created sprint row was not found", 1)
        return row

    def _create_values(self, intent: dict[str, Any]) -> dict[str, str]:
        values = {
            "sprint_goal": str(intent["goal"]),
            "sprint_definition_of_done": str(intent["definition_of_done"]),
            "sprint_repositories": json.dumps(list(intent["repositories"]), separators=(",", ":")),
            "sprint_status": str(intent.get("status") or "open"),
            "sprint_budget": _budget_json(_budget(thresholds=self.thresholds)),
            "sprint_current_task": "",
            "sprint_resume": "",
        }
        # Written with the fields, which is before the reference publishes the row: a sprint is
        # never readable open without the observer it was opened with.  A restored row that
        # carried no observer at all keeps carrying none, and the strict reader refuses it.
        if intent.get("observer") is not None:
            values[OBSERVER_FIELD] = encode_observer(intent["observer"])
        # A restored legacy row gets no ownership keys at all; `restore` then writes
        # back exactly the fields its own export carried.
        if intent["product"]:
            values["sprint_product"] = str(intent["product"])
        if intent["issues"]:
            values["sprint_issues"] = json.dumps(list(intent["issues"]), separators=(",", ":"))
        if intent["reservations"]:
            values["sprint_reservations"] = json.dumps(list(intent["reservations"]), separators=(",", ":"))
        return values

    def _ensure_metadata(
        self,
        document: dict[str, Any],
        task_id: int,
        values: dict[str, str],
        *,
        step: str,
    ) -> None:
        """Write one step of the sprint's fields once, and prove the backend kept it.

        Kanboard answers its metadata API with a boolean; anything other than `True` is a refusal. The
        step is recorded durably, so a repair of a later step never rewrites an earlier one back.
        """
        progress = document.setdefault("progress", {})
        if progress.get(f"{step}_done"):
            return
        if not self._metadata_matches(task_id, values):
            progress[f"{step}_started"] = True
            self.transactions.save(document)
            if self.client.call("saveTaskMetadata", task_id=task_id, values=values) is not True:
                raise TaskError("backend_error", "Kanboard rejected the sprint metadata", 1)
            if not self._metadata_matches(task_id, values):
                raise TaskError("backend_error", "sprint metadata remains incomplete", 1)
        progress[f"{step}_done"] = True
        self.transactions.save(document)

    def _metadata_matches(self, task_id: int, values: dict[str, str]) -> bool:
        actual = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(actual, dict):
            raise TaskError("backend_error", "Kanboard returned invalid sprint metadata", 1)
        stored = {str(key): _text(value) for key, value in actual.items()}
        return all(stored.get(key) == value for key, value in values.items())

    def _committed_result(self, action: str, committed: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": action,
            "sprint": self.reader.show(str(committed["ref"])),
            "event_id": str(committed["event_id"]),
        }

    def _check_ownership(self, product: str, issues: list[str], reservations: list[str]) -> None:
        """Prove the sprint owns a product, an open issue and registered projects.

        Every step is a read, so a rejected sprint leaves no row, no metadata and no audit event.
        """
        if not product:
            raise TaskError("validation", "sprint requires an owning product; pass --product", 2)
        if not issues:
            raise TaskError(
                "validation", "sprint requires at least one open issue of its product; pass --issue", 2
            )
        if not reservations:
            raise TaskError("validation", "sprint requires at least one reserved project; pass --project", 2)
        if self.instance is None:
            raise TaskError("validation", "sprint ownership needs the instance directory; pass --instance", 2)
        from secretary.product_issues import ProductIssueStore, registered_projects

        store = ProductIssueStore(self.client, data_dir=self.data_dir, instance=self.instance)
        try:
            store.show_product(product)
        except TaskError as exc:
            if exc.code != "not_found":
                raise
            raise TaskError("not_found", f"sprint product {product!r} was not found", 2) from None
        known = {str(issue.get("ref") or ""): issue for issue in store.list_issues(include_closed=True)}
        for reference in issues:
            issue = known.get(reference)
            if issue is None:
                raise TaskError("not_found", f"issue {reference!r} was not found", 2)
            owner = str(issue.get("product") or "")
            if owner != product:
                raise TaskError(
                    "validation",
                    f"issue {reference!r} belongs to product {owner!r}, not to {product!r}",
                    2,
                )
            if issue.get("closed"):
                raise TaskError(
                    "validation", f"issue {reference!r} is closed; a sprint needs an open issue", 2
                )
        unknown = sorted(set(reservations) - registered_projects(self.instance))
        if unknown:
            raise TaskError("validation", "unknown registered project(s): " + ", ".join(unknown), 2)

    def _check_reference_claim(self, reference: str, staged_id: int | None) -> None:
        """Refuse the reference this create is about to write when another sprint holds it.

        Both paths pass through here. A stalled create holds nothing, including its reference, so
        between its refusal and its repeat another sprint may legitimately open under that
        reference; the repeat is not its owner. An allocated reference is only as free as the
        enumeration it was counted from, and this is where that is proven against the backend.
        """
        board_id = _sprint_board(self.client, create=False)
        if board_id is None:
            return
        row = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
        owner = _positive_int(row.get("id")) if isinstance(row, dict) else None
        if owner is None or owner == staged_id:
            return
        raise TaskError(
            "sprint_conflict",
            f"sprint reference {reference} now belongs to another sprint; "
            "open this sprint again with a new request",
            2,
        )

    def _open_sprint_limit(self) -> int:
        """This installation's limit, read live at the moment admission runs.

        Nothing here can widen the limit, which is what makes a malformed setting harmless.
        """
        return instance_open_sprint_limit(self.instance)

    def _check_conflicts(
        self,
        candidate: dict[str, Any],
        *,
        excluding: str = "",
        excluding_id: int | None = None,
    ) -> None:
        """Refuse a sprint this installation has no room, or no disjoint room, for.

        Every collision the caller can act on is reported before the generic count refusal. A sprint is
        left out of the scan only when it is proven to be the very row this transition is about — the
        row `reopen` reads by reference, or the row a staged create recorded its task id for. A matching
        reference alone proves nothing.
        """
        others = [
            sprint
            for sprint in self.reader.list(statuses={"open"}, create=False)
            if not (
                (excluding and sprint["ref"] == excluding)
                or (excluding_id is not None and _sprint_number(sprint) == excluding_id)
            )
        ]
        _refuse_open_sprint(candidate, others, limit=self._open_sprint_limit())

    def comment(
        self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "worker", "reviewer", "steward", "retro"})
        if not body.strip():
            raise TaskError("validation", "comment requires a non-empty body", 2)
        return self._write(
            "commented",
            role,
            actor,
            reference,
            request_id,
            {"body_sha256": _digest(body)},
            lambda sprint: self.client.call(
                "createComment", task_id=_sprint_number(sprint), user_id=0, content=f"[{role}]\n{body}"
            ),
        )

    def set_current_task(
        self, *, role: str, actor: str, reference: str, task_reference: str, request_id: str | None = None
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "observer", "steward"})
        task_reference = task_reference.strip()
        if not task_reference:
            raise TaskError("validation", "current task requires a task reference", 2)
        # Guard the resume pointer to prevent cross-sprint cursor movement.
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(
            role=role,
            actor=actor,
            reference=reference,
            request_id=request_id,
        )

        def mutation(sprint: dict[str, Any]) -> None:
            task = TaskReader(self.client).show(task_reference)
            if task.get("sprint") != reference:
                raise TaskError("validation", "current task is not linked to this sprint", 2)
            self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={"sprint_current_task": task_reference},
            )

        return self._write(
            "current_task_set", role, actor, reference, request_id, {"task": task_reference}, mutation
        )

    def record_budget(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        event_type: str,
        request_id: str | None = None,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "steward"})
        if event_type not in BUDGET_RECORDED_EVENT_TYPES:
            raise TaskError("validation", "unknown budget event type " + repr(event_type), 2)
        # One recording path for both families; only the charge is conditional.  An uncharged type
        # can never reach the hard limit, so it never takes the typed hard-stop edge below.
        charged = event_type in BUDGET_EVENT_TYPES
        request_id = request_id or str(uuid.uuid4())
        existing = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if existing is not None:
            if existing.get("record_type") == "board.protocol_event":
                raise TaskError(
                    "validation",
                    "request id belongs to a typed Sprint lifecycle occurrence; retry its protocol recovery",
                    2,
                )
            payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
            if existing.get("kind") != "budget_recorded":
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            if bool(payload.get("hard_limit_stop")):
                return self._finish_hard_budget(
                    role=role,
                    actor=actor,
                    reference=reference,
                    event_type=event_type,
                    request_id=request_id,
                    source_event_id=source_event_id,
                    event=existing,
                )
            return (
                self._committed("budget_recorded", existing)
                if self.audit.committed_event(request_id)
                else self._pending("budget_recorded", existing)
            )
        before = self.reader.show(reference)
        before_budget = _budget(before.get("budget"), self.thresholds)
        hard_stop = (
            charged and before["status"] == "open" and before_budget["total"] + 1 >= self.thresholds["hard"]
        )
        if hard_stop:
            budget = _budget(
                {
                    "by_type": dict(before_budget["by_type"])
                    | {event_type: before_budget["by_type"][event_type] + 1}
                },
                self.thresholds,
            )
            event = self._event(
                "budget_recorded",
                role,
                actor,
                reference,
                request_id,
                {
                    "event_type": event_type,
                    "source_event_id": source_event_id or None,
                    "hard_limit_stop": True,
                    "budget": {"by_type": budget["by_type"]},
                },
                before,
            )
            self.audit.stage(request_id, event)
            return self._finish_hard_budget(
                role=role,
                actor=actor,
                reference=reference,
                event_type=event_type,
                request_id=request_id,
                source_event_id=source_event_id,
                event=event,
            )

        def mutation(sprint: dict[str, Any]) -> None:
            budget = _budget(sprint.get("budget"), self.thresholds)
            if charged:
                budget["by_type"][event_type] += 1
                budget = _budget({"by_type": budget["by_type"]}, self.thresholds)
                values = {"sprint_budget": _budget_json(budget)}
            else:
                budget["uncharged"][event_type] += 1
                values = {
                    BUDGET_UNCHARGED_FIELD: json.dumps(
                        budget["uncharged"], sort_keys=True, separators=(",", ":")
                    )
                }
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=values)

        result = self._write(
            "budget_recorded",
            role,
            actor,
            reference,
            request_id,
            {
                "event_type": event_type,
                "source_event_id": source_event_id or None,
                "hard_limit_stop": hard_stop,
            },
            mutation,
        )
        return result

    def _finish_hard_budget(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        event_type: str,
        request_id: str,
        source_event_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Finish one hard charge and its stopped state as one host-owned effect.

        The generic charge is the request owner and is staged first, but it may not publish until the
        typed hard-stop occurrence has staged, persisted the computed budget plus stopped state, and
        committed. A retry always drives that typed owner before publishing its charge.
        """
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if (
            event.get("kind") != "budget_recorded"
            or event.get("ref") != reference
            or event.get("actor") != {"role": role, "id": actor}
            or payload.get("event_type") != event_type
            or payload.get("source_event_id") != (source_event_id or None)
            or payload.get("hard_limit_stop") is not True
        ):
            raise TaskError("validation", "request id belongs to another operation or payload", 2)
        stored_budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
        by_type = stored_budget.get("by_type") if isinstance(stored_budget.get("by_type"), dict) else None
        if by_type is None or set(by_type) != set(BUDGET_EVENT_TYPES):
            raise TaskError("audit_pending", "hard-budget record lacks its normalized budget", 4)
        counts = tuple(sorted((name, value) for name, value in by_type.items()))
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for _name, value in counts):
            raise TaskError("audit_pending", "hard-budget record has an invalid normalized budget", 4)
        try:
            self._transition_host(
                role=role,
                actor=actor,
                reference=reference,
                target="stopped",
                reason="budget hard limit reached",
                request_id=request_id + ":typed-hard-stop",
                budget_by_type=counts,
            )
        except TaskError:
            # A generic charge may wait only on the exact typed occurrence
            # that owns the state effect.  If staging/effect admission failed
            # before that occurrence survived, retain neither record: the
            # released generic write discarded on every failed mutation, and
            # otherwise this request can become stranded once the Sprint
            # changes state through another command.
            typed_request_id = request_id + ":typed-hard-stop"
            if self.audit.event(typed_request_id) is None:
                self.audit.discard(request_id, event)
            raise
        result = (
            self._committed("budget_recorded", event)
            if self.audit.committed_event(request_id)
            else self._pending("budget_recorded", event)
        )
        self._record_hard_stop(
            role=role,
            actor=actor,
            reference=reference,
            request_id=request_id,
            budget_event_id=str(event.get("event_id") or ""),
            event_type=event_type,
            source_event_id=source_event_id,
        )
        # The generic event is deliberately returned only after the typed host
        # transition, so callers and dispatcher output observe the stopped row.
        result["sprint"] = self.reader.show(reference)
        return result

    def _record_hard_stop(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        budget_event_id: str,
        event_type: str,
        source_event_id: str,
    ) -> None:
        """Record the state transition separately from the charge that caused it."""
        stop_request_id = request_id + ":budget-hard-stop"
        if self.audit.committed_event(stop_request_id) is not None:
            return
        sprint = self.reader.show(reference)
        event = self._event(
            "budget_hard_stopped",
            role,
            actor,
            reference,
            stop_request_id,
            {
                "reason": "budget_hard_limit",
                "budget_event_id": budget_event_id or None,
                "event_type": event_type,
                "source_event_id": source_event_id or None,
            },
            sprint,
        )
        self.audit.stage(stop_request_id, event)
        self._record("budget_hard_stopped", event)

    def resume(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        entry: dict[str, Any],
        request_id: str | None = None,
        delivery_id: str = "",
        through_event: str = "",
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "observer", "steward"})
        normalized = _resume(entry, required=True)
        assert normalized is not None
        delivery_id = delivery_id.strip()
        through_event = through_event.strip()
        if bool(delivery_id) != bool(through_event):
            raise TaskError(
                "validation",
                "resume delivery acknowledgement requires both delivery_id and through_event",
                2,
            )
        if (delivery_id or through_event) and role != "observer":
            raise TaskError("role_forbidden", "only an observer resume can acknowledge delivery", 3)
        # Guard whole resumes by sprint as acknowledgements move its event cursor.
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(
            role=role,
            actor=actor,
            reference=reference,
            request_id=request_id,
        )

        def mutation(sprint: dict[str, Any]) -> None:
            self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={"sprint_resume": json.dumps(normalized, separators=(",", ":"))},
            )
            self.client.call(
                "createComment",
                task_id=_sprint_number(sprint),
                user_id=0,
                content="[sprint:resume]\n" + normalized["selected_step"],
            )

        payload = {"fields": list(RESUME_FIELDS)}
        if delivery_id:
            payload.update({"delivery_id": delivery_id, "through_event": through_event})
        return self._write("resume_recorded", role, actor, reference, request_id, payload, mutation)

    def close(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        decisions: dict[str, list[dict[str, str]]] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Close a sprint on decisions its caller states, not on decisions inferred here.

        What became of every declared issue and every card still in a working state are both stated by
        the caller and checked before the transaction opens; a close short of a decision writes nothing
        and names what is missing.

        The whole close runs under the admission gate every opening of a sprint takes, and that is the
        invariant it exists for: between the moment this sprint is published `closed` and the moment its
        last disposition is written, no `sprint create` may be admitted on the projects this sprint
        reserved. A successor admitted in that window would re-reserve the project and its guard would
        refuse a disposition of the already-closed sprint. Every path through the close holds the gate,
        and it is taken outside the sprint's reference lock: admission first, then anything narrower.
        """
        self._role(role, {"po"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = {"role": role, "actor": actor, "reference": reference}
        with sprint_admission_lock(self.data_dir), self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                document, committed = self.transactions.existing(
                    request_id,
                    kind=SPRINT_CLOSED,
                    intent=intent,
                )
                if committed is not None:
                    return self._close_result(committed)
                if document is not None:
                    self._check_staged_decisions(document, decisions)
                if document is None:
                    from secretary.sprint_close import plan_close_decisions

                    sprint = self.reader.show(reference, include_cards=False)
                    targets = self._close_targets(sprint)
                    declared = [str(issue) for issue in sprint.get("issues") or []]
                    plan = plan_close_decisions(
                        decisions,
                        declared_issues=declared,
                        remaining=list(targets["remaining"]),
                        states=dict(targets["remaining_states"]),
                        issue_states=self._declared_issue_states(declared),
                    )
                    self._check_close_decisions_are_writable(plan)
                    event = self._event(
                        SPRINT_CLOSED,
                        role,
                        actor,
                        reference,
                        request_id,
                        {
                            "intent": intent,
                            "targets": targets,
                            "archived_tasks": [],
                            "remaining_tasks": list(targets["remaining"]),
                            "decisions": plan,
                            "closed_issues": [],
                            "moved_tasks": [],
                            "disposed_tasks": [],
                            "conflicts": [],
                        },
                        sprint,
                    )
                    document, committed = self.transactions.begin(
                        request_id,
                        kind=SPRINT_CLOSED,
                        intent=intent,
                        event=event,
                    )
                    if committed is not None:
                        return self._close_result(committed)
                    if document is None:
                        raise TaskError("audit_pending", "sprint close transaction claim is unavailable", 4)
                return self._run_close(document)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _issue_store(self) -> Any:
        from secretary.product_issues import ProductIssueStore

        return ProductIssueStore(self.client, data_dir=self.data_dir, instance=self.instance)

    def _declared_issue_states(self, declared: list[str]) -> dict[str, dict[str, Any]]:
        """What every declared issue actually is, for the decisions to be matched against.

        An installation without its instance directory cannot read Product/Issue records at all; a close
        that needs to write one is refused for that separately.
        """
        if self.instance is None:
            return {}
        store = self._issue_store()
        states: dict[str, dict[str, Any]] = {}
        for reference in declared:
            try:
                issue = store.show_issue(reference)
            except TaskError:
                continue
            states[reference] = {
                "closed": bool(issue.get("closed")),
                "close_reason": str(issue.get("close_reason") or ""),
            }
        return states

    def _close_step_status(self, request_id: str) -> str:
        """Whether one step of a close is done, half-written, or still to do.

        The only proof that a step happened is its own derived request id carrying a committed event. A
        pending event under that id is a step that reached the backend but not the journal and is
        finished by driving the same id again — never by looking at the object and finding it already
        in the shape the step wanted, which is somebody else's change as easily as this close's.
        """
        if self.audit.committed_event(request_id) is not None:
            return "done"
        if self.audit.pending_event(request_id) is not None:
            return "pending"
        return "todo"

    def _require_close_step_settled(self, request_id: str) -> None:
        """A step is only over once its own event is committed, whatever the backend shows."""
        if self._close_step_status(request_id) != "done":
            raise TaskError(
                "audit_pending",
                "sprint close step is pending repair; retry with the same request id",
                4,
            )

    def _close_conflict(
        self,
        document: dict[str, Any],
        payload: dict[str, Any],
        *,
        section: str,
        reference: str,
        verdict: str,
        actual: str,
        message: str,
    ) -> None:
        """Stop on somebody else's change, recoverably, naming what has to be decided again.

        The close neither invents a verdict nor treats the other writer's as its own: it records the
        conflict where the retry of this request id will find it, and refuses.
        """
        conflicts = payload.setdefault("conflicts", [])
        if isinstance(conflicts, list) and not any(
            isinstance(item, dict) and item.get("ref") == reference for item in conflicts
        ):
            conflicts.append(
                {"section": section, "ref": reference, "verdict": verdict, "actual": actual},
            )
            self.transactions.save(document)
        raise TaskError("close_conflict", message, 3)

    def _close_targets(self, sprint: dict[str, Any]) -> dict[str, Any]:
        """Freeze this close's task set before any archival write.

        A sprint that predates reservations is retained for recovery only: closing it can change its
        status but must not retrospectively archive arbitrary cards. The record-type filter stays — a
        Product or an Issue is not executable work — and the states of cards that are not done travel
        with the set, because the refusal has to name them.
        """
        if "reservations" not in sprint:
            return {"archive": [], "remaining": [], "remaining_states": {}}
        cards = TaskReader(self.client).list(sprint=str(sprint["ref"]))
        tasks = [card for card in cards if card.get("record_type") not in {"product", "issue"}]
        return {
            "archive": sorted(str(card["ref"]) for card in tasks if card.get("state") == "done"),
            "remaining": sorted(str(card["ref"]) for card in tasks if card.get("state") != "done"),
            "remaining_states": {
                str(card["ref"]): str(card.get("state") or "unknown")
                for card in tasks
                if card.get("state") != "done"
            },
        }

    def _check_staged_decisions(
        self,
        document: dict[str, Any],
        decisions: dict[str, list[dict[str, str]]] | None,
    ) -> None:
        """A retry of a staged close carries the decisions that close was staged with.

        A second delivery under the same request id that states something else is refused rather than
        silently answered with the first one's verdicts; a retry that repeats no decisions at all is
        the ordinary recovery call. The one amendment a retry may carry is the answer to a conflict
        this close stopped on, for exactly the refs it recorded.
        """
        if decisions is None:
            return
        payload = (document.get("event") or {}).get("payload") or {}
        staged = payload.get("decisions")
        if not isinstance(staged, dict):
            return
        sections = ("issues", "cards")
        offered = {
            section: sorted(list(decisions.get(section) or []), key=lambda entry: entry.get("ref", ""))
            for section in sections
        }
        current = {section: list(staged.get(section) or []) for section in sections}
        if offered == current:
            return
        from secretary.sprint_close import ALREADY_CLOSED, ALREADY_MOVED

        confirmation = {"issues": ALREADY_CLOSED, "cards": ALREADY_MOVED}
        conflicts = {
            str(item.get("ref")): item for item in (payload.get("conflicts") or []) if isinstance(item, dict)
        }
        amended: list[dict[str, Any]] = []
        for section in sections:
            if len(offered[section]) != len(current[section]):
                self._refuse_restated_decisions()
            for was, now in zip(current[section], offered[section]):
                if was == now:
                    continue
                conflict = conflicts.get(str(now.get("ref")))
                if (
                    conflict is None
                    or was.get("ref") != now.get("ref")
                    or conflict.get("section") != section
                    or now.get("verdict") != confirmation[section]
                    or now.get("actual") != conflict.get("actual")
                ):
                    self._refuse_restated_decisions()
                amended.append(conflict)
        if not amended:
            self._refuse_restated_decisions()
        staged.update(offered)
        payload["conflicts"] = [item for item in (payload.get("conflicts") or []) if item not in amended]
        self.transactions.save(document)

    def _refuse_restated_decisions(self) -> None:
        raise TaskError(
            "validation",
            "this request id was staged with other decisions; retry it with the same file",
            2,
        )

    def _check_close_decisions_are_writable(self, plan: dict[str, list[dict[str, str]]]) -> None:
        """Refuse a plan this installation cannot perform, before the transaction opens.

        Closing an issue writes durable Product/Issue records addressed through the installation
        directory, and archiving a card whose head is still running is refused by the archive itself.
        Asked here, either refusal names the card and leaves the sprint open; asked halfway through the
        close, it would be a transaction to repair.
        """
        from secretary.sprint_close import ALREADY_CLOSED, KEEP_OPEN

        # A verdict that writes nothing to the issue needs nothing of the installation either.
        closing = [entry for entry in plan["issues"] if entry["verdict"] not in {KEEP_OPEN, ALREADY_CLOSED}]
        if closing and self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        if not plan["cards"]:
            return
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        live = []
        for entry in plan["cards"]:
            try:
                writer._check_dispatcher_archivable(entry["ref"])
            except TaskError as exc:
                if exc.code != "live_work":
                    raise
                live.append(entry["ref"])
        if live:
            raise TaskError(
                "live_work",
                "sprint close cannot dispose of card(s) whose dispatcher work is still live; "
                "settle them first: " + ", ".join(live),
                3,
            )

    def _run_close(self, document: dict[str, Any]) -> dict[str, Any]:
        event = document.get("event")
        if not isinstance(event, dict):
            raise TaskError("audit_pending", "sprint close transaction has no audit event", 4)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise TaskError("audit_pending", "sprint close transaction has no payload", 4)
        targets = payload.get("targets")
        if not isinstance(targets, dict):
            raise TaskError("audit_pending", "sprint close transaction has no task targets", 4)
        archive = targets.get("archive")
        if not isinstance(archive, list) or not all(isinstance(ref, str) for ref in archive):
            raise TaskError("audit_pending", "sprint close transaction has invalid archival targets", 4)
        archived = payload.setdefault("archived_tasks", [])
        if not isinstance(archived, list) or not all(isinstance(ref, str) for ref in archived):
            raise TaskError("audit_pending", "sprint close transaction has invalid archival progress", 4)
        decisions = payload.get("decisions")
        if decisions is not None and not isinstance(decisions, dict):
            raise TaskError("audit_pending", "sprint close transaction has invalid decisions", 4)
        try:
            self._close_declared_issues(document, event, payload, decisions or {})
            writer = TaskWriter(self.client, data_dir=self.data_dir) if archive else None
            for task_ref in archive:
                step_request_id = _close_archive_request_id(str(document["request_id"]), task_ref)
                # The payload says what this close has recorded; the step's own request id says
                # what it has performed, and only the second one may end a step.
                if self._close_step_status(step_request_id) != "done":
                    assert writer is not None
                    writer.archive(
                        role="po",
                        actor=str(document["intent"]["actor"]),
                        reference=task_ref,
                        reason=f"archived when sprint {event['ref']} closed",
                        request_id=step_request_id,
                    )
                    self._require_close_step_settled(step_request_id)
                if task_ref not in archived:
                    archived.append(task_ref)
                    self.transactions.save(document)
            # Publish closed last so unfinished closes retain project reservations.
            self._dispose_remaining_cards(document, event, payload, decisions or {})
            sprint = self.reader.show(str(event["ref"]), include_cards=False)
            typed_request_id = str(document["request_id"]) + ":typed-close"
            typed_pending = self.audit.pending_event(typed_request_id)
            if sprint["status"] != "closed" or (
                typed_pending is not None and typed_pending.get("record_type") == "board.protocol_event"
            ):
                document.setdefault("progress", {})["status_started"] = True
                self.transactions.save(document)
                self._transition_host(
                    role=str(document["intent"]["role"]),
                    actor=str(document["intent"]["actor"]),
                    reference=str(event["ref"]),
                    target="closed",
                    reason="Sprint closed",
                    request_id=typed_request_id,
                )
            document.setdefault("progress", {})["status_done"] = True
            self.transactions.save(document)
            self.transactions.complete(document)
        except TaskError as exc:
            if exc.code == "close_conflict":
                # Recoverable by construction: the conflict is on the record, and the retry of
                # this request id may answer exactly it. Saying so is the point of the refusal.
                raise
            if exc.code in {
                "validation",
                "closed",
                "not_found",
                "transition_forbidden",
                "live_work",
                "role_forbidden",
            } and not _close_progressed(document, payload):
                self.transactions.discard(document)
                raise
            raise TaskError(
                "audit_pending", "sprint close is pending repair; retry with the same request id", 4
            ) from None
        except (OSError, KeyError, TypeError):
            raise TaskError(
                "audit_pending", "sprint close is pending repair; retry with the same request id", 4
            ) from None
        update_active_sprint_projects(self.data_dir, self.reader.show(str(event["ref"]), include_cards=False))
        return self._close_result(event)

    def _close_declared_issues(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: dict[str, Any],
    ) -> None:
        """Perform the closing verdicts, one issue at a time, through the close's progress.

        An issue left open, or confirmed `already_closed`, is performed by doing nothing to it: its
        basis is already in this close's payload. Whether a closing verdict still has work is answered
        by the step's own derived request id and never by the issue looking closed — an issue closed
        with no committed step of ours is somebody else's close, and this close stops on it.
        """
        from secretary.sprint_close import ALREADY_CLOSED, KEEP_OPEN

        verdicts = decisions.get("issues")
        if not isinstance(verdicts, list):
            return
        closed = payload.setdefault("closed_issues", [])
        if not isinstance(closed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid issue progress", 4)
        pending = [
            entry
            for entry in verdicts
            if isinstance(entry, dict)
            and entry.get("verdict") not in {KEEP_OPEN, ALREADY_CLOSED}
            and entry.get("ref") not in closed
        ]
        if not pending:
            return
        if self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        store = self._issue_store()
        # Persist the close plan before its first issue write.
        document.setdefault("progress", {})["issues_started"] = True
        self.transactions.save(document)
        for entry in pending:
            reference = str(entry["ref"])
            step_request_id = _close_step_request_id(str(document["request_id"]), "issue", reference)
            status = self._close_step_status(step_request_id)
            if status != "done":
                current = store.show_issue(reference)
                if status == "todo" and current.get("closed"):
                    carried = str(current.get("close_reason") or "unknown")
                    self._close_conflict(
                        document,
                        payload,
                        section="issues",
                        reference=reference,
                        verdict=str(entry["verdict"]),
                        actual=carried,
                        message=(
                            f"issue {reference} was closed as {carried} by somebody else, and this "
                            f"close states {entry['verdict']}; retry with that decision amended to "
                            f"already_closed naming {carried}"
                        ),
                    )
                # A pending step is this close's own half-written one, and driving the same
                # derived request id again is what finishes it.
                store.close_issue(
                    reference=reference,
                    reason=str(entry["verdict"]),
                    actor=str(document["intent"]["actor"]),
                    request_id=step_request_id,
                )
                self._require_close_step_settled(step_request_id)
            closed.append(reference)
            self.transactions.save(document)

    def _dispose_remaining_cards(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: dict[str, Any],
    ) -> None:
        """Take every card that was not done into the recorded end its disposition names.

        Each disposition is two backend writes — the board state, then the archival — and each is a
        step under its own derived request id. A `drop` passes through Ready deliberately: it is the
        released edge that releases a retained worker, and a card still holding a claim cannot be
        archived at all.

        Whether a card needs the move is answered by the state this close froze into its plan, not by
        the board now; whether the move has happened is answered by the step's own request id.
        """
        from secretary.sprint_close import ALREADY_MOVED, DISPOSITION_TARGETS

        dispositions = decisions.get("cards")
        if not isinstance(dispositions, list) or not dispositions:
            return
        moved = payload.setdefault("moved_tasks", [])
        disposed = payload.setdefault("disposed_tasks", [])
        if not isinstance(moved, list) or not isinstance(disposed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid disposition progress", 4)
        planned = (payload.get("targets") or {}).get("remaining_states") or {}
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        reader = TaskReader(self.client)
        actor = str(document["intent"]["actor"])
        for entry in dispositions:
            if not isinstance(entry, dict):
                raise TaskError("audit_pending", "sprint close transaction has invalid dispositions", 4)
            reference = str(entry["ref"])
            verdict = str(entry["verdict"])
            reason = str(entry["reason"])
            # A confirmed card is where it needs to be already, and it was said so explicitly.
            target = "" if verdict == ALREADY_MOVED else DISPOSITION_TARGETS[verdict]
            if target and str(planned.get(reference) or "") != target:
                move_request_id = _close_step_request_id(
                    str(document["request_id"]),
                    "dispose-move",
                    reference,
                )
                status = self._close_step_status(move_request_id)
                if status != "done":
                    if status == "todo" and reader.show(reference)["state"] == target:
                        self._close_conflict(
                            document,
                            payload,
                            section="cards",
                            reference=reference,
                            verdict=verdict,
                            actual=target,
                            message=(
                                f"card {reference} was moved to {target} by somebody else, not by "
                                f"this close; retry with that disposition amended to already_moved "
                                f"naming {target}"
                            ),
                        )
                    writer.move(
                        role="po",
                        actor=actor,
                        reference=reference,
                        target=target,
                        reason=f"{verdict} when sprint {event['ref']} closed: {reason}",
                        sprint_override=True,
                        sprint_override_reason=f"disposed by the close of {event['ref']}: {reason}",
                        request_id=move_request_id,
                    )
                    self._require_close_step_settled(move_request_id)
            if reference not in moved:
                moved.append(reference)
                self.transactions.save(document)
            archive_request_id = _close_step_request_id(
                str(document["request_id"]),
                "dispose-archive",
                reference,
            )
            if self._close_step_status(archive_request_id) != "done":
                writer.archive(
                    role="po",
                    actor=actor,
                    reference=reference,
                    reason=f"archived when sprint {event['ref']} closed: {reason}",
                    request_id=archive_request_id,
                )
                self._require_close_step_settled(archive_request_id)
            if reference not in disposed:
                disposed.append(reference)
                self.transactions.save(document)

    def _close_result(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
        return {
            "action": SPRINT_CLOSED,
            "sprint": self.reader.show(str(event["ref"])),
            "event_id": str(event["event_id"]),
            "archived_tasks": list(payload.get("archived_tasks") or []),
            "remaining_tasks": list(payload.get("remaining_tasks") or []),
            "issue_decisions": list(decisions.get("issues") or []),
            "closed_issues": list(payload.get("closed_issues") or []),
            "card_dispositions": list(decisions.get("cards") or []),
            "disposed_tasks": list(payload.get("disposed_tasks") or []),
        }

    def reopen(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reopen a sprint that still satisfies every rule for an open sprint.

        The other transition into `open`, so it runs the same admission order as `create` on the same
        staged-intent primitive. The observer is decided again here and is never inherited: what the row
        carries is either the value of the run that closed or migration provenance, and neither is a
        decision about the run being opened now. It is written while the sprint is still closed, so the
        row is never readable open under a value the reopening caller did not choose.
        """
        self._role(role, {"po"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = {
            "role": role,
            "actor": actor,
            "reference": reference,
            "observer": self._observer_intent(observer, executable=True),
        }
        with sprint_admission_lock(self.data_dir):
            document, committed = self.transactions.existing(request_id, kind=SPRINT_REOPENED, intent=intent)
            if committed is not None:
                return self._committed_result(SPRINT_REOPENED, committed)
            if document is None:
                sprint = self.reader.show(reference, include_cards=False)
                self._check_reopen(sprint, reference, intent["observer"])
                event = self._event(
                    SPRINT_REOPENED,
                    role,
                    actor,
                    reference,
                    request_id,
                    {"intent": intent},
                    sprint,
                )
                document, committed = self.transactions.begin(
                    request_id, kind=SPRINT_REOPENED, intent=intent, event=event
                )
                if committed is not None:
                    return self._committed_result(SPRINT_REOPENED, committed)
                if document is None:
                    raise TaskError("audit_pending", "sprint transaction claim is unavailable", 4)
            return self._run_reopen(document)

    def _check_reopen(
        self,
        sprint: dict[str, Any],
        reference: str,
        observer: dict[str, Any] | None,
    ) -> None:
        """Every rule an open sprint has to satisfy, read live before any write.

        The candidate is the row as it stands, under the observer this reopen declares rather than the
        one the closed row happens to carry.
        """
        missing = [
            name
            for name, value in (
                ("product", sprint.get("product")),
                ("issues", sprint.get("issues")),
                ("reservations", sprint.get("reservations")),
            )
            if not value
        ]
        if missing:
            raise TaskError(
                "validation",
                f"sprint {reference} predates sprint ownership and has no "
                + ", ".join(missing)
                + "; open a new sprint that owns its issues instead of reopening it",
                2,
            )
        reservations = [str(project) for project in sprint.get("reservations") or []]
        self._check_ownership(
            str(sprint.get("product") or ""),
            [str(issue) for issue in sprint.get("issues") or []],
            reservations,
        )
        self._check_conflicts(dict(sprint) | {"observer": observer}, excluding=reference)

    def _run_reopen(self, document: dict[str, Any]) -> dict[str, Any]:
        """Drive the staged reopen to its single audit event, or leave it repairable."""
        reference = str(document["intent"]["reference"])
        try:
            sprint = self.reader.show(reference, include_cards=False)
            if not (document.get("progress") or {}).get("opened_done"):
                # A staged reopen held nothing while it waited for its repeat, so the
                # installation is measured again before this sprint becomes the open one.
                self._check_conflicts(
                    dict(sprint) | {"observer": document["intent"]["observer"]},
                    excluding=reference,
                )
            # The value the row carries now, recorded durably before the write that replaces
            # it: a reopen refused on a later attempt has to put back what it found, and by
            # then the row already carries the value this reopen wrote.
            self._record_observer_preimage(document, sprint)
            # Observer first, status second, and each step is recorded durably: a reopen that
            # dies between them leaves a still-closed row already carrying its fresh choice,
            # and the repeat finds that step done rather than writing it twice.
            document.setdefault("progress", {})["observer_started"] = True
            self.transactions.save(document)
            self._transition_host(
                role=str(document["intent"]["role"]),
                actor=str(document["intent"]["actor"]),
                reference=reference,
                target="open",
                reason="Sprint reopened",
                request_id=str(document["request_id"]) + ":typed-reopen",
                observer=encode_observer(document["intent"]["observer"]),
            )
            document.setdefault("progress", {})["observer_done"] = True
            document.setdefault("progress", {})["opened_done"] = True
            self.transactions.save(document)
            sprint = self.reader.show(reference)
            event = document["event"]
            event["task_id"] = sprint["id"]
            event["backend"]["revision"] = "updated_at:" + str(sprint["audit"]["updated_at"] or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint)
            return {"action": SPRINT_REOPENED, "sprint": sprint, "event_id": str(event["event_id"])}
        except TaskError as exc:
            answer = exc.code in _ADMISSION_REFUSALS or (
                exc.code in {"validation", "role_forbidden"} and not document.get("progress")
            )
            # The refusal is only this request's answer once the row is back the way it was
            # found and nothing is left staged.  A rollback that could not be written back
            # leaves the observer this attempt wrote on the row, so the caller is told the
            # request is repairable under the same request id rather than refused.
            if answer and self._compensate_reopen(document, reference):
                raise
            raise TaskError(
                "audit_pending",
                "sprint reopen is pending repair; retry with the same request id",
                4,
            ) from None
        except (OSError, KeyError, TypeError):
            raise TaskError(
                "audit_pending",
                "sprint reopen is pending repair; retry with the same request id",
                4,
            ) from None

    def _record_observer_preimage(self, document: dict[str, Any], sprint: dict[str, Any]) -> None:
        """Record what the row's observer was, once, before this reopen writes over it.

        A row that carries no value has no preimage; its absence is recorded as such, so a rollback
        knows it cannot put the row back and leaves the reopen repairable instead.
        """
        progress = document.setdefault("progress", {})
        if "observer_preimage" in progress:
            return
        current = sprint.get("observer") if "observer" in sprint else None
        try:
            progress["observer_preimage"] = encode_observer(current) if current else None
        except ValueError:
            progress["observer_preimage"] = None
        self.transactions.save(document)

    def _compensate_reopen(self, document: dict[str, Any], reference: str) -> bool:
        """Undo a refused reopen's observer write and drop its intent.

        This request is over, so it must leave the row exactly as it found it. Anything it cannot undo
        leaves the intent in place, because then something of this request does still exist. Returns
        whether the row and the journal are back the way this reopen found them.
        """
        progress = document.get("progress") or {}
        if progress.get("opened_done"):
            return False
        try:
            sprint = self.reader.show(reference, include_cards=False)
            # The status is read rather than taken from the staged steps: a step recorded as
            # started proves an attempt, not a write, and only a row still not open is one
            # this refusal may put back.
            if str(sprint.get("status") or "") == "open":
                return False
            if progress.get("observer_started") or progress.get("observer_done"):
                preimage = progress.get("observer_preimage")
                if not isinstance(preimage, str):
                    return False
                if (
                    self.client.call(
                        "saveTaskMetadata",
                        task_id=_sprint_number(sprint),
                        values={OBSERVER_FIELD: preimage},
                    )
                    is not True
                ):
                    return False
        except (TaskError, OSError, KeyError, TypeError):
            return False
        document["progress"] = {}
        self.transactions.save(document)
        self.transactions.discard(document)
        return True

    def restore(
        self, *, reference: str, values: dict[str, str], request_id: str | None = None
    ) -> dict[str, Any]:
        """Rewrite one sprint entity's fields verbatim from a checkpoint export.

        Not a sprint mutation an operator makes, so it is not refused on status the way `comment` or
        `resume` are.
        """
        unknown = sorted(set(values) - SPRINT_METADATA)
        if unknown:
            raise TaskError("validation", "restore carries unknown sprint fields: " + ", ".join(unknown), 2)

        def mutation(sprint: dict[str, Any]) -> None:
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=dict(values))

        return self._write(
            "restored", "steward", "restore", reference, request_id, {"fields": sorted(values)}, mutation
        )

    def restore_comment(
        self, *, reference: str, body: str, occurrence: int, request_id: str | None = None
    ) -> dict[str, Any]:
        """Append one exported record back to the entity, verbatim."""
        return self._write(
            "restored_comment",
            "steward",
            "restore",
            reference,
            request_id,
            {"body_sha256": _digest(body), "restore_occurrence": occurrence},
            lambda sprint: self.client.call(
                "createComment", task_id=_sprint_number(sprint), user_id=0, content=body
            ),
        )

    def _guard_observer_identity(self, *, role: str, actor: str, reference: str, request_id: str) -> None:
        """Refuse a sprint write of role `observer` that is not about the caller's own sprint.

        The card guard's counterpart on the entity side, with the same two codes and the same
        fail-closed rule: a head that names no sprint cannot be authenticated at all. The refusal is
        audited under its own request id so it neither consumes the operation's retry key nor is
        recorded twice on a retry.
        """
        if role != "observer":
            return
        from secretary.role_env import declared_observer_sprint
        from secretary.tasks import _sprint_guard_denial_request_id

        declared = declared_observer_sprint()
        if declared == reference:
            return
        code, message = (
            (
                "observer_identity_unbound",
                "this observer names no sprint, so its writes cannot be authenticated; "
                "it has to be launched by the dispatcher for one sprint",
            )
            if not declared
            else (
                "observer_sprint_mismatch",
                f"this observer belongs to sprint {declared}, not to {reference}",
            )
        )
        denial_request_id = _sprint_guard_denial_request_id(request_id)
        event = self.audit.committed_event(denial_request_id)
        if event is None:
            event = self._event(
                "sprint_guard_denied",
                role,
                actor,
                reference,
                denial_request_id,
                {
                    "code": code,
                    "message": message,
                    "project": "",
                    "sprint": declared,
                    "operation_request_id": request_id,
                },
            )
            event["outcome"] = "denied"
            event["backend"]["revision"] = "not_written"
            self.audit.stage(denial_request_id, event)
            try:
                self.audit.append(denial_request_id, event)
            except OSError:
                raise TaskError(
                    "audit_pending",
                    "sprint write was denied but audit repair is required",
                    4,
                ) from None
        payload = event.get("payload") if isinstance(event, dict) else {}
        raise TaskError(str(payload.get("code") or code), str(payload.get("message") or message), 3)

    def _write(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None,
        payload: dict[str, Any],
        mutation: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            return self._committed(kind, committed)
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            if pending.get("record_type") == "board.protocol_event":
                raise TaskError(
                    "validation",
                    "request id belongs to a typed Sprint lifecycle occurrence; retry its protocol recovery",
                    2,
                )
            return self._pending(kind, pending)
        sprint = self.reader.show(reference)
        if sprint["status"] in {"closed", "stopped"} and kind in {
            "commented",
            "current_task_set",
            "resume_recorded",
        }:
            raise TaskError("closed", "sprint is closed", 3)
        event = self._event(kind, role, actor, reference, request_id, payload, sprint)
        self.audit.stage(request_id, event)
        try:
            mutation(sprint)
        except Exception:
            self.audit.discard(request_id)
            raise
        return self._record(kind, event)

    def _record(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        sprint = self.reader.show(str(event["ref"]))
        event["task_id"] = sprint["id"]
        event["backend"]["revision"] = "updated_at:" + str(sprint["audit"]["updated_at"] or "unknown")
        request_id = str(event["request_id"])
        self.audit.stage(request_id, event)
        event_id = self.audit.append(request_id, event)
        update_active_sprint_projects(Path(self.audit.board_dir).parent, sprint)
        return {"action": kind, "sprint": sprint, "event_id": event_id}

    def _committed(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": kind,
            "sprint": self.reader.show(str(event["ref"])),
            "event_id": self.audit.append(str(event["request_id"]), event),
        }

    def _pending(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        # The staged event is only retained after a successful backend mutation in the
        # simple writes. Creation stages its Kanboard id before assigning metadata.
        sprint = self.reader.show(str(event["ref"]))
        event["task_id"] = sprint["id"]
        event["backend"]["revision"] = "updated_at:" + str(sprint["audit"]["updated_at"] or "unknown")
        self.audit.stage(str(event["request_id"]), event)
        event_id = self.audit.append(str(event["request_id"]), event)
        update_active_sprint_projects(Path(self.audit.board_dir).parent, sprint)
        return {"action": kind, "sprint": sprint, "event_id": event_id}

    @staticmethod
    def _event(
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        payload: dict[str, Any],
        sprint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": kind,
            "outcome": "success",
            "task_id": sprint["id"] if sprint else "",
            "ref": reference,
            "backend": {
                "kind": "kanboard",
                "task_id": _sprint_number(sprint) if sprint else None,
                "revision": "pending",
            },
            "request_id": request_id,
            "payload": payload,
        }

    @staticmethod
    def _role(role: str, allowed: set[str]) -> None:
        if role not in allowed:
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)


def _sprint_number(sprint: dict[str, Any] | None) -> int:
    number = _positive_int(str((sprint or {}).get("id", "")).removeprefix("sprint_kanboard_"))
    if number is None:
        raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
    return number


def _is_sprint_row(raw: Any) -> bool:
    """A row on the sprint board becomes a sprint when it carries a sprint reference.

    A create writes the reference after the fields the sprint was admitted with, so a row still
    without one is an unfinished create its own staged transaction repairs.
    """
    return isinstance(raw, dict) and str(raw.get("reference") or "").startswith(SPRINT_REFERENCE_PREFIX)


def _create_marker(request_id: str) -> str:
    return "[secretary-sprint-transaction:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest() + "]"


def _close_archive_request_id(request_id: str, reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"{request_id}:sprint-close-archive:{digest}"


_CLOSE_PROGRESS_STEPS = (
    "closed_issues",
    "archived_tasks",
    "moved_tasks",
    "disposed_tasks",
    "conflicts",
)


def _close_progressed(document: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Whether this close has already performed a step its retry has to continue.

    Once any step is on the record, the document is what the retry recovers from, and discarding it
    would lose both the work already done and the plan that says what is left.
    """
    if document.get("progress"):
        return True
    return any(payload.get(step) for step in _CLOSE_PROGRESS_STEPS)


def _close_step_request_id(request_id: str, step: str, reference: str) -> str:
    """One derived id per step of a close, so a retry replays it instead of repeating it."""
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"{request_id}:sprint-close-{step}:{digest}"


def canonical_repository_roots(paths: list[Any]) -> list[str]:
    """The absolute roots a declaration means, resolved where the caller declared them.

    A relative root only names a tree next to the process that wrote it, so it is resolved once,
    here, at declaration time. Resolving it later would answer against whichever process runs the
    check, and two sprints sharing a tree would read as disjoint. A root this host cannot resolve
    at all is refused rather than guessed at.
    """
    roots: list[str] = []
    for raw in paths:
        text = str(raw).strip()
        if not text:
            continue
        try:
            root = str(Path(text).expanduser().resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise TaskError(
                "validation",
                f"repository root {text!r} cannot be resolved on this host ({exc}); "
                "declare a repository root this host can name",
                2,
            ) from None
        if root not in roots:
            roots.append(root)
    return roots


def _scanned_roots(paths: list[Any], *, refusal: Callable[[str, str], TaskError]) -> list[Path]:
    """The stored roots of one sprint being scanned, or a refusal naming the bad value.

    Fails closed on anything that is not already absolute: such a value is canonical to no host,
    and resolving it here is exactly how two sprints sharing a working tree read as disjoint.
    """
    roots: list[Path] = []
    for raw in paths:
        text = str(raw).strip()
        if not text:
            continue
        root = Path(text)
        if not root.is_absolute():
            raise refusal(text, "is not an absolute path")
        try:
            root = root.resolve()
        except (OSError, RuntimeError, ValueError):
            raise refusal(text, "cannot be resolved on this host") from None
        if root not in roots:
            roots.append(root)
    return roots


def _roots_overlap(left: Path, right: Path) -> bool:
    """Whether two canonical roots name one tree, which includes one nested in the other."""
    return left == right or left in right.parents or right in left.parents


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _resume_lag_seconds(recorded_at: str, last_event_at: str) -> int | None:
    """Seconds a resume trails its latest linked-card event, or None for bad timestamps."""
    if not recorded_at or not last_event_at:
        return 0
    recorded = _timestamp(recorded_at)
    event = _timestamp(last_event_at)
    if recorded is None or event is None:
        return None
    return max(0, int((event - recorded).total_seconds()))


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _ownership(meta: dict[str, str]) -> dict[str, Any]:
    """The sprint's product, issues and reservations, only where the row has them.

    A sprint created before ownership existed carries none of the three keys. A reader answering
    `""` and `[]` for it would put values on the entity nobody wrote, and the next checkpoint would
    store them as if they were chosen.
    """
    result: dict[str, Any] = {}
    if "sprint_product" in meta:
        result["product"] = meta["sprint_product"]
    if "sprint_issues" in meta:
        result["issues"] = _json_list(meta["sprint_issues"])
    if "sprint_reservations" in meta:
        result["reservations"] = _json_list(meta["sprint_reservations"])
    return result


def _observer(meta: dict[str, str]) -> dict[str, Any]:
    """The sprint's declared observer, only where the row carries the field.

    Three states stay apart, because their repairs differ: the key is missing, the key holds one of
    the four tagged forms, or the key holds something else. The last is reported as `None` rather
    than dropped, so a corrupt row does not pass for one that carries nothing.
    """
    if OBSERVER_FIELD not in meta:
        return {}
    return {"observer": parse_observer(meta[OBSERVER_FIELD])}


def _json_list(value: str | None) -> list[str]:
    try:
        raw = json.loads(value or "[]")
    except ValueError:
        return []
    return _unique_strings(raw) if isinstance(raw, list) else []


def _budget(
    value: Any = None,
    thresholds: dict[str, int] | None = None,
    uncharged: Any = None,
) -> dict[str, Any]:
    """The normalized budget: charged counts that move the thresholds, uncharged counts beside them.

    `uncharged` is the separately stored quantity; where it is not given, an already normalized
    budget passed as `value` carries its own. Both families default to zero for every type, so a
    sprint stored before a type existed reads as zero rather than as an error.
    """
    source = value if isinstance(value, dict) else {}
    if isinstance(value, str):
        try:
            source = json.loads(value)
        except ValueError:
            source = {}
    by_type = source.get("by_type") if isinstance(source, dict) else {}
    counts = {event_type: _budget_count(by_type, event_type) for event_type in BUDGET_EVENT_TYPES}
    if uncharged is None:
        uncharged = source.get("uncharged") if isinstance(source, dict) else {}
    if isinstance(uncharged, str):
        try:
            uncharged = json.loads(uncharged)
        except ValueError:
            uncharged = {}
    spare = {event_type: _budget_count(uncharged, event_type) for event_type in BUDGET_UNCHARGED_EVENT_TYPES}
    limits = thresholds or budget_thresholds()
    # Deliberately only the charged counts: an uncharged outcome is visible, and moves nothing.
    total = sum(counts.values())
    return {
        "total": total,
        "by_type": counts,
        "uncharged": spare,
        "thresholds": limits,
        "signal_reached": total >= limits["signal"],
        "hard_reached": total >= limits["hard"],
    }


def _budget_count(counts: Any, event_type: str) -> int:
    if not isinstance(counts, dict):
        return 0
    try:
        return max(0, int(counts.get(event_type, 0)))
    except (TypeError, ValueError):
        return 0


def _budget_json(budget: dict[str, Any]) -> str:
    """The `sprint_budget` field's value. The uncharged counts have their own field and stay out."""
    return json.dumps(
        {key: value for key, value in budget.items() if key != "uncharged"}, separators=(",", ":")
    )


def _source_audit(value: Any) -> dict[str, str] | None:
    """The audit metadata a restored sprint was recreated from, when it has one."""
    source = value
    if isinstance(value, str):
        try:
            source = json.loads(value or "null")
        except ValueError:
            return None
    if not isinstance(source, dict):
        return None
    result = {field: _text(source.get(field)) for field in SOURCE_AUDIT_FIELDS}
    return result if any(result.values()) else None


def _resume(value: Any, *, required: bool = False) -> dict[str, Any] | None:
    source = value
    if isinstance(value, str):
        try:
            source = json.loads(value)
        except ValueError:
            source = None
    if not isinstance(source, dict):
        if required:
            raise TaskError("validation", "resume entry must be a JSON object", 2)
        return None
    missing = [
        field
        for field in RESUME_FIELDS
        if not isinstance(source.get(field), str) or not source[field].strip()
    ]
    if missing:
        if required:
            raise TaskError("validation", "resume entry is missing required fields: " + ", ".join(missing), 2)
        return None
    recorded_at = _text(source.get("recorded_at")) or _now()
    if required and _timestamp(recorded_at) is None:
        raise TaskError("validation", "resume recorded_at must include a timezone", 2)
    return {**{field: source[field].strip() for field in RESUME_FIELDS}, "recorded_at": recorded_at}
