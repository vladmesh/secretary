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
_ADMISSION_LOCK = "sprints/admission.lock"
SPRINT_CREATED = "created"
SPRINT_REOPENED = "reopened"
SPRINT_CLOSED = "closed"
SPRINT_STATUSES = {"open", "closed", "stopped"}
# An admitted create that the backend refused holds nothing: it compensates its own row
# and re-checks the installation before it publishes.  These are the refusals of that
# re-check, and they are answers to the caller rather than a pending repair.
_ADMISSION_REFUSALS = {"sprint_conflict", "resource_conflict"}


def active_sprint_repositories(data_dir: str | Path) -> dict[str, list[str]]:
    """Return the local index of repositories held by open sprints.

    This is an index, not a second sprint model.  Task writes use it to avoid a
    sprint-board read for repositories that no open sprint holds; a listed ref is
    always checked live before it authorizes a write.
    """
    path = Path(data_dir) / _GUARD_INDEX
    return _read_active_sprint_repositories(path)


def _read_active_sprint_repositories(path: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    repositories = raw.get("repositories") if isinstance(raw, dict) else None
    if not isinstance(repositories, dict):
        return {}
    return {
        str(repo): sorted({str(ref) for ref in refs if str(ref)})
        for repo, refs in repositories.items()
        if isinstance(refs, list) and str(repo)
    }


def sprint_guard_index_initialized(data_dir: str | Path) -> bool:
    try:
        raw = json.loads((Path(data_dir) / _GUARD_INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("version") == 1 and isinstance(raw.get("repositories"), dict)


def refresh_active_sprint_repositories(data_dir: str | Path, reader: Any) -> None:
    """Seed the index from the live board without racing a sprint mutation."""
    with _sprint_guard_index_lock(data_dir):
        _replace_active_sprint_repositories(data_dir, reader.list(statuses={"open"}, create=False))


def _replace_active_sprint_repositories(data_dir: str | Path, sprints: list[dict[str, Any]]) -> None:
    repositories: dict[str, list[str]] = {}
    for sprint in sprints:
        if sprint.get("status") != "open":
            continue
        reference = str(sprint.get("ref") or "")
        if not reference:
            continue
        for repo in sprint.get("repositories") or []:
            name = str(repo).strip()
            if name:
                repositories[name] = sorted(set(repositories.get(name, []) + [reference]))
    _write_active_sprint_repositories(data_dir, repositories)


def update_active_sprint_repositories(data_dir: str | Path, sprint: dict[str, Any]) -> None:
    """Update one sprint's entries in the local open-repository index."""
    with _sprint_guard_index_lock(data_dir):
        repositories = _read_active_sprint_repositories(Path(data_dir) / _GUARD_INDEX)
        reference = str(sprint.get("ref") or "")
        for repo in list(repositories):
            refs = [ref for ref in repositories[repo] if ref != reference]
            if refs:
                repositories[repo] = refs
            else:
                repositories.pop(repo)
        if reference and sprint.get("status") == "open":
            for repo in sprint.get("repositories") or []:
                name = str(repo).strip()
                if name:
                    repositories[name] = sorted(set(repositories.get(name, []) + [reference]))
        _write_active_sprint_repositories(data_dir, repositories)


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


def _write_active_sprint_repositories(data_dir: str | Path, repositories: dict[str, list[str]]) -> None:
    path = Path(data_dir) / _GUARD_INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "repositories": repositories}, sort_keys=True, separators=(",", ":")),
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
    ) -> dict[str, Any]:
        self._role(role, {"po", "steward"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = self._create_intent(
            role=role, actor=actor, goal=goal, definition_of_done=definition_of_done,
            repositories=repositories or [], product=product, issues=issues or [],
            reservations=projects or [], reference=reference,
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
                self._check_conflicts(intent["reservations"], excluding="")
                document, committed = self._begin_create(request_id, intent)
                if committed is not None:
                    return self._committed_result(SPRINT_CREATED, committed)
            return self._run_create(document, admitted=True)

    def restore_create(
        self, *, reference: str, goal: str, definition_of_done: str = "",
        repositories: list[str] | None = None, request_id: str | None = None,
    ) -> dict[str, Any]:
        """Recreate one exported sprint row, without the rules for opening a sprint.

        Recovery reproduces entities the installation already had, including sprints
        closed before a sprint owned a product.  `restore` writes their real fields
        immediately after, so this must not check or invent ownership, and it is not an
        admission: several restored entities are written one after another.
        """
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = self._create_intent(
            role="steward", actor="restore", goal=goal, definition_of_done=definition_of_done,
            repositories=repositories or [], product="", issues=[], reservations=[],
            reference=reference, require_goal=False,
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
        reference: str, require_goal: bool = True,
    ) -> dict[str, Any]:
        """The normalized request, which is both the replay key and the repair recipe.

        A repeat of the same request id carrying a different intent is another
        operation, and the transaction refuses it before any side effect.

        Only an operator opening a sprint has to state a goal; recovery reproduces the
        goal its export carries.
        """
        goal = goal.strip()
        reference = reference.strip()
        if require_goal and not goal:
            raise TaskError("validation", "create requires a non-empty goal", 2)
        if reference and not reference.startswith(SPRINT_REFERENCE_PREFIX):
            raise TaskError("validation", f"sprint reference must start with {SPRINT_REFERENCE_PREFIX}", 2)
        return {
            "role": role, "actor": actor, "goal": goal, "definition_of_done": definition_of_done,
            "repositories": _unique_strings(repositories), "product": product.strip(),
            "issues": _unique_strings(issues), "reservations": _unique_strings(reservations),
            "reference": reference,
        }

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
            update_active_sprint_repositories(self.data_dir, sprint)
            return {"action": SPRINT_CREATED, "sprint": sprint, "event_id": str(event["event_id"])}
        except TaskError as exc:
            answer = exc.code in _ADMISSION_REFUSALS or (
                exc.code in {"validation", "role_forbidden"} and not document.get("progress")
            )
            self._compensate_create(document)
            if answer:
                raise
            raise TaskError(
                "audit_pending", "sprint create is pending repair; retry with the same request id", 4,
            ) from None
        except (OSError, KeyError, TypeError):
            self._compensate_create(document)
            raise TaskError(
                "audit_pending", "sprint create is pending repair; retry with the same request id", 4,
            ) from None

    def _compensate_create(self, document: dict[str, Any]) -> None:
        """Take back the row of a create that never got as far as its reference.

        A row without a reference is on no reader's board, so leaving it would be
        invisible litter that the repair of this same request would then have to find.
        The staged intent stays either way: it is what a repeat resumes.  When the
        backend also refuses to take the row back, the intent carries it and the repair
        picks the same row up again.
        """
        progress = document.get("progress") or {}
        task_id = progress.get("task_id")
        if progress.get("reference_done") or not isinstance(task_id, int):
            return
        try:
            if self.client.call("removeTask", task_id=task_id) is not True:
                return
            document["progress"] = {}
            self.transactions.save(document)
        except (TaskError, OSError, KeyError, TypeError):
            return

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
            self._check_conflicts(list(intent["reservations"]), excluding_id=staged_id)
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
            "sprint_status": "open",
            "sprint_budget": json.dumps(_budget(thresholds=self.thresholds), separators=(",", ":")),
            "sprint_current_task": "",
            "sprint_resume": "",
        }
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

    def _check_conflicts(
        self, reservations: list[str], *, excluding: str = "", excluding_id: int | None = None,
    ) -> None:
        """Refuse a second open sprint, and a project another open sprint already holds.

        The resource conflict comes before the singleton rule, because the caller of a
        colliding reservation has to see which project it is, not only that some sprint
        is open.

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
        if others:
            raise TaskError(
                "sprint_conflict",
                "installation already has an open sprint: "
                + ", ".join(sorted(str(sprint["ref"]) for sprint in others))
                + "; close it before opening another",
                2,
            )

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
            raise TaskError("audit_pending", "sprint close is pending repair; retry with the same request id", 4) from exc
        except (OSError, KeyError, TypeError):
            raise TaskError("audit_pending", "sprint close is pending repair; retry with the same request id", 4) from None
        update_active_sprint_repositories(self.data_dir, self.reader.show(str(event["ref"]), include_cards=False))
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

    def reopen(self, *, role: str, actor: str, reference: str, request_id: str | None = None) -> dict[str, Any]:
        """Reopen a sprint that still satisfies every rule for an open sprint.

        This is the other transition into `open`, so it runs the same admission order as
        `create`, on the same staged-intent primitive: the request id is settled before
        any mutable precondition, a repeat carrying another payload is refused before a
        side effect, and a refused backend step leaves an intent its own request id
        repairs rather than a sprint reported back open.

        A sprint predating ownership is not completed here: recovery keeps it readable,
        and an operator who needs the work opens a new sprint that owns its issues.
        """
        self._role(role, {"po"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = {"role": role, "actor": actor, "reference": reference}
        with sprint_admission_lock(self.data_dir):
            document, committed = self.transactions.existing(
                request_id, kind=SPRINT_REOPENED, intent=intent
            )
            if committed is not None:
                return self._committed_result(SPRINT_REOPENED, committed)
            if document is None:
                sprint = self.reader.show(reference, include_cards=False)
                self._check_reopen(sprint, reference)
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

    def _check_reopen(self, sprint: dict[str, Any], reference: str) -> None:
        """Every rule an open sprint has to satisfy, read live before any write."""
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
        self._check_conflicts(reservations, excluding=reference)

    def _run_reopen(self, document: dict[str, Any]) -> dict[str, Any]:
        """Drive the staged reopen to its single audit event, or leave it repairable."""
        reference = str(document["intent"]["reference"])
        try:
            sprint = self.reader.show(reference, include_cards=False)
            if not (document.get("progress") or {}).get("opened_done"):
                # A staged reopen held nothing while it waited for its repeat, so the
                # installation is measured again before this sprint becomes the open one.
                self._check_conflicts(
                    [str(project) for project in sprint.get("reservations") or []],
                    excluding=reference,
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
            update_active_sprint_repositories(self.data_dir, sprint)
            return {"action": SPRINT_REOPENED, "sprint": sprint, "event_id": str(event["event_id"])}
        except TaskError as exc:
            if exc.code in _ADMISSION_REFUSALS or (
                exc.code in {"validation", "role_forbidden"} and not document.get("progress")
            ):
                raise
            raise TaskError(
                "audit_pending", "sprint reopen is pending repair; retry with the same request id", 4,
            ) from None
        except (OSError, KeyError, TypeError):
            raise TaskError(
                "audit_pending", "sprint reopen is pending repair; retry with the same request id", 4,
            ) from None

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
        update_active_sprint_repositories(Path(self.audit.board_dir).parent, sprint)
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
        update_active_sprint_repositories(Path(self.audit.board_dir).parent, sprint)
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
