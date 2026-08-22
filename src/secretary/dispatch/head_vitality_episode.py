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
  * advancement from any non-advisory source ends a suspected or confirmed episode immediately;
  * an unavailable source freezes its evidence and never counts as no-progress; with every strong
    source dark the truthful verdict is ``Unverifiable`` -- except that an already-confirmed
    episode is not quietly laundered back to health by its observers going blind;
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

# The sources whose axis is Progress. An episode that has ever seen one of these answer --
# a cursor on file or a dark entry in ``unavailable_since`` -- has witnessed progress
# evidence, which is what separates "the progress channel broke" (freeze, never spend)
# from "nothing but the pid has ever spoken here" (issue 656: bare existence ages).
_PROGRESS_SOURCES = frozenset({SnapshotSource.PROVIDER_CURSOR.value})


class VitalityVerdict(StrEnum):
    """What the reducer concludes about one run, as of one reduction."""

    HEALTHY_ACTIVE = "healthy_active"
    HEALTHY_QUIET = "healthy_quiet"
    SUSPENDED = "suspended"
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
    """

    suspect_after: float
    confirm_after: float

    def __post_init__(self) -> None:
        for name in ("suspect_after", "confirm_after"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HeadVitalityError(f"vitality threshold {name} is a number")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise HeadVitalityError(f"vitality threshold {name} is positive and finite")
        object.__setattr__(self, "suspect_after", float(self.suspect_after))
        object.__setattr__(self, "confirm_after", float(self.confirm_after))


DEFAULT_VITALITY_THRESHOLDS = VitalityThresholds(
    suspect_after=float(IDLE_STALL_DEFAULT),
    confirm_after=2.0 * float(IDLE_STALL_DEFAULT),
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
    # Owned by the recovery policy (card S1-5). The reducer neither sets nor reads any of
    # them; ``replace`` carries them through every reduction untouched, which is exactly how
    # rung state survives from one tick's decision to the next.
    #
    # ``recovery_rung`` is the ladder position this head's current intervention episode has
    # reached (0 none, 1 suspicion noted, 2 SIGCONT sent, 3 response window running,
    # 4 operator escalated -- see ``head_vitality_policy``).
    #
    # ``recovery_span_started_at`` is the freeze stamp (``stall_frozen_since`` value) of the
    # suspension span those rungs were climbed in. Keying the rung to the span is what makes
    # SIGCONT-once-per-span work across ticks and dispatcher restarts: a resumed-and-
    # re-suspended head starts a new span and therefore a fresh ladder, while every tick
    # inside one span sees the same key.
    #
    # ``deterministic_refusals`` counts consecutive observations whose reason carried an
    # authoritative deterministic terminal class (the 1194 allowlist). It is deliberately
    # NOT keyed to the suspension span: a launch-refusal loop is not a suspension story, and
    # a resumed head does not un-refuse the command that refused.
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

    def __post_init__(self) -> None:
        if not str(self.run_id or "").strip():
            raise HeadVitalityError("a vitality episode is bound to a HeadRun.run_id")
        if not isinstance(self.verdict, VitalityVerdict):
            raise HeadVitalityError("a vitality episode carries a VitalityVerdict")
        for name in ("started_at", "suspected_since", "confirmed_since", "last_progress_at",
                     "updated_at", "stall_frozen_since"):
            object.__setattr__(self, name, _finite_timestamp(getattr(self, name), name))
        if isinstance(self.recovery_rung, bool) or not isinstance(self.recovery_rung, int):
            raise HeadVitalityError("a vitality episode recovery rung is an int")
        if self.recovery_rung < 0:
            raise HeadVitalityError("a vitality episode recovery rung is not negative")
        object.__setattr__(
            self, "recovery_span_started_at",
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
            self, "evidence_cursors",
            {
                str(name)[:80]: str(cursor)[:CURSOR_LIMIT]
                for name, cursor in self.evidence_cursors.items()
            },
        )
        object.__setattr__(
            self, "unavailable_since",
            {
                str(name)[:80]: _finite_timestamp(since, "unavailable_since")
                for name, since in self.unavailable_since.items()
            },
        )
        object.__setattr__(self, "basis", tuple(str(token)[:BASIS_TOKEN_LIMIT] for token in self.basis)[:BASIS_ENTRY_LIMIT])
        object.__setattr__(self, "reason", str(self.reason or "")[:REASON_LIMIT])

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
        # Fields the recovery policy (S1-5) added: absent on records written before it ran,
        # which is exactly "no rung climbed yet" -- mapped to their zero values rather than
        # refused, so pre-policy episodes load and keep deciding.
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
            evidence_cursors=dict(cursors),
            unavailable_since=dict(unavailable),
            basis=tuple(basis),
            reason=str(payload.get("reason") or ""),
            recovery_rung=rung,
            recovery_span_started_at=_finite_timestamp(span, "recovery_span_started_at"),
            deterministic_refusals=refusals,
            activity_epoch=epoch,
            updated_at=_finite_timestamp(payload.get("updated_at"), "updated_at"),
            stall_frozen_since=_finite_timestamp(
                payload.get("stall_frozen_since"), "stall_frozen_since"
            ),
        )


def reduce_vitality(
    previous: VitalityEpisode | None,
    snapshots: Sequence[VitalitySnapshot],
    now: float,
    thresholds: VitalityThresholds = DEFAULT_VITALITY_THRESHOLDS,
) -> VitalityEpisode:
    """Fold one tick's snapshots into the run's episode, purely and deterministically.

    ``previous`` is the persisted episode for the run (``None`` before the first). ``snapshots``
    are this tick's observations, in any order -- duplicates collapse to each source's latest
    reading. The function never reads a clock, a file or a host: everything it concludes is a
    function of its arguments, which is what makes a persisted episode replayable and its tests
    able to reproduce historical incidents tick by tick.
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

    if not snapshots:
        # No observation, no movement: an episode must not age on ticks that looked at nothing.
        if previous is None:
            raise HeadVitalityError("a first episode needs at least one snapshot")
        return previous

    target_run_id, foreign = _resolve_run_id(previous, snapshots)
    owned = _latest_per_source(
        [snapshot for snapshot in snapshots if snapshot.run_id == target_run_id]
    )
    # ``basis`` is durable provenance that tests and humans compare; it is built from sorted
    # sources so batch order cannot change the words a deterministic reduction writes.
    basis = [f"dropped-foreign-run:{name}" for name in sorted(foreign)]
    if previous is not None and previous.run_id != target_run_id:
        basis.append(f"identity-changed-from:{previous.run_id}")

    episode = previous if previous is not None and previous.run_id == target_run_id else VitalityEpisode(
        run_id=target_run_id, started_at=now, updated_at=now,
    )
    episode = _unfreeze_if_resumed(episode, owned, now)

    strong = [
        snapshot for snapshot in owned.values()
        if snapshot.availability is SourceAvailability.AVAILABLE and not snapshot.advisory
    ]
    progress_evidence = [
        snapshot for snapshot in strong
        if snapshot.progress in (ProgressState.ADVANCING, ProgressState.QUIET)
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
                        name: stamp for name, stamp in episode.unavailable_since.items()
                        if name != snapshot.source.value
                    },
                )
            if snapshot.cursor:
                episode = replace(
                    episode,
                    evidence_cursors={**episode.evidence_cursors, snapshot.source.value: snapshot.cursor},
                )

    verdict = VitalityVerdict.UNVERIFIABLE
    if any(snapshot.process is ProcessState.DEAD for snapshot in strong):
        # Death outranks everything: a gone process cannot also be quietly working.
        verdict = VitalityVerdict.DEAD
        basis.append("dead@" + ",".join(sorted(
            snapshot.source.value for snapshot in strong
            if snapshot.process is ProcessState.DEAD
        )))
    elif any(snapshot.process is ProcessState.SUSPENDED for snapshot in strong):
        verdict = VitalityVerdict.SUSPENDED
        basis.append("suspended@pid_heartbeat" if any(
            snapshot.source is SnapshotSource.PID_HEARTBEAT for snapshot in strong
            if snapshot.process is ProcessState.SUSPENDED
        ) else "suspended")
        episode = _freeze_stall_clocks(episode, now)
    elif any(snapshot.progress is ProgressState.ADVANCING for snapshot in progress_evidence):
        verdict = VitalityVerdict.HEALTHY_ACTIVE
        advancing = max(
            (snapshot for snapshot in progress_evidence
             if snapshot.progress is ProgressState.ADVANCING),
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
        reference = episode.last_progress_at or episode.started_at
        quiet_seconds = max(0.0, now - reference)
        basis.append(f"quiet:{int(quiet_seconds)}s@{quietest.source.value}")
        episode = replace(episode, stall_frozen_since=0.0)
        if episode.verdict is VitalityVerdict.CONFIRMED_STALL and not episode.confirmed_since:
            episode = replace(episode, confirmed_since=reference + thresholds.suspect_after + thresholds.confirm_after)
        if quiet_seconds >= thresholds.suspect_after + thresholds.confirm_after:
            verdict = VitalityVerdict.CONFIRMED_STALL
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since
                    or reference + thresholds.suspect_after + thresholds.confirm_after
                ),
                suspected_since=(
                    episode.suspected_since
                    or reference + thresholds.suspect_after
                ),
                reason=f"strong quiet for {int(quiet_seconds)}s with no advancement",
            )
            basis.append("confirmed-stall")
        elif quiet_seconds >= thresholds.suspect_after:
            verdict = VitalityVerdict.SUSPECTED_STALL
            episode = replace(
                episode,
                suspected_since=(
                    episode.suspected_since or reference + thresholds.suspect_after
                ),
                reason=f"strong quiet for {int(quiet_seconds)}s with no advancement",
            )
            basis.append("suspected-stall")
        else:
            verdict = VitalityVerdict.HEALTHY_QUIET
    elif strong:
        # A strong channel answered about the process but none could speak about progress
        # (the pid heartbeat alone, or a provider still on its first observation). Two
        # sub-cases the plan separates, and conflating them caused the incidents:
        #
        #   * A progress source this episode has *witnessed* -- it left a cursor, or it is
        #     tracked dark in ``unavailable_since`` -- has evidence on file. Its silence is
        #     unavailability, and unavailable evidence freezes instead of spending (the
        #     plan's ``Unavailable != no progress``; sprint Done-when: an unavailable source
        #     never feeds the stall counter), so no NEW stall time accrues here: a broken
        #     channel must not age a live head toward its death.
        #   * No progress source has ever answered: the pid is this episode's only witness.
        #     The run is provably alive, and claiming ``quiet`` would assert an observation
        #     nobody made -- but neither may it rest healthy forever. Issue 656's contract
        #     is that the existence of a process is not proof of liveness, so the pid's own
        #     sustained answer of "running, and nothing else" ages from the same reference
        #     every quiet conclusion uses. The absent source contributes no vote of its
        #     own: it is neither progress (the reference never moves) nor quiet (no
        #     ``quiet:<n>s`` token names it).
        #
        # In both arms the sticky-confirmation invariant holds unchanged (only progress,
        # suspension, death or an identity change ends an earned confirmation): a phase the
        # ladder has already reached is preserved below, never demoted by its observers'
        # silence.
        reference = episode.last_progress_at or episode.started_at
        quiet_seconds = max(0.0, now - reference)
        basis.append("alive-no-progress-source@" + ",".join(sorted(
            snapshot.source.value for snapshot in strong
        )))
        episode = replace(episode, stall_frozen_since=0.0)
        progress_witnessed = bool(
            episode.unavailable_since.keys() & _PROGRESS_SOURCES
            or episode.evidence_cursors.keys() & _PROGRESS_SOURCES
        )
        if progress_witnessed:
            # The freeze arm: no new stall time accrues while the witnessed progress source
            # is silent. One exception, and it is the sticky-confirmation invariant, not an
            # aging rule: a confirmation the ladder already EARNED on real quiet evidence
            # is not laundered back to health by its observers going dark -- exactly as in
            # the all-strong-dark branch below. Only progress, suspension, death or an
            # identity change ends it; its onset stands so the phase stays auditable.
            # A dark source never advances AND never rewinds a phase. Whatever the ladder has
            # EARNED on real evidence before the channel went dark stands frozen -- with its
            # onset -- until one of the four authorised endings moves it; only phases below
            # suspicion stay (truthfully) at HealthyQuiet, because freezing must not invent
            # a suspicion nobody earned.
            if episode.verdict is VitalityVerdict.CONFIRMED_STALL:
                verdict = VitalityVerdict.CONFIRMED_STALL
                episode = replace(
                    episode,
                    confirmed_since=(
                        episode.confirmed_since
                        or reference + thresholds.suspect_after + thresholds.confirm_after
                    ),
                    reason="progress source known but not answering; confirmation stands",
                )
                basis.append("preserved-confirmation:provider-dark-pid-alive")
            elif episode.verdict is VitalityVerdict.SUSPECTED_STALL:
                verdict = VitalityVerdict.SUSPECTED_STALL
                episode = replace(
                    episode,
                    suspected_since=(
                        episode.suspected_since or reference + thresholds.suspect_after
                    ),
                    reason="progress source known but not answering; suspicion stands",
                )
                basis.append("preserved-suspicion:provider-dark-pid-alive")
            else:
                verdict = VitalityVerdict.HEALTHY_QUIET
                episode = replace(
                    episode,
                    reason="progress source known to this episode but not answering; frozen",
                )
        elif quiet_seconds >= thresholds.suspect_after + thresholds.confirm_after:
            # The pid-only aging arm: no progress source has ever answered, so issue 656's
            # "existence is not liveness" applies and the pid's own long silence about
            # progress climbs the ladder from the episode's reference.
            verdict = VitalityVerdict.CONFIRMED_STALL
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since
                    or reference + thresholds.suspect_after + thresholds.confirm_after
                ),
                suspected_since=(
                    episode.suspected_since
                    or reference + thresholds.suspect_after
                ),
                reason=f"running with no progress evidence for {int(quiet_seconds)}s",
            )
            basis.append("confirmed-stall")
        elif quiet_seconds >= thresholds.suspect_after:
            verdict = VitalityVerdict.SUSPECTED_STALL
            episode = replace(
                episode,
                suspected_since=(
                    episode.suspected_since
                    or reference + thresholds.suspect_after
                ),
                reason=f"running with no progress evidence for {int(quiet_seconds)}s",
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
            quiet_reference = episode.last_progress_at or episode.started_at
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
    for snapshot in owned.values():
        if snapshot.advisory and snapshot.availability is SourceAvailability.AVAILABLE:
            # Corroboration only: recorded so a human reading the basis sees the pane agreed,
            # weightless by construction.
            basis.append(f"advisory:{snapshot.turn.value}@{snapshot.source.value}")

    return replace(
        episode,
        verdict=verdict,
        # A dark source's own bounded diagnostic rides on the episode's reason whenever no
        # conclusion of our own claims the field: the recovery policy (S1-5) keys its
        # deterministic-refusal allowlist on exactly this string, so a launch refusal like
        # ``terminal_split_source_not_found`` must survive reduction, not be flattened into
        # an anonymous "nothing answered". Verdicts that DO write a reason (stalls,
        # suspension) keep theirs -- those conclusions outrank quoting a channel.
        reason=episode.reason if episode.reason else next(
            (snapshot.reason for name in sorted(owned)
             if (snapshot := owned[name]).availability is SourceAvailability.UNAVAILABLE
             and snapshot.reason),
            "",
        ),
        basis=tuple(basis[:BASIS_ENTRY_LIMIT]),
        updated_at=now,
    )


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


def _freeze_stall_clocks(episode: VitalityEpisode, now: float) -> VitalityEpisode:
    """Park the quiet clock for a suspended reading: suspension is not stall evidence."""
    episode = replace(
        episode,
        # Suspension is a distinct, recoverable state -- SIGCONT territory, not stall territory.
        suspected_since=0.0,
        confirmed_since=0.0,
        stall_frozen_since=episode.stall_frozen_since or now,
        reason="process suspended on a stop signal",
    )
    return episode


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
        suspected_since=shift(episode.suspected_since),
        confirmed_since=shift(episode.confirmed_since),
        stall_frozen_since=0.0,
    )
