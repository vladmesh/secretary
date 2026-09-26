"""Typed failures of the read layer.

The layer answers callers that are not HTTP: a Telegram head, a CLI command, a future web
transport. So a failure is an exception with a stable ``code``, never a status number, and the
transport is what maps a code onto whatever its protocol says "not found" in. The codes are the
ones the task protocol already uses (``not_found``, ``validation``, ``backend_unavailable``), so a
caller that already reads `secretary task` errors reads these without a second table.

Not every failure is an exception. A source that cannot answer *part* of a snapshot -- the board,
the dispatcher's own state, the installation health collector -- is a field of the result instead
(:mod:`secretary.webproto.sources`): a dashboard must still render the parts that did answer, and
raising would turn one dead source into a blank page.
"""

from __future__ import annotations

from typing import Any


class ReadError(Exception):
    """A read the layer refused, with the protocol code its callers already know.

    A refusal may also carry ``data``: the machine-readable half of it, for the cases where a
    caller has to *do* something and the sentence would otherwise be the only place saying what.
    It is a plain JSON object with a `reason` token and, where one exists, the safe action the
    caller may take, so a transport hands the caller a decision rather than a string to display.
    A refusal with nothing to add carries no `data` key at all.
    """

    code = "read_error"

    def __init__(self, message: str, *, data: dict[str, Any] | None = None) -> None:
        self.message = message
        self.data = dict(data or {})
        super().__init__(message)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            payload["data"] = self.data
        return payload


class TaskNotFound(ReadError):
    """The board answered and holds no card under this reference."""

    code = "not_found"


class InvalidCursor(ReadError):
    """A cursor this layer did not issue, or one the journal can no longer honour.

    Deliberately not "start from the beginning": a client that resumes from a cursor the journal
    cannot place would silently re-read or skip events, which is the one thing the cursor exists to
    prevent. It is told, and it decides.
    """

    code = "validation"


class InstallationUnavailable(ReadError):
    """The instance config itself does not validate, so nothing below it can be read."""

    code = "backend_unavailable"


# -- the operation half (secretary-1562) --------------------------------------------------------
#
# The operations return the same kind of failure the reads do, and deliberately from the same base:
# a transport that already maps `ReadError.code` onto its own vocabulary maps these with no second
# table, and a caller that catches `ReadError` around a whole request catches both halves. What is
# added is the two refusals only a mutation can make.


class ValidationRefused(ReadError):
    """The operation was asked for something it cannot do: a missing input, an unusable profile."""

    code = "validation"


class RunNotFound(ReadError):
    """No product run of this installation is named by this identifier."""

    code = "not_found"


class HeadRunNotFound(ReadError):
    """The card recorded no head run under this identifier: not its own, or no run at all."""

    code = "not_found"


class OwnerConflict(ReadError):
    """Somebody else owns this card, or this run, and a second owner is not created.

    Its own code rather than `validation` because it is not a malformed request: the request was
    well formed and is refused on the state of the world, and a caller that retries it after the
    other owner has finished will be admitted. See :mod:`secretary.webproto.admission` for the one
    place this is decided and the order it decides in.
    """

    code = "owner_conflict"


class RuntimeUnavailable(ReadError):
    """The product runtime could not raise, reach or record a head, and says which.

    Deliberately not a run outcome: a run that exists and whose head died has a state
    (`process_failed`), and only a failure that leaves *no* run behind is reported as an error.
    """

    code = "backend_unavailable"


# -- the sprint half (secretary-1569) -----------------------------------------------------------


class OperationPending(ReadError):
    """An operation that is part-done, durably repairable, and repeated with the same request id.

    A sprint create writes a board row, its fields and its reference in that order, and the writer
    below this layer keeps the staged intent that resumes an attempt which stopped between them. So
    "it did not finish" is not "it did not happen", and the two must not answer the same way: the
    safe move is to repeat *this* request, which resumes the row that exists, and inventing a new
    request id would open a second sprint beside the half-written one.

    It carries no code of its own. The transport table maps `backend_unavailable` already, and the
    thing that could not answer really is a durable source of this installation; what is added is
    :attr:`~ReadError.data`, which says the reason and the action in a shape a client can act on
    rather than parse out of a sentence.
    """

    code = "backend_unavailable"


class IdentityRefused(ReadError):
    """A write refused on who is asking, carried under the writer's own code rather than folded.

    Not `validation`: the request is well formed, and the answer is about the caller. A caller that
    reads `role_masquerade` is told to write as the observer; one that reads
    `observer_identity_unbound` or `observer_sprint_mismatch` is a head writing outside the sprint it
    was launched for. Folding either into `validation` would leave a sentence the only place saying so.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: The writer codes :class:`IdentityRefused` carries unchanged.
IDENTITY_REFUSAL_CODES = ("role_masquerade", "observer_identity_unbound", "observer_sprint_mismatch")


# -- the PO head half (secretary-1631) ----------------------------------------------------------


class PoSessionNotFound(ReadError):
    """The board store answered and holds no PO session under this id."""

    code = "not_found"


class PoTurnInProgress(ReadError):
    """A turn is already running in this PO session; nothing was written."""

    code = "owner_conflict"


class PoRequestConflict(ReadError):
    """A /po request id already belongs to another operation or other inputs; nothing was written.

    Its own code: the request is well formed and not about a running turn, and repeating it can never
    succeed, so a client must not read it as `owner_conflict` and wait.
    """

    code = "request_conflict"


#: The `data` of a /po refusal known to have written nothing, set where the refusal is raised. A web
#: form that took a request id gets a fresh one only after such a refusal; every other refusal of it
#: keeps its id, so a resend is a replay (`secretary.web.app._keeps_request_id`).
NOTHING_WRITTEN: dict[str, Any] = {"nothing_written": True}


class PoOutcomeUnknown(ReadError):
    """A /po write reached the PO service and no answer came back, so it may have been carried out.

    Not "nothing was written": the request was delivered. The safe move is the same request again —
    the same form with the same request id, which the service answers as a replay, or the same stop or
    close, which are idempotent — and never a new request id. `data` says so for a client:
    ``{"reason": "outcome_unknown", "action": "repeat_same_request"}``.
    """

    code = "backend_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(message, data={"reason": "outcome_unknown", "action": "repeat_same_request"})


class PoSessionClosed(ReadError):
    """The owner closed this PO session; a message into it starts no turn and nothing was written.

    Its own code: unlike `owner_conflict` it never clears by waiting, since a closed session is not reopened.
    """

    code = "session_closed"
