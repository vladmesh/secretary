"""Sprint entities stored as tasks on a dedicated Kanboard board."""

from __future__ import annotations

import fcntl
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from secretary.sprint_observer import (
    KIND_HEAD,
    KIND_NONE,
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
    is_significant_card_event,
    _digest,
    _now,
    _positive_int,
    _rfc3339,
    _text,
)


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
    "sprint_current_task",
    "sprint_resume",
    "sprint_source_audit",
    "sprint_observer",
}
SOURCE_AUDIT_FIELDS = ("created_at", "updated_at", "board")
BUDGET_EVENT_TYPES = (
    "red_review",
    "blocked",
    "red_ci",
    "preempt",
    "recreated_task",
    "hotfix",
)
DEFAULT_BUDGET_SIGNAL = 3
DEFAULT_BUDGET_HARD = 6
# How many sprints an installation may hold open at once.  One is what this product
# has always been; two is the pilot, and it is opt-in per installation.
DEFAULT_OPEN_SPRINT_LIMIT = 1
MAX_OPEN_SPRINT_LIMIT = 2
# An observer gets a short window to durably reflect a card transition.  Freshness is
# based on the transition itself, rather than the time a status reader happens to run,
# so a timely resume stays fresh until another meaningful transition occurs.
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
# Version 2 keys the index by reserved project instead of repository path.  The card
# guards ask about a card's project, and `repositories` are filesystem paths, so a
# version 1 index answers a question nobody asks; it is rebuilt rather than read.
_GUARD_INDEX_VERSION = 2
_ADMISSION_LOCK = "sprints/admission.lock"
SPRINT_CREATED = "created"
SPRINT_REOPENED = "reopened"
SPRINT_CLOSED = "closed"
SPRINT_STATUSES = {"open", "closed", "stopped"}
# An admitted create that the backend refused holds nothing: it compensates its own row
# and re-checks the installation before it publishes.  These are the refusals of that
# re-check, and they are answers to the caller rather than a pending repair.
_ADMISSION_REFUSALS = {"sprint_conflict", "resource_conflict"}


def active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """Return the local index of projects reserved by open sprints.

    This is an index, not a second sprint model.  Task writes use it to avoid a
    sprint-board read for projects that no open sprint reserves; a listed ref is
    always checked live before it authorizes a write.  It is keyed by project id
    because that is what a card carries; a sprint's `repositories` are filesystem
    paths and answer a different question.
    """
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
            # An index of an older key space cannot be updated one sprint at a time:
            # the entries of the other open sprints are not in this call.  Dropping it
            # leaves the next guarded write to rebuild it from the board.
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

    The rules for opening a sprint are reads of live state, so two writers that both
    check before either writes would each see no open sprint and both create one.
    Holding this lock across the check and the backend write makes the admission one
    transition per installation; it is a gate on the data directory, not a second
    sprint model, and it holds nothing after the write.
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
            sort_keys=True, separators=(",", ":"),
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

    An absent setting, an unreadable one and an installation with no config at all all
    answer one, which is the behaviour every installation has today.  This answers
    rather than raises, because a malformed value must not be able to stop admission
    either; `open_sprint_limit_invalid` is what tells the operator the value was refused.
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

    An installation nobody named, one whose config cannot be read and one that never set
    the value are the same answer: the singleton limit.  Nothing here can widen the
    limit, which is what makes a malformed setting harmless.
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

    Admission judges one candidate against the sprints already open, so a whole set is
    judged by admitting it one row at a time, in reference order, against the rows
    already accepted.  Recovery is the caller: an export names a set nobody is admitting
    row by row, and it must not be a way to arrive at a pair of open sprints that
    `create` would have refused.
    """
    admitted: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda row: str(row.get("ref") or "")):
        try:
            _refuse_open_sprint(row, admitted, limit=limit)
        except TaskError as exc:
            return f"{row.get('ref') or '?'}: {exc.message}"
        admitted.append(row)
    return None


def _refuse_open_sprint(
    candidate: dict[str, Any], others: list[dict[str, Any]], *, limit: int
) -> None:
    """Refuse a sprint this installation has no room, or no disjoint room, for.

    Every collision the caller can act on is reported before the generic count refusal,
    because the caller of a colliding project, product or repository has to see which
    resource it is, not only that some sprint is open.

    Above the singleton limit an installation already holding its limit is the other way
    round: nothing about the candidate's resources can admit it, so it is told the count
    it collided with rather than a resource it might try to move.  At the singleton limit
    the order is the one every installation prints today, where the reservation refusal
    predates the limit and reads the same either way.
    """
    saturated = len(others) >= limit
    if saturated and limit > DEFAULT_OPEN_SPRINT_LIMIT:
        raise _open_sprint_count_error(others, limit)
    _refuse_shared_reservations(
        [str(project) for project in candidate.get("reservations") or []], others,
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

    Two open sprints may only exist while nothing they work on is shared: a different
    product, no shared project reservation, and no repository tree either of them
    contains.  Reservations are checked ahead of this by the caller, because that refusal
    predates the limit and reads the same at either limit.

    Repository overlap includes nesting, and is judged on canonical paths: two spellings
    of one working tree are one working tree, and a root that contains another's is the
    same tree twice.  A stored root that is not already absolute is refused on either
    side rather than resolved here, because the tree it names would depend on the working
    directory of whichever process happens to run this check.

    The observer ceiling is last of the specific refusals: while nothing binds an
    observer call to the sprint it is about, two heads observing at once would each read
    the other's cards as their own, so at most one open sprint may declare one.
    """
    product = str(candidate.get("product") or "").strip()
    for sprint in sorted(others, key=lambda row: str(row["ref"])):
        reference = str(sprint["ref"])
        other_product = str(sprint.get("product") or "").strip()
        # Both sides are judged, and the candidate first: a row from before sprints owned
        # a product carries none, and it is no more disjoint as the sprint being admitted
        # than as the sprint already open.  Judging only the other side would make the
        # answer depend on which of the two was looked at first.
        if not product:
            raise TaskError(
                "resource_conflict",
                "this sprint declares no product, so it cannot be proven disjoint from "
                f"open sprint {reference}",
                2,
            )
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
        roots = _scanned_roots(
            candidate.get("repositories") or [],
            refusal=lambda text, why: TaskError(
                "resource_conflict",
                f"this sprint declares repository root {text!r}, which {why}, so it cannot "
                f"be proven disjoint from open sprint {reference}",
                2,
            ),
        )
        held_roots = _scanned_roots(
            sprint.get("repositories") or [],
            refusal=lambda text, why: TaskError(
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
    observer = candidate.get("observer")
    if not (isinstance(observer, dict) and observer.get("kind") == KIND_HEAD):
        return
    holder = next(
        (
            str(sprint["ref"]) for sprint in sorted(others, key=lambda row: str(row["ref"]))
            if _declares_observer_head(sprint)
        ),
        None,
    )
    if holder is not None:
        raise TaskError(
            "sprint_conflict",
            "the pilot's one-observer ceiling allows one open sprint with an observer head, "
            f"and {holder} already declares one; open this sprint with observer none",
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


class SprintReader:
    def __init__(self, client: KanboardClient, *, data_dir: str | Path | None = None, thresholds: dict[str, int] | None = None) -> None:
        self.client = client
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.thresholds = budget_thresholds({"sprint_budget": thresholds}) if thresholds else budget_thresholds()

    def list(self, *, statuses: set[str] | None = None, create: bool = True) -> list[dict[str, Any]]:
        board_id = _sprint_board(self.client, create=create)
        if board_id is None:
            return []
        raw_sprints = self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
        if not isinstance(raw_sprints, list):
            raise TaskError("backend_error", "Kanboard returned an invalid sprint list", 1)
        result = []
        for raw in raw_sprints:
            if not _is_sprint_row(raw):
                continue
            sprint = self._normalize(raw, comments=None)
            # Without live cards this value would claim freshness based on incomplete data.  `show`
            # and `status` populate it after reading the linked cards instead.
            sprint.pop("resume_freshness", None)
            if statuses and sprint["status"] not in statuses:
                continue
            result.append(sprint)
        return sorted(result, key=lambda sprint: (sprint["status"], sprint["ref"], sprint["id"]))

    def export(self) -> list[dict[str, Any]]:
        """Every sprint entity with its records, in a deterministic order.

        `show` also lists the sprint's Pipeline cards; the checkpoint keeps the two
        sets apart, so this reads the entity and its records only.
        """
        board_id = _sprint_board(self.client, create=False)
        if board_id is None:
            return []
        raw_sprints = self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
        if not isinstance(raw_sprints, list):
            raise TaskError("backend_error", "Kanboard returned an invalid sprint list", 1)
        result = []
        for raw in raw_sprints:
            if not _is_sprint_row(raw):
                continue
            task_id = _positive_int(raw.get("id"))
            if task_id is None:
                raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
            comments_raw = self.client.call("getAllComments", task_id=task_id) or []
            comments = [
                {"created_at": _rfc3339(comment.get("date_creation")), "body": _text(comment.get("comment"))}
                for comment in comments_raw if isinstance(comment, dict)
            ]
            sprint = self._normalize(raw, comments=comments)
            # Freshness needs the linked cards this view deliberately does not read.
            sprint.pop("resume_freshness", None)
            result.append(sprint)
        return sorted(result, key=lambda sprint: (sprint["ref"], sprint["id"]))

    def show(self, reference: str, *, include_cards: bool = True) -> dict[str, Any]:
        board_id = ensure_sprint_board(self.client)
        raw = self.client.call("getTaskByReference", project_id=board_id, reference=reference)
        if not isinstance(raw, dict):
            raise TaskError("not_found", "sprint was not found", 2)
        task_id = _positive_int(raw.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
        comments = None
        if include_cards:
            comments_raw = self.client.call("getAllComments", task_id=task_id) or []
            comments = [
                {"created_at": _rfc3339(comment.get("date_creation")), "body": _text(comment.get("comment"))}
                for comment in comments_raw if isinstance(comment, dict)
            ]
        sprint = self._normalize(raw, comments=comments)
        if include_cards:
            sprint["cards"] = TaskReader(self.client).list(sprint=reference)
            sprint["resume_freshness"] = self._resume_freshness(sprint, sprint.get("resume"))
        return sprint

    def _normalize(self, raw: dict[str, Any], *, comments: list[dict[str, Any]] | None) -> dict[str, Any]:
        task_id = _positive_int(raw.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
        metadata = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(metadata, dict):
            raise TaskError("backend_error", "Kanboard returned invalid sprint metadata", 1)
        meta = {
            str(key): _text(value)
            for key, value in metadata.items()
            if str(key) in SPRINT_METADATA
        }
        repositories = _json_list(meta.get("sprint_repositories"))
        budget = _budget(meta.get("sprint_budget"), self.thresholds)
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
        result["resume_freshness"] = self._resume_freshness(result, resume)
        return result

    def status(self, reference: str, *, observer: dict[str, Any] | None = None) -> dict[str, Any]:
        sprint = self.show(reference)
        cards = sprint.get("cards") or []
        states: dict[str, list[str]] = {}
        for card in cards:
            if isinstance(card, dict):
                states.setdefault(str(card.get("state") or "unknown"), []).append(str(card.get("ref") or ""))
        return {
            "ref": sprint["ref"], "goal": sprint["goal"], "status": sprint["status"],
            **{field: sprint[field] for field in ("product", "issues", "reservations") if field in sprint},
            "current_task": sprint["current_task"], "cards": {key: sorted(value) for key, value in sorted(states.items())},
            "budget": sprint["budget"], "resume_freshness": sprint["resume_freshness"],
            "stop_reason": "budget_hard_limit" if sprint["status"] == "stopped" else None,
            "observer": observer or {"state": "unknown"},
        }

    def _resume_freshness(self, sprint: dict[str, Any], resume: dict[str, Any] | None) -> dict[str, Any]:
        if not resume:
            return {
                "fresh": False, "error": "resume_missing", "recorded_at": None,
                "last_event_at": None, "lag_seconds": None,
                "threshold_seconds": RESUME_FRESHNESS_GRACE_SECONDS,
            }
        last_event = ""
        if self.data_dir is not None:
            refs = {
                str(card.get("ref") or "")
                for card in sprint.get("cards") or []
                if isinstance(card, dict) and str(card.get("ref") or "")
            }
            for event in TaskAudit(self.data_dir).events():
                if is_significant_card_event(event, linked_refs=refs):
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
        self, client: KanboardClient, *, data_dir: str | Path, thresholds: dict[str, int] | None = None,
        instance: str | Path | None = None,
    ) -> None:
        from secretary.product_issues import ProductIssueTransaction

        self.client = client
        self.thresholds = budget_thresholds({"sprint_budget": thresholds}) if thresholds else budget_thresholds()
        self.reader = SprintReader(client, data_dir=data_dir, thresholds=self.thresholds)
        self.audit = TaskAudit(data_dir)
        # Opening a sprint has the semantics the Product/Issue transaction already
        # carries: one staged intent per request id, sub-steps that recognise their own
        # earlier attempt, and one audit event however often the delivery repeats.  The
        # primitive is reused as it stands rather than reproduced beside it.
        self.transactions = ProductIssueTransaction(data_dir, self.audit)
        self.data_dir = Path(data_dir)
        self.instance = Path(instance) if instance is not None else None

    def create(
        self, *, role: str, actor: str, goal: str, definition_of_done: str = "",
        repositories: list[str] | None = None, product: str = "", issues: list[str] | None = None,
        projects: list[str] | None = None, reference: str = "", request_id: str | None = None,
        observer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._role(role, {"po", "steward"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = self._create_intent(
            role=role, actor=actor, goal=goal, definition_of_done=definition_of_done,
            repositories=repositories or [], product=product, issues=issues or [],
            reservations=projects or [], reference=reference, observer=observer,
        )
        # Admission and the row it admits are one transition on this installation: a
        # concurrent create that only read before this one wrote would open a second
        # sprint.  Request ownership is settled first and under the same lock: a repeat
        # of one delivery resumes its own staged intent, and only a request nobody has
        # seen yet is measured against live product, issue and conflict state.
        with sprint_admission_lock(self.data_dir):
            document, committed = self.transactions.existing(
                request_id, kind=SPRINT_CREATED, intent=intent
            )
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
        self, *, reference: str, goal: str, definition_of_done: str = "",
        repositories: list[str] | None = None, request_id: str | None = None,
        observer: dict[str, Any] | None = None, status: str = "open",
    ) -> dict[str, Any]:
        """Recreate one exported sprint row, without the rules for opening a sprint.

        Recovery reproduces entities the installation already had, including sprints
        closed before a sprint owned a product.  `restore` writes their real fields
        immediately after, so this must not check or invent ownership, and it is not an
        admission: several restored entities are written one after another.

        Status and observer are the two fields recovery cannot leave to that second
        write.  The row becomes visible the moment its reference lands, and between the
        two writes a reader would see an open sprint with no observer — the one state
        the strict reader calls corrupt.  Both are therefore written before the
        reference, so the row is never published in a shape nobody chose.
        """
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = self._create_intent(
            role="steward", actor="restore", goal=goal, definition_of_done=definition_of_done,
            repositories=repositories or [], product="", issues=[], reservations=[],
            reference=reference, require_goal=False, observer=observer,
            status=status, require_executable_observer=False,
            canonical_repositories=False,
        )
        document, committed = self.transactions.existing(
            request_id, kind=SPRINT_CREATED, intent=intent
        )
        if committed is not None:
            return self._committed_result(SPRINT_CREATED, committed)
        if document is None:
            document, committed = self._begin_create(request_id, intent)
            if committed is not None:
                return self._committed_result(SPRINT_CREATED, committed)
        return self._run_create(document, admitted=False)

    def _create_intent(
        self, *, role: str, actor: str, goal: str, definition_of_done: str,
        repositories: list[str], product: str, issues: list[str], reservations: list[str],
        reference: str, require_goal: bool = True, observer: dict[str, Any] | None = None,
        status: str = "open", require_executable_observer: bool = True,
        canonical_repositories: bool = True,
    ) -> dict[str, Any]:
        """The normalized request, which is both the replay key and the repair recipe.

        A repeat of the same request id carrying a different intent is another
        operation, and the transaction refuses it before any side effect.

        Only an operator opening a sprint has to state a goal; recovery reproduces the
        goal its export carries.

        The observer is part of the staged intent for the same reason the reservations
        are: it is a decision the caller made, and a repair of this create has to write
        the value the caller chose rather than one it picks up on the retry.

        Repository roots are canonicalized here, where the operator declaring them is:
        the intent is the recipe every later step writes from, so the absolute root is
        what the row persists, and a root this host cannot resolve is refused before any
        board row, staged intent, metadata or audit event exists.  Recovery declares
        nothing and canonicalizes nothing: it reproduces the values its export carries,
        including those of closed rows written before this rule.
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
            "role": role, "actor": actor, "goal": goal, "definition_of_done": definition_of_done,
            "repositories": (
                canonical_repository_roots(repositories) if canonical_repositories
                else _unique_strings(repositories)
            ),
            "product": product.strip(),
            "issues": _unique_strings(issues), "reservations": _unique_strings(reservations),
            "reference": reference, "status": status,
            "observer": self._observer_intent(
                observer, executable=require_executable_observer,
            ),
        }

    def _observer_intent(
        self, observer: dict[str, Any] | None, *, executable: bool,
    ) -> dict[str, Any] | None:
        """The observer value a create writes, refused here rather than at the backend.

        An operator opening a sprint has to state one: absent, null, empty, `default` and
        `inherited` are not interpretations this model has, so there is nothing to fall
        back to and the create is a validation error.  Recovery is the other caller, and
        it reproduces whatever its export carried, including the migration provenance of
        a closed row, which is not executable and must not be turned into one here.

        A declared profile is resolved against the installation's head registry, the same
        one the dispatcher launches from.  A sprint may not be opened on a head that does
        not exist: the fence would stop its projects on the very first tick, and an
        operator would be reading a critical outcome instead of a validation error.
        """
        if observer is None:
            if executable:
                raise TaskError(
                    "validation",
                    "sprint requires an explicit observer; pass --observer <profile> or "
                    "--observer none",
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
                    value, installed_observer_profiles(self.instance), subject="sprint",
                )
            except ObserverMetadataError as exc:
                raise TaskError("validation", exc.message, 2) from None
        return value

    def _begin_create(self, request_id: str, intent: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
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
            SPRINT_CREATED, str(intent["role"]), str(intent["actor"]), reference, request_id,
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
            clean = self._compensate_create(document)
            if answer:
                # A refusal answered to the caller is the end of this request: it holds no
                # row, and leaving its staged intent behind would leave a repair nobody is
                # going to run.  The intent is only kept when the backend would not take
                # the row back, because then something of this request does still exist.
                if clean:
                    self.transactions.discard(document)
                raise
            raise TaskError(
                "audit_pending", "sprint create is pending repair; retry with the same request id", 4,
            ) from None
        except (OSError, KeyError, TypeError):
            self._compensate_create(document)
            raise TaskError(
                "audit_pending", "sprint create is pending repair; retry with the same request id", 4,
            ) from None

    def _compensate_create(self, document: dict[str, Any]) -> bool:
        """Take back the row of a create that never got as far as its reference.

        A row without a reference is on no reader's board, so leaving it would be
        invisible litter that the repair of this same request would then have to find.
        The staged intent stays here either way: it is what a repeat resumes, and only
        the caller of a refusal it will never repeat discards it.

        Returns whether the request now holds nothing on the backend, which is what makes
        discarding its intent safe.
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
        # A staged create that is being resumed was admitted before the refusal that
        # stalled it, and it held nothing meanwhile.  The installation is measured again
        # here, before anything of this sprint is published, so a repeat that lost the
        # slot to another sprint is refused instead of opening a second one.
        if admitted and not progress.get("reference_done"):
            staged = progress.get("task_id")
            staged_id = staged if isinstance(staged, int) else None
            self._check_reference_claim(str(intent["reference"]), staged_id)
            self._check_conflicts(intent, excluding_id=staged_id)
        board_id = ensure_sprint_board(self.client)
        row = self._create_row(document, board_id, admitted=admitted)
        task_id = _positive_int(row.get("id"))
        if task_id is None:
            raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
        created_ref = str(intent["reference"]) or f"{SPRINT_REFERENCE_PREFIX}{task_id}"
        event.update({"ref": created_ref, "task_id": f"sprint_kanboard_{task_id}"})
        event["backend"]["task_id"] = task_id
        progress["task_id"] = task_id
        self.transactions.save(document)
        self._ensure_metadata(document, task_id, self._create_values(intent), step="fields")
        # The reference is the last step, and it is what publishes the sprint: until it
        # is written the row carries no sprint reference and no reader counts it as a
        # sprint, so nothing observes one open without the product, issues and
        # reservations it was admitted with.
        if str(row.get("reference") or "") != created_ref:
            progress["reference_started"] = True
            self.transactions.save(document)
            if not self.client.call(
                "updateTask", id=task_id, reference=created_ref, description="",
            ):
                raise TaskError("backend_error", "Kanboard rejected the sprint reference", 1)
            row = self._create_row(document, board_id, admitted=admitted)
            if str(row.get("reference") or "") != created_ref:
                raise TaskError("backend_error", "sprint reference remains incomplete", 1)
        progress["reference_done"] = True
        self.transactions.save(document)
        return created_ref

    def _create_row(self, document: dict[str, Any], board_id: int, *, admitted: bool) -> dict[str, Any]:
        """The row this request created, creating it once when it has none yet.

        A row counts as this request's own only when the staged progress names its task
        id, or, for recovery, when nothing has been written for this reference yet.  An
        admitted create never adopts a row it merely shares a reference with: that row
        may belong to another sprint that took the slot while this one was stalled.
        """
        rows = [
            row for row in self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
            if isinstance(row, dict)
        ]
        staged = (document.get("progress") or {}).get("task_id")
        if isinstance(staged, int):
            row = next((row for row in rows if _positive_int(row.get("id")) == staged), None)
            if row is None:
                raise TaskError("backend_error", "the sprint row of this request was not found", 1)
            return row
        reference = str(document["intent"].get("reference") or "")
        if reference and not admitted:
            row = next((row for row in rows if str(row.get("reference") or "") == reference), None)
            if row is not None:
                return row
        # A sprint whose reference is derived from its own task id has no identifier
        # before the row exists, so the request id travels in the description until the
        # reference is written.
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
        created = _positive_int(self.client.call(
            "createTask", project_id=board_id, title=str(document["intent"]["goal"]),
            description=marker, column_id=column_id,
        ))
        if created is None:
            raise TaskError("backend_error", "Kanboard rejected the sprint write", 1)
        document["progress"]["task_id"] = created
        self.transactions.save(document)
        rows = [
            row for row in self.client.call("getAllTasks", project_id=board_id, status_id=1) or []
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
            "sprint_budget": json.dumps(_budget(thresholds=self.thresholds), separators=(",", ":")),
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
            values["sprint_reservations"] = json.dumps(
                list(intent["reservations"]), separators=(",", ":")
            )
        return values

    def _ensure_metadata(
        self, document: dict[str, Any], task_id: int, values: dict[str, str], *, step: str,
    ) -> None:
        """Write one step of the sprint's fields once, and prove the backend kept it.

        Kanboard answers its metadata API with a boolean; anything other than `True`
        is a refusal, and reporting the sprint created on it would leave an open sprint
        without the ownership it was admitted with.  The step is recorded durably, so a
        repair of a later step never rewrites an earlier one back.
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
            "action": action, "sprint": self.reader.show(str(committed["ref"])),
            "event_id": str(committed["event_id"]),
        }

    def _check_ownership(self, product: str, issues: list[str], reservations: list[str]) -> None:
        """Prove the sprint owns a product, an open issue and registered projects.

        Every step is a read of durable Product/Issue records and of the project
        registry, so a rejected sprint leaves no row, no metadata and no audit event.
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
            raise TaskError(
                "validation", "sprint ownership needs the instance directory; pass --instance", 2
            )
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
            raise TaskError(
                "validation", "unknown registered project(s): " + ", ".join(unknown), 2
            )

    def _check_reference_claim(self, reference: str, staged_id: int | None) -> None:
        """Refuse a caller-supplied reference another sprint has taken meanwhile.

        A stalled create holds nothing, including its reference, so between its refusal
        and its repeat another sprint may legitimately open under that very reference.
        The repeat is not its owner and must not write over it.
        """
        if not reference:
            return
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

        An installation the caller did not name, one whose config cannot be read and one
        that never set the value are the same answer: the singleton limit.  Nothing here
        can widen the limit, which is what makes a malformed setting harmless.
        """
        return instance_open_sprint_limit(self.instance)

    def _check_conflicts(
        self, candidate: dict[str, Any], *, excluding: str = "", excluding_id: int | None = None,
    ) -> None:
        """Refuse a sprint this installation has no room, or no disjoint room, for.

        Every collision the caller can act on is reported before the generic count
        refusal, because the caller of a colliding project, product or repository has to
        see which resource it is, not only that some sprint is open.

        A sprint is left out of the scan only when it is proven to be the very row this
        transition is about: the row `reopen` reads by reference, or the row a staged
        create recorded its task id for.  A matching reference alone proves nothing.
        """
        others = [
            sprint for sprint in self.reader.list(statuses={"open"}, create=False)
            if not (
                (excluding and sprint["ref"] == excluding)
                or (excluding_id is not None and _sprint_number(sprint) == excluding_id)
            )
        ]
        _refuse_open_sprint(candidate, others, limit=self._open_sprint_limit())

    def comment(self, *, role: str, actor: str, reference: str, body: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "worker", "reviewer", "steward", "retro"})
        if not body.strip():
            raise TaskError("validation", "comment requires a non-empty body", 2)
        return self._write("commented", role, actor, reference, request_id, {"body_sha256": _digest(body)}, lambda sprint: self.client.call("createComment", task_id=_sprint_number(sprint), user_id=0, content=f"[{role}]\n{body}"))

    def set_current_task(self, *, role: str, actor: str, reference: str, task_reference: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "observer", "steward"})
        task_reference = task_reference.strip()
        if not task_reference:
            raise TaskError("validation", "current task requires a task reference", 2)
        def mutation(sprint: dict[str, Any]) -> None:
            task = TaskReader(self.client).show(task_reference)
            if task.get("sprint") != reference:
                raise TaskError("validation", "current task is not linked to this sprint", 2)
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values={"sprint_current_task": task_reference})
        return self._write("current_task_set", role, actor, reference, request_id, {"task": task_reference}, mutation)

    def record_budget(self, *, role: str, actor: str, reference: str, event_type: str, request_id: str | None = None, source_event_id: str = "") -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "steward"})
        if event_type not in BUDGET_EVENT_TYPES:
            raise TaskError("validation", "unknown budget event type " + repr(event_type), 2)
        request_id = request_id or str(uuid.uuid4())
        before = self.reader.show(reference)
        before_budget = _budget(before.get("budget"), self.thresholds)
        hard_stop = before["status"] == "open" and before_budget["total"] + 1 >= self.thresholds["hard"]
        def mutation(sprint: dict[str, Any]) -> None:
            budget = _budget(sprint.get("budget"), self.thresholds)
            budget["by_type"][event_type] += 1
            budget = _budget({"by_type": budget["by_type"]}, self.thresholds)
            values = {"sprint_budget": json.dumps(budget, separators=(",", ":"))}
            if budget["hard_reached"] and sprint["status"] == "open":
                values["sprint_status"] = "stopped"
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=values)
        result = self._write(
            "budget_recorded", role, actor, reference, request_id,
            {
                "event_type": event_type,
                "source_event_id": source_event_id or None,
                "hard_limit_stop": hard_stop,
            },
            mutation,
        )
        recorded = self.audit.committed_event(request_id)
        if recorded and recorded.get("payload", {}).get("hard_limit_stop"):
            self._record_hard_stop(
                role=role, actor=actor, reference=reference, request_id=request_id,
                budget_event_id=str(recorded.get("event_id") or ""),
                event_type=event_type, source_event_id=source_event_id,
            )
        return result

    def _record_hard_stop(
        self, *, role: str, actor: str, reference: str, request_id: str,
        budget_event_id: str, event_type: str, source_event_id: str,
    ) -> None:
        """Record the state transition separately from the charge that caused it."""
        stop_request_id = request_id + ":budget-hard-stop"
        if self.audit.committed_event(stop_request_id) is not None:
            return
        sprint = self.reader.show(reference)
        event = self._event(
            "budget_hard_stopped", role, actor, reference, stop_request_id,
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
        def mutation(sprint: dict[str, Any]) -> None:
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values={"sprint_resume": json.dumps(normalized, separators=(",", ":"))})
            self.client.call("createComment", task_id=_sprint_number(sprint), user_id=0, content="[sprint:resume]\n" + normalized["selected_step"])
        payload = {"fields": list(RESUME_FIELDS)}
        if delivery_id:
            payload.update({"delivery_id": delivery_id, "through_event": through_event})
        return self._write("resume_recorded", role, actor, reference, request_id, payload, mutation)

    def close(self, *, role: str, actor: str, reference: str, request_id: str | None = None) -> dict[str, Any]:
        self._role(role, {"po"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = {"role": role, "actor": actor, "reference": reference}
        with self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                document, committed = self.transactions.existing(
                    request_id, kind=SPRINT_CLOSED, intent=intent,
                )
                if committed is not None:
                    return self._close_result(committed)
                if document is None:
                    sprint = self.reader.show(reference, include_cards=False)
                    targets = self._close_targets(sprint)
                    event = self._event(
                        SPRINT_CLOSED, role, actor, reference, request_id,
                        {
                            "intent": intent,
                            "targets": targets,
                            "archived_tasks": [],
                            "remaining_tasks": list(targets["remaining"]),
                        },
                        sprint,
                    )
                    document, committed = self.transactions.begin(
                        request_id, kind=SPRINT_CLOSED, intent=intent, event=event,
                    )
                    if committed is not None:
                        return self._close_result(committed)
                    if document is None:
                        raise TaskError("audit_pending", "sprint close transaction claim is unavailable", 4)
                return self._run_close(document)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _close_targets(self, sprint: dict[str, Any]) -> dict[str, list[str]]:
        """Freeze this close's task set before any archival write.

        A sprint that predates reservations is retained for recovery only.  Closing it
        can change its status, but must not retrospectively archive arbitrary cards.
        """
        if "reservations" not in sprint:
            return {"archive": [], "remaining": []}
        cards = TaskReader(self.client).list(sprint=str(sprint["ref"]))
        tasks = [
            card for card in cards
            if card.get("record_type") not in {"product", "issue"}
        ]
        return {
            "archive": sorted(str(card["ref"]) for card in tasks if card.get("state") == "done"),
            "remaining": sorted(str(card["ref"]) for card in tasks if card.get("state") != "done"),
        }

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
        try:
            writer = TaskWriter(self.client, data_dir=self.data_dir) if archive else None
            for task_ref in archive:
                if task_ref in archived:
                    continue
                assert writer is not None
                writer.archive(
                    role="po",
                    actor=str(document["intent"]["actor"]),
                    reference=task_ref,
                    reason=f"archived when sprint {event['ref']} closed",
                    request_id=_close_archive_request_id(str(document["request_id"]), task_ref),
                )
                archived.append(task_ref)
                self.transactions.save(document)
            sprint = self.reader.show(str(event["ref"]), include_cards=False)
            if sprint["status"] != "closed":
                document.setdefault("progress", {})["status_started"] = True
                self.transactions.save(document)
                if self.client.call(
                    "saveTaskMetadata",
                    task_id=_sprint_number(sprint),
                    values={"sprint_status": "closed"},
                ) is not True:
                    raise TaskError("backend_error", "Kanboard rejected sprint closure", 1)
                sprint = self.reader.show(str(event["ref"]), include_cards=False)
                if sprint["status"] != "closed":
                    raise TaskError("backend_error", "sprint closure remains incomplete", 1)
            document.setdefault("progress", {})["status_done"] = True
            self.transactions.save(document)
            self.transactions.complete(document)
        except TaskError as exc:
            if exc.code in {"validation", "closed", "not_found", "transition_forbidden", "live_work", "role_forbidden"} and not document.get("progress"):
                self.transactions.discard(document)
                raise
            raise TaskError("audit_pending", "sprint close is pending repair; retry with the same request id", 4) from None
        except (OSError, KeyError, TypeError):
            raise TaskError("audit_pending", "sprint close is pending repair; retry with the same request id", 4) from None
        update_active_sprint_projects(self.data_dir, self.reader.show(str(event["ref"]), include_cards=False))
        return self._close_result(event)

    def _close_result(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return {
            "action": SPRINT_CLOSED,
            "sprint": self.reader.show(str(event["ref"])),
            "event_id": str(event["event_id"]),
            "archived_tasks": list(payload.get("archived_tasks") or []),
            "remaining_tasks": list(payload.get("remaining_tasks") or []),
        }

    def reopen(
        self, *, role: str, actor: str, reference: str, request_id: str | None = None,
        observer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reopen a sprint that still satisfies every rule for an open sprint.

        This is the other transition into `open`, so it runs the same admission order as
        `create`, on the same staged-intent primitive: the request id is settled before
        any mutable precondition, a repeat carrying another payload is refused before a
        side effect, and a refused backend step leaves an intent its own request id
        repairs rather than a sprint reported back open.

        A sprint predating ownership is not completed here: recovery keeps it readable,
        and an operator who needs the work opens a new sprint that owns its issues.

        The observer is decided again here, and it is never inherited: what the row
        carries is either the executable value of the run that closed, or the migration
        provenance of one nobody recorded, and neither is a decision about the run being
        opened now.  It is written while the sprint is still closed, so the row is never
        readable open under a value the reopening caller did not choose.
        """
        self._role(role, {"po"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = {
            "role": role, "actor": actor, "reference": reference,
            "observer": self._observer_intent(observer, executable=True),
        }
        with sprint_admission_lock(self.data_dir):
            document, committed = self.transactions.existing(
                request_id, kind=SPRINT_REOPENED, intent=intent
            )
            if committed is not None:
                return self._committed_result(SPRINT_REOPENED, committed)
            if document is None:
                sprint = self.reader.show(reference, include_cards=False)
                self._check_reopen(sprint, reference, intent["observer"])
                event = self._event(
                    SPRINT_REOPENED, role, actor, reference, request_id, {"intent": intent}, sprint,
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
        self, sprint: dict[str, Any], reference: str, observer: dict[str, Any] | None,
    ) -> None:
        """Every rule an open sprint has to satisfy, read live before any write.

        The candidate is the row as it stands, under the observer this reopen declares
        rather than the one the closed row happens to carry: the ceiling is about the
        run being opened now.
        """
        missing = [
            name for name, value in (
                ("product", sprint.get("product")),
                ("issues", sprint.get("issues")),
                ("reservations", sprint.get("reservations")),
            ) if not value
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
            self._ensure_metadata(
                document,
                _sprint_number(sprint),
                {OBSERVER_FIELD: encode_observer(document["intent"]["observer"])},
                step="observer",
            )
            self._ensure_metadata(
                document, _sprint_number(sprint), {"sprint_status": "open"}, step="opened",
            )
            sprint = self.reader.show(reference)
            event = document["event"]
            event["task_id"] = sprint["id"]
            event["backend"]["revision"] = "updated_at:" + str(sprint["audit"]["updated_at"] or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint)
            return {"action": SPRINT_REOPENED, "sprint": sprint, "event_id": str(event["event_id"])}
        except TaskError as exc:
            if exc.code in _ADMISSION_REFUSALS or (
                exc.code in {"validation", "role_forbidden"} and not document.get("progress")
            ):
                self._compensate_reopen(document, reference)
                raise
            raise TaskError(
                "audit_pending", "sprint reopen is pending repair; retry with the same request id", 4,
            ) from None
        except (OSError, KeyError, TypeError):
            raise TaskError(
                "audit_pending", "sprint reopen is pending repair; retry with the same request id", 4,
            ) from None

    def _record_observer_preimage(self, document: dict[str, Any], sprint: dict[str, Any]) -> None:
        """Record what the row's observer was, once, before this reopen writes over it.

        A row that carries no value at all has no preimage to record: it predates the
        observer migration, and the reopen of such a row is refused on ownership long
        before this.  Its absence is recorded as such, so a rollback knows it cannot put
        the row back and leaves the reopen repairable instead.
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

    def _compensate_reopen(self, document: dict[str, Any], reference: str) -> None:
        """Undo a refused reopen's observer write and drop its intent.

        The refusal is answered to the caller, so this request is over: it must leave the
        row exactly as it found it and no staged intent for a repair nobody will run.
        Anything this cannot undo — a status already touched, a preimage never recorded,
        a backend that refuses the write back — leaves the intent in place, because then
        something of this request does still exist.
        """
        progress = document.get("progress") or {}
        if progress.get("opened_done"):
            return
        try:
            sprint = self.reader.show(reference, include_cards=False)
            # The status is read rather than taken from the staged steps: a step recorded as
            # started proves an attempt, not a write, and only a row still not open is one
            # this refusal may put back.
            if str(sprint.get("status") or "") == "open":
                return
            if progress.get("observer_started") or progress.get("observer_done"):
                preimage = progress.get("observer_preimage")
                if not isinstance(preimage, str):
                    return
                if self.client.call(
                    "saveTaskMetadata",
                    task_id=_sprint_number(sprint),
                    values={OBSERVER_FIELD: preimage},
                ) is not True:
                    return
        except (TaskError, OSError, KeyError, TypeError):
            return
        document["progress"] = {}
        self.transactions.save(document)
        self.transactions.discard(document)

    def restore(self, *, reference: str, values: dict[str, str], request_id: str | None = None) -> dict[str, Any]:
        """Rewrite one sprint entity's fields verbatim from a checkpoint export.

        Restore is not a sprint mutation an operator makes: it reproduces fields a
        closed or stopped sprint already carried, so it is not refused on status the
        way `comment` or `resume` are.
        """
        unknown = sorted(set(values) - SPRINT_METADATA)
        if unknown:
            raise TaskError("validation", "restore carries unknown sprint fields: " + ", ".join(unknown), 2)

        def mutation(sprint: dict[str, Any]) -> None:
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=dict(values))

        return self._write(
            "restored", "steward", "restore", reference, request_id, {"fields": sorted(values)}, mutation
        )

    def backfill_observer(
        self, *, reference: str, value: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        """Write one row's observer metadata for the migration, and never over another value.

        The refusal is the point.  A row that already carries a different value was written by
        somebody who knew something this migration does not — an operator repairing it, or a
        second run whose journal disagrees — and overwriting it would destroy that.  An exact
        match is the retry of this very write and reports success without touching the backend.
        """
        encoded = encode_observer(value)
        current = self.reader.show(reference, include_cards=False)
        if "observer" in current:
            existing = current["observer"]
            if existing is None or encode_observer(existing) != encoded:
                raise TaskError(
                    "validation",
                    f"sprint {reference} already carries observer metadata that is not the value "
                    "this migration selected; resolve it by hand before running the migration again",
                    2,
                )

        def mutation(sprint: dict[str, Any]) -> None:
            if self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={OBSERVER_FIELD: encoded},
            ) is not True:
                raise TaskError("backend_error", "Kanboard rejected the sprint observer metadata", 1)

        return self._write(
            "observer_backfilled", "steward", "observer-migration", reference, request_id,
            {"observer": dict(parse_observer(value) or {})}, mutation,
        )

    def restore_comment(self, *, reference: str, body: str, occurrence: int, request_id: str | None = None) -> dict[str, Any]:
        """Append one exported record back to the entity, verbatim."""
        return self._write(
            "restored_comment", "steward", "restore", reference, request_id,
            {"body_sha256": _digest(body), "restore_occurrence": occurrence},
            lambda sprint: self.client.call(
                "createComment", task_id=_sprint_number(sprint), user_id=0, content=body
            ),
        )

    def _write(self, kind: str, role: str, actor: str, reference: str, request_id: str | None, payload: dict[str, Any], mutation: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            return self._committed(kind, committed)
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            return self._pending(kind, pending)
        sprint = self.reader.show(reference)
        if sprint["status"] in {"closed", "stopped"} and kind in {"commented", "current_task_set", "resume_recorded"}:
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
        return {"action": kind, "sprint": self.reader.show(str(event["ref"])), "event_id": self.audit.append(str(event["request_id"]), event)}

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
    def _event(kind: str, role: str, actor: str, reference: str, request_id: str, payload: dict[str, Any], sprint: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "event_id": "evt_" + uuid.uuid4().hex, "schema_version": 1, "occurred_at": _now(),
            "actor": {"role": role, "id": actor}, "kind": kind, "outcome": "success",
            "task_id": sprint["id"] if sprint else "", "ref": reference,
            "backend": {"kind": "kanboard", "task_id": _sprint_number(sprint) if sprint else None, "revision": "pending"},
            "request_id": request_id, "payload": payload,
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

    A create writes the reference after the fields the sprint was admitted with, so a
    row still without one is an unfinished create its own staged transaction repairs.
    Counting it would show a sprint open without its product, issues and reservations.
    """
    return isinstance(raw, dict) and str(raw.get("reference") or "").startswith(SPRINT_REFERENCE_PREFIX)


def _create_marker(request_id: str) -> str:
    return "[secretary-sprint-transaction:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest() + "]"


def _close_archive_request_id(request_id: str, reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"{request_id}:sprint-close-archive:{digest}"


def canonical_repository_roots(paths: list[Any]) -> list[str]:
    """The absolute roots a declaration means, resolved where the caller declared them.

    A relative root only names a tree next to the process that wrote it, so it is
    resolved once, here, at declaration time, and the absolute answer is what the sprint
    persists.  Resolving it later instead would answer against whichever process happens
    to run the check, and two sprints sharing a tree would read as disjoint.

    A root this host cannot resolve at all is refused rather than guessed at: a sprint
    whose tree cannot be named is not a sprint any other sprint can be judged against.
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

    Scanning fails closed on anything that is not already absolute.  Such a value is
    canonical to no host: resolving it here would resolve it against the working
    directory of whichever process runs admission, and that is exactly how two sprints
    sharing a working tree come to read as disjoint.
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


def _declares_observer_head(sprint: dict[str, Any]) -> bool:
    """Whether an open sprint counts against the one-observer ceiling.

    Only `none` proves a sprint runs without a head.  A row whose observer is missing or
    unreadable proves nothing, and the ceiling has to read it as a head: admitting a
    second head because one row could not be read is the collision it exists to prevent.
    """
    observer = sprint.get("observer")
    return not (isinstance(observer, dict) and observer.get("kind") == KIND_NONE)


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

    A sprint created before ownership existed carries none of the three keys.  A
    reader that answered `""` and `[]` for it would put values on the entity that
    nobody wrote, and the next checkpoint would store them as if they were chosen.
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

    Three states have to stay apart, because their repairs differ: the key is missing (a row that
    predates the field, or one a migration has not reached), the key holds one of the four tagged
    forms, or the key holds something else.  The last is reported as `None` rather than dropped:
    a reader that answered "absent" for an unreadable value would let a corrupt row pass for an
    unmigrated one, and the strict reader is precisely the thing that must tell them apart.
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


def _budget(value: Any = None, thresholds: dict[str, int] | None = None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    if isinstance(value, str):
        try:
            source = json.loads(value)
        except ValueError:
            source = {}
    by_type = source.get("by_type") if isinstance(source, dict) else {}
    counts = {event_type: max(0, int(by_type.get(event_type, 0))) if isinstance(by_type, dict) else 0 for event_type in BUDGET_EVENT_TYPES}
    limits = thresholds or budget_thresholds()
    total = sum(counts.values())
    return {"total": total, "by_type": counts, "thresholds": limits, "signal_reached": total >= limits["signal"], "hard_reached": total >= limits["hard"]}


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
    missing = [field for field in RESUME_FIELDS if not isinstance(source.get(field), str) or not source[field].strip()]
    if missing:
        if required:
            raise TaskError("validation", "resume entry is missing required fields: " + ", ".join(missing), 2)
        return None
    recorded_at = _text(source.get("recorded_at")) or _now()
    if required and _timestamp(recorded_at) is None:
        raise TaskError("validation", "resume recorded_at must include a timezone", 2)
    return {**{field: source[field].strip() for field in RESUME_FIELDS}, "recorded_at": recorded_at}
