"""The read half of the sprint surface: what a sprint can be built from, and what sprints are doing.

Three reads, and they are the halves of one screen plus the page in front of it. Before a sprint
exists a client has to be able to *offer* the choices this installation actually has -- its
products, the issues those products still have open, the projects it has registered, and the head
profiles it runs off -- after it exists somebody has to watch it, and somebody standing in front of
the whole installation has to be able to ask what is being worked on right now. None of the three is
a new fact. Every value below is read from the source that already owns it:

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

**The listing and the watched sprint are one read with two framings.** `sprint_list` and
`sprint_state` are assembled by `_read_once` from the same three sources -- the sprint board, the
Pipeline listing and the dispatcher's production state -- and both carry the same `work` sections,
decided by the same code. So neither can answer "what is this sprint doing" differently from the
other, and reading sixty sprints costs what reading one does: one pass over each source, never one
per sprint. What that deliberately does not buy is anything per sprint -- no comments, no card
opened, no CI backend asked -- and where a field cannot be established at that cost, the section
carrying it says so with a reason rather than reporting a value nothing backs.

**A finished sprint is not a working one.** Its current card is kept, because it is where the sprint
got to, and it is qualified: `current_task.live` is false, its observer is `ended` rather than
"waiting to be raised", and its checks are `not_applicable`. Roughly sixty closed sprints of this
installation read as work in progress until that distinction existed.

All three reads hold the properties of this package rather than describing them. They write
nothing -- including, deliberately, no sprint board: `SprintReader.show` would create the board it reads from,
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.config import InstanceReport, validate_instance
from secretary.dispatch.headless import headless_cards
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
    SPRINT_STATUSES,
    SPRINT_TERMINAL_STATUSES,
    SprintReader,
    active_sprint_projects,
    sprint_guard_index_initialized,
)
from secretary.tasks import KanboardClient, TaskError
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import InstallationUnavailable, TaskNotFound, ValidationRefused

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
#: And the sixth, for the same reason the fifth exists. A closed or stopped sprint is not waiting
#: for a head to be raised: the tick stops the observer of a sprint that ended and drops its record,
#: so "no record" means the head is gone, not that it is coming. Reporting `not_started` there is
#: exactly what described roughly sixty finished sprints of this installation as sprints whose
#: observer had not come up yet.
OBSERVER_ENDED = "ended"

OBSERVER_LAUNCH_STATES = (
    OBSERVER_NOT_STARTED,
    OBSERVER_RUNNING,
    OBSERVER_UNAVAILABLE,
    OBSERVER_STOPPED,
    OBSERVER_NOT_DECLARED,
    OBSERVER_ENDED,
)

#: The state of the mandatory checks of a sprint's current card. `unknown` is not a failure of this
#: read and not a claim about the card: it is what a card no dispatcher record names looks like, and
#: it is never folded into `not_green`, because "the gate has not passed" and "nothing here says
#: whether it passed" are repaired by different people.
CHECKS_GREEN = "green"
CHECKS_NOT_GREEN = "not_green"
CHECKS_UNKNOWN = "unknown"
CHECKS_NOT_APPLICABLE = "not_applicable"

CHECK_STATES = (CHECKS_GREEN, CHECKS_NOT_GREEN, CHECKS_UNKNOWN, CHECKS_NOT_APPLICABLE)

#: Where a sprint stands: something is being worked on, something is waited for, something is
#: blocked, the sprint has ended, or the source that would say could not be read.
WAITING_WORKING = "working"
WAITING_WAITING = "waiting"
WAITING_BLOCKED = "blocked"
WAITING_ENDED = "ended"
WAITING_UNKNOWN = "unknown"

WAITING_STATES = (WAITING_WORKING, WAITING_WAITING, WAITING_BLOCKED, WAITING_ENDED, WAITING_UNKNOWN)

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


@dataclass(frozen=True, slots=True)
class _SprintPass:
    """One read of the three sources a sprint document is built from, and their availability.

    It exists so that the sections cannot each go and read again: everything below takes this and
    asks it, so listing sixty sprints costs the same three reads that watching one does.
    """

    #: The sprint rows, and the status view of each one, in the same order.
    rows: list[dict[str, Any]]
    views: list[dict[str, Any]]
    sprints: sources.Source
    #: Every sprint's linked cards, keyed by sprint reference, from the one Pipeline listing.
    linked: dict[str, list[dict[str, Any]]]
    cards: sources.Source
    #: The dispatcher's production state, or `None` when nobody could read it.
    production: dict[str, Any] | None
    liveness: sources.Source

    def find(self, reference: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        for row, view in zip(self.rows, self.views, strict=True):
            if str(row.get("ref") or "") == reference:
                return row, view
        return None, None

    def record(self, card: str) -> dict[str, Any] | None:
        """The dispatcher's record for one card, or `None` when it holds none for it."""
        records = (self.production or {}).get("records")
        record = records.get(card) if isinstance(records, dict) else None
        return record if isinstance(record, dict) else None


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

    def sprint_list(self, *, statuses: Sequence[str] | None = None) -> dict[str, Any]:
        """Every sprint of this installation, and what each one is actually doing.

        The listing and :meth:`sprint_state` are one read with two framings: both are assembled by
        `_read_once` from the same three sources, and every sprint in either document carries the
        same sections, decided by the same code. A field the cheap read cannot establish says so in
        both, rather than being answered in one and omitted from the other.

        `statuses` filters by sprint status (`open`, `closed`, `stopped`) and never by anything the
        listing would have to read more to know. Filtering happens after the one board pass, so a
        filtered listing costs exactly what an unfiltered one does.
        """
        now = self._clock()
        wanted = _wanted_statuses(statuses)
        report = self.report()
        data_dir = self.data_dir(report)
        read = self._read_once(report, data_dir, now=now)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_list",
            "observed_at": sources.isoformat(now),
            "filter": {"statuses": sorted(wanted)},
            "sprints": {
                "source": read.sprints.to_json(),
                "items": [
                    {
                        **_identity(row, view),
                        **self._work(row, view, read, now=now),
                        "observer": self._observer(str(view["ref"]), row, read, now=now),
                    }
                    for row, view in zip(read.rows, read.views, strict=True)
                    if not wanted or str(view["status"]) in wanted
                ],
            },
            # The two sources every item's sections are marked by, said once for the document as
            # well: a board that will not answer leaves no items to carry a source of their own,
            # and a reader still has to be able to tell that from an installation with no sprints.
            "cards": {"source": read.cards.to_json()},
            "liveness": {"source": read.liveness.to_json()},
        }

    def sprint_state(self, ref: str) -> dict[str, Any]:
        """One sprint, and whether its observer is up: the page somebody watches a sprint on.

        The sprint's own fields and the observer's liveness are separate sources and fail apart. A
        dispatcher state that cannot be read leaves the goal, the reservations and the pins on the
        page and says that the liveness is what nobody could establish -- which is the opposite
        answer from an observer that is provably not running.

        `work` is the same object one item of :meth:`sprint_list` carries, built by the same call:
        what the sprint's current card is and whether it is live, the last observer decision and
        its freshness, the state of the current card's mandatory checks, and what the sprint is
        waiting on.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a sprint reference is required")
        report = self.report()
        data_dir = self.data_dir(report)
        read = self._read_once(report, data_dir, now=now)
        row, view = read.find(reference)
        if row is None and read.sprints.state == sources.AVAILABLE:
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint",
            "observed_at": sources.isoformat(now),
            "ref": reference,
            "sprint": {"source": read.sprints.to_json(), "value": _sprint_value(row)},
            "observer": self._observer(reference, row, read, now=now),
            "work": self._work(row, view, read, now=now),
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

        `catalogue` reads that board once for both halves, so the form does not pay a second full
        pass to answer the same question twice.
        """
        evidence = data_dir / "board" / "cards.ndjson"
        try:
            store = ProductIssueStore(
                self._client(), data_dir=data_dir, instance=report.instance_path.parent
            )
            # `include_closed=False` is the admissible half of `_check_ownership` and not a
            # convenience: a closed issue is refused there, so offering one would be offering a
            # request this installation will not accept.
            raw_products, raw_issues = store.catalogue(include_closed=False)
            products = [
                {
                    "id": str(product.get("id") or ""),
                    "label": str(product.get("title") or "") or str(product.get("id") or ""),
                    "ref": str(product.get("ref") or ""),
                    "projects": [str(project) for project in product.get("projects") or []],
                }
                for product in raw_products
                if str(product.get("id") or "")
            ]
            issues = [
                {
                    "ref": str(issue.get("ref") or ""),
                    "label": str(issue.get("title") or "") or str(issue.get("ref") or ""),
                    "product": str(issue.get("product") or ""),
                    "kind": str(issue.get("kind") or "") or None,
                    "priority": str(issue.get("priority") or "") or None,
                }
                for issue in raw_issues
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

    def _read_once(self, report: InstanceReport, data_dir: Path, *, now: float) -> _SprintPass:
        """The three sources every sprint document of this layer is built from, read once each.

        Once for the document and never once per sprint: the sprint rows and their metadata are one
        board pass, the linked cards of *every* sprint are one Pipeline listing, and the
        dispatcher's production state is one file read. That is the whole cost of listing sixty
        sprints, and it is the cost of watching one, because the two are the same read.

        The three fail apart, and each one's failure marks only the sections it feeds. The Pipeline
        board is the one an installation may legitimately not have yet, and losing it must not blank
        the sprint that is right there on the board that answered.

        `SprintReader.list(create=False)` and deliberately not `show`: `show` calls
        `ensure_sprint_board`, which creates the sprint board when the installation has none, and a
        read of this layer creates nothing. `linked_cards` reads the Pipeline board through
        `TaskReader`, which has no create at all.
        """
        payload, liveness = self._production(data_dir, now=now)
        reader = SprintReader(self._client(), data_dir=data_dir, thresholds=_thresholds(report))
        linked, cards = self._linked_cards(reader, data_dir, now=now)
        try:
            rows = reader.list(create=False)
            # Every rule about what a sprint's status view is stays in `SprintReader`; this call
            # re-decides none of them, and the observer rows and the headless episodes it takes are
            # the ones `secretary sprint status` already hands it.
            views = reader.status_views(
                rows,
                linked or {},
                observers=_observer_rows(payload),
                headless=headless_cards(payload or {}),
            )
            sprints = sources.available(now)
        except _SOURCE_FAILURES as exc:
            rows, views = [], []
            sprints = sources.unavailable(
                f"the sprint board could not be read: {_reason(exc)}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )
        return _SprintPass(
            rows=rows,
            views=views,
            sprints=sprints,
            linked=linked or {},
            cards=cards,
            production=payload,
            liveness=liveness,
        )

    def _production(self, data_dir: Path, *, now: float) -> tuple[dict[str, Any] | None, sources.Source]:
        """The dispatcher's durable production state, read once for the whole document.

        `None` is "nobody could say", never "nothing is running": every section built from this
        payload carries the refusal below rather than an empty value that reads as health.
        """
        path = data_dir / "dispatcher" / "production-state.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("the dispatcher production state is not an object")
        except _SOURCE_FAILURES as exc:
            return None, sources.unavailable(
                f"the dispatcher production state could not be read: {_reason(exc)}",
                now=now,
                evidence=path,
            )
        return payload, sources.available(now)

    def _linked_cards(
        self, reader: SprintReader, data_dir: Path, *, now: float
    ) -> tuple[dict[str, list[dict[str, Any]]] | None, sources.Source]:
        """Every sprint's cards, in one Pipeline listing, or the reason there are none to show."""
        try:
            return reader.linked_cards(), sources.available(now)
        except _SOURCE_FAILURES as exc:
            return None, sources.unavailable(
                f"the Pipeline board could not be read: {_reason(exc)}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )

    def _work(
        self,
        row: dict[str, Any] | None,
        view: dict[str, Any] | None,
        read: _SprintPass,
        *,
        now: float,
    ) -> dict[str, Any]:
        """What one sprint is doing, in the sections a listing and a watched page both carry.

        Every section says which source answered it. `current_task` and `decision` come off the
        sprint row; `cards` and the freshness verdict need the Pipeline listing; `checks`,
        `degraded_cards` and `waiting` need the dispatcher's production state. None of them is
        inferred from another being empty.
        """
        reference = str((view or row or {}).get("ref") or "")
        status = str((view or row or {}).get("status") or "")
        current = str((view or row or {}).get("current_task") or "") or None
        cards = self._cards_section(view, read, now=now)
        return {
            "current_task": self._current_task(reference, status, current, read, now=now),
            "decision": self._decision(view, status, read, now=now),
            "cards": cards,
            # A sprint nobody could read has no cards to group and none to call degraded, and the
            # source that could not answer is the sprint board rather than the two behind these
            # sections: a null under an `available` source would be a section contradicting itself.
            "degraded_cards": (
                {"source": read.sprints.to_json(), "items": None}
                if view is None
                else {
                    "source": read.liveness.to_json(),
                    "items": view.get("degraded_cards") if read.production is not None else None,
                }
            ),
            "checks": self._checks(reference, status, current, view, read, now=now),
            "waiting": self._waiting(reference, status, current, view, read, now=now),
        }

    def _cards_section(
        self, view: dict[str, Any] | None, read: _SprintPass, *, now: float
    ) -> dict[str, Any]:
        """This sprint's cards by board state, or the reason nobody could group them.

        `states` is `null` and never `{}` when the Pipeline listing failed: an empty grouping is the
        affirmative claim that the sprint has no cards, which is the opposite of not knowing.
        """
        if view is None:
            return {"source": read.sprints.to_json(), "states": None}
        if read.cards.state != sources.AVAILABLE:
            return {"source": read.cards.to_json(), "states": None}
        return {"source": read.cards.to_json(), "states": view.get("cards") or {}}

    def _current_task(
        self, reference: str, status: str, current: str | None, read: _SprintPass, *, now: float
    ) -> dict[str, Any]:
        """The sprint's current card, and whether it names work or a finished sprint's last card.

        The card of a sprint that ended is a fact worth keeping -- it is where the sprint got to --
        and it is exactly the field that made roughly sixty closed sprints of this installation read
        as if they were working. So it is kept and it is qualified: `live` is false for a closed or
        stopped sprint, and the reason says the card is the record of a sprint that ended.
        """
        if read.sprints.state != sources.AVAILABLE:
            return {"source": read.sprints.to_json(), "ref": None, "live": False, "reason": None}
        terminal = status in SPRINT_TERMINAL_STATUSES
        if current is None:
            reason = (
                f"{reference} ended with no current card"
                if terminal
                else f"{reference} has no current card: nobody has cut one for it"
            )
            return {"source": read.sprints.to_json(), "ref": None, "live": False, "reason": reason}
        if terminal:
            reason = (
                f"{reference} is {status}: {current} is the card it was on when it ended, "
                "not work in progress"
            )
        else:
            reason = f"{reference} is open and its observer has {current} as the current card"
        return {
            "source": read.sprints.to_json(),
            "ref": current,
            "live": not terminal,
            "reason": reason,
        }

    def _decision(
        self, view: dict[str, Any] | None, status: str, read: _SprintPass, *, now: float
    ) -> dict[str, Any]:
        """The last observer decision on this sprint, and how fresh that decision is.

        The entry itself is on the sprint row and always answered. Its freshness is judged against
        the significant events of the sprint's linked cards, so an open sprint whose cards could not
        be listed gets a freshness section marked unavailable rather than a verdict computed over a
        card list nobody read. A closed or stopped sprint is judged against its own frozen record
        and no cards at all, which is `SprintReader._resume_freshness`'s own rule -- so its verdict
        stands even when the Pipeline board does not answer.
        """
        if view is None:
            return {
                "source": read.sprints.to_json(),
                "entry": None,
                "freshness": {"source": read.sprints.to_json(), "value": None},
            }
        backed = read.cards.state == sources.AVAILABLE or status in SPRINT_TERMINAL_STATUSES
        freshness_source = sources.available(now) if backed else read.cards
        return {
            "source": read.sprints.to_json(),
            "entry": view.get("resume"),
            "freshness": {
                "source": freshness_source.to_json(),
                "value": view.get("resume_freshness") if backed else None,
            },
        }

    def _checks(
        self,
        reference: str,
        status: str,
        current: str | None,
        view: dict[str, Any] | None,
        read: _SprintPass,
        *,
        now: float,
    ) -> dict[str, Any]:
        """The mandatory checks of this sprint's current card, as the dispatcher's record has them.

        The mechanical gate is the check the pipeline makes mandatory for a card, and the
        dispatcher's own production record is where its result lives: `gate_state` is `green` only
        for the current code state of that card, and it is cleared on every fresh entry to validate.
        Nothing is re-run here and no CI backend is called; a read establishes what is recorded, and
        says so when nothing records it.

        The order is the same rule `_waiting` follows, and for the same reason: the two answers the
        sprint row settles on its own -- a sprint that has ended, and one with no current card --
        are given first and carry the `sprints` source, so a dispatcher state nobody could read
        neither changes them nor lends them its own unavailability. Only the states that really are
        the dispatcher's to say are sourced from `liveness`.
        """
        card = {"card": current, "gate": None}
        if view is None:
            # The sprint itself could not be read, so neither its status nor its current card is
            # established, and there is nothing here to be `not_applicable` about.
            return {
                **card,
                "source": read.sprints.to_json(),
                "state": CHECKS_UNKNOWN,
                "reason": read.sprints.reason,
            }
        if status in SPRINT_TERMINAL_STATUSES:
            return {
                **card,
                "source": read.sprints.to_json(),
                "state": CHECKS_NOT_APPLICABLE,
                "reason": f"{reference} is {status}: no card of it is running checks",
            }
        if current is None:
            return {
                **card,
                "source": read.sprints.to_json(),
                "state": CHECKS_NOT_APPLICABLE,
                "reason": f"{reference} has no current card, so no card's checks are due",
            }
        card = {**card, "source": read.liveness.to_json()}
        if read.production is None:
            return {**card, "state": CHECKS_UNKNOWN, "reason": read.liveness.reason}
        record = read.record(current)
        if record is None:
            return {
                **card,
                "state": CHECKS_UNKNOWN,
                "reason": (
                    f"the dispatcher holds no record for {current}, so nothing here says whether "
                    "its mandatory checks have passed"
                ),
            }
        gate = _gate(record)
        if gate["state"] == "green":
            return {
                **card,
                "gate": gate,
                "state": CHECKS_GREEN,
                "reason": (
                    f"the mechanical gate is green for {gate['attested_sha'] or 'the recorded candidate'}"
                ),
            }
        return {**card, "gate": gate, "state": CHECKS_NOT_GREEN, "reason": _not_green_reason(gate, current)}

    def _waiting(
        self,
        reference: str,
        status: str,
        current: str | None,
        view: dict[str, Any] | None,
        read: _SprintPass,
        *,
        now: float,
    ) -> dict[str, Any]:
        """Where this sprint stands, and what it is standing on.

        Decided from what has already been read and never from a fresh source, and in the order the
        sources can actually answer in, which is the point of the ordering rather than an accident
        of it: **a source that refused never shadows an answer another source already gave.** Each
        branch below is reached only when every source that could have answered more definitely has
        been asked, and each carries the source that decided it:

        * the sprint row alone decides `ended` (closed), `blocked` (stopped, with the stop reason)
          and the `waiting` of a sprint with no current card. Nothing the Pipeline or the dispatcher
          could say would change any of those, so they are answered first and sourced from `sprints`;
        * the Pipeline listing decides `blocked` for a current card standing in Blocked -- the
          board's own statement, with the card's `blocked_by` -- before anything is asked of the
          dispatcher at all, readable or not, and sourced from `cards`. Its other two answers, the
          `waiting` of a card in Ready, Issues or Done, are used wherever the dispatcher has nothing
          to add: it could not be read, or it holds no record for the card;
        * only what is left needs the dispatcher: whether an active column really has a head behind
          it. A column is not evidence of that (`docs/OPERATIONS.md`, "A card sitting in In progress
          is not on its own evidence that anything is running"), so `working`, the degraded
          `blocked` and the `unknown` of an unreadable production state are its answers and carry
          `liveness` -- and that `unknown` is now the answer only to the part the board could not
          settle, naming the column it did establish.

        Round 1 of this card had the production-availability branch above the board ones, so an
        unreadable dispatcher state hid a blocked reason the Pipeline listing had already
        established at the listing's fixed cost. That is the collapse `sources.py` exists to
        prevent, and `WaitingSourceIsolationTests` is what holds the order now.
        """
        if view is None:
            return {"source": read.sprints.to_json(), "state": WAITING_UNKNOWN, "reason": read.sprints.reason}
        if status == "closed":
            return {
                "source": read.sprints.to_json(),
                "state": WAITING_ENDED,
                "reason": f"{reference} is closed: nothing is waiting on it",
            }
        if status == "stopped":
            return {
                "source": read.sprints.to_json(),
                "state": WAITING_BLOCKED,
                "reason": f"{reference} was stopped: {view.get('stop_reason') or 'no reason recorded'}",
            }
        if current is None:
            return {
                "source": read.sprints.to_json(),
                "state": WAITING_WAITING,
                "reason": f"{reference} has no current card: nobody has cut one for it",
            }
        card = self._current_card(reference, current, read)
        settled = _board_wait(current, card)
        board = (
            None
            if settled is None
            else {"source": read.cards.to_json(), "state": settled[0], "reason": settled[1]}
        )
        # A card the board holds in Blocked is blocked, and no dispatcher record makes it less so:
        # this is the board's own statement of why the sprint is standing still, and it is answered
        # before anything is asked of the production state -- readable or not.
        if board is not None and board["state"] == WAITING_BLOCKED:
            return board
        if read.production is None:
            # The rest of the board's answers are used exactly here, where the dispatcher cannot
            # improve on them: "nobody has claimed it" and "the card is done" are established by the
            # column, and returning `unknown` instead would hide an answer this document already
            # holds behind a source that has nothing to do with it.
            return board or {
                "source": read.liveness.to_json(),
                "state": WAITING_UNKNOWN,
                "reason": _unsettled_reason(current, card, read.liveness.reason),
            }
        degraded = (view.get("degraded_cards") or {}).get(current)
        if degraded is not None:
            return {
                "source": read.liveness.to_json(),
                "state": WAITING_BLOCKED,
                "reason": (
                    f"{current} stands in an active column with no worker the dispatcher can name "
                    f"({degraded.get('state') or 'no record state'})"
                ),
            }
        record = read.record(current)
        if record is None:
            # Same rule once more: with no record to describe the card, the column is the better
            # answer where it settles one, and the bare "no record" is what is left when it does not.
            return board or {
                "source": read.liveness.to_json(),
                "state": WAITING_WAITING,
                "reason": (
                    f"the dispatcher holds no record for {current}: nothing of it has been claimed yet"
                ),
            }
        return {
            "source": read.liveness.to_json(),
            "state": WAITING_WORKING,
            "reason": f"the dispatcher record for {current} is {record.get('state') or 'unnamed'!s}",
        }

    def _current_card(
        self, reference: str, current: str, read: _SprintPass
    ) -> dict[str, Any] | None:
        """The sprint's current card as the Pipeline listing has it, or `None` when it does not.

        `None` covers both "the listing could not be read" and "the listing holds no such card", and
        the caller treats them the same way on purpose: neither establishes anything about the card,
        and the sections that say why a source could not answer are `cards` and `liveness`.
        """
        if read.cards.state != sources.AVAILABLE:
            return None
        return next(
            (
                entry
                for entry in read.linked.get(reference) or []
                if isinstance(entry, dict) and str(entry.get("ref") or "") == current
            ),
            None,
        )

    def _observer(
        self, reference: str, sprint: dict[str, Any] | None, read: _SprintPass, *, now: float
    ) -> dict[str, Any]:
        """What this sprint declared, and whether that observer is actually up.

        Two facts, and they are read from two places on purpose. The declaration is the sprint's
        own field; the liveness is the dispatcher's durable production state, classified by
        `observer_snapshot` -- the same rows `secretary sprint status` shows. Nothing here consults
        a terminal, and no branch below treats the existence of one as evidence.
        """
        declared = _declared_observer(sprint)
        if read.production is None:
            return {
                "declared": declared,
                "launch": {
                    "source": read.liveness.to_json(),
                    "state": OBSERVER_UNAVAILABLE,
                    "reason": "the dispatcher production state could not be read",
                    "record": None,
                },
            }
        row = _observer_rows(read.production).get(reference)
        state, reason = _launch_state(declared, row, str((sprint or {}).get("status") or ""))
        return {
            "declared": declared,
            "launch": {
                "source": read.liveness.to_json(),
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


def _wanted_statuses(statuses: Sequence[str] | None) -> set[str]:
    """The status filter, refused rather than silently answered when it names a status nobody has.

    An empty filter is every sprint. A status this product does not have is a `validation` refusal:
    answering it with an empty listing would tell a client its filter matched nothing, which is a
    different fact from its filter being wrong.
    """
    wanted = {str(status) for status in statuses or ()}
    unknown = sorted(wanted - SPRINT_STATUSES)
    if unknown:
        raise ValidationRefused(
            f"unknown sprint statuses: {', '.join(unknown)}; this product has "
            + ", ".join(sorted(SPRINT_STATUSES))
        )
    return wanted


def _identity(row: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    """Which sprint this is, and what it was opened for, from the status view that already has it."""
    return {
        "ref": str(view.get("ref") or ""),
        "goal": str(view.get("goal") or ""),
        "status": str(view.get("status") or ""),
        "product": view.get("product"),
        "issues": view.get("issues"),
        "reservations": view.get("reservations"),
        "repositories": row.get("repositories") or [],
        "executors": view.get("executors") or stored_executors({}),
        "budget": view.get("budget"),
    }


def _thresholds(report: InstanceReport) -> dict[str, int] | None:
    """The installation's own budget thresholds, so a listed budget is judged by this instance."""
    thresholds = report.instance.get("sprint_budget") if isinstance(report.instance, dict) else None
    return thresholds if isinstance(thresholds, dict) else None


def _observer_rows(production: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The dispatcher's observer rows, keyed by sprint, classified once for the whole document."""
    if production is None:
        return {}
    return {
        str(row.get("sprint") or ""): row
        for row in observer_snapshot(production)
        if str(row.get("sprint") or "")
    }


def _gate(record: dict[str, Any]) -> dict[str, Any]:
    """The mechanical gate as one card's dispatcher record holds it, and nothing more.

    Copied out rather than passed through: a record carries the whole attempt, and a document that
    handed all of it to a reader would be publishing the dispatcher's internals as a contract.
    """
    attestation = record.get("gate_attestation")
    attestation = attestation if isinstance(attestation, dict) else {}
    return {
        "state": str(record.get("gate_state") or "") or None,
        # `validated_sha` is what the receipt calls the candidate it is bound to; `base_sha` is what
        # it was validated against, and reporting that one as the attested candidate would name the
        # wrong commit.
        "attested_sha": str(attestation.get("validated_sha") or "") or None,
        "base_sha": str(attestation.get("base_sha") or "") or None,
        "gate_mode": str(attestation.get("gate_mode") or "") or None,
        "pending_since": record.get("gate_pending_since") or None,
        "transport_error": str(record.get("gate_transport_error") or "") or None,
        "record_state": str(record.get("state") or "") or None,
    }


def _not_green_reason(gate: dict[str, Any], card: str) -> str:
    """Why the current card's gate is not green, from the record's own evidence and no other."""
    if gate["transport_error"]:
        return f"the gate backend did not answer for {card}: {gate['transport_error']}"
    if gate["pending_since"]:
        return f"the gate for {card} is waiting on a run that has not finished"
    return (
        f"the mechanical gate has not passed for the current code state of {card} "
        f"(the dispatcher record is {gate['record_state'] or 'unnamed'})"
    )


#: Board states of a current card that settle where its sprint stands on their own. `blocked` is the
#: board's own statement that the card is held, with the reason recorded on it; the other three are
#: columns in which the board itself says nothing is running -- a card nobody has claimed, and one
#: whose work is finished and is waiting for the next cut. Deliberately not here: `in_progress`,
#: `validate` and `assessment`, because a column is not evidence that a head is behind it, which is
#: the whole lesson of `degraded_cards` (secretary-1544).
_BOARD_SETTLED_STATES = ("blocked", "ready", "issues", "done")


def _board_wait(reference: str, card: dict[str, Any] | None) -> tuple[str, str] | None:
    """Where the Pipeline listing alone puts the sprint, or `None` when it does not settle it.

    Answered before the dispatcher's production state is consulted, and independently of whether
    that state can be read at all: this is established at the listing's own cost, and an unrelated
    source that refused must not take it away.
    """
    if card is None:
        return None
    state = str(card.get("state") or "")
    if state == "blocked":
        reason = str(card.get("blocked_by") or "") or "no reason recorded on the card"
        return WAITING_BLOCKED, f"{reference} stands in Blocked: {reason}"
    if state in ("ready", "issues"):
        return (
            WAITING_WAITING,
            f"{reference} stands in {state.replace('_', ' ')} and nothing has claimed it yet",
        )
    if state == "done":
        return (
            WAITING_WAITING,
            f"{reference} is done: the sprint is waiting for its observer to cut the next card",
        )
    return None


def _unsettled_reason(reference: str, card: dict[str, Any] | None, refusal: str | None) -> str:
    """Why the dispatcher was needed here, and what the board did say before it refused.

    The board's column is named when it is known, precisely so that the `unknown` does not read as
    "nothing at all is known about this card": what could not be established is narrower than that,
    and it is the part only the production state answers.
    """
    refused = refusal or "the dispatcher production state could not be read"
    if card is None:
        return refused
    state = str(card.get("state") or "") or "an unnamed column"
    return (
        f"{reference} stands in {state.replace('_', ' ')}, which is not on its own evidence that "
        f"anything is running on it, and {refused}"
    )


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


def _launch_state(
    declared: dict[str, Any], row: dict[str, Any] | None, status: str
) -> tuple[str, str]:
    """The observer's launch state, from the declaration, the sprint's status and the record.

    The status is here because the absence of a record means opposite things on the two sides of a
    sprint's end. Before it, the tick has not raised the observer yet; after it, the tick has
    stopped that head and dropped its record, and calling that `not_started` describes a sprint that
    finished long ago as one whose observer is still coming up.
    """
    if row is None:
        if status in SPRINT_TERMINAL_STATUSES:
            return (
                OBSERVER_ENDED,
                f"this sprint is {status}: the tick stopped its observer and holds no record for it",
            )
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
    """The part of the dispatcher's observer row a watching page reads, and nothing more.

    `delivery` is in it because it is not decoration: the observer skill copies `delivery_id` and
    `through_event` from this record into the resume that acknowledges the batch it was woken for,
    so a document that narrowed it away would take a live protocol's own evidence off the surface
    the observer reads. `deferred_reason` and the idle pair are here for the same reason -- they are
    why a declared observer is not up, and a launch state alone does not say it.
    """
    delivery = row.get("delivery")
    return {
        "head": str(row.get("head") or "") or None,
        "delivery": delivery if isinstance(delivery, dict) else None,
        "deferred_reason": str(row.get("deferred_reason") or "") or None,
        "idle_since": row.get("idle_since") or None,
        "idle_reason": str(row.get("idle_reason") or "") or None,
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
    "CHECKS_GREEN",
    "CHECKS_NOT_APPLICABLE",
    "CHECKS_NOT_GREEN",
    "CHECKS_UNKNOWN",
    "CHECK_STATES",
    "OBSERVER_ABSENT",
    "OBSERVER_DECLARED",
    "OBSERVER_ENDED",
    "OBSERVER_FIELD",
    "OBSERVER_LAUNCH_STATES",
    "OBSERVER_MALFORMED",
    "OBSERVER_NOT_DECLARED",
    "OBSERVER_NOT_STARTED",
    "OBSERVER_RUNNING",
    "OBSERVER_STOPPED",
    "OBSERVER_UNAVAILABLE",
    "SCHEMA_VERSION",
    "WAITING_BLOCKED",
    "WAITING_ENDED",
    "WAITING_STATES",
    "WAITING_UNKNOWN",
    "WAITING_WAITING",
    "WAITING_WORKING",
    "SprintReadLayer",
]
