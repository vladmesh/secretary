"""What was commanded on this installation, and what became of one request id.

Two reads, and one written contract between them.

**`command_history`** is the operator's "what happened here lately": a page of the last commands
*across every entity* -- cards, sprints, products, issues -- each one carrying the four fields a
history is made of. Who initiated it (`actor`), what the action was (`action`), which entity it was
aimed at (`entity`), and how it ended (`result`). Until it existed, the only cross-entity answer was
to open the journal file, and the only protocol answer was
:meth:`~secretary.webproto.reads.ReadLayer.task_events`, which is one card's slice and cannot be
asked about the installation.

**`command_request`** is the answer to "what became of the request id I sent". Before it, a caller
learned that by *re-sending the operation* and reading the repeat's answer. That is safe -- every
operation below is idempotent on its request id -- but it is not a read: it is available only to
whoever still holds the original arguments, and it makes an operator perform a mutation to satisfy
a question. So this answers the same thing from the audit's own two lookups, and never by doing
anything: not found, pending with what is already done and how to continue safely, or committed
with its result and the entity it produced.

**Neither opens a second store, index, scheduler or registry of operations.** Everything below is
already durable and already read by the writers themselves:

* the history is :meth:`secretary.tasks.TaskAudit.events`, the released cross-entity traversal,
  which defaults to every entity and returns committed records in append order;
* the request lookup is :meth:`secretary.tasks.TaskAudit.committed_event` and
  :meth:`secretary.tasks.TaskAudit.pending_event` -- the pair `SprintWriter._write` itself consults
  to decide that a repeat is a no-op. This read re-decides none of it and calls no writer;
* the paging is this layer's own frozen-offset cursor (:mod:`secretary.webproto.cursor`) and its
  :data:`~secretary.webproto.journal.DEFAULT_LIMIT` and
  :data:`~secretary.webproto.journal.MAX_LIMIT`.

**Newest first means the journal's order reversed, and nothing else.** `occurred_at` is stamped by
the writer, so two events can share a second and a clock can go backwards; the append order is the
only order that is a fact. A page is therefore the tail of the traversal, handed back in reverse,
and the document says which order it is in rather than letting a reader assume a sort by time.

**A position is an ordinal here, not a byte offset.** `task_events` pages one card's slice of the
journal by seeking to a byte, because it reads the file itself. This read pages the traversal's own
sequence, so its cursor carries how many committed records stand before the oldest row of the next
page. Both are positions in one append-only file and both are frozen for the same reason -- nothing
before them can ever change -- and the two cannot be confused for each other: a history cursor is
bound to :data:`HISTORY_SCOPE`, the empty reference, and every card cursor is bound to a card.

**What the history is honest about.** An audit nobody could read is an unavailable source and never
an empty history -- including the case the released traversal answers `[]` for, a journal file that
is not there at all, which this read refuses rather than publishes. A page that reached the
beginning of the history is told from one the limit cut short by `has_more`. And an entity kind the
record does not carry stays `null`: the generic audit records beside the typed ones say which
reference they are about but not what kind of entity it is, and inventing "card" for them would be
the fabrication this layer's section seam exists to prevent.

**And `unknown` is a state of its own, in both reads.** A request id the audit could not be read for
is `unknown`, never `not_found`: "this installation never saw that request" and "nobody could say"
are opposite answers, and folding them is how a caller is told an operation did not happen when it
may well have. That is the blank of the section, so it is what a refusal *must* say -- the seam
raises rather than letting a rule claim otherwise. `unknown` covers the audit as a whole and not one
lookup of it: a staged record found beside a journal nobody could read still answers `unknown`,
because a committed record for the same request cannot be ruled out and "pending" would be a claim
that it was ruled out.

**Neither read performs, retries or repairs anything.** A read that repairs is not a read: nothing
here calls a writer, stages, commits, discards or reconciles a pending record, and nothing takes the
audit lock. What a pending request needs is described (:data:`OPERATION_IDENTITY`) and left to the
caller who owns the operation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.board.models import Event
from secretary.config import InstanceReport, validate_instance
from secretary.tasks import TaskAudit, _event_action
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.cursor import Cursor, decode
from secretary.webproto.errors import InvalidCursor, ValidationRefused
from secretary.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from secretary.webproto.section import Reading, Section, SectionSet, SourceSet, render, rule

SCHEMA_VERSION = 1

#: The sources of these documents, in the precedence a refusal is attributed in. The installation
#: locates the data plane and therefore the journal; the audit is the journal itself.
SOURCE_INSTALLATION = "installation"
SOURCE_AUDIT = "audit"

#: What a cross-entity page's cursor is bound to: no entity, because the page is about all of them.
#:
#: The cursor codec binds a position to a reference so that a cursor from another card is an error
#: rather than a plausible-looking wrong answer. That property is what makes this safe: every card
#: cursor names a card, this one names none, and neither reader will honour the other's.
HISTORY_SCOPE = ""

#: The four states :meth:`CommandReadLayer.command_request` may answer with. `unknown` is one of
#: them and is never spelled as one of the others.
STATE_COMMITTED = "committed"
STATE_PENDING = "pending"
STATE_NOT_FOUND = "not_found"
STATE_UNKNOWN = "unknown"

#: What the history covers, stated on the document rather than left to be inferred from its rows.
#: Read from no source: it is what this read *is*, so it is said even when every source refused.
HISTORY_EXTENT = {
    "entities": "all",
    "records": "committed",
    "order": "journal_append_reversed",
    "statement": (
        "This is the committed board audit of the whole installation, newest first, across every "
        "entity it holds -- cards, sprints, products and issues alike. Newest means the journal's "
        "append order reversed and not a sort by `occurred_at`, which the writer stamps and which "
        "two commands can share. A staged operation that has not committed is not in it: it is not "
        "an event yet, and `command_request` is where one is read. A record the released traversal "
        "cannot parse is not in it either, and neither is anything the audit does not hold."
    ),
}

#: The one place the operation-identity contract is written down, and the value the prose is held to.
#:
#: Which operations take a `request_id`, what repeating one means for each, which take none and why,
#: and what the part-done failures promise about what is already done. All of it was true before this
#: card and none of it was in one place, so a caller learned it from five docstrings and a test.
#:
#: It is a value rather than prose for the reason `PAUSE_ERRORS` is: a published sentence that only a
#: document states goes stale on the next change. `tests/test_web_command_protocol.py` derives both
#: sets from the operation layers' own signatures -- the operations that name a `request_id`
#: parameter, and the pause operations that do not -- and holds `docs/PROTOCOLS.md` to this table, so
#: an operation that gains or loses its identity fails here rather than leaving a public promise
#: behind.
OPERATION_IDENTITY: dict[str, Any] = {
    "with_request_id": {
        "run_start": {
            "layer": "OperationLayer",
            "repeat": (
                "returns the same run. It raises no second head and cuts no second workspace: the "
                "request id owns the run, and a reconnect is the same call."
            ),
        },
        "run_review": {
            "layer": "OperationLayer",
            "repeat": (
                "returns the same reviewer run over the same worker result, and raises no second "
                "reviewer head."
            ),
        },
        "sprint_create": {
            "layer": "SprintOperationLayer",
            "repeat": (
                "resumes the sprint this request already opened, finishing whatever step was owed. "
                "A new request id would open a second sprint beside the half-written one, which is "
                "why a part-done create answers with this id rather than with a plain failure."
            ),
        },
        "sprint_comment": {
            "layer": "SprintOperationLayer",
            "repeat": (
                "returns the comment this request already saved. It never writes a second comment "
                "on the sprint."
            ),
        },
        "sprint_close": {
            "layer": "SprintOperationLayer",
            "repeat": (
                "resumes the staged close, keeps the plan it was staged with and repeats no step it "
                "already committed. A new request id would open a second close beside a "
                "half-finished one."
            ),
        },
    },
    "without_request_id": {
        "pause_drain": {
            "layer": "PauseOperationLayer",
            "reason": (
                "the pause is idempotent in its own mode by its own rule: a drain over a pipeline "
                "already draining changes nothing and says so, and a drain over a freeze is refused "
                "as a conflict. A repeat therefore needs no key, and adding one for uniformity "
                "would buy an operation-id ceremony over a command that completes in one call."
            ),
        },
        "pause_resume": {
            "layer": "PauseOperationLayer",
            "reason": (
                "a resume over a pipeline that is not paused is a no-op that reports itself as one. "
                "Its idempotence is the state of the flag, not a recorded request."
            ),
        },
    },
    "errors": {
        "OperationPending": {
            "code": "backend_unavailable",
            "promises": (
                "the operation is durably part-done and repairable, so 'it did not finish' is not "
                "'it did not happen'. What it already did is staged, and the safe move is to repeat "
                "this same request id."
            ),
            "action": ["operation", "repeat_request", "request_id", "reference"],
        },
        "audit_pending": {
            "exit_status": 4,
            "promises": (
                "the writer's own spelling of the same fact, and the exit status the sprint close "
                "has always answered with. The staged record is kept, never discarded, and a repeat "
                "with the same request id resumes it."
            ),
        },
        "close_conflict": {
            "exit_status": 3,
            "promises": (
                "the close was refused on the state of the world rather than on its arguments -- "
                "the sprint moved under it -- and nothing was written. It is not a part-done "
                "operation and repeating it unchanged will be refused again."
            ),
        },
        "PauseCommandCompleted": {
            "promises": (
                "the pause or resume itself completed and only the report of it could not be "
                "rendered. The command answers with what it did rather than failing, because a "
                "failure would invite a retry of a command that already succeeded."
            ),
        },
    },
}

#: The codes each read of this module can refuse with, checked against `docs/PROTOCOLS.md` by a test.
#: `backend_unavailable` reaching a caller from :mod:`secretary.webproto.boundary` for an
#: implementation failure is the layer-wide contract of every operation here and is deliberately not
#: listed per read.
COMMAND_ERRORS: dict[str, tuple[str, ...]] = {
    "command_history": ("validation",),
    "command_request": ("validation",),
}


class _Unreadable(Exception):
    """Inside one source read: the durable document could not be read at all."""


def _source(
    key: str,
    produce: Callable[[], Any],
    *,
    refusal: Callable[[Exception], str],
    now: float,
    evidence: Path | None,
) -> Reading:
    """Read one source's durable document, or say that it could not answer.

    The whole span of the broad catch, and the span is the contract -- the rule
    :mod:`secretary.webproto.pause_reads` states and holds. A source read is the one place whose
    entire job is to answer "did this source answer", so anything raised while reading and
    converting that one document becomes an unavailable `Reading`. It enumerates no exception types,
    because a list is what has to be kept in step: the audit alone can raise `OSError` for a journal
    it cannot open and `TaskError` for a pending directory in a layout this release will not guess
    at, and the next durable read added here would be the next hole.

    Everything outside the span -- the sections, the paging, the assembly of a document -- is this
    layer's own work, and a failure there is a defect that travels as itself.
    """
    try:
        return Reading(key, sources.available(now), produce())
    except Exception as exc:  # noqa: BLE001 -- the span above is the reason this is broad
        return Reading(key, sources.unavailable(refusal(exc), now=now, evidence=evidence), None)


@dataclass(frozen=True, slots=True)
class _History:
    """The committed audit, read and converted once for the whole document."""

    records: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Lookup:
    """What the audit's own two lookups say about one request id, read once for the document.

    Both are read inside the source span, so a pending directory this release refuses to guess at is
    a source that did not answer rather than an exception past the seam -- and never a `not_found`.
    """

    request_id: str
    committed: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None


def _page(records: tuple[dict[str, Any], ...], cursor: Cursor | None, limit: int) -> dict[str, Any]:
    """The newest `limit` records at or before `cursor`, newest first, and where reading continues.

    The position is an ordinal in the traversal's append-ordered sequence: `offset` is how many
    committed records stand before the oldest row handed out, so the next page is what is older than
    this one. It is frozen for the same reason a byte offset is -- the journal only grows, so no
    record can ever appear before one already counted.
    """
    end = len(records) if cursor is None else cursor.offset
    if end > len(records):
        raise InvalidCursor(
            "this cursor is past the end of the committed audit, which only ever grows; "
            "the journal it was issued for is not the journal being read"
        )
    start = max(0, end - limit)
    return {
        "items": [_row(record) for record in reversed(records[start:end])],
        "next_cursor": Cursor(ref=HISTORY_SCOPE, offset=start).encode(),
        # True only when the limit cut the page short, so a page that reached the beginning of the
        # history is told from one that stopped because it was full.
        "has_more": start > 0,
    }


def _row(record: dict[str, Any]) -> dict[str, Any]:
    """One committed audit record as a command: initiator, action, target entity, result.

    Deliberately not :func:`secretary.webproto.journal._item`, and deliberately holding the same
    rule. That one is a card's own history: it carries a cursor per event, the transition and the
    related refs, because a client watching one card needs them. This is a line of a command
    history, which is the four fields and nothing else. What is shared is the rule that matters: a
    typed protocol event carries the `reason` its writer gave and a released generic audit record
    carries the `outcome` of its backend effect, and neither is renamed into the other's field.
    """
    actor = record.get("actor")
    # The journal's own discriminator, and the one `EventJournal` and `BoardEventCanon` read: a
    # typed protocol event declares its record type, and everything else on this journal is a
    # released generic audit record.
    typed = record.get("record_type") == Event.RECORD_TYPE
    return {
        "occurred_at": _text(record.get("occurred_at")) or None,
        "request_id": _text(record.get("request_id")) or None,
        "event_id": _text(record.get("event_id")) or None,
        "actor": (
            {"id": _text(actor.get("id")), "role": _text(actor.get("role"))}
            if isinstance(actor, dict)
            else None
        ),
        "action": _event_action(record),
        "kind": _text(record.get("kind")),
        "entity": _entity(record, typed),
        "result": {
            "outcome": _text(record.get("outcome")) or None,
            "reason": _text(record.get("reason")) or None,
        },
        "typed": typed,
    }


def _entity(record: dict[str, Any], typed: bool) -> dict[str, Any]:
    """The target entity of one record, with the kind left `null` when the record does not say it.

    A typed protocol event writes `subject: {kind, ref}` on purpose, so a reader never has to infer
    the entity from an event-kind prefix. The released generic records beside it carry the reference
    only. Answering "card" for those would be a claim nothing on the record supports, so the kind is
    `null` and the reader can see which rows say it and which do not.
    """
    reference = _text(record.get("ref"))
    subject = record.get("subject") if typed else None
    kind: str | None = None
    if isinstance(subject, dict):
        kind = _text(subject.get("kind")) or None
        reference = _text(subject.get("ref")) or reference
    return {"ref": reference, "kind": kind}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class CommandSections(SectionSet):
    """Every section of both documents, and the only place a source is attributed to one."""

    def commands(self, read: SourceSet) -> Section:
        """One page of the committed history, newest first.

        `items` is `null` and never `[]` when the audit did not answer: an empty list is the
        affirmative claim that this installation has commanded nothing, which is the opposite of a
        journal nobody could read.
        """
        return read.decide(
            rule(SOURCE_AUDIT, lambda page: dict(page), needs=(SOURCE_AUDIT,)),
            blank={"items": None, "next_cursor": None, "has_more": None},
            narrates=(),
        )

    def operation(self, read: SourceSet) -> Section:
        """What became of one request id, decided from the audit's own two lookups and nothing else.

        Committed wins over staged, exactly as :meth:`secretary.tasks.TaskAudit.event` decides it:
        a committed record is proof the operation finished, whatever else is still on disk. When a
        stale pending record stands beside it, `staged` says so -- an owed cleanup is a fact about
        this installation and not a reason to answer differently.

        The blank is `unknown`, so an audit that refused cannot be published as `not_found`. That is
        held by the seam rather than by a branch here: a rule needing a source that did not answer
        is not run at all, and the fields of a refusal are checked against this blank.
        """
        return read.decide(
            rule(SOURCE_AUDIT, _outcome, needs=(SOURCE_AUDIT,)),
            blank={
                "state": STATE_UNKNOWN,
                "action": None,
                "kind": None,
                "entity": None,
                "actor": None,
                "occurred_at": None,
                "event_id": None,
                "result": None,
                "staged": None,
                "continuation": None,
            },
            narrates=(),
        )


def _outcome(lookup: _Lookup) -> dict[str, Any]:
    """The three answers a request id can have when the audit answered, and no fourth."""
    record = lookup.committed
    if record is not None:
        found = _row(record)
        return {
            "state": STATE_COMMITTED,
            "action": found["action"],
            "kind": found["kind"],
            "entity": found["entity"],
            "actor": found["actor"],
            "occurred_at": found["occurred_at"],
            "event_id": found["event_id"],
            "result": found["result"],
            # An uncleared staged record beside a committed one is an owed repair, said as one.
            "staged": lookup.pending is not None,
            "continuation": None,
        }
    staged = lookup.pending
    if staged is not None:
        found = _row(staged)
        return {
            "state": STATE_PENDING,
            "action": found["action"],
            "kind": found["kind"],
            "entity": found["entity"],
            "actor": found["actor"],
            "occurred_at": found["occurred_at"],
            "event_id": found["event_id"],
            # A staged record describes an effect whose backend write may still fail, so its result
            # is not established. Publishing the staged reason as a result would be reporting an
            # intention as an outcome.
            "result": None,
            "staged": True,
            "continuation": _continuation(lookup.request_id, found["action"]),
        }
    return {
        "state": STATE_NOT_FOUND,
        "action": None,
        "kind": None,
        "entity": None,
        "actor": None,
        "occurred_at": None,
        "event_id": None,
        "result": None,
        "staged": False,
        "continuation": None,
    }


def _continuation(request_id: str, action: str) -> dict[str, Any]:
    """How a part-done operation is continued safely: by repeating exactly this request.

    The same shape `OperationPending` carries on the failure itself
    (`data.action.repeat_request`), so a caller that already handles one refusal handles this read
    with no second table. It is a description and never an act: this read repeats nothing.
    """
    return {
        "repeat_request": True,
        "request_id": request_id,
        "action": action,
        "statement": (
            "This request is staged and not committed, so what it did is durably recorded and may "
            "be part-done. The safe continuation is to repeat the operation with this same request "
            "id, which resumes what exists; a new request id would start a second operation beside "
            "it. This read performs no part of it."
        ),
    }


#: One instance is enough: no section holds state, and the set exists to be enumerated as much as to
#: be called.
SECTIONS = CommandSections()


class CommandReadLayer(ProtocolBoundary):
    """One installation's committed commands, read with no knowledge of who is asking.

    Construction does no I/O, as every other layer's does not: the installation and the audit are
    resolved when a read is called. `clock` is the seam a test supplies directly; it is not a mode.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._clock = clock

    # -- operations ---------------------------------------------------------------------------

    def command_history(self, cursor: str | None = None, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """A page of the last commands across every entity, newest first.

        Each row is the four fields a history is made of: the initiator, the action, the target
        entity and the result. `next_cursor` continues into older commands and `has_more` says
        whether the limit cut this page short.

        A read in the full sense of this layer: it opens the committed journal for reading through
        the traversal that already exists, writes nothing, takes no lock, and starts nothing.
        """
        now = self._clock()
        bounded = max(1, min(int(limit), MAX_LIMIT))
        position = None if not cursor else decode(cursor, ref=HISTORY_SCOPE)
        report, installation = self._installation(now=now)
        audit = self._history(self.data_dir(report), now=now)
        if audit.answered:
            # Paged outside the source span on purpose: a cursor this reader did not issue is the
            # caller being refused, not the audit failing to answer, and a defect in the paging is
            # this layer's own. The span is the read and the conversion of the document, and stops
            # where the audit has answered.
            audit = Reading(SOURCE_AUDIT, audit.source, _page(audit.value.records, position, bounded))
        read = SourceSet([installation, audit])
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "command_history",
                "observed_at": sources.isoformat(now),
                "limit": bounded,
                "extent": dict(HISTORY_EXTENT),
                "commands": SECTIONS.commands(read),
                "sources": self._marks(read),
            }
        )

    def command_request(self, request_id: str) -> dict[str, Any]:
        """What became of one request id: not found, pending, committed -- or unknown.

        The read that replaces re-sending an operation to find out what it did. It consults the two
        lookups the writer itself consults and re-decides nothing with them; it never performs,
        retries or repairs the operation, and a pending answer describes the safe continuation
        rather than taking it.

        The operation-identity contract travels on the document (:data:`OPERATION_IDENTITY`), so a
        caller holding a pending answer can see what repeating that particular operation means
        without leaving the answer.
        """
        now = self._clock()
        identifier = str(request_id or "")
        if not identifier:
            raise ValidationRefused(
                "reading what became of an operation needs the request id it was sent with"
            )
        report, installation = self._installation(now=now)
        read = SourceSet([installation, self._request(self.data_dir(report), identifier, now=now)])
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "command_request",
                "observed_at": sources.isoformat(now),
                "request_id": identifier,
                "operation": SECTIONS.operation(read),
                # Not a section, because it is read from nothing on this installation: it is what a
                # request id *is* in this product, and it is stated whatever every source did.
                "identity": _identity(),
                "sources": self._marks(read),
            }
        )

    # -- shared plumbing ----------------------------------------------------------------------

    def report(self) -> InstanceReport:
        report, refused = self._installation(now=self._clock())
        if report is None:
            raise ValidationRefused(str(refused.source.reason))
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _installation(self, *, now: float) -> tuple[InstanceReport | None, Reading]:
        """The installation config as a source, and the refusal only it can force.

        The same shape the sprint and pause reads use: with an explicit data directory a config that
        does not validate takes away only what it owns and the audit is still read; without one
        there is nothing left to locate the journal with, and the caller named an installation that
        is not one -- which is `validation`, as it is on every other read of this layer.
        """

        def produce() -> InstanceReport:
            report = validate_instance(self.instance)
            if not report.ok or report.data_dir is None:
                raise _Unreadable(
                    "this instance config does not validate: "
                    + "; ".join(str(error) for error in report.errors[:5])
                    if report.errors
                    else "this instance config names no data directory"
                )
            return report

        reading = _source(
            SOURCE_INSTALLATION,
            produce,
            refusal=lambda exc: (
                str(exc)
                if isinstance(exc, _Unreadable)
                else f"this instance config could not be read: {_reason(exc)}"
            ),
            now=now,
            evidence=self._instance_file(),
        )
        if reading.answered:
            return reading.value, Reading(SOURCE_INSTALLATION, reading.source, None)
        if self._data_dir is None:
            raise ValidationRefused(str(reading.source.reason))
        return None, reading

    # -- the source ---------------------------------------------------------------------------

    def _history(self, data_dir: Path, *, now: float) -> Reading:
        """The committed audit, read once for the document through the released traversal.

        `TaskAudit.events()` answers `[]` for a journal that is not there, which is the one answer
        this read may not publish: an installation whose journal is missing has not commanded
        nothing, it has no evidence either way. So the file's absence refuses the source, and
        everything else the read can raise -- a journal the filesystem will not open, a record shape
        the conversion will not take -- refuses it through the span rather than through a list of
        types.

        What is deliberately *not* second-guessed: a line the traversal cannot parse is skipped by
        the traversal, as it is for every other reader of this journal. This read publishes what the
        traversal parsed and :data:`HISTORY_EXTENT` says so.

        And what is deliberately *not* optimised: the traversal parses the whole journal to answer a
        page of it, exactly as `SprintReader.status_views` already does over the same file. The
        alternative is an index of this layer's own, which is the second store this read exists
        without; a cross-entity page is an operator read, not a hot path, and the cost is one pass
        over one append-only file.
        """

        def produce(audit: TaskAudit) -> _History:
            return _History(tuple(audit.events()))

        return self._audit(data_dir, produce, now=now)

    def _request(self, data_dir: Path, request_id: str, *, now: float) -> Reading:
        """The audit's own two lookups for one request id, inside one source span.

        `committed_event` and `pending_event` are the pair `SprintWriter._write` consults to decide
        that a repeat is a no-op, and they are called here exactly as they are there: committed
        first, then staged. Nothing is written, cleared, resolved or repaired by either.

        Both are read for every answer rather than the second only when the first is empty, because
        a stale staged record beside a committed one is a fact this read reports (`staged`) instead
        of a difference a caller has to go and find.
        """

        def produce(audit: TaskAudit) -> _Lookup:
            return _Lookup(request_id, audit.committed_event(request_id), audit.pending_event(request_id))

        return self._audit(data_dir, produce, now=now)

    def _audit(self, data_dir: Path, produce: Callable[[TaskAudit], Any], *, now: float) -> Reading:
        """One read of the one durable source of both documents, and the whole of the broad span.

        The journal's absence is checked before anything reads it, because both released lookups
        answer a missing journal with the same value as an empty one -- `[]` and `None` -- and those
        are the two answers this layer may not publish as an empty history and as `not_found`.
        """
        audit = TaskAudit(data_dir)
        journal = Path(audit.events_path)

        def read() -> Any:
            if not journal.exists():
                raise _Unreadable("the journal is not there, so nothing about it is established")
            return produce(audit)

        return _source(
            SOURCE_AUDIT,
            read,
            refusal=lambda exc: f"the committed board audit could not be read: {journal} ({_reason(exc)})",
            now=now,
            evidence=journal,
        )

    @staticmethod
    def _marks(read: SourceSet) -> dict[str, Any]:
        """The availability of every source, said once for the document and claiming nothing."""
        return {key: read.mark(key) for key in (SOURCE_AUDIT, SOURCE_INSTALLATION)}

    def _instance_file(self) -> Path:
        """The config file itself, so a refusal can be dated by it even when reading it failed."""
        return self.instance if self.instance.is_file() else self.instance / "instance.yaml"


def _identity() -> dict[str, Any]:
    """:data:`OPERATION_IDENTITY` as a document field, copied so a caller cannot edit the contract."""
    return {
        "with_request_id": {
            name: dict(entry) for name, entry in OPERATION_IDENTITY["with_request_id"].items()
        },
        "without_request_id": {
            name: dict(entry) for name, entry in OPERATION_IDENTITY["without_request_id"].items()
        },
        "errors": {name: dict(entry) for name, entry in OPERATION_IDENTITY["errors"].items()},
    }


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


__all__ = [
    "COMMAND_ERRORS",
    "HISTORY_EXTENT",
    "HISTORY_SCOPE",
    "OPERATION_IDENTITY",
    "SCHEMA_VERSION",
    "SOURCE_AUDIT",
    "SOURCE_INSTALLATION",
    "STATE_COMMITTED",
    "STATE_NOT_FOUND",
    "STATE_PENDING",
    "STATE_UNKNOWN",
    "CommandReadLayer",
    "CommandSections",
]
