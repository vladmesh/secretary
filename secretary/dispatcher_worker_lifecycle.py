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

    @property
    def retained(self) -> bool:
        return self.stage != WorkerContinuationStage.NONE

    @property
    def awaiting_continuation(self) -> bool:
        return self.stage == WorkerContinuationStage.RETAINED

    @property
    def validation_move_pending(self) -> bool:
        return self.stage == WorkerContinuationStage.VALIDATION_MOVE_PENDING

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

    def confirm_validation_move(self) -> None:
        if self.stage == WorkerContinuationStage.RETAINED:
            return
        if self.stage != WorkerContinuationStage.VALIDATION_MOVE_PENDING:
            raise ValueError(f"cannot confirm validation move from {self.stage}")
        self.stage = WorkerContinuationStage.RETAINED

    def begin_red_transition(self, phase: str, report_baseline: int) -> None:
        """Record the red verdict before the board is moved.

        The board move and the state write are separate durable facts. Without this the record of a
        crashed tick still names the report that closed the previous round, and recovery reads that
        report as a new completion instead of finishing the red transition.
        """
        if self.stage not in {
            WorkerContinuationStage.RETAINED,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
        }:
            raise ValueError(f"cannot open a red transition from {self.stage}")
        self.stage = WorkerContinuationStage.RED_TRANSITION_PENDING
        self.phase = phase
        self.report_baseline = int(report_baseline)

    def begin_delivery(self, phase: str, now: float) -> None:
        if self.stage not in {
            WorkerContinuationStage.RETAINED,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
        }:
            raise ValueError(f"cannot resume worker from {self.stage}")
        self.stage = WorkerContinuationStage.DELIVERY_PENDING
        self.phase = phase
        self.sent_at = now

    def confirm_delivery(self) -> None:
        if self.stage not in {
            WorkerContinuationStage.DELIVERY_PENDING,
            WorkerContinuationStage.DELIVERY_CONFIRMED,
        }:
            raise ValueError(f"cannot confirm worker delivery from {self.stage}")
        self.stage = WorkerContinuationStage.DELIVERY_CONFIRMED

    def clear(self) -> None:
        self.stage = WorkerContinuationStage.NONE
        self.phase = ""
        self.retained_at = 0.0
        self.sent_at = 0.0
        self.report_baseline = 0

    def to_json(self) -> dict[str, Any]:
        if self.stage == WorkerContinuationStage.NONE:
            return {}
        return {
            "stage": self.stage.value,
            "phase": self.phase,
            "retained_at": self.retained_at,
            "sent_at": self.sent_at,
            "report_baseline": self.report_baseline,
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
        )
