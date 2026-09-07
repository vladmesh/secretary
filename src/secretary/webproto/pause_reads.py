"""What the pipeline-wide pause is right now, and what a command would reach before it is issued.

Two reads, and one thing they both refuse to let a caller believe.

**`pause_state`** is the pause as `secretary pause-status` has always reported it: the mode, who set
it and when, the flag it lives in, the heads of every tracked card, the sprint observers, and
whether an automation-owned freeze will lift itself.

**`pause_scope`** is the read this layer did not have. An operator could always ask what the pause
*is*; nothing answered "if I drain right now, what exactly does that reach". So the scope read says
it before the command: that the pause is one pipeline-wide flag and not a per-sprint control, which
dispatcher and which files it acts on, which sprints are open and which of their cards are inside
that scope, and which heads are running right now -- together with the two statements that keep the
answer from being read as something it is not. It is derived from exactly the durable state
`pause_status` already reads, and it writes nothing: no flag, no lock, no head, no wake.

**The four properties of the pause, stated on every document rather than left to be inferred.**

1. :data:`PIPELINE_WIDE` -- there is one flag and one pipeline. A read reached from a sprint is
   still a read of the whole installation, and every open sprint of it is inside the scope.
2. :data:`DRAIN_CONTRACT` -- a drain stops no running head. It stops claiming new cards and
   dispatching background roles; a card already in flight rides its cycle to the end. No field of
   these documents may be read as saying otherwise, which is why the heads a drain leaves alone are
   reported under `heads` and never under anything named "stopped".
3. :data:`FREEZE_CONTRACT` -- a freeze is a different command that stops those heads, and it is
   never an implicit upgrade of a drain. It is named here so an operator can see what the other
   command would do; it is deliberately not offered as a variant of the soft path, and
   `secretary.dispatcher_pause_ops.pause` refuses to change mode while paused.
4. The `state` section carries the `stopped_worker`, `stopped_reviewer` and `stopped_observer`
   lists and the `on_resume` sentence that :mod:`secretary.dispatcher_pause` already writes, so what
   a resume would put back is said by the thing that stopped it.

**Every rule stays where it already is.** `normalize_pause_mode`, `on_resume_text` and
`auto_resume_status` are :mod:`secretary.dispatcher_pause`'s; the per-card head line -- what it means
that a head is missing -- is `dispatcher_pause_ops.head_lines`, the same call `pause_status` makes;
the observer rows are `observer_snapshot`'s. Nothing here re-decides any of them and nothing here
opens a second flag, store or lock.

**What is not reused, and why.** `pause_status` reads the flag and the dispatcher's production state
into one flat answer, so either of them failing would take the other's fields with it. These
documents are assembled through :mod:`secretary.webproto.section`, where a source that refused
reaches no claim, so the two are read as two sources and every section names the one that answered
it. The pause flag being unreadable therefore leaves the heads, the sprints and the cards standing,
and vice versa -- and the flag's refusal says in words that the production tick reads an unreadable
flag as a freeze, which is the rule `ProductionPause.load` already holds and this read does not
restate as a claim of its own.

**And "could not answer" is a span here, never a list.** Each source read (:func:`_source`) catches
everything raised while reading and converting its one durable document and answers with a refused
`Reading` carrying the cause. It enumerates no exception types: the list this module used to share
had already drifted -- `DispatcherError`, which `DispatcherRecord.from_json` raises for a record
shape this release does not store, was in none of them -- and a span cannot be forgotten the way an
entry can. Everything outside that span, this module's sections and the assembly of its documents
included, is this layer's own work: a failure there is a defect and travels as itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.board.backend import SPRINT, board_client
from secretary.config import InstanceReport, validate_instance
from secretary.dispatcher_observer import observer_snapshot
from secretary.dispatcher_pause import (
    ProductionPause,
    auto_resume_status,
    legacy_mirror_path,
    normalize_pause_mode,
    on_resume_text,
)
from secretary.dispatcher_pause_ops import head_lines
from secretary.dispatcher_production import ProductionState
from secretary.sprints import SprintReader
from secretary.tasks import _TYPED_RECORD_TYPES
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import ValidationRefused
from secretary.webproto.section import Reading, Section, SectionSet, SourceSet, render, rule

SCHEMA_VERSION = 1

#: The name of the soft pause, spelled once. Every operation and every document that says "drain"
#: says it from here, so there is no second spelling for a mode to be reached under.
DRAIN = "drain"

#: The sources of a pause document, in the precedence they are consulted in -- which is the order a
#: refusal is attributed in, because it is the order the chain needs them. The installation locates
#: the data plane and therefore the files a pause acts on; the flag says whether the pipeline is
#: paused; the dispatcher's production state says what is behind the cards; the sprint board says
#: which sprints are open; and the Pipeline listing says which cards those sprints hold.
SOURCE_INSTALLATION = "installation"
SOURCE_PAUSE = "pause"
SOURCE_LIVENESS = "liveness"
SOURCE_SPRINTS = "sprints"
SOURCE_CARDS = "cards"

#: Property 1, on every document this module returns. It is not read from a source, because it is
#: not a fact about this installation: it is what the pause *is* in this product, and a sprint-shaped
#: view of a global switch that does not say it is global is the failure this field exists to
#: prevent.
PIPELINE_WIDE = (
    "The pause is one pipeline-wide flag on the production dispatcher. There is no per-sprint "
    "pause and no way to pause one sprint: a pause reached from a sprint stops the dispatcher "
    "claiming every card on this installation's Pipeline board -- whichever sprint holds it, and "
    "whether or not one does -- and stops claiming for every open sprint at once."
)

#: Property 2. Named `stops`/`does_not_stop` rather than left to a reader of the head list.
DRAIN_CONTRACT = {
    "mode": DRAIN,
    "operation": "pause_drain",
    "stops": [
        "claiming Ready cards",
        "dispatching background roles",
        "raising an observer for a sprint opened during the pause",
    ],
    "does_not_stop": [
        "a worker head that is already running",
        "a reviewer head that is already running",
        "a sprint observer head that is already running",
    ],
    "statement": (
        "A drain stops no running head. A card already in flight rides its cycle to the end: its "
        "worker keeps writing, its reviewer keeps judging, and a green branch still merges. The "
        "heads listed under `heads` keep running through a drain."
    ),
}

#: Property 3. A freeze is described here so an operator can see the difference before choosing, and
#: is deliberately not reachable from the soft path: this layer has no freeze operation, and
#: `pause_drain` takes no mode.
FREEZE_CONTRACT = {
    "mode": "freeze",
    "operation": None,
    "stops": [
        "everything a drain stops",
        "the live worker and reviewer heads of every tracked card",
        "the sprint observer heads",
        "the tick itself, which then advances nothing",
    ],
    "statement": (
        "A freeze is a different command, not a stronger drain, and it is never reached "
        "implicitly: this layer exposes no freeze operation, `pause_drain` takes no mode, and "
        "`secretary.dispatcher_pause_ops.pause` refuses to change mode while the pipeline is "
        "paused, so a drain is never quietly turned into a freeze. Freezing is `secretary pause "
        "freeze`, issued deliberately, after a resume."
    ),
}

#: What is on the Pipeline board but is not a card a pause reaches. The board's own distinction,
#: taken from where it is already made: a Product or an Issue is a record that never takes a claim
#: or a task transition, whatever column it currently sits in.
NOT_A_CARD = frozenset(_TYPED_RECORD_TYPES)


class _Unreadable(Exception):
    """Inside one source read: the document could not be read or parsed at all.

    Not a second kind of refusal -- both answers are the same unavailable source -- but the one
    distinction that changes what the refusal may say. A pause flag whose bytes are unreadable is a
    file the production tick has already decided its own behaviour for; a flag that parses and holds
    the wrong shapes is not, and must not borrow that sentence.
    """


def _source(
    key: str,
    produce: Callable[[], Any],
    *,
    refusal: Callable[[Exception], str],
    now: float,
    evidence: Path | None,
) -> Reading:
    """Read and convert one source's durable document, or say that it could not answer.

    **This is the whole span of the broad catch, and the span is the contract.** A source read is
    the one place whose entire job is to answer "did this source answer", so *anything* raised while
    reading and converting that one document becomes an unavailable `Reading` -- not a list of
    exception types, because a list is what has to be kept in step and the next durable document
    read through here would be the next hole. `DispatcherError` out of `DispatcherRecord.from_json`
    is exactly that hole arriving: it was the step nobody added to the tuple.

    **What is deliberately outside it.** Section building, the assembly of a document, and every
    other line of this layer are not in any span: a failure there is a defect of this layer and
    travels as itself, to the reader best placed to fix it. That boundary is why the conversions
    live inside `produce` rather than inside a section -- the conversion of a durable document is
    part of reading it, and everything after it is this layer's own work.

    The cause is not swallowed: `refusal` composes the reason from it, so the type and message of
    whatever failed are on the document, in the section's own `source.reason`.
    """
    try:
        return Reading(key, sources.available(now), produce())
    except Exception as exc:  # noqa: BLE001 -- the span above is the reason this is broad
        return Reading(key, sources.unavailable(refusal(exc), now=now, evidence=evidence), None)


@dataclass(frozen=True, slots=True)
class _Dispatcher:
    """The dispatcher's production state, read once and converted once for the whole document.

    Converted *in the source read* and not in a section, and that is the point of the class: a
    production state can be perfectly readable JSON and still not be convertible into the records
    this reports -- an `attempt_round` of `"not-an-integer"` is the case that made this a defect --
    and a conversion left inside a section would raise past `SourceSet.decide` instead of marking
    the source unavailable. Everything that can fail happens where the failure becomes a refusal.
    """

    phase: str
    owner: str
    heads: list[dict[str, Any]]
    observers: list[dict[str, Any]]


class PauseSections(SectionSet):
    """Every section of every pause document, and the only place a source is attributed to one.

    One method per section, and a section is covered by being one. Nothing here reads a file: every
    value comes from a source read once for the document, and a rule receives exactly the sources it
    declares.
    """

    # -- what the pause acts on ---------------------------------------------------------------

    def target(self, read: SourceSet) -> Section:
        """Which dispatcher's flag a pause command would write, and where it lives.

        Sourced `installation`, because these are locations of the data plane and not readings of
        the flag: they are the answer to "which flag" even when that flag cannot be read.
        """
        return read.decide(
            rule(
                SOURCE_INSTALLATION,
                lambda paths: {
                    "dispatcher": "production",
                    "pause_file": str(paths["pause_file"]),
                    "state_file": str(paths["state_file"]),
                    "legacy_mirror_file": str(paths["legacy_mirror_file"]),
                },
            ),
            blank={
                "dispatcher": None,
                "pause_file": None,
                "state_file": None,
                "legacy_mirror_file": None,
            },
            narrates=(),
        )

    def dispatcher(self, read: SourceSet) -> Section:
        """The dispatcher that would be paused, as its own durable state describes it."""
        return read.decide(
            rule(
                SOURCE_LIVENESS,
                lambda live: {
                    "kind": "production",
                    "phase": live.phase,
                    "owner": live.owner,
                    "tracked_cards": len(live.heads),
                },
            ),
            blank={"kind": None, "phase": None, "owner": None, "tracked_cards": None},
            narrates=(),
        )

    # -- the pause itself ---------------------------------------------------------------------

    def state(self, read: SourceSet) -> Section:
        """Whether the pipeline is paused, and everything the flag itself says about it.

        Every field is the flag's, decided by the rules that already own them and applied in the
        source read (:func:`_flag_state`): `normalize_pause_mode` for the mode, `on_resume_text` for
        what a resume would put back, and `auto_resume_status` for whether the pause will lift
        itself. The conversion is there rather than here so a flag that is readable JSON but holds
        the wrong shapes -- `stopped_worker: 1` -- refuses as a source instead of raising past the
        seam. This publishes what it produced.
        """
        return read.decide(
            rule(SOURCE_PAUSE, dict),
            blank={
                "paused": None,
                "mode": None,
                "since": None,
                "actor": None,
                "pause_reason": None,
                "stopped_worker": None,
                "stopped_reviewer": None,
                "stopped_observer": None,
                "excluded_worker": None,
                "on_resume": None,
                "auto_resume": None,
                "legacy_mirror": None,
            },
            narrates=(),
        )

    # -- what is inside the scope --------------------------------------------------------------

    def heads(self, read: SourceSet) -> Section:
        """The heads the dispatcher holds right now, per card and per sprint observer.

        The per-card lines are `dispatcher_pause_ops.head_lines` -- the same call `pause_status`
        makes, so "this head is missing because a freeze stopped it" is decided once. A drain stops
        none of these; that is said in :data:`DRAIN_CONTRACT` and not in a field name here.
        """
        return read.decide(
            rule(SOURCE_LIVENESS, lambda live: {"cards": live.heads, "observers": live.observers}),
            blank={"cards": None, "observers": None},
            narrates=(),
        )

    def sprints(self, read: SourceSet) -> Section:
        """The open sprints inside the scope, and the ones that are not open, counted.

        `items` is `null` and never `[]` when the sprint board did not answer: an empty list is the
        affirmative claim that this installation has no open sprint, which is the opposite of a
        board nobody could read -- and the claim that would make a pipeline-wide pause look narrow.
        """
        return read.decide(
            rule(
                SOURCE_SPRINTS,
                lambda rows: {
                    "items": [
                        {
                            "ref": str(row.get("ref") or ""),
                            "goal": str(row.get("goal") or ""),
                            "status": str(row.get("status") or ""),
                            "current_task": row.get("current_task"),
                        }
                        for row in _open(rows)
                    ],
                    "other_sprints": len(rows) - len(_open(rows)),
                },
            ),
            blank={"items": None, "other_sprints": None},
            narrates=(),
        )

    def cards(self, read: SourceSet) -> Section:
        """Every card on the Pipeline board, with the sprint that holds it where one does.

        The whole board and not the open sprints' cards, because the whole board is the scope: a
        drain stops the dispatcher claiming a Ready card whether or not a sprint holds it, so a card
        no open sprint holds is inside the pause exactly as much as one that is. Saying "there are
        others" while listing only the linked ones is not the scope; it is the admission that the
        scope was not shown.

        It costs no extra board call. The Pipeline listing is read once for the document and this
        publishes what it holds, rather than filtering it down to the sprints named beside it.

        Product and Issue records are not cards here, and that is the board's own rule rather than
        a judgement of this read: they live on the same board and in a column, but a Product or an
        Issue never takes a claim or a task transition
        (`secretary.tasks._TYPED_RECORD_TYPES`), so a pause reaches no such record and listing one
        as inside its scope would be the same misdescription in the other direction.

        The sprint is the relationship the listing itself carries, and it is `null` for a card no
        sprint holds -- never the empty string, and never omitted.
        """

        def from_listing(linked: dict[str, list[dict[str, Any]]]):
            items = [
                {
                    "ref": str(card.get("ref") or ""),
                    "sprint": str(group) or None,
                    "state": str(card.get("state") or ""),
                }
                for group, cards in linked.items()
                for card in cards
                if isinstance(card, dict) and card.get("record_type") not in NOT_A_CARD
            ]
            return {"items": sorted(items, key=lambda entry: entry["ref"])}

        return read.decide(
            rule(SOURCE_CARDS, from_listing),
            blank={"items": None},
            narrates=(),
        )


def _flag_state(state: dict[str, Any]) -> dict[str, Any]:
    """The pause flag as the `state` section publishes it, from the rules that already own it.

    Called inside the source read, so every conversion that can fail on a semantically corrupt flag
    fails where the failure becomes an unavailable source rather than an exception past the seam.
    """
    mode = normalize_pause_mode(state.get("mode"))
    stopped_worker = _refs(state.get("stopped_worker"))
    stopped_reviewer = _refs(state.get("stopped_reviewer"))
    mirror = state.get("legacy_mirror")
    return {
        "paused": bool(mode),
        "mode": mode or None,
        "since": str(state.get("since") or "") or None,
        "actor": str(state.get("actor") or "") or None,
        "pause_reason": str(state.get("reason") or "") or None,
        "stopped_worker": stopped_worker,
        "stopped_reviewer": stopped_reviewer,
        "stopped_observer": _refs(state.get("stopped_observer")),
        "excluded_worker": _refs(state.get("excluded_worker")),
        "on_resume": on_resume_text(mode, stopped_worker, stopped_reviewer),
        "auto_resume": auto_resume_status(state),
        "legacy_mirror": mirror if isinstance(mirror, dict) else {},
    }


def _refs(value: Any) -> list[str]:
    """One of the flag's head lists. A value that is not a list of references is not one."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise TypeError(f"a pause flag head list is not a list of references: {value!r}")
    return list(value)


def _open(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("status") or "") == "open"]


#: One instance is enough: no section holds state, and the set exists to be enumerated as much as
#: to be called.
SECTIONS = PauseSections()


class PauseReadLayer(ProtocolBoundary):
    """One installation's pause, read with no knowledge of who is asking.

    Construction does no I/O, as the other layers' does not. Every read resolves the installation,
    the flag, the dispatcher's state and the boards when it is called.

    `board_client` and `clock` are the seams a test -- or a transport with its own connection policy
    -- supplies directly. Neither is a mode: the same code path runs against the live installation.
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

    # -- operations ---------------------------------------------------------------------------

    def pause_state(self) -> dict[str, Any]:
        """Whether the pipeline is paused, in what mode, since when, and what is behind its cards.

        The read `secretary pause-status` answers with. It writes nothing, takes no lock, and starts,
        stops and wakes nothing: it reads the flag, the dispatcher's durable state and the pid
        heartbeats those records name, and reports them.
        """
        now = self._clock()
        report, installation = self._installation(now=now)
        data_dir = self.data_dir(report)
        read = SourceSet([installation, self._flag(data_dir, now=now), self._production(data_dir, now=now)])
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "pause_state",
                "observed_at": sources.isoformat(now),
                "extent": extent(),
                "target": SECTIONS.target(read),
                "dispatcher": SECTIONS.dispatcher(read),
                "state": SECTIONS.state(read),
                "heads": SECTIONS.heads(read),
                "modes": {"drain": DRAIN_CONTRACT, "freeze": FREEZE_CONTRACT},
                "sources": self._marks(read, (SOURCE_PAUSE, SOURCE_LIVENESS, SOURCE_INSTALLATION)),
            }
        )

    def pause_scope(self) -> dict[str, Any]:
        """What a pause command would reach, answered before the command is issued.

        Everything `pause_state` says, plus what is inside the scope: the open sprints, their cards,
        how many Pipeline cards belong to no open sprint, and the heads that are running now. The
        two statements beside it are the point of the read as much as the lists are -- a drain
        stops none of those heads, and what a freeze would stop instead is a different command.

        It is a read in the full sense of this layer: no flag is written, the production tick lock is
        not taken, no head is stopped, started or woken, and nothing is scheduled.
        """
        now = self._clock()
        report, installation = self._installation(now=now)
        data_dir = self.data_dir(report)
        sprints, cards = self._boards(data_dir, report, now=now)
        read = SourceSet(
            [
                installation,
                self._flag(data_dir, now=now),
                self._production(data_dir, now=now),
                sprints,
                cards,
            ]
        )
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "pause_scope",
                "observed_at": sources.isoformat(now),
                "extent": extent(),
                "target": SECTIONS.target(read),
                "dispatcher": SECTIONS.dispatcher(read),
                "state": SECTIONS.state(read),
                "sprints": SECTIONS.sprints(read),
                "cards": SECTIONS.cards(read),
                "heads": SECTIONS.heads(read),
                "modes": {"drain": DRAIN_CONTRACT, "freeze": FREEZE_CONTRACT},
                "sources": self._marks(
                    read,
                    (
                        SOURCE_PAUSE,
                        SOURCE_LIVENESS,
                        SOURCE_SPRINTS,
                        SOURCE_CARDS,
                        SOURCE_INSTALLATION,
                    ),
                ),
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

        The same shape the sprint reads use, for the same reason: with an explicit data directory a
        config that does not validate takes away only what it owns, and the flag and the
        dispatcher's state are still read. Without one there is nothing left to locate them with.

        The refusal is `validation` and not `backend_unavailable`, and that is a compatibility
        promise rather than a taste: `secretary pause-status` reached this installation through
        `runtime_from_args`, whose `invalid_instance` is a `DispatcherError` with exit status 2, and
        an operator or a script that reads that status must keep reading it now that the command is
        a client of this layer. A config that does not validate is the caller naming an installation
        that is not one; nothing of this installation refused.
        """

        def produce() -> tuple[InstanceReport, dict[str, Path]]:
            report = validate_instance(self.instance)
            if not report.ok or report.data_dir is None:
                raise _Unreadable(
                    "this instance config does not validate: "
                    + "; ".join(str(error) for error in report.errors[:5])
                    if report.errors
                    else "this instance config names no data directory"
                )
            return report, self._paths(report.data_dir)

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
            report, paths = reading.value
            return report, Reading(SOURCE_INSTALLATION, reading.source, paths)
        # The refusal is raised outside the span on purpose: it is this layer answering the caller,
        # not a source failing, and a broad catch that swallowed it would answer a document about an
        # installation nobody could locate.
        if self._data_dir is None:
            raise ValidationRefused(str(reading.source.reason))
        return None, reading

    @staticmethod
    def _paths(data_dir: Path) -> dict[str, Path]:
        """The three files a pause acts on, from the one data plane the installation names."""
        return {
            "pause_file": ProductionPause(data_dir).path,
            "state_file": ProductionState(data_dir).path,
            "legacy_mirror_file": legacy_mirror_path(),
        }

    # -- the sources --------------------------------------------------------------------------

    def _flag(self, data_dir: Path, *, now: float) -> Reading:
        """The pause flag, read once through the class the production tick reads it with.

        `ProductionPause.load` answers `{}` for a flag that is not there -- which is the pipeline
        running, an answer and not a refusal -- and marks a flag it could not read or parse as
        corrupt. That corrupt marking is this source refusing: what the flag says is not
        established, so no section may claim it. The reason states the consequence the product has
        already decided for that case, which is a rule of `ProductionPause` and not a claim of this
        read: an unreadable flag is treated as a freeze by every tick until it is repaired.
        """
        flag = ProductionPause(data_dir)

        def produce() -> dict[str, Any]:
            state = flag.load()
            if state.get("corrupt"):
                raise _Unreadable("the flag could not be read or parsed")
            return _flag_state(state)

        def refusal(exc: Exception) -> str:
            if isinstance(exc, _Unreadable):
                # The case `ProductionPause.load` has already decided the pipeline's behaviour for.
                return (
                    f"the pause flag could not be read: {flag.path}. Until it is repaired every "
                    "production tick reads an unreadable flag as a freeze and advances nothing"
                )
            # Any other fault, and it must not borrow the sentence above: the file parses, so the
            # tick keeps reading the same flag and behaving by it. What is unestablished is what the
            # flag says here, not what the pipeline does.
            return (
                f"the pause flag parses but does not hold a pause state: {flag.path} "
                f"({_reason(exc)}). The production tick reads the same file, so what could not be "
                "established here is what the flag says, not the pipeline's behaviour"
            )

        return _source(SOURCE_PAUSE, produce, refusal=refusal, now=now, evidence=flag.path)

    def _production(self, data_dir: Path, *, now: float) -> Reading:
        """The dispatcher's durable production state, read once for the document.

        `ProductionState.load` reports a state it could not read as the `unavailable` phase -- the
        same predicate `pause` and `resume` branch on -- and that is this source refusing. A refusal
        is "nobody could say which heads are up", never "no head is up".
        """
        state = ProductionState(data_dir)

        def produce() -> _Dispatcher:
            payload = state.load()
            if str(payload.get("phase") or "") == "unavailable":
                raise _Unreadable("the state could not be read or parsed")
            return _Dispatcher(
                phase=str(payload.get("phase") or "new"),
                owner=str(payload.get("owner") or ""),
                # Every conversion of the durable document is inside this span, records included:
                # `DispatcherRecord.from_json` refuses record shapes this release does not store,
                # and that refusal is this source failing to answer, not an exception for a caller.
                heads=head_lines(state.records(payload)),
                observers=observer_snapshot(payload),
            )

        return _source(
            SOURCE_LIVENESS,
            produce,
            refusal=lambda exc: (
                f"the dispatcher production state could not be read: {state.path}"
                if isinstance(exc, _Unreadable)
                else f"the dispatcher production state could not be read: {state.path} ({_reason(exc)})"
            ),
            now=now,
            evidence=state.path,
        )

    def _boards(
        self, data_dir: Path, report: InstanceReport | None, *, now: float
    ) -> tuple[Reading, Reading]:
        """The sprint board and the Pipeline listing, as two sources that fail apart.

        `SprintReader.list(create=False)` and `linked_cards`, exactly as the sprint reads take them:
        a read of this layer creates no board, and the two calls are two board passes that can fail
        independently -- an installation without a Pipeline must not lose its open sprints.
        """
        evidence = data_dir / "board" / "cards.ndjson"
        sprints = _source(
            SOURCE_SPRINTS,
            lambda: SprintReader(self._client(), data_dir=data_dir).list(create=False),
            refusal=lambda exc: f"the sprint board could not be read: {_reason(exc)}",
            now=now,
            evidence=evidence,
        )
        cards = _source(
            SOURCE_CARDS,
            lambda: SprintReader(self._client(), data_dir=data_dir).linked_cards(),
            refusal=lambda exc: f"the Pipeline board could not be read: {_reason(exc)}",
            now=now,
            evidence=evidence,
        )
        return sprints, cards

    @staticmethod
    def _marks(read: SourceSet, keys: tuple[str, ...]) -> dict[str, Any]:
        """The availability of every source of the document, said once for the document.

        Under `sources` rather than beside the sections, because two of these sources are named the
        same as the sections they feed -- `sprints` and `cards` -- and a mark that overwrote its own
        section would answer "which sprints are in the scope" with an availability record.
        """
        return {key: read.mark(key) for key in keys}

    def _client(self) -> Any:
        """The sprint board of this installation, named through the switch (board/backend.py)."""
        return self._board_client or board_client(self._instance_dir(), serves=(SPRINT,))

    def _instance_dir(self) -> Path:
        return self.instance.parent if self.instance.is_file() else self.instance

    def _instance_file(self) -> Path:
        """The config file itself, so a refusal can be dated by it even when reading it failed."""
        return self.instance if self.instance.is_file() else self.instance / "instance.yaml"


def extent() -> dict[str, Any]:
    """Property 1 as a field, on every document, read from no source at all.

    Not a section, for the reason the sprint delivery document's `acceptance` is not one: it is not
    established by anything on this installation. It is what the pause is, and it is stated whatever
    every source did -- including on a document where every source refused, which is exactly when a
    reader most needs to know that what they are looking at is global.
    """
    return {"scope": "pipeline", "per_sprint": False, "statement": PIPELINE_WIDE}


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


__all__ = [
    "DRAIN",
    "DRAIN_CONTRACT",
    "FREEZE_CONTRACT",
    "PIPELINE_WIDE",
    "SCHEMA_VERSION",
    "SOURCE_CARDS",
    "SOURCE_INSTALLATION",
    "SOURCE_LIVENESS",
    "SOURCE_PAUSE",
    "SOURCE_SPRINTS",
    "PauseReadLayer",
    "PauseSections",
    "extent",
]
