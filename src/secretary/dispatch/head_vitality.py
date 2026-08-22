"""Head-vitality observation vocabulary: three independent axes on one identity-bound snapshot.

The dispatcher's destructive history (secretary-1063, the 2026-08 incidents behind cards
codegen-orchestrator-1194..1197) came from collapsing several different questions into one boolean:
"is the head working?". This module splits that question into the three independent axes of the
head-vitality plan (`state/knowledge/plans/2026-08-21-head-vitality-runtime-orca-migration.md`,
section "Observation") and turns the signals the codebase already produces into pure data:

    Process  = Running | Suspended | Dead | Unknown      does a kernel process exist?
    Turn     = Active | Idle | Unknown                   is a turn in flight?
    Progress = Advancing | Quiet | Stagnant | Unknown    is the work moving?

Nothing here decides anything. There is no threshold, no timer and no recovery ladder in this
module: an observation reports facts, fusion forms suspicion, policy chooses intent, and only the
runtime that owns delivery can make intervention safe. Keeping every producer dumb and pure is what
lets later cards persist episodes and switch the watchdog without re-deriving these meanings at the
call site.

Invariants every consumer may rely on, and every builder below enforces:

  * **A snapshot is bound to one ``HeadRun.run_id``.** A builder refuses to attach a reading to an
    unnamed run, and a source whose own attestation names a *different* live run degrades to
    ``Unknown``/``Unavailable`` -- never to ``Dead``, because "not mine" is not evidence of death.
    A snapshot therefore never combines a new run's pid with an old run's provider cursor: each
    builder stamps exactly the run it proved.
  * **An unavailable source is not evidence of no progress.** Every failure mode maps to
    ``Unknown`` plus ``SourceAvailability.UNAVAILABLE`` with a bounded reason. In particular a
    single unchanged provider cursor is ``Quiet``, never ``Stagnant``: stagnation is a conclusion a
    reducer draws over time, not something one observation can see.
  * **Pane and terminal readings are advisory.** They fill the ``Turn`` axis alone, are stamped
    with the ``pane_advisory`` source, and can never by themselves grant a stop capability --
    readiness answers whether a pane will accept input, not whether the head behind it may be
    killed.

Adapters here are pure functions over plain values: the caller reads ``/proc``, the pid file, the
provider journal or the pane inventory and passes the result in. The module performs no I/O and
imports no runtime host, so a snapshot can be built in a unit test from a hand-written status dict
and the producers' shapes can drift only through a failing test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from secretary.dispatcher_watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_IDENTITY_MISMATCH,
    HEARTBEAT_LIVE_MATCH,
)

# Serialisation version of ``VitalitySnapshot``. The next card persists episodes keyed by this
# vocabulary, so a schema change later must bump this and answer for old payloads explicitly
# rather than reinterpreting them silently.
SNAPSHOT_VERSION = 1

# Reasons travel in dispatcher records and operator output. Longer than any diagnostic this module
# writes, short enough that one snapshot can never smuggle a transcript into durable state.
REASON_LIMIT = 240
CURSOR_LIMIT = 240


class HeadVitalityError(RuntimeError):
    """A snapshot was asked to say something no observation can truthfully say."""


class ProcessState(StrEnum):
    """Whether the kernel still has a process behind the run's launch identity."""

    RUNNING = "running"
    SUSPENDED = "suspended"
    DEAD = "dead"
    UNKNOWN = "unknown"


class TurnState(StrEnum):
    """Whether a conversational turn is believed to be in flight right now."""

    ACTIVE = "active"
    IDLE = "idle"
    UNKNOWN = "unknown"


class ProgressState(StrEnum):
    """Whether the work itself is seen to move, as of one instant.

    ``STAGNANT`` exists in the vocabulary because the reducer needs somewhere to record a
    stagnation conclusion, but no single-snapshot builder may ever produce it: one unchanged
    cursor is ``QUIET``, and calling it stagnation is the six-hour-ceiling mistake this sprint
    exists to remove.
    """

    ADVANCING = "advancing"
    QUIET = "quiet"
    STAGNANT = "stagnant"
    UNKNOWN = "unknown"


class SourceAvailability(StrEnum):
    """Whether the observing channel answered at all.

    ``UNAVAILABLE`` is a statement about the channel, never about the head: a missing pid file, a
    refused pane probe or an unreadable journal freezes the observer's knowledge instead of voting
    for a stall.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class SnapshotSource(StrEnum):
    """Which channel produced a snapshot.

    One snapshot comes from one channel. Fusing channels is the reducer's job, and keeping the
    source name on the snapshot is what lets the reducer weight them differently (an advisory pane
    reading and a pid heartbeat are not equally authoritative about anything).
    """

    PID_HEARTBEAT = "pid_heartbeat"
    PROVIDER_CURSOR = "provider_cursor"
    PANE_ADVISORY = "pane_advisory"
    EXECUTION_RECEIPT = "execution_receipt"


@dataclass(frozen=True)
class VitalitySnapshot:
    """One channel's reading of one head run at one instant.

    Frozen so a snapshot can be stored on a dispatcher record, compared structurally in tests and
    handed across tick boundaries without a reader being able to edit history in place. Axes the
    producing channel cannot answer stay ``Unknown``: independence means a pid heartbeat that says
    nothing about turns fills exactly one axis, and a consumer that wanted all three filled from
    one source would be re-fusing the channels this module exists to keep apart.
    """

    run_id: str
    source: SnapshotSource
    observed_at: float
    availability: SourceAvailability
    process: ProcessState = ProcessState.UNKNOWN
    turn: TurnState = TurnState.UNKNOWN
    progress: ProgressState = ProgressState.UNKNOWN
    # Opaque, compared but never parsed: the reducer detects advancement per source by inequality
    # alone. ``None`` when this channel exposed no cursor or could not be read.
    cursor: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.run_id or "").strip():
            raise HeadVitalityError("a vitality snapshot is bound to a HeadRun.run_id")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float)):
            raise HeadVitalityError("a vitality snapshot carries an epoch-seconds timestamp")
        observed_at = float(self.observed_at)
        if not math.isfinite(observed_at) or observed_at < 0:
            raise HeadVitalityError("a vitality snapshot timestamp is finite and not negative")
        # Frozen fields are normalised through ``object.__setattr__`` once, at construction, so a
        # snapshot built from a chatty diagnostic and its serialised form compare equal.
        object.__setattr__(self, "run_id", str(self.run_id))
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "cursor",
                           None if self.cursor is None else str(self.cursor)[:CURSOR_LIMIT])
        object.__setattr__(self, "reason", str(self.reason or "")[:REASON_LIMIT])

    @property
    def advisory(self) -> bool:
        """Whether this snapshot is advisory-only and can never authorise intervention."""
        return self.source is SnapshotSource.PANE_ADVISORY

    def to_json(self) -> dict[str, Any]:
        """The durable form a later episode write stores beside the run it describes."""
        return {
            "version": SNAPSHOT_VERSION,
            "run_id": self.run_id,
            "source": self.source.value,
            "observed_at": self.observed_at,
            "availability": self.availability.value,
            "process": self.process.value,
            "turn": self.turn.value,
            "progress": self.progress.value,
            "cursor": self.cursor,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, payload: Any) -> VitalitySnapshot:
        """Read one snapshot back, refusing shapes that would silently change its meaning.

        Following ``DispatcherRecord``, a malformed payload raises instead of being repaired: a
        snapshot that came back with a renamed axis or an unbound run is damaged evidence, and
        quietly normalising it would let a later consumer act on words nobody wrote.
        """
        if not isinstance(payload, dict):
            raise HeadVitalityError("a vitality snapshot is read from an object")
        # ``True == 1`` in Python, so an equality check alone would read a boolean version as a
        # supported one; the version is either this exact int or the payload is not ours.
        if type(payload.get("version")) is not int or payload.get("version") != SNAPSHOT_VERSION:
            raise HeadVitalityError("vitality snapshot has an unsupported version")
        raw_observed_at = payload.get("observed_at")
        if isinstance(raw_observed_at, bool) or not isinstance(raw_observed_at, (int, float)):
            # Unlike a record that may default its claim time to now, a snapshot has no honest
            # default for "when was this observed": a damaged timestamp is refused, not zeroed.
            raise HeadVitalityError("vitality snapshot timestamp is not a number")
        observed_at = float(raw_observed_at)
        if not math.isfinite(observed_at):
            # NaN sorts neither before nor after anything and inf never ages out, so either value
            # would silently break every "how long has it been quiet" comparison downstream.
            raise HeadVitalityError("vitality snapshot timestamp is not finite")
        try:
            source = SnapshotSource(str(payload.get("source") or ""))
            availability = SourceAvailability(str(payload.get("availability") or ""))
            process = ProcessState(str(payload.get("process") or ""))
            turn = TurnState(str(payload.get("turn") or ""))
            progress = ProgressState(str(payload.get("progress") or ""))
        except ValueError:
            raise HeadVitalityError("vitality snapshot names an axis outside its vocabulary") from None
        cursor = payload.get("cursor")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            source=source,
            observed_at=observed_at,
            availability=availability,
            process=process,
            turn=turn,
            progress=progress,
            cursor=None if cursor is None else str(cursor),
            reason=str(payload.get("reason") or ""),
        )

    @classmethod
    def from_pid_heartbeat(
        cls, status: Any, *, run_id: str, observed_at: float
    ) -> VitalitySnapshot:
        """Wrap one ``dispatcher_watchdog.head_process_status`` answer.

        The heartbeat is authoritative about the ``Process`` axis and about nothing else. Its
        inconclusive states (file not yet written, unreadable, wrong version) are observations of a
        broken channel, so they arrive ``Unknown``/``Unavailable`` with the distinct state named in
        the reason rather than collapsed into one lie about the process. An identity mismatch is
        fenced the same way: some *other* live process owns that heartbeat, which proves nothing
        about this run and must never read as ``Dead``.
        """
        if not isinstance(status, dict):
            return cls._unavailable(
                run_id=run_id, observed_at=observed_at, source=SnapshotSource.PID_HEARTBEAT,
                reason="pid heartbeat status is not an object",
            )
        state = str(status.get("state") or "")
        if state == HEARTBEAT_LIVE_MATCH:
            return cls(
                run_id=run_id,
                source=SnapshotSource.PID_HEARTBEAT,
                observed_at=observed_at,
                availability=SourceAvailability.AVAILABLE,
                process=(
                    ProcessState.SUSPENDED
                    if bool(status.get("stopped"))
                    else ProcessState.RUNNING
                ),
                reason="",
            )
        if state == HEARTBEAT_DEAD:
            return cls(
                run_id=run_id,
                source=SnapshotSource.PID_HEARTBEAT,
                observed_at=observed_at,
                availability=SourceAvailability.AVAILABLE,
                process=ProcessState.DEAD,
                reason="heartbeat names a process that is gone or unreaped",
            )
        if state == HEARTBEAT_IDENTITY_MISMATCH:
            return cls._unavailable(
                run_id=run_id, observed_at=observed_at, source=SnapshotSource.PID_HEARTBEAT,
                reason="pid heartbeat belongs to another live HeadRun",
            )
        # ``not-yet-written``, ``unreadable`` and their kin: the channel said why it cannot answer.
        return cls._unavailable(
            run_id=run_id, observed_at=observed_at, source=SnapshotSource.PID_HEARTBEAT,
            reason=f"pid heartbeat is inconclusive: {state or 'no classification'}",
        )

    @classmethod
    def from_provider_cursor(
        cls,
        evidence: Any,
        *,
        run_id: str,
        previous_cursor: str = "",
        observed_at: float,
    ) -> VitalitySnapshot:
        """Wrap one ``dispatcher_tui.provider_progress_for_run`` answer against the earlier cursor.

        ``previous_cursor`` is the opaque cursor of this exact run's previous snapshot of this
        exact source (empty before the first observation). Only the admitted, bound evidence counts
        as an answer: an unadmitted or foreign-bound reading is a channel problem and lands on
        ``Unknown``/``Unavailable`` -- in particular it never becomes ``Quiet``, so a reducer can
        never spend stall evidence on a source that went away. Movement and stillness are the only
        two things one cursor pair can prove, and they map to ``Advancing`` and ``Quiet``;
        deciding that quiet has lasted too long belongs to the episode reducer.
        """
        if not isinstance(evidence, dict):
            return cls._unavailable(
                run_id=run_id, observed_at=observed_at, source=SnapshotSource.PROVIDER_CURSOR,
                reason="provider cursor evidence is not an object",
            )
        if (
            str(evidence.get("state") or "") != "observed"
            or str(evidence.get("admission") or "") != "accepted"
        ):
            detail = str(evidence.get("reason") or "")
            if str(evidence.get("state") or "") == "identity_mismatch":
                detail = detail or "provider cursor is bound to another HeadRun"
            return cls._unavailable(
                run_id=run_id, observed_at=observed_at, source=SnapshotSource.PROVIDER_CURSOR,
                reason=f"provider cursor is not admitted: {detail}".strip(": "),
            )
        if str(evidence.get("head_run_id") or "") != str(run_id):
            return cls._unavailable(
                run_id=run_id, observed_at=observed_at, source=SnapshotSource.PROVIDER_CURSOR,
                reason="provider cursor names a HeadRun other than the snapshot's run",
            )
        cursor = str(evidence.get("cursor") or "")
        if not cursor:
            return cls._unavailable(
                run_id=run_id, observed_at=observed_at, source=SnapshotSource.PROVIDER_CURSOR,
                reason="admitted provider cursor carries no cursor value",
            )
        if not previous_cursor:
            return cls(
                run_id=run_id,
                source=SnapshotSource.PROVIDER_CURSOR,
                observed_at=observed_at,
                availability=SourceAvailability.AVAILABLE,
                progress=ProgressState.UNKNOWN,
                cursor=cursor,
                reason="first observation of this source: no earlier cursor to compare against",
            )
        advanced = cursor[:CURSOR_LIMIT] != previous_cursor[:CURSOR_LIMIT]
        return cls(
            run_id=run_id,
            source=SnapshotSource.PROVIDER_CURSOR,
            observed_at=observed_at,
            availability=SourceAvailability.AVAILABLE,
            progress=ProgressState.ADVANCING if advanced else ProgressState.QUIET,
            cursor=cursor,
            reason="" if advanced else "provider cursor unchanged since the previous snapshot",
        )

    @classmethod
    def from_pane_readiness(
        cls, status: Any, *, run_id: str, observed_at: float
    ) -> VitalitySnapshot:
        """Wrap one pane readiness answer (`{"idle": bool}` as callers of ``PaneHost`` build it).

        Advisory by construction: the session manager answers whether a pane will take input, which
        speaks to the ``Turn`` axis alone. A busy pane is a head possibly mid-turn, an idle pane is
        a head between turns, and neither reading says anything about the process or the work --
        which is why this source can never, alone, ground a stop decision. An unanswerable probe
        (no status object, no usable flag) is ``Unknown`` and still advisory.
        """
        idle = status.get("idle") if isinstance(status, dict) else None
        if isinstance(idle, bool):
            turn = TurnState.IDLE if idle else TurnState.ACTIVE
            reason = ""
        else:
            turn = TurnState.UNKNOWN
            reason = "pane readiness did not answer"
        return cls(
            run_id=run_id,
            source=SnapshotSource.PANE_ADVISORY,
            observed_at=observed_at,
            availability=SourceAvailability.AVAILABLE if isinstance(idle, bool)
            else SourceAvailability.UNAVAILABLE,
            turn=turn,
            reason=reason,
        )

    @classmethod
    def _unavailable(
        cls, *, run_id: str, observed_at: float, source: SnapshotSource, reason: str
    ) -> VitalitySnapshot:
        """The one shape every failed observation takes, so consumers learn it once."""
        return cls(
            run_id=run_id,
            source=source,
            observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason=reason[:REASON_LIMIT],
        )
