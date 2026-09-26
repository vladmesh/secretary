"""The owner's two writes on one card, transport-independent like every other operation here.

A dashboard that can watch a card park in Assessment and cannot say anything about it is a
dashboard whose operator opens a terminal. The two things the owner does to a card from outside a
head are both writes `secretary task` already makes, under the same writer and the same rules:

* :meth:`CardOperationLayer.task_comment` -- a comment on the card, which is how the owner talks to
  the head working it;
* :meth:`CardOperationLayer.task_move` -- a transition, which is how the owner intervenes: a card
  parked in Assessment is released, sent back or dropped by *moving* it, with a reason, and a card of
  an open sprint is moved past the sprint's reservation only with `sprint_override` and a reason for
  that as well.

Neither is `task decide`. A decision is the observer's and nobody else's (`TaskWriter.decide`
refuses every other role), and this layer does not impersonate an observer to record one: the
owner's intervention is a move, and the audit says so.

Every rule about what a comment or a move *is* stays in `TaskWriter`: which roles may write, which
transitions are permitted from which state, what a sprint reservation refuses, how a request id is
replayed. This layer names the request, hands it down, and translates the writer's vocabulary into
this package's typed codes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.board.backend import card_client
from secretary.board.owner_handover import OWNER_ROLE, waiting_owner
from secretary.config import InstanceReport, validate_instance
from secretary.tasks import TaskError, TaskWriter
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import (
    InstallationUnavailable,
    OwnerConflict,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)

SCHEMA_VERSION = 1

#: The states a move from outside a head may name, in the spelling `TaskWriter.move` takes. Not a
#: rule of this layer -- the writer decides which of them a given card may go to -- but the whole
#: vocabulary, so a client can offer the choice and a misspelling is refused before the board.
MOVE_TARGETS = ("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done")

#: The writer's own codes, as this layer's typed failures. `transition_forbidden` is a refusal on
#: the state of the card and not on the shape of the request, which is what `owner_conflict` means.
_CODES: dict[str, Any] = {
    "validation": ValidationRefused,
    "role_forbidden": ValidationRefused,
    "not_found": TaskNotFound,
    "transition_forbidden": OwnerConflict,
    "sprint_conflict": OwnerConflict,
    "resource_conflict": OwnerConflict,
    "owner_conflict": OwnerConflict,
}


class CardOperationLayer(ProtocolBoundary):
    """One installation's owner-side card writes, with no knowledge of who is asking.

    Construction does no I/O: the instance, the board and the writer are resolved when an
    operation is called. `board_client` and `clock` are the seams a test supplies directly.
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

    def task_comment(
        self, *, request_id: str, actor: str, reference: str, body: str, role: str = "po"
    ) -> dict[str, Any]:
        """Put one comment on a card, under the request id the caller names.

        A repeat of the same request id is the writer's to answer, exactly as a CLI retry is: it
        hands back the event the id already owns and writes nothing.
        """
        now = self._clock()
        _named(request_id, "a card operation names the request it is made under")
        _named(reference, "a card comment names the card it is put on")
        _named(body, "a card comment carries a non-empty body")
        written = self._write(
            lambda writer: writer.comment(
                role=_comment_role(writer, role, reference, request_id),
                actor=actor,
                reference=reference,
                body=body,
                request_id=request_id,
            )
        )
        return self._document("card_commented", written, request_id=request_id, reference=reference, now=now)

    def task_move(
        self,
        *,
        request_id: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        sprint_override: bool = False,
        sprint_override_reason: str = "",
        role: str = "po",
    ) -> dict[str, Any]:
        """Move one card to a named state, with the reason the audit will carry.

        `sprint_override` is the owner stepping past an open sprint's reservation of the card, and
        the writer requires a reason for that step separately from the reason for the move. The
        target is checked against :data:`MOVE_TARGETS` here only so a misspelling is a `validation`
        refusal naming the choices; whether *this* card may go there is the writer's judgement, and
        its refusal comes back as `owner_conflict`.
        """
        now = self._clock()
        _named(request_id, "a card operation names the request it is made under")
        _named(reference, "a card move names the card it moves")
        _named(reason, "a card move states why the card is moved")
        if target not in MOVE_TARGETS:
            raise ValidationRefused(
                f"a card move names one of {', '.join(MOVE_TARGETS)} as its target; {target!r} is not one"
            )
        if sprint_override and not str(sprint_override_reason or "").strip():
            raise ValidationRefused(
                "moving a card past its sprint's reservation states why, separately from the move's reason"
            )
        written = self._write(
            lambda writer: writer.move(
                role=role,
                actor=actor,
                reference=reference,
                target=target,
                reason=reason,
                sprint_override=bool(sprint_override),
                sprint_override_reason=str(sprint_override_reason or ""),
                request_id=request_id,
            )
        )
        return self._document("card_moved", written, request_id=request_id, reference=reference, now=now)

    # -- plumbing ---------------------------------------------------------------------------

    def _write(self, operation: Callable[[TaskWriter], Any]) -> dict[str, Any]:
        report = self.report()
        data_dir = self.data_dir(report)
        try:
            writer = TaskWriter(self._client(), data_dir=data_dir)
            written = operation(writer)
        except TaskError as exc:
            raise _CODES.get(exc.code, RuntimeUnavailable)(exc.message) from None
        return written if isinstance(written, dict) else {}

    def _client(self) -> Any:
        return self._board_client or card_client(
            self.instance.parent if self.instance.is_file() else self.instance
        )

    def _document(
        self, kind: str, written: dict[str, Any], *, request_id: str, reference: str, now: float
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "observed_at": sources.isoformat(now),
            "request_id": request_id,
            "ref": reference,
            "event_id": str(written.get("event_id") or "") or None,
            # What the writer answered, as it answered it: the CLI prints this same object.
            "result": written,
        }


def _comment_role(writer: TaskWriter, role: str, reference: str, request_id: str) -> str:
    """The role the dashboard's comment is written as: the owner's on a card that waits for the owner.

    The web front is behind the owner's password, so a comment on a card carrying `waiting_owner` is
    the owner's answer (`task comment --role owner`), which the dispatcher forwards to the PO; as
    `po` it would never be (secretary-1770). A repeat of a request id keeps the role its first
    comment was written as, so a card completed in between does not turn the replay into a conflict.
    """
    if role != "po":
        return role
    recorded = writer.audit.committed_event(request_id) or writer.audit.pending_event(request_id)
    payload = recorded.get("payload") if isinstance(recorded, dict) else None
    if isinstance(payload, dict) and payload.get("marker") in {OWNER_ROLE, "po"}:
        return str(payload["marker"])
    try:
        card = writer.reader.show(reference)
    except TaskError:
        return role
    return OWNER_ROLE if waiting_owner(card) is not None else role


def _named(value: Any, message: str) -> None:
    if not str(value or "").strip():
        raise ValidationRefused(message)
