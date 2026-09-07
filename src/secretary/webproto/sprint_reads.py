"""The read half of the sprint surface: what a sprint can be built from, and what sprints are doing.

Five reads, and they are the halves of one screen plus the pages in front of it. Before a sprint
exists a client has to be able to *offer* the choices this installation actually has -- its
products, the issues those products still have open, the projects it has registered, and the head
profiles it runs off -- after it exists somebody has to watch it, somebody standing in front of
the whole installation has to be able to ask what is being worked on right now, a PO who left a
comment on a running sprint has to be able to ask what happened to it, and after it ends somebody
has to be able to read what its close decided. None of the five is
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
* what a close decided from the committed audit event it wrote, and which reservations survived it
  from `sprints/active-repositories.json` -- the index the board's own write guard authorises
  against, refused rather than answered `{}` here, because "this sprint holds no project any more"
  and "nobody could read the index" are opposite answers;
* the sprint itself from :class:`secretary.sprints.SprintReader`, and the observer's liveness from
  :func:`secretary.dispatcher_observer.observer_snapshot` over the dispatcher's own production
  state.

**The listing and the watched sprint are one read with two framings.** `sprint_list` and
`sprint_state` are assembled by `_read_once` from the same sources and both carry the same `work`
sections, decided by the same code. So neither can answer "what is this sprint doing" differently
from the other, and reading sixty sprints costs what reading one does: one pass over each source,
never one per sprint. What that deliberately does not buy is anything per sprint -- no comments, no
card opened, no CI backend asked -- and where a field cannot be established at that cost, the
section carrying it says so with a reason rather than reporting a value nothing backs.

**Five sources, told apart.** The installation config, the sprint board, the Pipeline listing, the
committed audit journal and the dispatcher's production state are read once each and fail apart:

| source | what it is | what it alone can settle |
| --- | --- | --- |
| `installation` | `instance.yaml`, validated | where this installation keeps its data, and its own budget thresholds |
| `sprints` | the sprint board, one pass with batched metadata | which sprints exist, and everything on their rows |
| `cards` | the Pipeline, one listing with batched metadata | which column each of a sprint's cards stands in |
| `journal` | `board/events.ndjson`, the committed audit | when the last significant event of an open sprint's cards happened |
| `liveness` | `dispatcher/production-state.json` | whether a head is really behind a card, and behind a sprint |

The journal is a source of its own and not a corner of the sprint board, even though
`SprintReader.status_views` is where it is consumed: it is a different file, it fails for different
reasons, and folding it in made an unreadable `board/events.ndjson` blank the sprint rows of a board
that had answered (secretary-1574, site 3). It is read here and handed to `status_views`, which then
opens nothing.

**One place enforces what every section owes.** No section decides its own attribution: they are all
assembled by :class:`SprintSections` through `secretary.webproto.section`, which runs a section's
rule only when every source that rule needs has answered, attributes the answer to the source that
produced it, and replaces what a refusal would have said with the section's declared no-claim shape.
Adding a section is adding a method there, and it is covered by being one. That module's docstring
carries the invariant and why it exists.

**A finished sprint is not a working one.** Its current card is kept, because it is where the sprint
got to, and it is qualified: `current_task.live` is false, its observer is `ended` rather than
"waiting to be raised", and its checks are `not_applicable`. Roughly sixty closed sprints of this
installation read as work in progress until that distinction existed.

**An installation whose config will not validate is a source that refused, not a refusal of the
operation.** With an explicit data directory and a usable board transport, a caller keeps every
answer the board can still give and the `installation` section says what could not be established --
which is the same rule as everywhere else, applied at the edge of the operation. Only a caller with
no explicit data directory is refused, because then nothing at all can be located.

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

**And so is delivery.** :meth:`SprintReadLayer.sprint_comment_delivery` answers where one saved
comment stands by placing its committed audit event against the delivery cursors
`secretary.dispatcher_observer` already keeps -- `acknowledged_through`, the batch stage and
`through_event`. It opens no second cursor, keeps no second store and schedules nothing: redelivery
belongs to the production tick, and this reports what that tick recorded. Delivery is a *batch*
fact, never a per-comment one, which is why the answer is a relation between two ids rather than a
field somebody would have to write per comment.

**Delivery is not acceptance, and this module never says otherwise.** Whether the observer read a
comment, agreed with it or took it into account is a semantic acknowledgement this product does not
have. It is deferred by the owner and tracked as :data:`ACCEPTANCE_ISSUE`; no state, field or
sentence below implies it, and the document says so in words (:data:`ACCEPTANCE_NOTICE`) exactly
where a reader might otherwise infer it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.board.backend import PRODUCT_ISSUE, SPRINT, board_client
from secretary.config import InstanceReport, validate_instance
from secretary.dispatch.headless import headless_cards
from secretary.dispatcher_observer import (
    DeliveryStage,
    ObserverDelivery,
    delivery_evidence_summary,
    observer_snapshot,
)
from secretary.head_registry import installed_heads
from secretary.product_issues import ProductIssueStore, registered_projects
from secretary.sprint_close import CLOSE_NOT_DONE
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
    SPRINT_CLOSED,
    SPRINT_STATUSES,
    SPRINT_TERMINAL_STATUSES,
    SprintReader,
    active_sprint_projects,
    audit_traversal,
    require_active_sprint_projects,
    sprint_guard_index_initialized,
)
from secretary.tasks import TaskAudit
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import InstallationUnavailable, TaskNotFound, ValidationRefused
from secretary.webproto.section import Reading, Rule, Section, SectionSet, SourceSet, render, rule

SCHEMA_VERSION = 1

#: The sources of a sprint document, in the precedence they are consulted in -- which is the order
#: in which a refusal is attributed, because it is the order in which the chain needs them. The
#: installation locates the data plane; the sprint board says which sprints exist at all; the
#: Pipeline listing says where each of their cards stands; the journal dates what happened to those
#: cards; and only then does the dispatcher say whether anything is actually behind them.
SOURCE_INSTALLATION = "installation"
SOURCE_SPRINTS = "sprints"
SOURCE_CARDS = "cards"
SOURCE_JOURNAL = "journal"
SOURCE_LIVENESS = "liveness"
#: And the reserved-project index, `sprints/active-repositories.json`. A source of its own for the
#: reason the journal is one: it is a different file, it fails for its own reasons, and a close is
#: answered about the reservations it released from it and from nothing else. Reading the sprint's
#: own declared reservations off the board says which projects the sprint holds; only this index says
#: whether the installation still holds them for it.
SOURCE_RESERVATIONS = "reservations"

#: The sources of the catalogue, in the same sense: the board the products and issues come off, the
#: project registry a refusal reads, and the installed head registry.
SOURCE_CATALOGUE = "catalogue"
SOURCE_REGISTRY = "registry"
SOURCE_HEADS = "heads"

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
#: And the fourth, which is not a state of a row but the absence of one: nobody could read the
#: sprint board, so what this sprint declares is not established. `absent` there would be an
#: affirmative claim about a row nobody has seen -- the dispatcher's production state proves only
#: that it holds no observer for this reference, never that the sprint declared none
#: (secretary-1574, site 4).
OBSERVER_UNKNOWN = "unknown"

OBSERVER_DECLARATION_STATES = (
    OBSERVER_DECLARED,
    OBSERVER_ABSENT,
    OBSERVER_MALFORMED,
    OBSERVER_UNKNOWN,
)

#: Whether the committed audit holds one comment of one sprint. `absent` is the journal's own
#: answer that no such event is on it; `unknown` is a journal nobody could read, and the two are
#: never spelled the same way for the reason every other section keeps them apart.
COMMENT_SAVED = "saved"
COMMENT_ABSENT = "absent"
COMMENT_UNKNOWN = "unknown"

COMMENT_STATES = (COMMENT_SAVED, COMMENT_ABSENT, COMMENT_UNKNOWN)

#: Where one saved comment stands in the observer's *technical* delivery, and nothing beyond it.
#:
#: Delivery is a batch fact: the dispatcher's cursor is `through_event` over this installation's
#: committed event stream, and no cursor is per comment. So the answer is the relation between this
#: comment's event and those cursors -- at or before `acknowledged_through` the batch carrying it
#: was acknowledged; after it the comment belongs to the current or a later batch, at whatever
#: stage that batch is in.
#:
#: **None of these five says the observer read, accepted or took the comment into account.** That
#: is a semantic acknowledgement this product does not have; it is deferred and tracked as
#: :data:`ACCEPTANCE_ISSUE`, and :data:`ACCEPTANCE_NOTICE` says so on every document that carries
#: one of these states.
DELIVERY_SAVED = "saved"
DELIVERY_WAITING = "waiting"
DELIVERY_HANDED_OVER = "handed_over"
DELIVERY_ERROR = "error"
#: And the fifth, which is never folded into any of the other four: the dispatcher's production
#: state could not be read, it holds no observer record for this sprint, or a cursor it does hold
#: names an event the committed audit cannot place. "Nobody could say where this comment is" and
#: "it is still waiting" are repaired by different people.
DELIVERY_UNKNOWN = "unknown"
#: And the sixth, which is the honest answer for a comment on a sprint that has ended. A PO may
#: comment on a closed or stopped sprint -- that is how the outcome is added after the fact -- and
#: no delivery batch will ever carry it: the production tick stops the observer of a sprint that is
#: no longer open and drops its record, so there is no head to wake and no cursor to move. Answering
#: `saved` there would say the batch has not carried it *yet*, which implies a delivery that cannot
#: happen; answering `unknown` would say nobody could tell, when this is exactly known.
DELIVERY_NOT_DELIVERABLE = "not_deliverable"

DELIVERY_STATES = (
    DELIVERY_SAVED,
    DELIVERY_WAITING,
    DELIVERY_HANDED_OVER,
    DELIVERY_ERROR,
    DELIVERY_NOT_DELIVERABLE,
    DELIVERY_UNKNOWN,
)

#: The delivery stages that mean a batch has been fixed and sent, or is being retried. `idle` is no
#: batch at all and `waiting_for_idle` is a batch with no upper bound yet, so both are answered
#: without a `through_event`.
_ACTIVE_STAGES = (DeliveryStage.DELIVERY_INTENT, DeliveryStage.AWAITING_ACK, DeliveryStage.RETRY_DEFERRED)

#: The deferred mechanism, named on the document rather than only in the documentation. A reader who
#: might otherwise take `handed_over` for "the observer has taken this into account" is told, in the
#: answer itself, that this product does not establish that and where the work to establish it is.
ACCEPTANCE_ISSUE = "issue:cf5c9f03ee0f92d3d347"
ACCEPTANCE_NOTICE = (
    "Delivery is not acceptance. Nothing in this document says the sprint's observer read this "
    "comment, agreed with it, or took it into account: what is established here is that the comment "
    "is saved and where the dispatcher's own delivery machinery has got it to. A semantic "
    f"acknowledgement is deferred by the owner and tracked as {ACCEPTANCE_ISSUE}."
)

#: Whether the committed audit holds the close this result is about. The same three states, kept
#: apart for the same reason: `absent` is the journal's own answer that no such close is on it, and
#: `unknown` is a journal nobody could read.
CLOSE_RECORDED = "recorded"
CLOSE_ABSENT = "absent"
CLOSE_UNKNOWN = "unknown"

CLOSE_STATES = (CLOSE_RECORDED, CLOSE_ABSENT, CLOSE_UNKNOWN)

#: Failures a source read may answer with instead of a value, caught per section exactly as the
#: card reads catch theirs.
#:
#: The tuple itself now lives in :data:`secretary.webproto.sources.SOURCE_FAILURES`, because the
#: pause reads have to catch exactly the same set and two hand-kept lists of "what a refused source
#: can raise" drift the first time one of them reads a new durable document (secretary-1576). The
#: name is kept here: it is what this module's own reads and their tests refer to.
_SOURCE_FAILURES = sources.SOURCE_FAILURES


@dataclass(frozen=True, slots=True)
class _Production:
    """The dispatcher's production state, read once and classified once for the whole document."""

    payload: dict[str, Any]
    #: `observer_snapshot`'s rows keyed by sprint, computed once rather than once per sprint.
    observers: dict[str, dict[str, Any]]

    def record(self, card: str) -> dict[str, Any] | None:
        """The dispatcher's record for one card, or `None` when it holds none for it."""
        records = self.payload.get("records")
        record = records.get(card) if isinstance(records, dict) else None
        return record if isinstance(record, dict) else None


#: One sprint as the sources have it: its board row and the status view over it, or `(None, None)`
#: when the sprint board did not answer. It is the value of the `sprints` source, narrowed to one
#: sprint, so every section sees exactly the part of that source it is about.
_Sprint = tuple[dict[str, Any] | None, dict[str, Any] | None]


class SprintSections(SectionSet):
    """Every section of every sprint document, and the only place a source is attributed to one.

    One method per section, and a section is covered by being one: `SectionSet` wraps each public
    method at class creation, so a section that answers with anything but a decided `Section` is a
    failure here rather than a document that quietly claims too much. Inside each method the rules
    are declarative -- which source may answer, which sources it needs, and what this section says
    when none of them can -- and `SourceSet.decide` is what holds the invariant over all of them.

    Nothing here reads a file. Every value comes from a source that was read once for the document,
    and a rule receives exactly the sources it declares, so a section physically cannot see a source
    it did not name.
    """

    # -- the watched sprint and the listing ------------------------------------------------

    def sprint(self, read: SourceSet) -> Section:
        """The sprint's own record, as a watching page reads it."""
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda sprint: None if sprint[0] is None else {"value": _sprint_value(sprint[0])},
            ),
            blank={"value": None},
            narrates=(),
        )

    def listing(self, read: SourceSet, items: Callable[[], list[dict[str, Any]]]) -> Section:
        """Every sprint of the installation, or `null` items when the board did not answer.

        `null` and never `[]`: an empty listing is the affirmative claim that this installation has
        no sprints, which is the opposite of a board that could not be read.
        """
        return read.decide(
            rule(SOURCE_SPRINTS, lambda _sprints: {"items": items()}),
            blank={"items": None},
            narrates=(),
        )

    # -- what one sprint is doing ------------------------------------------------------------

    def current_task(self, read: SourceSet) -> Section:
        """The sprint's current card, and whether it names work or a finished sprint's last card.

        The card of a sprint that ended is a fact worth keeping -- it is where the sprint got to --
        and it is exactly the field that made roughly sixty closed sprints of this installation read
        as if they were working. So it is kept and it is qualified: `live` is false for a closed or
        stopped sprint, and the reason says the card is the record of a sprint that ended.
        """

        def from_row(sprint: _Sprint) -> dict[str, Any] | None:
            if sprint[0] is None:
                return None
            reference, status, current = _subject(sprint)
            terminal = status in SPRINT_TERMINAL_STATUSES
            if current is None:
                return {
                    "ref": None,
                    "live": False,
                    "reason": (
                        f"{reference} ended with no current card"
                        if terminal
                        else f"{reference} has no current card: nobody has cut one for it"
                    ),
                }
            reason = (
                f"{reference} is {status}: {current} is the card it was on when it ended, "
                "not work in progress"
                if terminal
                else f"{reference} is open and its observer has {current} as the current card"
            )
            return {"ref": current, "live": not terminal, "reason": reason}

        return read.decide(
            rule(SOURCE_SPRINTS, from_row),
            blank={"ref": None, "live": False, "reason": None},
        )

    def decision(self, read: SourceSet, freshness: Section) -> Section:
        """The last observer decision on this sprint, with the freshness verdict beside it.

        The entry itself is on the sprint row. Its freshness is a different question with different
        sources, so it is a section of its own rather than a field of this one.
        """
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda sprint: (
                    None
                    if sprint[1] is None
                    else {"entry": sprint[1].get("resume"), "freshness": freshness}
                ),
            ),
            blank={"entry": None, "freshness": freshness},
            narrates=(),
        )

    def freshness(self, read: SourceSet) -> Section:
        """How fresh the last observer decision is, judged by whoever can judge it.

        A closed or stopped sprint is judged against its own frozen record and no cards at all,
        which is `SprintReader._resume_freshness`'s own rule, so the sprint row settles it and the
        verdict stands whatever else failed. An open sprint is judged against the significant events
        of its linked cards, which needs both the Pipeline listing and the committed journal: with
        either missing there is no verdict, and saying so is the whole of site 3's repair -- an
        unreadable `board/events.ndjson` marks *this* section unavailable and leaves the sprint row,
        the current card and the observer standing.
        """

        def frozen(sprint: _Sprint) -> dict[str, Any] | None:
            view = sprint[1]
            if view is None or str(view.get("status") or "") not in SPRINT_TERMINAL_STATUSES:
                return None
            return {"value": view.get("resume_freshness")}

        def judged(sprint: _Sprint, _linked: Any, _events: Any) -> dict[str, Any] | None:
            view = sprint[1]
            return None if view is None else {"value": view.get("resume_freshness")}

        return read.decide(
            rule(SOURCE_SPRINTS, frozen),
            Rule(SOURCE_JOURNAL, (SOURCE_SPRINTS, SOURCE_CARDS, SOURCE_JOURNAL), judged),
            blank={"value": None},
            narrates=(),
        )

    def cards(self, read: SourceSet) -> Section:
        """This sprint's cards by board state, or the reason nobody could group them.

        `states` is `null` and never `{}` when the Pipeline listing failed: an empty grouping is the
        affirmative claim that the sprint has no cards, which is the opposite of not knowing.
        """
        return read.decide(
            Rule(
                SOURCE_CARDS,
                (SOURCE_SPRINTS, SOURCE_CARDS),
                lambda sprint, _linked: (
                    None if sprint[1] is None else {"states": sprint[1].get("cards") or {}}
                ),
            ),
            blank={"states": None},
            narrates=(),
        )

    def degraded_cards(self, read: SourceSet) -> Section:
        """This sprint's cards standing in an active column with no worker anything can name."""
        return read.decide(
            Rule(
                SOURCE_LIVENESS,
                (SOURCE_SPRINTS, SOURCE_LIVENESS),
                lambda sprint, _production: (
                    None if sprint[1] is None else {"items": sprint[1].get("degraded_cards")}
                ),
            ),
            blank={"items": None},
            narrates=(),
        )

    def checks(self, read: SourceSet) -> Section:
        """The mandatory checks of this sprint's current card, as the dispatcher's record has them.

        The mechanical gate is the check the pipeline makes mandatory for a card, and the
        dispatcher's own production record is where its result lives: `gate_state` is `green` only
        for the current code state of that card, and it is cleared on every fresh entry to validate.
        Nothing is re-run here and no CI backend is called; a read establishes what is recorded, and
        says so when nothing records it.

        The two answers the sprint row settles on its own -- a sprint that has ended, and one with
        no current card -- are `not_applicable` under the `sprints` source, so a dispatcher state
        nobody could read neither changes them nor lends them its own unavailability. Only the
        states that really are the dispatcher's to say carry `liveness`.
        """

        def from_row(sprint: _Sprint) -> dict[str, Any] | None:
            reference, status, current = _subject(sprint)
            if sprint[1] is None:
                return None
            if status in SPRINT_TERMINAL_STATUSES:
                return {
                    "card": current,
                    "gate": None,
                    "state": CHECKS_NOT_APPLICABLE,
                    "reason": f"{reference} is {status}: no card of it is running checks",
                }
            if current is None:
                return {
                    "card": None,
                    "gate": None,
                    "state": CHECKS_NOT_APPLICABLE,
                    "reason": f"{reference} has no current card, so no card's checks are due",
                }
            return None

        def from_dispatcher(sprint: _Sprint, production: _Production) -> dict[str, Any] | None:
            _reference, _status, current = _subject(sprint)
            if sprint[1] is None or current is None:
                return None
            record = production.record(current)
            if record is None:
                return {
                    "card": current,
                    "gate": None,
                    "state": CHECKS_UNKNOWN,
                    "reason": (
                        f"the dispatcher holds no record for {current}, so nothing here says "
                        "whether its mandatory checks have passed"
                    ),
                }
            gate = _gate(record)
            if gate["state"] == "green":
                return {
                    "card": current,
                    "gate": gate,
                    "state": CHECKS_GREEN,
                    "reason": (
                        "the mechanical gate is green for "
                        f"{gate['attested_sha'] or 'the recorded candidate'}"
                    ),
                }
            return {
                "card": current,
                "gate": gate,
                "state": CHECKS_NOT_GREEN,
                "reason": _not_green_reason(gate, current),
            }

        return read.decide(
            rule(SOURCE_SPRINTS, from_row),
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), from_dispatcher),
            blank={"card": None, "gate": None, "state": CHECKS_UNKNOWN, "reason": None},
            # `card` is which card the answer would have been about, not a claim about its checks:
            # it is the sprint row's own field, and it is carried whenever the row answered.
            narrates=("reason", "card"),
            unresolved=lambda reading: {
                "card": _current_of(read),
                "gate": None,
                "state": CHECKS_UNKNOWN,
                "reason": reading.source.reason,
            },
        )

    def waiting(self, read: SourceSet) -> Section:
        """Where this sprint stands, and what it is standing on.

        Decided from what has already been read and never from a fresh source, in the order the
        sources can actually answer in, and each answer carries the source that decided it:

        * the sprint row alone decides `ended` (closed), `blocked` (stopped, with the stop reason)
          and the `waiting` of a sprint with no current card. Nothing the Pipeline or the dispatcher
          could say would change any of those;
        * the Pipeline listing decides `blocked` for a current card standing in Blocked -- the
          board's own statement, with the card's `blocked_by` -- before anything is asked of the
          dispatcher at all, readable or not. Its other answers, the `waiting` of a card in Ready,
          Issues or Done, are used wherever the dispatcher has nothing to add: it could not be read,
          or it holds no record for the card, which is why the dispatcher's own rule sits between
          the two;
        * only what is left needs the dispatcher: whether an active column really has a head behind
          it. A column is not evidence of that (`docs/OPERATIONS.md`, "A card sitting in In progress
          is not on its own evidence that anything is running"), so `working`, the degraded `blocked`
          and the bare "no record" are its answers.

        With every source in hand this changes nothing: the dispatcher's record still decides an
        active column, and the board's columns still decide the ones it settles.
        """

        def from_row(sprint: _Sprint) -> dict[str, Any] | None:
            reference, status, current = _subject(sprint)
            if sprint[1] is None:
                return None
            if status == "closed":
                return {"state": WAITING_ENDED, "reason": f"{reference} is closed: nothing is waiting on it"}
            if status == "stopped":
                stopped = sprint[1].get("stop_reason") or "no reason recorded"
                return {"state": WAITING_BLOCKED, "reason": f"{reference} was stopped: {stopped}"}
            if current is None:
                return {
                    "state": WAITING_WAITING,
                    "reason": f"{reference} has no current card: nobody has cut one for it",
                }
            return None

        def board_holds_it(sprint: _Sprint, linked: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
            """A card the board holds in Blocked is blocked, and no record makes it less so."""
            settled = _board_wait(*_card_of(sprint, linked))
            return None if settled is None or settled[0] != WAITING_BLOCKED else _said(settled)

        def dispatcher_holds_it(sprint: _Sprint, production: _Production) -> dict[str, Any] | None:
            _reference, _status, current = _subject(sprint)
            if sprint[1] is None or current is None:
                return None
            degraded = (sprint[1].get("degraded_cards") or {}).get(current)
            if degraded is not None:
                return {
                    "state": WAITING_BLOCKED,
                    "reason": (
                        f"{current} stands in an active column with no worker the dispatcher can "
                        f"name ({degraded.get('state') or 'no record state'})"
                    ),
                }
            record = production.record(current)
            if record is None:
                return None
            return {
                "state": WAITING_WORKING,
                "reason": f"the dispatcher record for {current} is {record.get('state') or 'unnamed'!s}",
            }

        def board_settled(sprint: _Sprint, linked: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
            settled = _board_wait(*_card_of(sprint, linked))
            return None if settled is None else _said(settled)

        def nothing_claimed(sprint: _Sprint, _production: _Production) -> dict[str, Any] | None:
            _reference, _status, current = _subject(sprint)
            if sprint[1] is None or current is None:
                return None
            return {
                "state": WAITING_WAITING,
                "reason": (
                    f"the dispatcher holds no record for {current}: nothing of it has been claimed yet"
                ),
            }

        return read.decide(
            rule(SOURCE_SPRINTS, from_row),
            Rule(SOURCE_CARDS, (SOURCE_SPRINTS, SOURCE_CARDS), board_holds_it),
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), dispatcher_holds_it),
            Rule(SOURCE_CARDS, (SOURCE_SPRINTS, SOURCE_CARDS), board_settled),
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), nothing_claimed),
            blank={"state": WAITING_UNKNOWN, "reason": None},
            unresolved=lambda reading: {
                "state": WAITING_UNKNOWN,
                "reason": _unsettled_reason(read, reading.source.reason),
            },
        )

    # -- the observer ------------------------------------------------------------------------

    def declaration(self, read: SourceSet) -> Section:
        """What this sprint's row declares, in the states a row can be in -- and `unknown` for none.

        A sprint board nobody could read leaves this `unknown`. `absent` would be the affirmative
        claim that the row carries no observer field, and no other source can establish that: the
        production state proves only that it holds no observer for this reference.
        """
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda sprint: None if sprint[0] is None else _declared_observer(sprint[0]),
            ),
            blank={"state": OBSERVER_UNKNOWN, "value": None, "profile": None},
            narrates=(),
        )

    def launch(self, read: SourceSet) -> Section:
        """Whether an observer is actually up, from the dispatcher's own production state.

        It needs the sprint row as well as the production state, and that is the point: what "no
        observer record" means depends on whether the sprint is saved and open, or finished, or
        declared none -- all facts of the row. Without the row the dispatcher's silence establishes
        nothing at all, so the section is unavailable rather than `not_started`.
        """

        def from_dispatcher(sprint: _Sprint, production: _Production) -> dict[str, Any] | None:
            row, _view = sprint
            if row is None:
                return None
            reference, status, _current = _subject(sprint)
            observer = production.observers.get(reference)
            state, reason = _launch_state(_declared_observer(row), observer, status)
            return {
                "state": state,
                "reason": reason,
                "record": None if observer is None else _observer_record(observer),
            }

        return read.decide(
            Rule(SOURCE_LIVENESS, (SOURCE_SPRINTS, SOURCE_LIVENESS), from_dispatcher),
            blank={"state": OBSERVER_UNAVAILABLE, "reason": None, "record": None},
            unresolved=lambda reading: {
                "state": OBSERVER_UNAVAILABLE,
                "reason": reading.source.reason or "the source that would say could not be read",
                "record": None,
            },
        )

    # -- one comment, and what happened to it ------------------------------------------------

    def comment(self, read: SourceSet, reference: str, comment_id: str) -> Section:
        """Whether the committed audit holds this comment on this sprint, and nothing more.

        The subject is an identifier, not a source: which comment is asked about comes from the
        caller, and the only thing that may answer is the journal the write landed in. A journal
        that could not be read leaves `unknown` -- never `absent`, which is the affirmative claim
        that the write is not on a file nobody has seen.
        """

        def from_journal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            event = _comment_event(events, reference, comment_id)
            if event is None:
                return {
                    "id": comment_id,
                    "state": COMMENT_ABSENT,
                    "occurred_at": None,
                    "role": None,
                    "reason": (
                        f"the committed audit holds no comment {comment_id} on {reference}"
                    ),
                }
            actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
            return {
                "id": comment_id,
                "state": COMMENT_SAVED,
                "occurred_at": str(event.get("occurred_at") or "") or None,
                "role": str(actor.get("role") or "") or None,
                "reason": f"the committed audit holds this comment on {reference}",
            }

        return read.decide(
            rule(SOURCE_JOURNAL, from_journal),
            blank={
                "id": None,
                "state": COMMENT_UNKNOWN,
                "occurred_at": None,
                "role": None,
                "reason": None,
            },
            # `id` is which comment the answer would have been about, not a claim about it.
            narrates=("reason", "id"),
            unresolved=lambda reading: {
                "id": comment_id,
                "state": COMMENT_UNKNOWN,
                "occurred_at": None,
                "role": None,
                "reason": reading.source.reason,
            },
        )

    def delivery(self, read: SourceSet, reference: str, comment_id: str) -> Section:
        """Where the dispatcher's own delivery machinery has got this comment to, and no further.

        Two sources and both are needed for the four answers that are about a batch: the committed
        audit places this comment and the dispatcher's cursors in one order, and the production
        state is where those cursors live. Neither is asked to do the other's job -- a journal that
        answered and holds no such comment settles the question on its own, because no cursor of any
        record could then be placed against it.

        Nothing here delivers. No head is woken, no retry is scheduled and no byte of the
        dispatcher's state is written: redelivery is the production tick's, and this is a read of
        what that tick has already recorded. And nothing here is a semantic acknowledgement --
        see :data:`ACCEPTANCE_NOTICE`.
        """

        def not_in_the_journal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            if _comment_event(events, reference, comment_id) is not None:
                return None
            return {
                "state": DELIVERY_UNKNOWN,
                "reason": (
                    f"the committed audit holds no comment {comment_id} on {reference}, so there is "
                    "nothing here to place against the observer's delivery cursors"
                ),
                "batch": None,
            }

        def sprint_has_ended(
            sprint: _Sprint, events: list[dict[str, Any]]
        ) -> dict[str, Any] | None:
            _ref, status, _current = _subject(sprint)
            if sprint[0] is None or status not in SPRINT_TERMINAL_STATUSES:
                return None
            return {
                "state": DELIVERY_NOT_DELIVERABLE,
                "reason": (
                    f"{reference} is {status}, so no delivery batch will carry this comment: the "
                    "production tick stops the observer of a sprint that is no longer open and drops "
                    "its record. The comment is saved on the committed audit, which is what a PO "
                    "adding the outcome after the fact is doing"
                ),
                "batch": None,
            }

        def from_dispatcher(
            events: list[dict[str, Any]], production: _Production
        ) -> dict[str, Any] | None:
            row = production.observers.get(reference)
            carried = (row or {}).get("delivery")
            delivery = (
                ObserverDelivery.from_json(carried) if isinstance(carried, dict) else None
            )
            state, reason = _delivery_state(
                events, _event_position(events, comment_id), delivery, reference=reference
            )
            return {
                "state": state,
                "reason": reason,
                "batch": None if delivery is None else _delivery_batch(delivery),
            }

        return read.decide(
            Rule(SOURCE_JOURNAL, (SOURCE_JOURNAL,), not_in_the_journal),
            # Before the dispatcher is consulted at all: a sprint that has ended has no observer to
            # deliver to, and its production record is dropped by the tick, so asking the cursors
            # would answer `unknown` -- "nobody could say" -- for something that is exactly known.
            Rule(SOURCE_SPRINTS, (SOURCE_SPRINTS, SOURCE_JOURNAL), sprint_has_ended),
            Rule(SOURCE_LIVENESS, (SOURCE_JOURNAL, SOURCE_LIVENESS), from_dispatcher),
            blank={"state": DELIVERY_UNKNOWN, "reason": None, "batch": None},
        )

    # -- one close, and what it decided ------------------------------------------------------

    def close(self, read: SourceSet, reference: str, event_id: str) -> Section:
        """What the committed audit records this close decided, and nothing this layer re-decides.

        The subject is an identifier the write answered with, exactly as a comment's is, and the only
        thing that may answer is the journal the close's own event landed in. Every field below is
        copied out of that event: the verdict on each declared issue, the disposition of each card
        that was not done, what was archived, the closeout the close wrote and where it is. A journal
        that could not be read leaves `unknown` -- never `absent`, which would be the affirmative
        claim that this installation never closed the sprint.

        Nothing here says the sprint's Definition of Done was reached; the document carries
        :data:`secretary.sprint_close.CLOSE_NOT_DONE` beside this section for the reader who might
        otherwise take `closed` for `done`.
        """

        def from_journal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            event = _close_event(events, reference, event_id)
            if event is None:
                return {
                    **_CLOSE_BLANK,
                    "id": event_id,
                    "state": CLOSE_ABSENT,
                    "reason": f"the committed audit holds no close {event_id} of {reference}",
                }
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
            actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
            return {
                "id": event_id,
                "state": CLOSE_RECORDED,
                "occurred_at": str(event.get("occurred_at") or "") or None,
                "closed_by": str(actor.get("id") or "") or None,
                "closing_reason": str(payload.get("reason") or "") or None,
                "issue_decisions": list(decisions.get("issues") or []),
                "closed_issues": list(payload.get("closed_issues") or []),
                "card_dispositions": list(decisions.get("cards") or []),
                "archived_tasks": list(payload.get("archived_tasks") or []),
                "disposed_tasks": list(payload.get("disposed_tasks") or []),
                "closeout": _closeout_of(payload.get("closeout")),
                "reason": f"the committed audit holds this close of {reference}",
            }

        return read.decide(
            rule(SOURCE_JOURNAL, from_journal),
            blank=dict(_CLOSE_BLANK),
            # `id` is which close the answer would have been about, not a claim about it.
            narrates=("reason", "id"),
            unresolved=lambda reading: {
                **_CLOSE_BLANK,
                "id": event_id,
                "reason": reading.source.reason,
            },
        )

    def reservations(self, read: SourceSet, reference: str) -> Section:
        """Which of this sprint's reserved projects the installation still holds for it.

        Two sources and both are needed: the sprint's row says which projects it reserved, and the
        reserved-project index -- the file the board's own write guard authorises against -- says
        which of them are still held. Neither can do the other's job, and an index nobody could read
        is never folded into "released": that would report a successor as admissible on the strength
        of a file nobody has seen.
        """

        def from_index(sprint: _Sprint, index: dict[str, list[str]]) -> dict[str, Any] | None:
            row = sprint[0]
            if row is None:
                return None
            declared = [str(project) for project in row.get("reservations") or []]
            held = [project for project in declared if reference in (index.get(project) or [])]
            released = [project for project in declared if project not in held]
            return {
                "declared": declared,
                "released": released,
                "held": held,
                "reason": (
                    f"the reserved-project index still holds {', '.join(held)} for {reference}"
                    if held
                    else f"the reserved-project index holds no project for {reference}"
                ),
            }

        return read.decide(
            Rule(SOURCE_RESERVATIONS, (SOURCE_SPRINTS, SOURCE_RESERVATIONS), from_index),
            blank={"declared": None, "released": None, "held": None, "reason": None},
        )

    # -- the catalogue -----------------------------------------------------------------------

    def products(self, read: SourceSet) -> Section:
        """The products a sprint may be opened on, off the board that owns them."""
        return read.decide(
            rule(SOURCE_CATALOGUE, lambda catalogue: {"items": catalogue[0]}),
            blank={"items": None},
            narrates=(),
        )

    def issues(self, read: SourceSet) -> Section:
        """The issues a create will admit -- the open ones, each carrying the product that owns it."""
        return read.decide(
            rule(SOURCE_CATALOGUE, lambda catalogue: {"items": catalogue[1]}),
            blank={"items": None},
            narrates=(),
        )

    def projects(self, read: SourceSet) -> Section:
        """The registered projects, with the open sprint holding each one where one does."""
        return read.decide(
            rule(SOURCE_REGISTRY, lambda registry: {"items": registry}),
            blank={"items": None},
            narrates=(),
        )

    def heads(self, read: SourceSet) -> Section:
        """The head profiles this installation runs off, as a sprint may name them."""
        return read.decide(
            rule(SOURCE_HEADS, lambda registry: registry),
            blank={
                "items": None,
                # The observer field takes one more answer than a profile id, and it is not a
                # profile: `none` says the sprint runs without an observer. It is offered here
                # because a client that had to know the word would be knowing a rule instead of
                # reading one. Both it and the roles below are this product's own vocabulary, so
                # they are the same whether the registry answered or not.
                "observer": {"none": NONE_SPELLING, "default": None},
                "role_defaults": {},
                "executor_roles": list(EXECUTOR_FIELDS),
            },
            narrates=(),
        )


#: One instance is enough: no section holds state, and the set exists to be enumerated as much as
#: to be called.
SECTIONS = SprintSections()


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
        """The validated installation, or the refusal a caller that needs one gets.

        The reads below do not go through this: they take the config as a source and carry on with
        what the other sources can still answer. It is here for a caller that really does need the
        validated config -- resolving the data directory when none was given is the only one.
        """
        report, refused = self._installation(now=self._clock())
        if report is None:
            raise InstallationUnavailable(str(refused.source.reason))
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _installation(self, *, now: float) -> tuple[InstanceReport | None, Reading]:
        """The installation config as a source, and the refusal only it can force.

        A config that does not validate is one more source that refused, and it removes exactly what
        it owns: where the data plane is, and this installation's own budget thresholds. With an
        explicit data directory the rest of the document is answered from the sources that did
        answer -- criterion 6 of secretary-1573 is that a caller does not lose an answer it has
        today, and "the config could not be validated" is not a reason to lose the board's.

        Without one there is nothing to fall back on: the data directory is what the config was
        being read for, so the operation is refused rather than answered from a guess.
        """
        report = validate_instance(self.instance)
        if report.ok and report.data_dir is not None:
            return report, Reading(SOURCE_INSTALLATION, sources.available(now), report)
        reason = (
            "this instance config does not validate: "
            + "; ".join(str(error) for error in report.errors[:5])
            if report.errors
            else "this instance config names no data directory"
        )
        if self._data_dir is None:
            raise InstallationUnavailable(reason)
        return None, Reading(
            SOURCE_INSTALLATION,
            sources.unavailable(reason, now=now, evidence=report.instance_path),
            None,
        )

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
        report, installation = self._installation(now=now)
        data_dir = self.data_dir(report)
        read = SourceSet(
            [
                installation,
                self._catalogue(data_dir, now=now),
                self._registry(data_dir, now=now),
                self._head_profiles(now=now),
            ]
        )
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_options",
                "observed_at": sources.isoformat(now),
                "products": SECTIONS.products(read),
                "issues": SECTIONS.issues(read),
                "projects": SECTIONS.projects(read),
                "heads": SECTIONS.heads(read),
                "installation": read.mark(SOURCE_INSTALLATION),
            }
        )

    def sprint_list(self, *, statuses: Sequence[str] | None = None) -> dict[str, Any]:
        """Every sprint of this installation, and what each one is actually doing.

        The listing and :meth:`sprint_state` are one read with two framings: both are assembled by
        `_read_once` from the same sources, and every sprint in either document carries the same
        sections, decided by the same code. A field the cheap read cannot establish says so in
        both, rather than being answered in one and omitted from the other.

        `statuses` filters by sprint status (`open`, `closed`, `stopped`) and never by anything the
        listing would have to read more to know. Filtering happens after the one board pass, so a
        filtered listing costs exactly what an unfiltered one does.
        """
        now = self._clock()
        wanted = _wanted_statuses(statuses)
        report, installation = self._installation(now=now)
        read = self._read_once(report, installation, self.data_dir(report), now=now)

        def items() -> list[dict[str, Any]]:
            rows, views = read.value(SOURCE_SPRINTS)
            return [
                {
                    **_identity(row, view),
                    **self._work(read.replacing(SOURCE_SPRINTS, (row, view))),
                    "observer": self._observer(read.replacing(SOURCE_SPRINTS, (row, view))),
                }
                for row, view in zip(rows, views, strict=True)
                if not wanted or str(view["status"]) in wanted
            ]

        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_list",
                "observed_at": sources.isoformat(now),
                "filter": {"statuses": sorted(wanted)},
                "sprints": SECTIONS.listing(read, items),
                # The sources every item's sections are marked by, said once for the document as
                # well: a board that will not answer leaves no items to carry a source of their own,
                # and a reader still has to be able to tell that from an installation with no
                # sprints.
                **self._marks(read),
            }
        )

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
        report, installation = self._installation(now=now)
        read = self._read_once(report, installation, self.data_dir(report), now=now)
        row, view = _find(read, reference)
        if row is None and read.answered(SOURCE_SPRINTS):
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        sprint = read.replacing(SOURCE_SPRINTS, (row, view))
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint",
                "observed_at": sources.isoformat(now),
                "ref": reference,
                "sprint": SECTIONS.sprint(sprint),
                "observer": self._observer(sprint),
                "work": self._work(sprint),
                **self._marks(read),
            }
        )

    def sprint_comment_delivery(self, ref: str, comment_id: str) -> dict[str, Any]:
        """What happened to one saved comment, as far as durable state can say -- and no further.

        The read half of the PO comment scenario. It answers two separate facts and never lets one
        stand in for the other: whether the comment is on the committed audit (`comment`), and where
        the dispatcher's observer delivery machinery has got it to (`delivery`). It answers a third
        thing by refusing to: `acceptance` says in words that neither of those is the observer
        having read, accepted or taken the comment into account, and names the deferred issue that
        would establish it.

        It is a read in the full sense of this layer. It wakes nothing, nudges nothing, retries
        nothing, launches no head and writes nothing to the dispatcher's state -- redelivery belongs
        to the production tick, and this reports what that tick has already recorded.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a sprint reference is required")
        identifier = str(comment_id or "")
        if not identifier:
            raise ValidationRefused(
                "reading what happened to a comment needs the identifier the write answered with"
            )
        report, installation = self._installation(now=now)
        read = self._read_once(report, installation, self.data_dir(report), now=now)
        row, view = _find(read, reference)
        if row is None and read.answered(SOURCE_SPRINTS):
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        # Narrowed to this sprint before any section is decided, exactly as a watched sprint is: the
        # delivery answer turns on whether *this* sprint has ended, and a section handed the whole
        # board could not tell.
        read = read.replacing(SOURCE_SPRINTS, (row, view))
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_comment_delivery",
                "observed_at": sources.isoformat(now),
                "ref": reference,
                "comment_id": identifier,
                "comment": SECTIONS.comment(read, reference, identifier),
                "delivery": SECTIONS.delivery(read, reference, identifier),
                # Not a section, because it is not read from anything: it is this product saying
                # what its own answer does not mean, and it says it whatever every source did.
                "acceptance": {"established": False, "issue": ACCEPTANCE_ISSUE, "reason": ACCEPTANCE_NOTICE},
                **self._marks(read),
            }
        )

    def sprint_close_result(self, ref: str, event_id: str) -> dict[str, Any]:
        """What one close decided and what it left behind, as far as durable state can say.

        The read half of the close scenario, and the answer a
        :meth:`~secretary.webproto.sprint_ops.SprintOperationLayer.sprint_close` carries back: what
        was decided for each declared issue and each remaining card, which reservations the
        installation still holds, where the closeout was written, and the sprint's new status. Each
        of those comes from the source that owns it -- the committed audit, the reserved-project
        index and the sprint board -- and they fail apart.

        **It says in one field and one sentence what a close is not.** `definition_of_done` is not a
        section, because it is not read from anything: it is this product stating that closing a
        sprint says what became of the work and never that the goal was reached. It says it whatever
        every source did.

        A read in the full sense: it writes nothing, closes nothing and reopens nothing.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a sprint reference is required")
        identifier = str(event_id or "")
        if not identifier:
            raise ValidationRefused(
                "reading what a close decided needs the identifier the close answered with"
            )
        report, installation = self._installation(now=now)
        read = self._read_once(report, installation, self.data_dir(report), now=now)
        row, view = _find(read, reference)
        if row is None and read.answered(SOURCE_SPRINTS):
            raise TaskNotFound(f"the board holds no sprint {reference!r}")
        sprint = read.replacing(SOURCE_SPRINTS, (row, view))
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "sprint_close_result",
                "observed_at": sources.isoformat(now),
                "ref": reference,
                "event_id": identifier,
                "close": SECTIONS.close(sprint, reference, identifier),
                "reservations": SECTIONS.reservations(sprint, reference),
                "sprint": SECTIONS.sprint(sprint),
                "definition_of_done": {"satisfied": False, "reason": CLOSE_NOT_DONE},
                **self._marks(read),
            }
        )

    # -- assembly ----------------------------------------------------------------------------

    def _work(self, sprint: SourceSet) -> dict[str, Any]:
        """What one sprint is doing, in the sections a listing and a watched page both carry."""
        return {
            "current_task": SECTIONS.current_task(sprint),
            "decision": SECTIONS.decision(sprint, SECTIONS.freshness(sprint)),
            "cards": SECTIONS.cards(sprint),
            "degraded_cards": SECTIONS.degraded_cards(sprint),
            "checks": SECTIONS.checks(sprint),
            "waiting": SECTIONS.waiting(sprint),
        }

    def _observer(self, sprint: SourceSet) -> dict[str, Any]:
        """What this sprint declared, and whether that observer is actually up.

        Two facts, and they are two sections on purpose. The declaration is the sprint's own field;
        the liveness is the dispatcher's durable production state, classified by `observer_snapshot`
        -- the same rows `secretary sprint status` shows. Nothing here consults a terminal, and no
        branch below treats the existence of one as evidence.
        """
        return {"declared": SECTIONS.declaration(sprint), "launch": SECTIONS.launch(sprint)}

    def _marks(self, read: SourceSet) -> dict[str, Any]:
        """The availability of every source of the document, said once for the document."""
        return {
            key: read.mark(key)
            for key in (SOURCE_CARDS, SOURCE_JOURNAL, SOURCE_LIVENESS, SOURCE_INSTALLATION)
        }

    # -- the sources -------------------------------------------------------------------------

    def _read_once(
        self, report: InstanceReport | None, installation: Reading, data_dir: Path, *, now: float
    ) -> SourceSet:
        """Every source a sprint document is built from, read once each.

        Once for the document and never once per sprint: the sprint rows and their metadata are one
        board pass, the linked cards of *every* sprint are one Pipeline listing, the committed audit
        is one traversal and the dispatcher's production state is one file read. That is the whole
        cost of listing sixty sprints, and it is the cost of watching one, because the two are the
        same read.

        They fail apart, and each one's failure marks only the sections it feeds. The Pipeline board
        is the one an installation may legitimately not have yet, and losing it must not blank the
        sprint that is right there on the board that answered. The journal is read here rather than
        inside `status_views` for exactly that reason: sharing a `try` with the board pass made an
        unreadable `board/events.ndjson` look like a sprint board that had failed.

        `SprintReader.list(create=False)` and deliberately not `show`: `show` calls
        `ensure_sprint_board`, which creates the sprint board when the installation has none, and a
        read of this layer creates nothing. `linked_cards` reads the Pipeline board through
        `TaskReader`, which has no create at all.
        """
        liveness = self._production(data_dir, now=now)
        journal = self._journal(data_dir, now=now)
        reader = SprintReader(self._client(), data_dir=data_dir, thresholds=_thresholds(report))
        cards = self._linked_cards(reader, data_dir, now=now)
        production: _Production | None = liveness.value if liveness.answered else None
        try:
            rows = reader.list(create=False)
            # Every rule about what a sprint's status view is stays in `SprintReader`; this call
            # re-decides none of them, and the observer rows and the headless episodes it takes are
            # the ones `secretary sprint status` already hands it. The journal it would otherwise
            # walk is handed to it, so nothing it does can fail for the journal's reasons.
            views = reader.status_views(
                rows,
                cards.value if cards.answered else {},
                observers=production.observers if production is not None else {},
                headless=headless_cards(production.payload if production is not None else {}),
                audit=audit_traversal(journal.value if journal.answered else []),
            )
            sprints = Reading(SOURCE_SPRINTS, sources.available(now), (rows, views))
        except _SOURCE_FAILURES as exc:
            sprints = Reading(
                SOURCE_SPRINTS,
                sources.unavailable(
                    f"the sprint board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ),
                None,
            )
        return SourceSet(
            [installation, sprints, cards, journal, liveness, self._reservations(data_dir, now=now)]
        )

    def _reservations(self, data_dir: Path, *, now: float) -> Reading:
        """The reserved-project index, read once for the document like every other source.

        One small file, read with the rest rather than per sprint, and refused rather than answered
        `{}`: for the write guard "nothing proven reserved" is the safe answer, and for a read that
        reports which reservations a close released it is the opposite of one.
        """
        path = data_dir / "sprints" / "active-repositories.json"
        try:
            index = require_active_sprint_projects(data_dir)
        except _SOURCE_FAILURES as exc:
            return Reading(
                SOURCE_RESERVATIONS,
                sources.unavailable(
                    f"the reserved-project index could not be read: {_reason(exc)}",
                    now=now,
                    evidence=path,
                ),
                None,
            )
        return Reading(SOURCE_RESERVATIONS, sources.available(now), index)

    def _production(self, data_dir: Path, *, now: float) -> Reading:
        """The dispatcher's durable production state, read and classified once for the document.

        A refusal is "nobody could say", never "nothing is running": every section built from this
        payload carries it rather than an empty value that reads as health.
        """
        path = data_dir / "dispatcher" / "production-state.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("the dispatcher production state is not an object")
            production = _Production(payload, _observer_rows(payload))
        except _SOURCE_FAILURES as exc:
            return Reading(
                SOURCE_LIVENESS,
                sources.unavailable(
                    f"the dispatcher production state could not be read: {_reason(exc)}",
                    now=now,
                    evidence=path,
                ),
                None,
            )
        return Reading(SOURCE_LIVENESS, sources.available(now), production)

    def _journal(self, data_dir: Path, *, now: float) -> Reading:
        """The committed audit, walked once for the whole document.

        A source of its own: it is the file the resume-freshness verdict is judged against, it is
        not the sprint board, and an installation can lose one without losing the other.
        """
        try:
            events = TaskAudit(data_dir).events()
        except _SOURCE_FAILURES as exc:
            return Reading(
                SOURCE_JOURNAL,
                sources.unavailable(
                    f"the committed audit journal could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "events.ndjson",
                ),
                None,
            )
        return Reading(SOURCE_JOURNAL, sources.available(now), events)

    def _linked_cards(self, reader: SprintReader, data_dir: Path, *, now: float) -> Reading:
        """Every sprint's cards, in one Pipeline listing, or the reason there are none to show."""
        try:
            linked = reader.linked_cards()
        except _SOURCE_FAILURES as exc:
            return Reading(
                SOURCE_CARDS,
                sources.unavailable(
                    f"the Pipeline board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ),
                None,
            )
        return Reading(SOURCE_CARDS, sources.available(now), linked)

    def _catalogue(self, data_dir: Path, *, now: float) -> Reading:
        """The board's two halves of the catalogue, read once and reported apart.

        One store, one failure: the products and the issues come off the same board through the
        same reader, so a board that will not answer marks both sections unavailable with the same
        reason rather than leaving a client to wonder which of two reads failed.

        `catalogue` reads that board once for both halves, so the form does not pay a second full
        pass to answer the same question twice.
        """
        try:
            store = ProductIssueStore(self._client(), data_dir=data_dir, instance=self._instance_dir())
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
            return Reading(
                SOURCE_CATALOGUE,
                sources.unavailable(
                    f"the board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ),
                None,
            )
        return Reading(
            SOURCE_CATALOGUE,
            sources.available(now),
            (
                sorted(products, key=lambda item: item["id"]),
                sorted(issues, key=lambda item: item["ref"]),
            ),
        )

    def _registry(self, data_dir: Path, *, now: float) -> Reading:
        """The registered projects, and which open sprint holds each one.

        Both halves come from the two sources the refusals read: `registered_projects` is what an
        unknown project is refused against, and the guard index is what a project already reserved
        by an open sprint is refused against. A project marked held here is a project the create
        will refuse, said before the request is made rather than after.
        """
        try:
            registered = sorted(registered_projects(self.instance))
        except _SOURCE_FAILURES as exc:
            return Reading(
                SOURCE_REGISTRY,
                sources.unavailable(
                    f"the project registry could not be read: {_reason(exc)}",
                    now=now,
                    evidence=self._instance_dir() / "projects",
                ),
                None,
            )
        # The guard index collapses "absent or unreadable" into an empty mapping, which here would
        # read as "held by nobody" -- the opposite of what an unreadable index proves. So the
        # predicate that tells the two apart is asked first, and `reserved_by` is null when the
        # index could not be established at all.
        reserved_known = sprint_guard_index_initialized(data_dir)
        held = active_sprint_projects(data_dir) if reserved_known else {}
        return Reading(
            SOURCE_REGISTRY,
            sources.available(now),
            [
                {
                    "id": project,
                    "label": project,
                    "reserved_by": (sorted(held.get(project, [])) if reserved_known else None),
                }
                for project in registered
            ],
        )

    def _head_profiles(self, *, now: float) -> Reading:
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
            return Reading(
                SOURCE_HEADS,
                sources.unavailable(
                    f"the head registry could not be read: {_reason(exc)}",
                    now=now,
                    evidence=self._instance_dir() / "heads" / "heads.yaml",
                ),
                None,
            )
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
        return Reading(
            SOURCE_HEADS,
            sources.available(now),
            {
                "items": items,
                "observer": {"none": NONE_SPELLING, "default": role_defaults.get("observer")},
                "role_defaults": role_defaults,
                # The two roles a sprint may pin, named by the model that owns them rather than
                # spelled again here.
                "executor_roles": list(EXECUTOR_FIELDS),
            },
        )

    # -- plumbing --------------------------------------------------------------------------

    def _client(self) -> Any:
        """One client for both boards this layer reads: sprints and Product/Issue."""
        return self._board_client or board_client(
            self._instance_dir(), serves=(SPRINT, PRODUCT_ISSUE)
        )

    def _instance_dir(self) -> Path:
        return self.instance.parent if self.instance.is_file() else self.instance


def _find(read: SourceSet, reference: str) -> _Sprint:
    """One sprint's row and status view, or `(None, None)` when the board did not answer."""
    if not read.answered(SOURCE_SPRINTS):
        return None, None
    rows, views = read.value(SOURCE_SPRINTS)
    for row, view in zip(rows, views, strict=True):
        if str(row.get("ref") or "") == reference:
            return row, view
    return None, None


def _subject(sprint: _Sprint) -> tuple[str, str, str | None]:
    """Which sprint a section is about, from the row the sprint board gave: ref, status, card."""
    row, view = sprint
    held = view or row or {}
    return (
        str(held.get("ref") or ""),
        str(held.get("status") or ""),
        str(held.get("current_task") or "") or None,
    )


def _current_of(read: SourceSet) -> str | None:
    """The current card of the sprint in front of these sources, when the board answered at all."""
    if not read.answered(SOURCE_SPRINTS):
        return None
    return _subject(read.value(SOURCE_SPRINTS))[2]


def _card_of(
    sprint: _Sprint, linked: dict[str, list[dict[str, Any]]]
) -> tuple[str | None, dict[str, Any] | None]:
    """The sprint's current card as the Pipeline listing has it, or `None` when it holds none."""
    reference, _status, current = _subject(sprint)
    if sprint[1] is None or current is None:
        return current, None
    return current, next(
        (
            entry
            for entry in linked.get(reference) or []
            if isinstance(entry, dict) and str(entry.get("ref") or "") == current
        ),
        None,
    )


def _said(settled: tuple[str, str]) -> dict[str, Any]:
    return {"state": settled[0], "reason": settled[1]}


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


def _thresholds(report: InstanceReport | None) -> dict[str, int] | None:
    """The installation's own budget thresholds, so a listed budget is judged by this instance.

    `None` when the config could not be validated: the product's own defaults are what is left, and
    the `installation` section of the document says that this installation's were not established.
    """
    instance = report.instance if report is not None else None
    thresholds = instance.get("sprint_budget") if isinstance(instance, dict) else None
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


def _unsettled_reason(read: SourceSet, refusal: str | None) -> str:
    """Why nothing settled this sprint, and what the sources that did answer had already said.

    The board's column is named when it is known, precisely so that the `unknown` does not read as
    "nothing at all is known about this card": what could not be established is narrower than that,
    and it is the part only the production state answers. Where the sprint board itself is what
    refused there is no card to name, and the refusal is the whole answer.
    """
    refused = refusal or "the source that would say could not be read"
    if not (read.answered(SOURCE_SPRINTS) and read.answered(SOURCE_CARDS)):
        return refused
    current, card = _card_of(read.value(SOURCE_SPRINTS), read.value(SOURCE_CARDS))
    if card is None:
        return refused
    state = str(card.get("state") or "") or "an unnamed column"
    return (
        f"{current} stands in {state.replace('_', ' ')}, which is not on its own evidence that "
        f"anything is running on it, and {refused}"
    )


#: What the close section says when nothing established it: every claim field at the value that
#: claims nothing. Declared once because three branches answer with it -- the journal's own "no such
#: close", a journal nobody could read, and the section's blank -- and a branch that spelled one of
#: them differently would be claiming something none of them establishes.
_CLOSE_BLANK: dict[str, Any] = {
    "id": None,
    "state": CLOSE_UNKNOWN,
    "occurred_at": None,
    "closed_by": None,
    "closing_reason": None,
    "issue_decisions": None,
    "closed_issues": None,
    "card_dispositions": None,
    "archived_tasks": None,
    "disposed_tasks": None,
    "closeout": None,
    "reason": None,
}


def _close_event(
    events: list[dict[str, Any]], reference: str, event_id: str
) -> dict[str, Any] | None:
    """The committed close event of this sprint under this identifier, or nothing.

    All three match, for the reason `_comment_event` matches all three: an event id alone would let
    a caller ask about one sprint's close and be answered about another sprint's write.
    """
    if not event_id:
        return None
    for event in events:
        if (
            str(event.get("event_id") or "") == event_id
            and str(event.get("ref") or "") == reference
            and str(event.get("kind") or "") == SPRINT_CLOSED
        ):
            return event
    return None


def _closeout_of(plan: Any) -> dict[str, Any] | None:
    """The closeout a close recorded, as the answer carries it, or `None` when it wrote none."""
    if not isinstance(plan, dict) or not plan.get("document"):
        return None
    return {
        "document": str(plan.get("document") or ""),
        "commit": str(plan.get("commit") or "") or None,
        "written": bool(plan.get("written")),
    }


def _comment_event(
    events: list[dict[str, Any]], reference: str, comment_id: str
) -> dict[str, Any] | None:
    """The committed `commented` event of this sprint under this identifier, or nothing.

    All three have to match. An event id alone would let a caller ask about one sprint's comment and
    be answered about another sprint's write, and the identifier this layer publishes is the audit
    event id precisely because the audit is what makes it durable.
    """
    if not comment_id:
        return None
    for event in events:
        if (
            str(event.get("event_id") or "") == comment_id
            and str(event.get("ref") or "") == reference
            and str(event.get("kind") or "") == "commented"
        ):
            return event
    return None


def _event_position(events: list[dict[str, Any]], event_id: str) -> int:
    """Where one event stands in the committed stream, or `-1` when the stream does not hold it.

    The whole stream and never one sprint's slice: the dispatcher's delivery cursors are ids of
    events of this installation, not of this sprint, so a narrowed stream could not place them.
    """
    if not event_id:
        return -1
    for index, event in enumerate(events):
        if str(event.get("event_id") or "") == event_id:
            return index
    return -1


def _delivery_state(
    events: list[dict[str, Any]],
    comment_at: int,
    delivery: ObserverDelivery | None,
    *,
    reference: str,
) -> tuple[str, str]:
    """One comment's technical delivery, from the cursors the dispatcher already keeps.

    The mapping, stated once and in one place:

    * **no observer record** for this sprint, or a cursor the committed audit cannot place --
      `unknown`. Neither is "not delivered": nothing here establishes where the comment is;
    * `acknowledged_through` at or after this comment's event -- `handed_over`. The batch that
      carried it was acknowledged by the head that was woken for it. That is delivery evidence and
      deliberately nothing more (:data:`ACCEPTANCE_NOTICE`);
    * `waiting_for_idle` -- `waiting`. A batch is owed and is held until the head is idle; it
      carries every event after the acknowledged cursor, so it carries this comment;
    * `delivery_intent` or `awaiting_ack` whose `through_event` is at or after this comment --
      `waiting`. The batch was fixed and sent and has not been acknowledged;
    * `retry_deferred` over the same range -- `error`, with the failure the record recorded. The
      dispatcher owns the retry; this says what it recorded, never what to do about it;
    * anything else -- `saved`. `idle`, or an active batch fixed *before* this comment arrived,
      which is the ordinary case: an event appended after a delivery intent is deliberately left
      for the next batch.
    """
    if delivery is None:
        return DELIVERY_UNKNOWN, (
            f"the dispatcher's production state holds no observer record for {reference}, so nothing "
            "here says whether a delivery batch has carried this comment"
        )
    acknowledged = delivery.acknowledged_through
    if acknowledged:
        at = _event_position(events, acknowledged)
        if at < 0:
            return DELIVERY_UNKNOWN, (
                f"the acknowledged delivery cursor {acknowledged} names an event the committed audit "
                "does not hold, so this comment cannot be placed against it"
            )
        if at >= comment_at:
            return DELIVERY_HANDED_OVER, (
                f"the observer of {reference} acknowledged a delivery batch through {acknowledged}, "
                "which is this comment's event or a later one; that is technical delivery of the "
                "batch and not a statement that the comment was read, accepted or taken into account"
            )
    if delivery.stage == DeliveryStage.WAITING_FOR_IDLE:
        return DELIVERY_WAITING, (
            f"a delivery batch for {reference} is held until its observer head is idle: "
            f"{delivery.reason or 'no reason recorded'}"
        )
    if delivery.stage in _ACTIVE_STAGES and delivery.through_event:
        at = _event_position(events, delivery.through_event)
        if at < 0:
            return DELIVERY_UNKNOWN, (
                f"the active delivery cursor {delivery.through_event} names an event the committed "
                "audit does not hold, so this comment cannot be placed against it"
            )
        if at >= comment_at:
            if delivery.stage == DeliveryStage.RETRY_DEFERRED:
                return DELIVERY_ERROR, (
                    "the delivery batch carrying this comment failed and is deferred for retry: "
                    + (delivery.last_failure_reason or "no reason recorded")
                    + "; the dispatcher owns the redelivery"
                )
            return DELIVERY_WAITING, (
                f"the delivery batch carrying this comment is {delivery.stage.value} and has not "
                "been acknowledged"
            )
    return (
        DELIVERY_SAVED,
        f"this comment is saved and no delivery batch of {reference}'s observer carries it yet",
    )


def _delivery_batch(delivery: ObserverDelivery) -> dict[str, Any]:
    """The part of one observer's delivery record this answer stands on, and nothing more.

    Copied out rather than passed through, exactly as `_gate` copies the mechanical gate: the record
    is the dispatcher's internal state, and a document handing all of it to a reader would publish
    those internals as a contract. `evidence` is `delivery_evidence_summary`, the same line the head
    that has to report its delivery history is given, so the two cannot disagree.
    """
    return {
        "stage": delivery.stage.value,
        "delivery_id": delivery.delivery_id or None,
        "through_event": delivery.through_event or None,
        "acknowledged_through": delivery.acknowledged_through or None,
        "acknowledged_delivery_id": delivery.acknowledged_delivery_id or None,
        "wake_attempts": delivery.wake_attempts,
        "wake_failures": delivery.wake_failures,
        "launch_delivery_failures": delivery.launch_delivery_failures,
        "last_failure_reason": delivery.last_failure_reason or None,
        "evidence": delivery_evidence_summary(delivery) or None,
    }


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
    "ACCEPTANCE_ISSUE",
    "ACCEPTANCE_NOTICE",
    "CHECKS_GREEN",
    "CHECKS_NOT_APPLICABLE",
    "CHECKS_NOT_GREEN",
    "CHECKS_UNKNOWN",
    "CHECK_STATES",
    "COMMENT_ABSENT",
    "COMMENT_SAVED",
    "COMMENT_STATES",
    "COMMENT_UNKNOWN",
    "DELIVERY_ERROR",
    "DELIVERY_HANDED_OVER",
    "DELIVERY_SAVED",
    "DELIVERY_STATES",
    "DELIVERY_UNKNOWN",
    "DELIVERY_WAITING",
    "OBSERVER_ABSENT",
    "OBSERVER_DECLARATION_STATES",
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
    "OBSERVER_UNKNOWN",
    "SCHEMA_VERSION",
    "SOURCE_CARDS",
    "SOURCE_INSTALLATION",
    "SOURCE_JOURNAL",
    "SOURCE_LIVENESS",
    "SOURCE_SPRINTS",
    "WAITING_BLOCKED",
    "WAITING_ENDED",
    "WAITING_STATES",
    "WAITING_UNKNOWN",
    "WAITING_WAITING",
    "WAITING_WORKING",
    "SprintReadLayer",
    "SprintSections",
]
