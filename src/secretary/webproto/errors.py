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


class ReadError(Exception):
    """A read the layer refused, with the protocol code its callers already know."""

    code = "read_error"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


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
