"""Durable state for handing a worker across mechanical validation.

The board state and the head state are separate machines.  A card can still be In progress while
its completed worker is already suspended, or already be back In progress while a retained worker
is accepting its continuation.  Keeping those facts in one typed value avoids invalid combinations
of timestamps and string flags on ``DispatcherRecord``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WorkerContinuationStage(StrEnum):
    NONE = "none"
    VALIDATION_MOVE_PENDING = "validation_move_pending"
    RETAINED = "retained"
    # A substantive reviewer verdict, before and after the board move that parks the card in
    # Assessment. The two are separate stages for the same reason the red transition splits its
    # move: the intent has to be on disk before anything observable happens, and recovery has to
    # be able to tell "the move may not have landed" from "the card is parked and waiting".
    ASSESSMENT_PENDING = "assessment_pending"
    ASSESSMENT_PARKED = "assessment_parked"
    RED_TRANSITION_PENDING = "red_transition_pending"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERY_CONFIRMED = "delivery_confirmed"


@dataclass
class WorkerContinuation:
    stage: WorkerContinuationStage = WorkerContinuationStage.NONE
    phase: str = ""
    retained_at: float = 0.0
    sent_at: float = 0.0
    report_baseline: int = 0
    move_reason: str = ""
    """The comment the red move carries, kept so recovery finishes the move it opened.

    A gate verdict is not reconstructible from the record: the CI summary and its tail live in the
    tick that read them. Recovery that re-derived a reason would either move the card with a
    different body or have to re-run the gate, which is exactly the replay this stage exists to
    prevent.
    """
    verdict_outcome: str = ""
    decision: str = ""
    """The observer decision this transition is performing, empty when nobody decided anything.

    A red mechanical gate opens its transition with no decision behind it; a rework decision on a
    parked card opens the same transition and must carry the decision into the board move, which
    refuses to take a card out of Assessment without one.
    """
    session_held: bool = False
    """Whether a suspended session of this round is still there to be resumed.

    Separate from the stage because a red transition outlives the session it was opened over: the
    intent has to be durable whether or not anything can take the continuation, and a confirmed
    stop in the middle of one drops the session without dropping the transition.
    """

    @property
    def retained(self) -> bool:
        return self.session_held and self.stage != WorkerContinuationStage.NONE

    @property
    def awaiting_continuation(self) -> bool:
        return self.stage == WorkerContinuationStage.RETAINED

    @property
    def validation_move_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.VALIDATION_MOVE_PENDING

    @property
    def assessment_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.ASSESSMENT_PENDING

    @property
    def parked(self) -> bool:
        """The card is held by a verdict nobody has acted on, move landed or not."""
        return self.stage in {
            WorkerContinuationStage.ASSESSMENT_PENDING,
            WorkerContinuationStage.ASSESSMENT_PARKED,
        }

    @property
    def red_transition_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.RED_TRANSITION_PENDING

    @property
    def delivery_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.DELIVERY_PENDING

    @property
    def delivery_confirmed(self) -> bool:
        return self.stage == WorkerContinuationStage.DELIVERY_CONFIRMED

    def begin_retention(self, now: float) -> None:
        self.stage = WorkerContinuationStage.VALIDATION_MOVE_PENDING
        self.phase = ""
        self.retained_at = now
        self.sent_at = 0.0
        self.session_held = True

    def confirm_validation_move(self) -> None:
        if self.stage == WorkerContinuationStage.RETAINED:
            return
        if self.stage != WorkerContinuationStage.VALIDATION_MOVE_PENDING:
            raise ValueError(f"cannot confirm validation move from {self.stage}")
        self.stage = WorkerContinuationStage.RETAINED

    def begin_park(
        self, phase: str, report_baseline: int, move_reason: str, verdict_outcome: str
    ) -> None:
        """Record the reviewer's verdict before the card is parked in Assessment.

        This is the whole point of the seam: the verdict is durable here, and nothing has yet
        merged, resumed a worker or moved the board. A tick that dies between this write and the
        move is recovered by finishing the move, never by re-deciding what the verdict meant.

        Re-entry from `ASSESSMENT_PENDING` is allowed so the recovery of an unlanded move is the
        same call as the first attempt.
        """
        if self.stage not in {
            WorkerContinuationStage.NONE,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
            WorkerContinuationStage.RETAINED,
            WorkerContinuationStage.ASSESSMENT_PENDING,
        }:
            raise ValueError(f"cannot park from {self.stage}")
        self.stage = WorkerContinuationStage.ASSESSMENT_PENDING
        self.phase = phase
        self.report_baseline = int(report_baseline)
        self.move_reason = move_reason
        self.verdict_outcome = verdict_outcome

    def confirm_park(self) -> None:
        """The card is in Assessment. From here only a recorded decision moves it."""
        if self.stage == WorkerContinuationStage.ASSESSMENT_PARKED:
            return
        if self.stage != WorkerContinuationStage.ASSESSMENT_PENDING:
            raise ValueError(f"cannot confirm a park from {self.stage}")
        self.stage = WorkerContinuationStage.ASSESSMENT_PARKED

    def begin_red_transition(
        self, phase: str, report_baseline: int, move_reason: str, verdict_outcome: str,
        decision: str = "",
    ) -> None:
        """Record the red verdict before the board is moved.

        The board move and the state write are separate durable facts. Without this the record of a
        crashed tick still names the report that closed the previous round, and recovery reads that
        report as a new completion instead of finishing the red transition.

        Opening one over a round that holds no session is the same transition, not a lesser case:
        whether the continuation ends up in the old conversation or in a replacement is decided
        after this, and a round with nothing to reuse is exactly the one whose replacement must not
        be lost to a crash.

        An open transition is immutable. It carries everything its own completion needs, so a tick
        that finds one finishes it rather than opening a second one over a freshly read verdict.
        """
        if self.stage not in {
            WorkerContinuationStage.NONE,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
            WorkerContinuationStage.RETAINED,
            # A rework decision on a parked card opens the transition the park was holding back.
            WorkerContinuationStage.ASSESSMENT_PARKED,
        }:
            raise ValueError(f"cannot open a red transition from {self.stage}")
        self.stage = WorkerContinuationStage.RED_TRANSITION_PENDING
        self.phase = phase
        self.report_baseline = int(report_baseline)
        self.move_reason = move_reason
        self.verdict_outcome = verdict_outcome
        self.decision = decision

    def begin_delivery(self, phase: str, now: float) -> None:
        # `DELIVERY_PENDING` is allowed back in: a tick that died between the send and its
        # checkpoint is recovered by re-entering the same delivery, not by a different path.
        if self.stage not in {
            WorkerContinuationStage.RETAINED,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
            WorkerContinuationStage.DELIVERY_PENDING,
        }:
            raise ValueError(f"cannot resume worker from {self.stage}")
        if self.stage != WorkerContinuationStage.DELIVERY_PENDING:
            # The earliest send is what a turn is looked for after. Moving this forward on a retry
            # would hide a turn the first send already started and cost the worker a second prompt.
            self.sent_at = now
        self.stage = WorkerContinuationStage.DELIVERY_PENDING
        self.phase = phase

    def confirm_delivery(self) -> None:
        if self.stage not in {
            WorkerContinuationStage.DELIVERY_PENDING,
            WorkerContinuationStage.DELIVERY_CONFIRMED,
        }:
            raise ValueError(f"cannot confirm worker delivery from {self.stage}")
        self.stage = WorkerContinuationStage.DELIVERY_CONFIRMED

    def drop_session(self) -> None:
        """The session is gone, by a confirmed stop or by its own death.

        A retention that has nothing else pending disappears with it. A park and a red transition
        do not. A parked card still owes the observer a decision; a red transition still owes the
        card a replacement, and losing that intent here is what would let a later tick read the
        closed round's report as a new completion.
        """
        self.session_held = False
        if self.stage in {
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
            WorkerContinuationStage.RETAINED,
        }:
            self.clear()

    def clear(self) -> None:
        self.stage = WorkerContinuationStage.NONE
        self.phase = ""
        self.retained_at = 0.0
        self.sent_at = 0.0
        self.report_baseline = 0
        self.move_reason = ""
        self.verdict_outcome = ""
        self.decision = ""
        self.session_held = False

    def to_json(self) -> dict[str, Any]:
        if self.stage == WorkerContinuationStage.NONE:
            return {}
        return {
            "stage": self.stage.value,
            "phase": self.phase,
            "retained_at": self.retained_at,
            "sent_at": self.sent_at,
            "report_baseline": self.report_baseline,
            "move_reason": self.move_reason,
            "verdict_outcome": self.verdict_outcome,
            "decision": self.decision,
            "session_held": self.session_held,
        }

    @classmethod
    def from_json(cls, value: Any) -> "WorkerContinuation":
        if not isinstance(value, dict) or not value:
            return cls()
        stage = WorkerContinuationStage(str(value.get("stage") or "none"))
        return cls(
            stage=stage,
            phase=str(value.get("phase") or ""),
            retained_at=float(value.get("retained_at") or 0.0),
            sent_at=float(value.get("sent_at") or 0.0),
            report_baseline=int(value.get("report_baseline") or 0),
            move_reason=str(value.get("move_reason") or ""),
            verdict_outcome=str(value.get("verdict_outcome") or ""),
            decision=str(value.get("decision") or ""),
            # Records written before the session flag existed only ever reached a stage by
            # retaining one.
            session_held=bool(value.get("session_held", stage != WorkerContinuationStage.NONE)),
        )

    @classmethod
    def from_legacy_record(cls, payload: dict[str, Any]) -> "WorkerContinuation":
        """Read records written by the abandoned flat-field implementation."""
        retained_at = float(payload.get("worker_retained_at") or 0.0)
        delivery = str(payload.get("worker_resume_delivery") or "")
        if delivery == "confirmed":
            stage = WorkerContinuationStage.DELIVERY_CONFIRMED
        elif delivery == "pending":
            stage = WorkerContinuationStage.DELIVERY_PENDING
        elif payload.get("state") == "worker_retained":
            stage = WorkerContinuationStage.VALIDATION_MOVE_PENDING
        elif retained_at:
            stage = WorkerContinuationStage.RETAINED
        else:
            stage = WorkerContinuationStage.NONE
        return cls(
            stage=stage,
            phase=str(payload.get("worker_resume_phase") or ""),
            retained_at=retained_at,
            sent_at=float(payload.get("worker_resume_sent_at") or 0.0),
            session_held=stage != WorkerContinuationStage.NONE,
        )
