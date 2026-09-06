"""The read half of the sprint surface: what a sprint can be built from, and what one is doing.

Two reads, and they are the two halves of one screen. Before a sprint exists a client has to be
able to *offer* the choices this installation actually has -- its products, the issues those
products still have open, the projects it has registered, and the head profiles it runs off -- and
after it exists somebody has to watch it. Neither is a new fact. Every value below is read from
the source that already owns it:

* products and issues from :class:`secretary.product_issues.ProductIssueStore`, the store
  `SprintWriter._check_ownership` proves ownership against;
* projects from :func:`secretary.product_issues.registered_projects`, the same set that refusal
  reads, and the reservations from `sprints/active-repositories.json`, the index the board's own
  write guard authorises against;
* head profiles from the installation's head registry (`heads/heads.yaml`), through
  :func:`secretary.head_registry.installed_heads`, with observer eligibility decided by calling
  :func:`secretary.sprint_observer.check_observer_profile` -- the check a create makes -- rather
  than by restating its rule here;
* the sprint itself from :class:`secretary.sprints.SprintReader`, and the observer's liveness from
  :func:`secretary.dispatcher_observer.observer_snapshot` over the dispatcher's own production
  state.

Both reads hold the properties of this package rather than describing them. They write nothing --
including, deliberately, no sprint board: `SprintReader.show` would create the board it reads from,
so a sprint is read here through `SprintReader.list(create=False)`, which cannot. They know nothing
about a transport. Each section carries its own availability, so an unreadable head registry blanks
the profile list and not the products beside it, and a dispatcher state nobody can read leaves the
sprint's own fields intact while saying that the observer's liveness is what could not be
established.

**Liveness is state the dispatcher already keeps.** A sprint's observer is raised by the production
tick -- an open sprint with no observer record gets one -- and this read never launches, never
looks at a terminal and never counts a pane. It reads the record, and the record's own heartbeat
classification, exactly as `secretary sprint status` does.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.config import InstanceReport, validate_instance
from secretary.dispatcher_observer import observer_snapshot
from secretary.head_registry import HeadRegistryConfigError, installed_heads
from secretary.product_issues import ProductIssueStore, registered_projects
from secretary.sprint_observer import (
    EXECUTOR_FIELDS,
    NONE_SPELLING,
    OBSERVER_FIELD,
    ObserverMetadataError,
    check_observer_profile,
    head_choice,
    installed_head_profiles,
    stored_executors,
)
from secretary.sprints import (
    SprintReader,
    active_sprint_projects,
    sprint_guard_index_initialized,
)
from secretary.tasks import KanboardClient, TaskError
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import InstallationUnavailable, TaskNotFound

SCHEMA_VERSION = 1

#: What a sprint's observer is doing, as far as anything durable can say. The first three are the
#: three states a watching page has to tell apart: the entity is saved and the tick has not raised
#: an observer for it yet; an observer is really up; nothing could be established at all. The other
#: two are distinctions the same source already makes and that folding into one of the three would
#: turn into a lie -- a head that was raised and is now gone did not "not start", and a sprint that
#: declared `--observer none` is not waiting for one.
OBSERVER_NOT_STARTED = "not_started"
OBSERVER_RUNNING = "running"
OBSERVER_UNAVAILABLE = "unavailable"
OBSERVER_STOPPED = "stopped"
OBSERVER_NOT_DECLARED = "not_declared"

OBSERVER_LAUNCH_STATES = (
    OBSERVER_NOT_STARTED,
    OBSERVER_RUNNING,
    OBSERVER_UNAVAILABLE,
    OBSERVER_STOPPED,
    OBSERVER_NOT_DECLARED,
)

#: How a sprint row carries its declared observer, kept as three states for the same reason
#: :func:`secretary.sprints._observer` keeps them: the repairs differ. A row with no field at all is
#: `absent`; one whose field is not an observer value is `malformed` and never read as absent.
OBSERVER_DECLARED = "declared"
OBSERVER_ABSENT = "absent"
OBSERVER_MALFORMED = "malformed"

#: Failures a source read may answer with instead of a value, caught per section exactly as the
#: card reads catch theirs.
_SOURCE_FAILURES = (
    TaskError,
    HeadRegistryConfigError,
    ObserverMetadataError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
)


class SprintReadLayer(ProtocolBoundary):
    """One installation's sprints, read with no knowledge of who is asking.

    Construction does no I/O, as both other layers' does not: every read resolves the instance, the
    board and the registry when it is called, so a long-lived transport never answers from a
    configuration it read at start-up.

    `board_client` is the seam a test -- or a transport with its own connection policy -- supplies
    its own board through. It is not a mode: the same code path runs with the live client.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._clock = clock

    # -- shared plumbing -------------------------------------------------------------------

    def report(self) -> InstanceReport:
        report = validate_instance(self.instance)
        if not report.ok or report.data_dir is None:
            raise InstallationUnavailable(
                "this instance config does not validate: "
                + "; ".join(str(error) for error in report.errors[:5])
            )
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    # -- operations ------------------------------------------------------------------------

    def sprint_options(self) -> dict[str, Any]:
        """What a sprint of this installation can be built from, from the sources that own it.

        The issues are the *admissible* ones and not every issue on the board: `_check_ownership`
        admits an open issue of the sprint's own product, so what is offered is exactly the open
        issues, each carrying the product that owns it. A client filters by the product it picked
        and cannot assemble a request the create would refuse for that reason.

        Nothing here needs the caller to know a technical identifier. Every entry has a `label`
        composed from what the source actually holds, and the identifier is a field beside it --
        which is what the create takes back.
        """
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_options",
            "observed_at": sources.isoformat(now),
            **self._catalogue(report, data_dir, now=now),
        }

    def sprint_state(self, ref: str) -> dict[str, Any]:
        """One sprint, and whether its observer is up: the page somebody watches a sprint on.

        The sprint's own fields and the observer's liveness are separate sources and fail apart. A
        dispatcher state that cannot be read leaves the goal, the reservations and the pins on the
        page and says that the liveness is what nobody could establish -- which is the opposite
        answer from an observer that is provably not running.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a sprint reference is required")
        report = self.report()
        data_dir = self.data_dir(report)
        sprint, source = self._sprint(reference, report, data_dir, now=now)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint",
            "observed_at": sources.isoformat(now),
            "ref": reference,
            "sprint": {"source": source.to_json(), "value": _sprint_value(sprint)},
            "observer": self._observer(reference, sprint, data_dir, now=now),
        }

    # -- sections --------------------------------------------------------------------------

    def _catalogue(self, report: InstanceReport, data_dir: Path, *, now: float) -> dict[str, Any]:
        products, issues = self._products_and_issues(report, data_dir, now=now)
        return {
            "products": products,
            "issues": issues,
            "projects": self._projects(data_dir, now=now),
            "heads": self._heads(now=now),
        }

    def _products_and_issues(
        self, report: InstanceReport, data_dir: Path, *, now: float
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The board's two halves of the catalogue, read once and reported apart.

        One store, one failure: the products and the issues come off the same board through the
        same reader, so a board that will not answer marks both sections unavailable with the same
        reason rather than leaving a client to wonder which of two reads failed.
        """
        evidence = data_dir / "board" / "cards.ndjson"
        try:
            store = ProductIssueStore(
                self._client(), data_dir=data_dir, instance=report.instance_path.parent
            )
            products = [
                {
                    "id": str(product.get("id") or ""),
                    "label": str(product.get("title") or "") or str(product.get("id") or ""),
                    "ref": str(product.get("ref") or ""),
                    "projects": [str(project) for project in product.get("projects") or []],
                }
                for product in store.list_products()
                if str(product.get("id") or "")
            ]
            # `include_closed=False` is the admissible half of `_check_ownership` and not a
            # convenience: a closed issue is refused there, so offering one would be offering a
            # request this installation will not accept.
            issues = [
                {
                    "ref": str(issue.get("ref") or ""),
                    "label": str(issue.get("title") or "") or str(issue.get("ref") or ""),
                    "product": str(issue.get("product") or ""),
                    "kind": str(issue.get("kind") or "") or None,
                    "priority": str(issue.get("priority") or "") or None,
                }
                for issue in store.list_issues(include_closed=False)
                if str(issue.get("ref") or "")
            ]
        except _SOURCE_FAILURES as exc:
            refused = sources.unavailable(
                f"the board could not be read: {_reason(exc)}", now=now, evidence=evidence
            ).to_json()
            return ({"source": refused, "items": []}, {"source": refused, "items": []})
        answered = sources.available(now).to_json()
        return (
            {"source": answered, "items": sorted(products, key=lambda item: item["id"])},
            {"source": answered, "items": sorted(issues, key=lambda item: item["ref"])},
        )

    def _projects(self, data_dir: Path, *, now: float) -> dict[str, Any]:
        """The registered projects, with the open sprint holding each one where one does.

        Both halves come from the two sources the refusals read: `registered_projects` is what an
        unknown project is refused against, and the guard index is what a project already reserved
        by an open sprint is refused against. A project marked held here is a project the create
        will refuse, said before the request is made rather than after.
        """
        try:
            registered = sorted(registered_projects(self.instance))
        except _SOURCE_FAILURES as exc:
            return {
                "source": sources.unavailable(
                    f"the project registry could not be read: {_reason(exc)}",
                    now=now,
                    evidence=self._instance_dir() / "projects",
                ).to_json(),
                "items": [],
            }
        # The guard index collapses "absent or unreadable" into an empty mapping, which here would
        # read as "held by nobody" -- the opposite of what an unreadable index proves. So the
        # predicate that tells the two apart is asked first, and `reserved_by` is null when the
        # index could not be established at all.
        reserved_known = sprint_guard_index_initialized(data_dir)
        held = active_sprint_projects(data_dir) if reserved_known else {}
        return {
            "source": sources.available(now).to_json(),
            "items": [
                {
                    "id": project,
                    "label": project,
                    "reserved_by": (sorted(held.get(project, [])) if reserved_known else None),
                }
                for project in registered
            ],
        }

    def _heads(self, *, now: float) -> dict[str, Any]:
        """The head profiles this installation runs off, as a sprint may name them.

        Read from the installed registry and never from a constant here: the profiles an
        installation has are its own, and a list written into this product would offer heads that
        do not exist on the host and hide the ones that do. Eligibility for the observer role is
        not restated either -- `check_observer_profile` is asked about each profile, so what is
        offered is exactly what a create will accept.
        """
        try:
            registry = installed_heads(self.instance)
            eligible = installed_head_profiles(self.instance)
        except _SOURCE_FAILURES as exc:
            return {
                "source": sources.unavailable(
                    f"the head registry could not be read: {_reason(exc)}",
                    now=now,
                    evidence=self._instance_dir() / "heads" / "heads.yaml",
                ).to_json(),
                "items": [],
                "observer": {"none": NONE_SPELLING, "default": None},
                "role_defaults": {},
                "executor_roles": list(EXECUTOR_FIELDS),
            }
        profiles = registry.get("profiles") or {}
        role_defaults = {
            str(role): str(profile)
            for role, profile in (registry.get("role_defaults") or {}).items()
            if isinstance(profile, str) and profile
        }
        defaults_by_profile: dict[str, list[str]] = {}
        for role, profile in sorted(role_defaults.items()):
            defaults_by_profile.setdefault(profile, []).append(role)
        items = []
        for profile_id in sorted(profiles):
            entry = profiles[profile_id] if isinstance(profiles[profile_id], dict) else {}
            observer_ok, observer_reason = _observer_eligibility(profile_id, eligible)
            items.append(
                {
                    "id": profile_id,
                    "label": _profile_label(profile_id, entry),
                    "adapter": str(entry.get("adapter") or "") or None,
                    "model": str(entry.get("model") or "") or None,
                    "effort": str(entry.get("effort") or "") or None,
                    "resource": str(entry.get("resource") or "") or None,
                    "observer": observer_ok,
                    "observer_reason": observer_reason,
                    "role_default_for": defaults_by_profile.get(profile_id, []),
                }
            )
        return {
            "source": sources.available(now).to_json(),
            "items": items,
            # The observer field takes one more answer than a profile id, and it is not a profile:
            # `none` says the sprint runs without an observer. It is offered here because a client
            # that had to know the word would be knowing a rule instead of reading one.
            "observer": {"none": NONE_SPELLING, "default": role_defaults.get("observer")},
            "role_defaults": role_defaults,
            # The two roles a sprint may pin, named by the model that owns them rather than spelled
            # again here.
            "executor_roles": list(EXECUTOR_FIELDS),
        }

    def _sprint(
        self, reference: str, report: InstanceReport, data_dir: Path, *, now: float
    ) -> tuple[dict[str, Any] | None, sources.Source]:
        """One sprint row, without creating the board it is on.

        `SprintReader.list(create=False)` and deliberately not `show`: `show` calls
        `ensure_sprint_board`, which creates the sprint board when the installation has none, and a
        read of this layer creates nothing. The listing carries everything a watching page needs --
        the goal, the definition of done, the ownership, the observer, the pins, the status, the
        current card and the resume -- because they all live in the row's own metadata.
        """
        thresholds = report.instance.get("sprint_budget") if isinstance(report.instance, dict) else None
        try:
            rows = SprintReader(
                self._client(),
                data_dir=data_dir,
                thresholds=thresholds if isinstance(thresholds, dict) else None,
            ).list(create=False)
        except _SOURCE_FAILURES as exc:
            return None, sources.unavailable(
                f"the sprint board could not be read: {_reason(exc)}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )
        row = next((sprint for sprint in rows if str(sprint.get("ref") or "") == reference), None)
        if row is None:
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        return row, sources.available(now)

    def _observer(
        self, reference: str, sprint: dict[str, Any] | None, data_dir: Path, *, now: float
    ) -> dict[str, Any]:
        """What this sprint declared, and whether that observer is actually up.

        Two facts, and they are read from two places on purpose. The declaration is the sprint's
        own field; the liveness is the dispatcher's durable production state, classified by
        `observer_snapshot` -- the same rows `secretary sprint status` shows. Nothing here consults
        a terminal, and no branch below treats the existence of one as evidence.
        """
        declared = _declared_observer(sprint)
        production = data_dir / "dispatcher" / "production-state.json"
        try:
            payload = json.loads(production.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("the dispatcher production state is not an object")
            rows = observer_snapshot(payload)
        except _SOURCE_FAILURES as exc:
            return {
                "declared": declared,
                "launch": {
                    "source": sources.unavailable(
                        f"the dispatcher production state could not be read: {_reason(exc)}",
                        now=now,
                        evidence=production,
                    ).to_json(),
                    "state": OBSERVER_UNAVAILABLE,
                    "reason": "the dispatcher production state could not be read",
                    "record": None,
                },
            }
        row = next((entry for entry in rows if str(entry.get("sprint") or "") == reference), None)
        state, reason = _launch_state(declared, row)
        return {
            "declared": declared,
            "launch": {
                "source": sources.available(now).to_json(),
                "state": state,
                "reason": reason,
                "record": None if row is None else _observer_record(row),
            },
        }

    # -- plumbing --------------------------------------------------------------------------

    def _client(self) -> Any:
        return self._board_client or KanboardClient.for_instance(self._instance_dir())

    def _instance_dir(self) -> Path:
        return self.instance.parent if self.instance.is_file() else self.instance


def _profile_label(profile_id: str, profile: dict[str, Any]) -> str:
    """A name a person can pick from, composed from what the registry actually holds.

    The registry has no display field, so one is composed rather than invented: the adapter, the
    model it pins and the effort it pins, in that order, and the words "default model" where a
    profile deliberately pins none. The identifier stays a field of its own beside this, because a
    label is for choosing and an id is for sending back.
    """
    parts = [str(profile.get("adapter") or "").strip() or profile_id]
    parts.append(str(profile.get("model") or "").strip() or "default model")
    effort = str(profile.get("effort") or "").strip()
    if effort:
        parts.append(f"{effort} effort")
    return " · ".join(parts)


def _observer_eligibility(profile_id: str, eligible: set[str]) -> tuple[bool, str | None]:
    """Whether a sprint may declare this profile as its observer, decided by the create's own check."""
    try:
        check_observer_profile(head_choice(profile_id), eligible, subject="sprint")
    except ObserverMetadataError as exc:
        return False, exc.message
    return True, None


def _declared_observer(sprint: dict[str, Any] | None) -> dict[str, Any]:
    """The observer a sprint row declares, in the three states the row can be in."""
    if sprint is None or "observer" not in sprint:
        return {"state": OBSERVER_ABSENT, "value": None, "profile": None}
    value = sprint.get("observer")
    if not isinstance(value, dict):
        return {"state": OBSERVER_MALFORMED, "value": None, "profile": None}
    return {
        "state": OBSERVER_DECLARED,
        "value": value,
        "profile": str(value.get("profile") or "") or None,
    }


def _launch_state(declared: dict[str, Any], row: dict[str, Any] | None) -> tuple[str, str]:
    """The observer's launch state, from the declaration and the dispatcher's own record."""
    if row is None:
        if declared["state"] == OBSERVER_DECLARED and (declared["value"] or {}).get("kind") == "none":
            return (
                OBSERVER_NOT_DECLARED,
                "this sprint declares no observer, so the tick raises none for it",
            )
        return (
            OBSERVER_NOT_STARTED,
            "the sprint is saved and the production tick holds no observer for it yet",
        )
    if bool(row.get("alive")):
        return OBSERVER_RUNNING, f"an observer head is up on {row.get('head') or 'an unnamed profile'}"
    return (
        OBSERVER_STOPPED,
        "the dispatcher holds an observer record whose head is not alive: "
        + str(row.get("heartbeat_state") or "no heartbeat evidence"),
    )


def _observer_record(row: dict[str, Any]) -> dict[str, Any]:
    """The part of the dispatcher's observer row a watching page reads, and nothing more."""
    return {
        "head": str(row.get("head") or "") or None,
        "state": str(row.get("state") or "") or None,
        "alive": bool(row.get("alive")),
        "pid_known": bool(row.get("pid_known")),
        "heartbeat_state": str(row.get("heartbeat_state") or "") or None,
        "bound": bool(row.get("bound")),
        "paused": bool(row.get("paused")),
        "launches": row.get("launches"),
        "last_action": str(row.get("last_action") or "") or None,
        "last_action_at": row.get("last_action_at"),
        "stopped_reason": str(row.get("stopped_reason") or "") or None,
    }


def _sprint_value(sprint: dict[str, Any] | None) -> dict[str, Any] | None:
    """One sprint as a watching page reads it: what it was opened with, and where it is now."""
    if sprint is None:
        return None
    executors = sprint.get("executors")
    return {
        "ref": str(sprint.get("ref") or ""),
        "goal": str(sprint.get("goal") or ""),
        "definition_of_done": str(sprint.get("definition_of_done") or ""),
        "status": str(sprint.get("status") or ""),
        "product": sprint.get("product"),
        "issues": sprint.get("issues"),
        "reservations": sprint.get("reservations"),
        "repositories": sprint.get("repositories") or [],
        "current_task": sprint.get("current_task"),
        # Always both roles and always a state, exactly as the reader gives them: "the owner pinned
        # nobody" is an answer and never a missing key.
        "executors": executors if isinstance(executors, dict) else stored_executors({}),
        "resume": sprint.get("resume"),
        "budget": sprint.get("budget"),
        "audit": sprint.get("audit"),
    }


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


#: Re-exported so a caller reading a sprint document does not have to know which module spells the
#: metadata field the declaration lives in.
__all__ = [
    "OBSERVER_ABSENT",
    "OBSERVER_DECLARED",
    "OBSERVER_FIELD",
    "OBSERVER_LAUNCH_STATES",
    "OBSERVER_MALFORMED",
    "OBSERVER_NOT_DECLARED",
    "OBSERVER_NOT_STARTED",
    "OBSERVER_RUNNING",
    "OBSERVER_STOPPED",
    "OBSERVER_UNAVAILABLE",
    "SCHEMA_VERSION",
    "SprintReadLayer",
]
