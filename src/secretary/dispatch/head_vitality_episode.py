"""The persisted vitality episode: hysteresis over snapshots, and nothing else.

Where :mod:`head_vitality` turns one channel reading into facts, this module folds successive
snapshots of one ``HeadRun`` into a *conclusion over time* -- the ``VitalityEpisode`` of the
head-vitality plan ("Vitality reducer"): a verdict, the timestamps that justify it, the per-source
cursors that prove advancement, and the sources that went dark. ``reduce_vitality`` is the only
place conclusions live, and it is a pure function: same
inputs in, same episode out, no I/O, no clock. The caller owns ``now``, which is what keeps the
reducer testable against the historical incidents and replayable from a persisted record.

Verdict ladder, in the plan's vocabulary::

    HealthyActive   strong evidence of advancement right now
    HealthyQuiet    alive, no advancement observed yet, below every threshold
    Suspended       the kernel has the process parked on a stop signal (NOT a stall)
    Retained        the same parked process, held there by the dispatcher's OWN retention
    SuspectedStall  strong quiet has outlived ``suspect_after``
    ConfirmedStall  strong quiet has additionally outlived ``confirm_after``
    Dead            the heartbeat names a gone or unreaped process
    Unverifiable    no strong channel answered; nothing may be concluded

Decisions this module deliberately does NOT make: it never stops, signals or replaces
anything, and it treats the two thresholds as description, not authority. It neither sets nor
reads ``recovery_rung`` and its sibling policy fields (they exist for the recovery-policy card,
S1-5, and ride every reduction untouched); until that policy wired in behind them, episodes are
recorded and logged by the caller -- stored and consulted only through the decision paths the
later cards define.

The invariants encoded here (each pinned by a named test):

  * snapshots naming another run never touch an episode; a wholesale identity change starts a
    fresh episode rather than splicing a new run's facts into an old run's history;
  * ``Dead`` and ``Suspended`` outrank everything else on their axis; suspension freezes the
    stall clocks instead of feeding them;
  * a parked process whose card carries a confirmed retention reduces to ``Retained``, not to
    ``Suspended``: the caller passes that intent in as ``retained=``, so the one fact "the
    process is in ``T``" stops having two owners with opposite intentions (secretary-1539). Death
    still outranks it -- a retained head that is provably gone is ``Dead``, not ``Retained`` --
    and a retention whose process is running again is not ``Retained`` either, so the caller's
    own "no longer confirmably suspended" failure still fires;
  * advancement from any non-advisory source ends a suspected or confirmed episode immediately;
  * an unavailable source freezes its evidence and never counts as no-progress; with every strong
    source dark the truthful verdict is ``Unverifiable`` -- except that an already-confirmed
    episode is not quietly laundered back to health by its observers going blind;
  * that freeze is BOUNDED (secretary-1543): a progress source known to this episode and not
    answering holds the stall clock for at most ``thresholds.dark_ceiling``, after which the
    episode ages on the pid's own sustained "running, and nothing else" exactly as a run whose
    progress source never answered at all. A live pid is never indefinite evidence of liveness;
  * a source does not have to SAY it is unavailable to be dark: a progress source this episode has
    heard from that produces no snapshot on a tick is stamped dark too, because the status shapes
    for a live head whose pane the inventory lost carry a heartbeat and no provider channel at all;
  * a rejected report the head has not answered, followed by an advisory turn seen to END, raises
    ``HealthyQuiet`` to ``SuspectedStall`` at once (``answer_owed_since``): the pair is an
    explicit signal, not something a ceiling eventually notices;
  * advisory pane readings corroborate in ``basis`` and can never drive a stall verdict;
  * quiet accumulates from ``last_progress_at`` (or episode start), not from the last tick, so
    irregular ticks cannot stretch or shrink a stall;
  * confirmation is sticky: only real progress, suspension, death or an identity change ends it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from secretary.dispatch.head_vitality import (
    CURSOR_LIMIT,
    REASON_LIMIT,
    HeadVitalityError,
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    TurnState,
    VitalitySnapshot,
)
from secretary.dispatcher_watchdog import IDLE_STALL_DEFAULT

# Serialisation version of ``VitalityEpisode``. Strictly coupled to SNAPSHOT_VERSION's discipline:
# a schema change bumps this and answers for old payloads explicitly instead of reinterpreting
# them silently.
EPISODE_VERSION = 1

# ``basis`` is structured provenance, not prose: fixed tokens naming the source and the fact that
# drove the verdict, bounded so one noisy batch cannot bloat a durable record.
BASIS_TOKEN_LIMIT = 80
BASIS_ENTRY_LIMIT = 10

# Progress sources distinguish a broken observed channel from pid-only aging.
_PROGRESS_SOURCES = frozenset({SnapshotSource.PROVIDER_CURSOR.value})

# How long a *dark* progress source may freeze an episode's stall clock before the episode ages
# anyway (secretary-1543). See ``VitalityThresholds.dark_ceiling`` for why this number.
DARK_CEILING_DEFAULT = 2.0 * float(IDLE_STALL_DEFAULT)


class VitalityVerdict(StrEnum):
    """What the reducer concludes about one run, as of one reduction."""

    HEALTHY_ACTIVE = "healthy_active"
    HEALTHY_QUIET = "healthy_quiet"
    SUSPENDED = "suspended"
    RETAINED = "retained"
    SUSPECTED_STALL = "suspected_stall"
    CONFIRMED_STALL = "confirmed_stall"
    DEAD = "dead"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class VitalityThresholds:
    """The quiet durations at which suspicion begins and confirmation follows.

    Defaults are chosen against the ceilings the dispatcher already lives with, for comparability
    rather than authority: ``suspect_after`` equals ``IDLE_STALL_DEFAULT`` (secretary-1063's
    five-minute readiness-idle window) -- the point where today's machinery first treats idleness
    as actionable -- and ``confirm_after`` doubles it, echoing the watchdog's principle that a
    destructive-looking conclusion wants its evidence separated in time. Both are far below the
    six-hour worker-report ceiling whose uncritical application produced the codegen-orchestrator
    incidents. Nothing in this module enforces these numbers on a decision: they parameterise a
    shadow verdict, and a later policy card owns whatever the production numbers turn out to be.

    ``dark_ceiling`` is the one number this vocabulary makes load-bearing for a real recovery
    (secretary-1543), so it is argued rather than borrowed. It bounds how long a progress source
    that is *known to this episode and not answering* may freeze the stall clock. Before this
    card the freeze was unbounded: on ``issue:7bff833fef6d9d9b404d`` (2026-08-30) a Codex head
    with no bound provider baseline sat ``healthy_quiet`` for 65+ minutes behind a live PID, with
    no wake, no replacement and no terminal outcome. Lower bound on the number: it must outlast a
    genuine provider-startup window, and secretary-1542 measured one on real Codex v0.152.1 --
    minutes, during which Orca answers ``tui-idle`` satisfied and the output cursor never moves,
    so nothing else distinguishes a starting head from a settled one. Upper bound: it must stay
    far below the six-hour worker-report ceiling whose uncritical application produced the
    codegen-orchestrator incidents, and it must leave the wake early enough to be worth having.
    Two ``IDLE_STALL_DEFAULT`` (ten minutes) sits between them: a dark-and-quiet head earns its
    first conversational nudge at ``max(dark_ceiling, suspect_after)`` -- ten minutes instead of
    never -- while nothing destructive happens before ``suspect_after + confirm_after`` of quiet
    AND the guard's own outer ceiling (:mod:`head_vitality_guard`).
    """

    suspect_after: float
    confirm_after: float
    dark_ceiling: float = float(DARK_CEILING_DEFAULT)

    def __post_init__(self) -> None:
        for name in ("suspect_after", "confirm_after", "dark_ceiling"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HeadVitalityError(f"vitality threshold {name} is a number")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise HeadVitalityError(f"vitality threshold {name} is positive and finite")
        object.__setattr__(self, "suspect_after", float(self.suspect_after))
        object.__setattr__(self, "confirm_after", float(self.confirm_after))
        object.__setattr__(self, "dark_ceiling", float(self.dark_ceiling))


DEFAULT_VITALITY_THRESHOLDS = VitalityThresholds(
    suspect_after=float(IDLE_STALL_DEFAULT),
    confirm_after=2.0 * float(IDLE_STALL_DEFAULT),
    dark_ceiling=float(DARK_CEILING_DEFAULT),
)


def _finite_timestamp(value: Any, name: str) -> float:
    """Validate one epoch-seconds field shared by construction and deserialisation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HeadVitalityError(f"vitality episode {name} is a number")
    stamp = float(value)
    if not math.isfinite(stamp) or stamp < 0:
        raise HeadVitalityError(f"vitality episode {name} is finite and not negative")
    return stamp


@dataclass(frozen=True)
class VitalityEpisode:
    """One run's vitality conclusion, durable enough to survive the dispatcher.

    Frozen and structurally compared like ``VitalitySnapshot``: an episode is written between
    ticks, read back by the next one, and handed to a future policy card that must be able to
    trust it verbatim. ``suspected_since``/``confirmed_since`` carry the *computed* onset of each
    phase (quiet-reference plus threshold, not the tick that happened to observe the crossing),
    because ticks are irregular while the quiet they sampled is not.
    """

    run_id: str
    verdict: VitalityVerdict = VitalityVerdict.UNVERIFIABLE
    # When this episode began accumulating evidence for its run; the quiet reference before any
    # progress was ever seen.
    started_at: float = 0.0
    suspected_since: float = 0.0
    confirmed_since: float = 0.0
    # The last instant a non-advisory source showed advancement, with its source's name. 0.0 when
    # no advancement has ever been observed in this episode.
    last_progress_at: float = 0.0
    last_progress_source: str = ""
    # A conversational rung's restart of the quiet clock. The dispatcher's one report nudge asks
    # the head a question it could not have answered before being asked, so the quiet the ladder
    # charges it with begins at the prompt -- but the head's real progress history stays on file,
    # which is why this is its own stamp instead of a rewritten ``last_progress_at``. 0.0 when no
    # rung has restarted anything; the quiet reference is the later of this and last progress.
    quiet_since: float = 0.0
    # Per-source opaque cursors, compared but never parsed: the caller feeds them back as
    # ``previous_cursor`` when building the next provider snapshot, which is how one reduction
    # knows the cursor moved.
    evidence_cursors: dict[str, str] = field(default_factory=dict)
    # Sources that failed to answer, mapped to the instant they were first seen dark in this
    # episode. A source that answers again leaves this map; while it stays, its stale evidence is
    # frozen rather than spent.
    unavailable_since: dict[str, float] = field(default_factory=dict)
    # Structured provenance: which sources and axes drove the verdict, newest reduction last.
    basis: tuple[str, ...] = ()
    reason: str = ""
    # Policy-owned recovery state survives reductions; rungs are keyed by suspension span,
    # while deterministic launch refusals remain consecutive across spans.
    recovery_rung: int = 0
    recovery_span_started_at: float = 0.0
    deterministic_refusals: int = 0
    # Count of distinct advancement boundaries observed for this run. Each real progress moves the
    # epoch, so a future policy can tell "quiet since the same old work" from "quiet since fresh
    # work" without trusting wall-clock arithmetic alone.
    activity_epoch: int = 0
    updated_at: float = 0.0
    # Non-zero while the stall clocks are frozen by a suspended reading: the instant the freeze
    # began. Cleared on the first reduction that no longer sees suspension, whose reference
    # timestamps are shifted forward by the frozen span so suspended time feeds no threshold.
    stall_frozen_since: float = 0.0
    # The last advisory Turn reading this episode saw ("active"/"idle"/""), and the instant a
    # reading went active -> idle. Advisory evidence stays advisory: neither field may raise a
    # verdict on its own, and ``turn_ended_at`` is read only to NARROW a conclusion the caller's
    # own ``answer_owed_since`` already established (secretary-1543). A head that never showed an
    # active turn -- a Codex head still starting, which answers idle throughout (secretary-1542's
    # measurement) -- never stamps it.
    last_turn: str = ""
    turn_ended_at: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.run_id or "").strip():
            raise HeadVitalityError("a vitality episode is bound to a HeadRun.run_id")
        if not isinstance(self.verdict, VitalityVerdict):
            raise HeadVitalityError("a vitality episode carries a VitalityVerdict")
        for name in (
            "started_at",
            "suspected_since",
            "confirmed_since",
            "last_progress_at",
            "quiet_since",
            "updated_at",
            "stall_frozen_since",
            "turn_ended_at",
        ):
            object.__setattr__(self, name, _finite_timestamp(getattr(self, name), name))
        if isinstance(self.recovery_rung, bool) or not isinstance(self.recovery_rung, int):
            raise HeadVitalityError("a vitality episode recovery rung is an int")
        if self.recovery_rung < 0:
            raise HeadVitalityError("a vitality episode recovery rung is not negative")
        object.__setattr__(
            self,
            "recovery_span_started_at",
            _finite_timestamp(
                getattr(self, "recovery_span_started_at", 0.0),
                "recovery_span_started_at",
            ),
        )
        refusals = getattr(self, "deterministic_refusals", 0)
        if isinstance(refusals, bool) or not isinstance(refusals, int) or refusals < 0:
            raise HeadVitalityError("a vitality episode deterministic refusal count is a non-negative int")
        if self.activity_epoch < 0:
            raise HeadVitalityError("a vitality episode activity epoch is not negative")
        object.__setattr__(
            self,
            "evidence_cursors",
            {str(name)[:80]: str(cursor)[:CURSOR_LIMIT] for name, cursor in self.evidence_cursors.items()},
        )
        object.__setattr__(
            self,
            "unavailable_since",
            {
                str(name)[:80]: _finite_timestamp(since, "unavailable_since")
                for name, since in self.unavailable_since.items()
            },
        )
        object.__setattr__(
            self, "basis", tuple(str(token)[:BASIS_TOKEN_LIMIT] for token in self.basis)[:BASIS_ENTRY_LIMIT]
        )
        object.__setattr__(self, "reason", str(self.reason or "")[:REASON_LIMIT])
        turn = str(self.last_turn or "")
        if turn not in ("", "active", "idle"):
            raise HeadVitalityError("a vitality episode last turn is active, idle or unrecorded")
        object.__setattr__(self, "last_turn", turn)

    def to_json(self) -> dict[str, Any]:
        """The durable form stored on the dispatcher record beside its run."""
        return {
            "version": EPISODE_VERSION,
            "run_id": self.run_id,
            "verdict": self.verdict.value,
            "started_at": self.started_at,
            "suspected_since": self.suspected_since,
            "confirmed_since": self.confirmed_since,
            "last_progress_at": self.last_progress_at,
            "last_progress_source": self.last_progress_source,
            "quiet_since": self.quiet_since,
            "evidence_cursors": dict(self.evidence_cursors),
            "unavailable_since": dict(self.unavailable_since),
            "basis": list(self.basis),
            "reason": self.reason,
            "recovery_rung": self.recovery_rung,
            "recovery_span_started_at": self.recovery_span_started_at,
            "deterministic_refusals": self.deterministic_refusals,
            "activity_epoch": self.activity_epoch,
            "updated_at": self.updated_at,
            "stall_frozen_since": self.stall_frozen_since,
            "last_turn": self.last_turn,
            "turn_ended_at": self.turn_ended_at,
        }

    @classmethod
    def from_json(cls, payload: Any) -> VitalityEpisode:
        """Read one episode back, refusing damaged evidence instead of normalising it.

        Same contract as ``VitalitySnapshot.from_json``: a payload outside this vocabulary is a
        damaged record, and quietly repairing it would let a later consumer act on words nobody
        wrote. Records written before this field existed are handled by ``DispatcherRecord``,
        which maps their absence to ``None`` -- no episode, no claim.
        """
        if not isinstance(payload, dict):
            raise HeadVitalityError("a vitality episode is read from an object")
        if type(payload.get("version")) is not int or payload.get("version") != EPISODE_VERSION:
            raise HeadVitalityError("vitality episode has an unsupported version")
        try:
            verdict = VitalityVerdict(str(payload.get("verdict") or ""))
        except ValueError:
            raise HeadVitalityError("vitality episode names a verdict outside its vocabulary") from None
        cursors = payload.get("evidence_cursors")
        unavailable = payload.get("unavailable_since")
        if not isinstance(cursors, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in cursors.items()
        ):
            raise HeadVitalityError("vitality episode evidence cursors are malformed")
        if not isinstance(unavailable, dict) or not all(isinstance(key, str) for key in unavailable):
            raise HeadVitalityError("vitality episode unavailable sources are malformed")
        basis = payload.get("basis")
        if not isinstance(basis, list) or not all(isinstance(token, str) for token in basis):
            raise HeadVitalityError("vitality episode basis is malformed")
        rung = payload.get("recovery_rung")
        epoch = payload.get("activity_epoch")
        if isinstance(rung, bool) or not isinstance(rung, int) or rung < 0:
            raise HeadVitalityError("vitality episode recovery rung is a non-negative int")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise HeadVitalityError("vitality episode activity epoch is a non-negative int")
        # Missing recovery fields mean no rung or deterministic refusal has been recorded.
        span = payload.get("recovery_span_started_at", 0.0)
        refusals = payload.get("deterministic_refusals", 0)
        if isinstance(refusals, bool) or not isinstance(refusals, int) or refusals < 0:
            raise HeadVitalityError("vitality episode deterministic refusal count is a non-negative int")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            verdict=verdict,
            started_at=_finite_timestamp(payload.get("started_at"), "started_at"),
            suspected_since=_finite_timestamp(payload.get("suspected_since"), "suspected_since"),
            confirmed_since=_finite_timestamp(payload.get("confirmed_since"), "confirmed_since"),
            last_progress_at=_finite_timestamp(payload.get("last_progress_at"), "last_progress_at"),
            last_progress_source=str(payload.get("last_progress_source") or "")[:80],
            # Absent in records written before secretary-1543's nudge restart, and absent is
            # exactly what "no rung has restarted this episode's quiet clock" says, so the field
            # answers for itself rather than stranding a live dispatcher's records.
            quiet_since=_finite_timestamp(payload.get("quiet_since", 0.0), "quiet_since"),
            evidence_cursors=dict(cursors),
            unavailable_since=dict(unavailable),
            basis=tuple(basis),
            reason=str(payload.get("reason") or ""),
            recovery_rung=rung,
            recovery_span_started_at=_finite_timestamp(span, "recovery_span_started_at"),
            deterministic_refusals=refusals,
            activity_epoch=epoch,
            updated_at=_finite_timestamp(payload.get("updated_at"), "updated_at"),
            stall_frozen_since=_finite_timestamp(payload.get("stall_frozen_since"), "stall_frozen_since"),
            # Written before secretary-1543 added the Turn-transition fields: their absence means
            # no advisory turn was ever recorded for this episode, which is exactly what an
            # unstamped pair says. Refusing such a record would strand every episode a running
            # dispatcher persisted under EPISODE_VERSION 1, so the version stays where it is and
            # the missing fields answer for themselves.
            last_turn=str(payload.get("last_turn") or ""),
            turn_ended_at=_finite_timestamp(payload.get("turn_ended_at", 0.0), "turn_ended_at"),
        )


def reduce_vitality(
    previous: VitalityEpisode | None,
    snapshots: Sequence[VitalitySnapshot],
    now: float,
    thresholds: VitalityThresholds = DEFAULT_VITALITY_THRESHOLDS,
    *,
    retained: bool = False,
    answer_owed_since: float = 0.0,
) -> VitalityEpisode:
    """Fold one tick's snapshots into the run's episode, purely and deterministically.

    ``previous`` is the persisted episode for the run (``None`` before the first). ``snapshots``
    are this tick's observations, in any order -- duplicates collapse to each source's latest
    reading. The function never reads a clock, a file or a host: everything it concludes is a
    function of its arguments, which is what makes a persisted episode replayable and its tests
    able to reproduce historical incidents tick by tick.

    ``retained`` is the caller's declaration that this run is parked on a stop signal the
    dispatcher itself sent and still holds (a confirmed ``WorkerContinuation`` retention). It is
    an INPUT, not an observation: the reducer cannot tell an intentional SIGSTOP from a hostile
    one, and guessing is what let the watchdog SIGCONT a retained worker out from under the gate
    (secretary-1539). It changes exactly one thing -- a suspended reading becomes ``Retained``
    instead of ``Suspended`` -- and nothing else about the fold.

    ``answer_owed_since`` is the other caller-declared fact (secretary-1543): the instant the
    dispatcher put a question to this head that it has not answered -- today, a rejected done
    report that sent the head back to rework. Like ``retained`` it is an INPUT: the reducer cannot
    see a board rejection. It changes exactly one thing too. When the head owes an answer that no
    progress has followed AND the advisory Turn axis has been seen to END a turn since the
    question was put (``active`` then ``idle``), a ``HealthyQuiet`` verdict is raised to
    ``SuspectedStall`` at once instead of waiting out the quiet thresholds: a head that took its
    turn, ended it and answered nothing is not quiet-below-threshold, it is finished and silent.
    The advisory reading still cannot convict on its own -- with no owed answer it stays exactly
    as weightless as before -- and it can never raise anything but ``HealthyQuiet``, so it lowers
    no verdict and launders no confirmation.
    """
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        raise HeadVitalityError("reduce_vitality needs a finite now")
    now = float(now)
    if not isinstance(thresholds, VitalityThresholds):
        raise HeadVitalityError("reduce_vitality needs VitalityThresholds")
    if not all(isinstance(snapshot, VitalitySnapshot) for snapshot in snapshots):
        raise HeadVitalityError("reduce_vitality reduces VitalitySnapshot values")
    if previous is not None and not isinstance(previous, VitalityEpisode):
        raise HeadVitalityError("reduce_vitality continues a VitalityEpisode")
    if not isinstance(retained, bool):
        raise HeadVitalityError("reduce_vitality needs a boolean retained")
    if (
        isinstance(answer_owed_since, bool)
        or not isinstance(answer_owed_since, (int, float))
        or not math.isfinite(float(answer_owed_since))
        or float(answer_owed_since) < 0
    ):
        raise HeadVitalityError("reduce_vitality needs a finite answer_owed_since")
    answer_owed_since = float(answer_owed_since)

    if not snapshots:
        # No observation, no movement: an episode must not age on ticks that looked at nothing.
        if previous is None:
            raise HeadVitalityError("a first episode needs at least one snapshot")
        return previous

    target_run_id, foreign = _resolve_run_id(previous, snapshots)
    owned = _latest_per_source([snapshot for snapshot in snapshots if snapshot.run_id == target_run_id])
    # Sort sources so durable provenance is independent of batch order.
    basis = [f"dropped-foreign-run:{name}" for name in sorted(foreign)]
    if previous is not None and previous.run_id != target_run_id:
        basis.append(f"identity-changed-from:{previous.run_id}")

    episode = (
        previous
        if previous is not None and previous.run_id == target_run_id
        else VitalityEpisode(
            run_id=target_run_id,
            started_at=now,
            updated_at=now,
        )
    )
    episode = _unfreeze_if_resumed(episode, owned, now)

    strong = [
        snapshot
        for snapshot in owned.values()
        if snapshot.availability is SourceAvailability.AVAILABLE and not snapshot.advisory
    ]
    progress_evidence = [
        snapshot for snapshot in strong if snapshot.progress in (ProgressState.ADVANCING, ProgressState.QUIET)
    ]
    # ``owned`` is a dict keyed by source, so iterating it directly would let the batch's answer
    # order decide the words a deterministic reduction writes. Every ``basis`` token emitted in
    # this loop names its source, so the loop walks the sorted sources instead: same inputs in any
    # order, same basis out (the order-independence tests pin exactly this for dark sources too).
    for source_value in sorted(snapshot.source.value for snapshot in owned.values()):
        snapshot = owned[SnapshotSource(source_value)]
        if snapshot.availability is SourceAvailability.UNAVAILABLE:
            if snapshot.source.value not in episode.unavailable_since:
                episode = replace(
                    episode,
                    unavailable_since={
                        **episode.unavailable_since,
                        snapshot.source.value: now,
                    },
                )
                basis.append(f"unavailable@{snapshot.source.value}")
        else:
            if snapshot.source.value in episode.unavailable_since:
                episode = replace(
                    episode,
                    unavailable_since={
                        name: stamp
                        for name, stamp in episode.unavailable_since.items()
                        if name != snapshot.source.value
                    },
                )
            if snapshot.cursor:
                episode = replace(
                    episode,
                    evidence_cursors={**episode.evidence_cursors, snapshot.source.value: snapshot.cursor},
                )

    # A progress source this episode has heard from that produced NO snapshot on this tick is
    # dark, exactly like one that answered "unavailable" (secretary-1543 round 2). The status
    # shapes that carry a live heartbeat and no ``provider_progress`` key at all -- an exact live
    # pid with no matching pane, a pane that is not connected -- used to leave ``unavailable_since``
    # empty while the cursor from the tick that did answer stayed on file, so the episode read as
    # "witnessed and not dark": the freeze window this card designed was skipped and the guard
    # counted one channel as two. Darkness is therefore recorded from the absence of an answer,
    # not only from an answer of absence, which is what makes "not dark" mean "answering now".
    answered = {snapshot.source.value for snapshot in owned.values()}
    witnessed_progress = (set(episode.evidence_cursors) | {episode.last_progress_source}) & _PROGRESS_SOURCES
    for source_value in sorted(witnessed_progress - answered):
        if source_value not in episode.unavailable_since:
            episode = replace(
                episode,
                unavailable_since={**episode.unavailable_since, source_value: now},
            )
            basis.append(f"absent@{source_value}")

    # The Turn axis, recorded and nothing more. An advisory reading that goes active -> idle
    # stamps ``turn_ended_at``; one that goes back to active clears it. Recording is not
    # convicting: only ``answer_owed_since`` below ever reads the stamp, and only to narrow.
    advisory_turn = next(
        (
            snapshot
            for snapshot in owned.values()
            if snapshot.advisory
            and snapshot.availability is SourceAvailability.AVAILABLE
            and snapshot.turn is not TurnState.UNKNOWN
        ),
        None,
    )
    if advisory_turn is not None:
        if advisory_turn.turn is TurnState.ACTIVE:
            episode = replace(episode, last_turn="active", turn_ended_at=0.0)
        else:
            episode = replace(
                episode,
                last_turn="idle",
                turn_ended_at=(episode.turn_ended_at if episode.last_turn == "idle" else now)
                if episode.last_turn
                else episode.turn_ended_at,
            )

    verdict = VitalityVerdict.UNVERIFIABLE
    if any(snapshot.process is ProcessState.DEAD for snapshot in strong):
        # Death outranks everything: a gone process cannot also be quietly working.
        verdict = VitalityVerdict.DEAD
        basis.append(
            "dead@"
            + ",".join(
                sorted(snapshot.source.value for snapshot in strong if snapshot.process is ProcessState.DEAD)
            )
        )
    elif any(snapshot.process is ProcessState.SUSPENDED for snapshot in strong):
        # One fact, two possible owners. The kernel says the process is parked; only the caller
        # knows whether the dispatcher is the one holding it there. A declared retention is the
        # dispatcher's own intent, so it may not also be read as a head to revive.
        verdict = VitalityVerdict.RETAINED if retained else VitalityVerdict.SUSPENDED
        by_heartbeat = any(
            snapshot.source is SnapshotSource.PID_HEARTBEAT
            for snapshot in strong
            if snapshot.process is ProcessState.SUSPENDED
        )
        token = verdict.value + ("@pid_heartbeat" if by_heartbeat else "")
        basis.append(token)
        episode = _freeze_stall_clocks(episode, now, retained=retained)
    elif any(snapshot.progress is ProgressState.ADVANCING for snapshot in progress_evidence):
        verdict = VitalityVerdict.HEALTHY_ACTIVE
        advancing = max(
            (snapshot for snapshot in progress_evidence if snapshot.progress is ProgressState.ADVANCING),
            key=lambda snapshot: (snapshot.observed_at, snapshot.source.value),
        )
        basis.append(f"advancing@{advancing.source.value}")
        progressed = float(advancing.observed_at)
        moved_ahead = progressed > episode.last_progress_at
        episode = replace(
            episode,
            suspected_since=0.0,
            confirmed_since=0.0,
            last_progress_at=max(episode.last_progress_at, progressed),
            last_progress_source=advancing.source.value,
            activity_epoch=episode.activity_epoch + (1 if moved_ahead else 0),
            stall_frozen_since=0.0,
            reason="",
        )
    elif progress_evidence:
        # Only observed quiet from a strong, admitted source accumulates toward a stall, and it
        # is measured from the last real progress (or the episode's start), never from the tick.
        quietest = max(progress_evidence, key=lambda snapshot: snapshot.observed_at)
        reference = _quiet_reference(episode)
        quiet_seconds = max(0.0, now - reference)
        basis.append(f"quiet:{int(quiet_seconds)}s@{quietest.source.value}")
        episode = replace(episode, stall_frozen_since=0.0)
        if episode.verdict is VitalityVerdict.CONFIRMED_STALL and not episode.confirmed_since:
            episode = replace(
                episode, confirmed_since=reference + thresholds.suspect_after + thresholds.confirm_after
            )
        if quiet_seconds >= thresholds.suspect_after + thresholds.confirm_after:
            verdict = VitalityVerdict.CONFIRMED_STALL
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since or reference + thresholds.suspect_after + thresholds.confirm_after
                ),
                suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                reason=f"strong quiet for {int(quiet_seconds)}s with no advancement",
            )
            basis.append("confirmed-stall")
        elif quiet_seconds >= thresholds.suspect_after:
            verdict = VitalityVerdict.SUSPECTED_STALL
            episode = replace(
                episode,
                suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                reason=f"strong quiet for {int(quiet_seconds)}s with no advancement",
            )
            basis.append("suspected-stall")
        else:
            verdict = VitalityVerdict.HEALTHY_QUIET
    elif strong:
        # A progress source that is known to this episode and not answering freezes stall time --
        # but only for ``dark_ceiling`` (secretary-1543). Past that window the episode ages
        # exactly as a pid-only run does, because the two situations are the same situation: a
        # live PID and nothing that can say the work moved. Either path still preserves an earned
        # confirmation, and a genuinely dark channel is never itself read as no-progress; what
        # ends is only its power to hold an episode healthy for as long as the PID lives.
        reference = _quiet_reference(episode)
        quiet_seconds = max(0.0, now - reference)
        basis.append(
            "alive-no-progress-source@" + ",".join(sorted(snapshot.source.value for snapshot in strong))
        )
        episode = replace(episode, stall_frozen_since=0.0)
        dark_progress = {
            name: since for name, since in episode.unavailable_since.items() if name in _PROGRESS_SOURCES
        }
        dark_since = min(dark_progress.values()) if dark_progress else 0.0
        dark_seconds = max(0.0, now - dark_since) if dark_progress else 0.0
        dark_names = ",".join(sorted(dark_progress))
        # Naming the source and its darkness on every tick of this arm: the operator question
        # this card exists to answer is "which channel is missing, and for how long".
        if dark_progress:
            basis.append(f"dark:{int(dark_seconds)}s@{dark_names}")
        within_ceiling = bool(dark_progress) and dark_seconds < thresholds.dark_ceiling
        if episode.verdict is VitalityVerdict.CONFIRMED_STALL:
            # Sticky, and sticky first: a dark source may not launder a confirmation back to
            # health, whatever the ceiling has done.
            verdict = VitalityVerdict.CONFIRMED_STALL
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since or reference + thresholds.suspect_after + thresholds.confirm_after
                ),
                reason=(
                    f"{dark_names} not answering for {int(dark_seconds)}s; confirmation stands"
                    if dark_progress
                    else "progress source known but not answering; confirmation stands"
                ),
            )
            basis.append("preserved-confirmation:provider-dark-pid-alive")
        elif within_ceiling and episode.verdict is VitalityVerdict.SUSPECTED_STALL:
            # Inside the freeze window a dark source neither rewinds nor advances a phase.
            verdict = VitalityVerdict.SUSPECTED_STALL
            episode = replace(
                episode,
                suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                reason=(
                    f"{dark_names} not answering for {int(dark_seconds)}s; suspicion stands"
                    if dark_progress
                    else "progress source known but not answering; suspicion stands"
                ),
            )
            basis.append("preserved-suspicion:provider-dark-pid-alive")
        elif within_ceiling:
            verdict = VitalityVerdict.HEALTHY_QUIET
            episode = replace(
                episode,
                reason=(
                    # The frozen note is the honest conclusion here, but a dark
                    # channel's own diagnostic wins when it has one: an authoritative
                    # launch refusal is more useful to the operator (and to the
                    # policy's deterministic allowlist) than the generic frozen words.
                    next(
                        (
                            snapshot.reason
                            for name in sorted(owned)
                            if (snapshot := owned[name]).availability is SourceAvailability.UNAVAILABLE
                            and snapshot.reason
                        ),
                        (
                            f"{dark_names} known to this episode but not answering for "
                            f"{int(dark_seconds)}s; frozen for another "
                            f"{int(thresholds.dark_ceiling - dark_seconds)}s"
                        ),
                    )
                ),
            )
        else:
            # Either no progress source ever answered (the issue 656 pid-only arm) or the one
            # that did has been dark past the ceiling. Both age on the pid's sustained answer of
            # "running, and nothing else"; the reason says which, and names the dark source.
            aged = (
                f"{dark_names} has been dark for {int(dark_seconds)}s "
                f"(past the {int(thresholds.dark_ceiling)}s ceiling) and the head has shown no "
                f"progress for {int(quiet_seconds)}s"
                if dark_progress
                else f"running with no progress evidence for {int(quiet_seconds)}s"
            )
            if quiet_seconds >= thresholds.suspect_after + thresholds.confirm_after:
                # A pid-only run ages: existence alone is not liveness.
                verdict = VitalityVerdict.CONFIRMED_STALL
                episode = replace(
                    episode,
                    confirmed_since=(
                        episode.confirmed_since
                        or reference + thresholds.suspect_after + thresholds.confirm_after
                    ),
                    suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                    reason=aged,
                )
                basis.append("confirmed-stall")
            elif quiet_seconds >= thresholds.suspect_after:
                verdict = VitalityVerdict.SUSPECTED_STALL
                episode = replace(
                    episode,
                    suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                    reason=aged,
                )
                basis.append("suspected-stall")
            else:
                verdict = VitalityVerdict.HEALTHY_QUIET
    else:
        # Every strong source is dark (or only the advisory pane answered). Nothing may be
        # concluded -- and an already-confirmed episode is not laundered back to health by its
        # observers going blind: its confirmation stands frozen until real evidence moves it.
        if episode.verdict is VitalityVerdict.CONFIRMED_STALL:
            verdict = VitalityVerdict.CONFIRMED_STALL
            # The confirmation stands, but its onset must be stamped here too: an episode that
            # arrived confirmed through deserialisation carries the field, yet one reduced into
            # confirmation by a tick whose observers then went dark does not, and a phase without
            # its onset is a claim nobody can audit. The quiet it confirmed is still measured
            # from the same reference every other quiet conclusion uses.
            quiet_reference = _quiet_reference(episode)
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since
                    or quiet_reference + thresholds.suspect_after + thresholds.confirm_after
                ),
            )
            basis.append("preserved-confirmation:strong-sources-unavailable")
        else:
            verdict = VitalityVerdict.UNVERIFIABLE
            basis.append("no-strong-source-answered")
    if (
        verdict is VitalityVerdict.HEALTHY_QUIET
        and answer_owed_since > 0.0
        and episode.last_progress_at < answer_owed_since
        and episode.turn_ended_at >= answer_owed_since
    ):
        # The explicit watchdog signal (secretary-1543): the dispatcher rejected this head's
        # report at ``answer_owed_since`` and sent it back to work, the head then ENDED a turn,
        # and no source has shown progress since. That pair is a conclusion on its own -- there is
        # nothing left running to wait for -- so it does not queue behind the quiet thresholds or
        # the outer ceiling. It can only raise HealthyQuiet: an advancing, dead, suspended,
        # retained or already-stalled head is decided by the arms above, and the advisory reading
        # that stamped ``turn_ended_at`` never establishes health here or anywhere else.
        verdict = VitalityVerdict.SUSPECTED_STALL
        owed_seconds = int(max(0.0, now - answer_owed_since))
        episode = replace(
            episode,
            suspected_since=(episode.suspected_since or episode.turn_ended_at),
            reason=(
                f"the dispatcher's rejected report has gone unanswered for {owed_seconds}s and "
                "the head's turn ended with no new work"
            ),
        )
        basis.append("answer-owed:turn-ended")

    for snapshot in owned.values():
        if snapshot.advisory and snapshot.availability is SourceAvailability.AVAILABLE:
            # Corroboration only: recorded so a human reading the basis sees the pane agreed,
            # weightless by construction.
            basis.append(f"advisory:{snapshot.turn.value}@{snapshot.source.value}")

    return replace(
        episode,
        verdict=verdict,
        # Preserve a dark source diagnostic unless a stronger conclusion supplies one.
        reason=episode.reason
        if episode.reason
        else next(
            (
                snapshot.reason
                for name in sorted(owned)
                if (snapshot := owned[name]).availability is SourceAvailability.UNAVAILABLE
                and snapshot.reason
            ),
            "",
        ),
        basis=tuple(basis[:BASIS_ENTRY_LIMIT]),
        updated_at=now,
    )


def recovery_outlook(
    episode: Any,
    now: float,
    thresholds: VitalityThresholds = DEFAULT_VITALITY_THRESHOLDS,
) -> dict[str, Any]:
    """What an operator needs to read off one episode: what is dark, how long, what happens next.

    Pure like everything else here, and derived from the same arithmetic the reducer uses, so the
    deadline a human is shown is the instant the next reduction will actually act on. It answers
    for one episode only: it starts nothing, and it never re-observes anything.

    ``next_deadline`` is ``None`` where the ladder has no further vitality rung to climb -- a
    confirmed stall (the recovery path owns it), a suspended or retained process (the clocks are
    frozen by the stop signal), death, and a verdict nobody could reach.
    """
    if not isinstance(episode, VitalityEpisode) or not isinstance(thresholds, VitalityThresholds):
        return {"quiet_seconds": 0.0, "dark_sources": [], "next_deadline": None, "note": ""}
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        return {"quiet_seconds": 0.0, "dark_sources": [], "next_deadline": None, "note": ""}
    now = float(now)
    reference = _quiet_reference(episode)
    quiet_seconds = max(0.0, now - reference)
    dark = [
        {
            "source": name,
            "dark_since": since,
            "dark_seconds": max(0.0, now - since),
            "freeze_expires_at": since + thresholds.dark_ceiling,
        }
        for name, since in sorted(episode.unavailable_since.items())
        if name in _PROGRESS_SOURCES
    ]
    frozen_until = max((entry["freeze_expires_at"] for entry in dark), default=0.0)
    note = ""
    at: float | None = None
    becomes = ""
    if episode.verdict is VitalityVerdict.HEALTHY_QUIET:
        becomes = VitalityVerdict.SUSPECTED_STALL.value
        at = max(reference + thresholds.suspect_after, frozen_until)
    elif episode.verdict is VitalityVerdict.SUSPECTED_STALL:
        becomes = VitalityVerdict.CONFIRMED_STALL.value
        at = max(reference + thresholds.suspect_after + thresholds.confirm_after, frozen_until)
    elif episode.verdict is VitalityVerdict.CONFIRMED_STALL:
        note = "the stall is confirmed; the recovery path owns this head, not another threshold"
    elif episode.verdict in (VitalityVerdict.SUSPENDED, VitalityVerdict.RETAINED):
        note = "the stall clocks are frozen while the process is parked on a stop signal"
    return {
        "quiet_seconds": quiet_seconds,
        "dark_sources": dark,
        "next_deadline": (
            None if at is None else {"verdict": becomes, "at": at, "in_seconds": max(0.0, at - now)}
        ),
        "note": note,
    }


def _resolve_run_id(
    previous: VitalityEpisode | None, snapshots: Sequence[VitalitySnapshot]
) -> tuple[str, list[str]]:
    """Pick the run this reduction speaks about, and name the foreign runs it drops.

    Snapshots naming another run are dropped with a ``basis`` note rather than raising: one stale
    reading in a batch must not poison the reduction, and the builders have already fenced
    identity at the source. When *every* snapshot agrees on a run other than the episode's, that
    is an identity change -- a respawned head -- and it starts a fresh episode instead of leaving
    the old one permanently starved.
    """
    run_ids = sorted({snapshot.run_id for snapshot in snapshots})
    chosen = previous.run_id if previous is not None and previous.run_id in run_ids else run_ids[0]
    return chosen, [name for name in run_ids if name != chosen]


def _latest_per_source(snapshots: Sequence[VitalitySnapshot]) -> dict[SnapshotSource, VitalitySnapshot]:
    """Collapse one batch to each source's latest reading, ties resolved by first appearance."""
    latest: dict[SnapshotSource, VitalitySnapshot] = {}
    for snapshot in snapshots:
        known = latest.get(snapshot.source)
        if known is None or snapshot.observed_at > known.observed_at:
            latest[snapshot.source] = snapshot
    return latest


def _freeze_stall_clocks(episode: VitalityEpisode, now: float, *, retained: bool = False) -> VitalityEpisode:
    """Park the quiet clock for a suspended reading: suspension is not stall evidence.

    The freeze itself is identical for both owners of the stop signal -- a parked process is not
    accumulating quiet either way. Only the recorded reason differs, so an operator reading
    ``head-status`` sees which one it is.
    """
    episode = replace(
        episode,
        # Suspension is a distinct, recoverable state -- SIGCONT territory, not stall territory.
        suspected_since=0.0,
        confirmed_since=0.0,
        stall_frozen_since=episode.stall_frozen_since or now,
        reason=(
            "process held on a stop signal by a confirmed retention"
            if retained
            else "process suspended on a stop signal"
        ),
    )
    return episode


def _quiet_reference(episode: VitalityEpisode) -> float:
    """The instant this episode's current quiet began: what every threshold is measured from.

    The later of the last observed advancement and the last conversational restart
    (``quiet_since``), falling back to the episode's start before either has happened. A nudge
    that restarts the clock therefore buys the head the grace its own comment promises, without
    erasing the progress history an operator reads (secretary-1543).
    """
    return max(episode.last_progress_at, episode.quiet_since) or episode.started_at


def _unfreeze_if_resumed(
    episode: VitalityEpisode, owned: Mapping[SnapshotSource, VitalitySnapshot], now: float
) -> VitalityEpisode:
    """Resume the quiet clock after suspension by shifting references past the frozen span.

    The frozen span belongs to neither work nor stall, so every quiet-derived timestamp moves
    forward by exactly it: the head is charged with the silence it was actually awake for.
    """
    if not episode.stall_frozen_since:
        return episode
    still_suspended = any(
        snapshot.availability is SourceAvailability.AVAILABLE
        and not snapshot.advisory
        and snapshot.process is ProcessState.SUSPENDED
        for snapshot in owned.values()
    )
    if still_suspended:
        return episode
    delta = max(0.0, now - episode.stall_frozen_since)

    def shift(stamp: float) -> float:
        return min(now, stamp + delta) if stamp else stamp

    return replace(
        episode,
        started_at=shift(episode.started_at),
        last_progress_at=shift(episode.last_progress_at),
        quiet_since=shift(episode.quiet_since),
        suspected_since=shift(episode.suspected_since),
        confirmed_since=shift(episode.confirmed_since),
        stall_frozen_since=0.0,
    )
