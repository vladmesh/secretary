"""What a request id owns when the thing it asked for is a sprint.

`run_start` made a request id own a run: the record is written before anything is provisioned, so a
repeat finds it and returns the same run instead of raising a second head. A sprint create needs the
same property for the same reason -- a retried command, a client that reconnected -- and it is the
same mechanism here, deliberately, rather than a second idea about idempotency.

What is different is what the record points at. A run is this layer's own object, so `RunStore`
holds the whole of it. A sprint is not: the sprint entity on the board is the one source of truth
about goals, issues, reservations and observers, and `SprintWriter.create` owns every rule about
it. So the record here holds exactly two things this layer cannot get anywhere else -- the request
this id was claimed under, and the reference of the sprint that request produced -- and nothing
about the sprint itself. Reading a sprint means reading the sprint.

That leaves the partial failure honest without a second store of truth. Between the claim and the
reference there is a window in which a sprint may exist while nothing here names it, and the
repair for it is not this record: the same request id is handed down to `SprintWriter.create`,
whose own staged transaction resumes the row it already began. This record's `reference` is
therefore a *shortcut* -- a repeat that finds it answers without touching the writer at all -- and
never the only thing standing between a repeat and a second sprint.

The failure vocabulary is the run store's on purpose. `RunStoreError` and `RequestMismatch` are
already what this layer's durable stores speak, already in
:data:`secretary.webproto.boundary.IMPLEMENTATION_FAILURES`, and already translated by the
boundary; a private exception type here would mean a second entry in that tuple and a second thing
for the next operation to remember.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from secretary._fsutil import file_lock, write_text_atomic
from secretary.webproto.runs import RequestMismatch, RunStoreError

#: The one operation a record here can be claimed under today. It is stored rather than assumed for
#: the same reason `RunStore` stores it: a request id is the idempotency key of *one* operation, and
#: an id reused for another one has to be refused rather than answered with this operation's sprint.
SPRINT_CREATE_OPERATION = "sprint_create"

#: Where this layer keeps the request index, inside the installation's own data plane and beside
#: the product runs'.
SPRINT_REQUESTS_RELATIVE = Path("webproto") / "sprint-requests"


@dataclass(frozen=True, slots=True)
class SprintRequest:
    """One claimed request, and the sprint it produced once it produced one."""

    request_id: str
    operation: str
    fingerprint: str
    #: The sprint this request created, recorded after the create returned it. Empty means the
    #: request is claimed and its outcome is not established here -- never that it created nothing.
    reference: str = ""
    claimed_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "operation": self.operation,
            "fingerprint": self.fingerprint,
            "reference": self.reference,
            "claimed_at": self.claimed_at,
        }

    @classmethod
    def from_json(cls, payload: Any) -> SprintRequest:
        if not isinstance(payload, dict):
            raise RunStoreError("a sprint request record is an object, and this is not one")
        return cls(
            request_id=_text(payload.get("request_id")),
            operation=_text(payload.get("operation")),
            fingerprint=_text(payload.get("fingerprint")),
            reference=_text(payload.get("reference")),
            claimed_at=_float(payload.get("claimed_at")),
        )


class SprintRequestStore:
    """Every sprint request of one installation, keyed by the request id that made it.

    One directory and one lock, exactly as `RunStore` has: the id is digested rather than used
    verbatim so a caller's request id never becomes a path here, and the lock is held across
    read-decide-write because "does this request id already own a sprint" is only a decision if
    nobody can answer it twice at once.
    """

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.root = Path(os.fspath(data_dir)) / SPRINT_REQUESTS_RELATIVE
        self.lock_path = self.root / ".sprint-requests.lock"

    # -- paths -----------------------------------------------------------------------------

    def _path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def _prepare(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunStoreError(f"the sprint request store at {self.root} could not be opened: {exc}") from None

    # -- reads -----------------------------------------------------------------------------

    def by_request(
        self, request_id: str, *, operation: str = "", fingerprint: str = ""
    ) -> SprintRequest | None:
        """The request this id already owns, if it owns one *and this is the same request*.

        Naming the operation and the fingerprint is what makes the answer a retry rather than an
        alias: a repeat that disagrees with either is a :class:`RequestMismatch`, never a document
        about a sprint somebody else asked for.
        """
        path = self._path(request_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the sprint request index at {path} could not be read: {exc}") from None
        record = SprintRequest.from_json(payload)
        if operation and record.operation and record.operation != operation:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a {record.operation} request; a request id "
                f"is the idempotency key of one operation and cannot be reused for {operation}"
            )
        if fingerprint and record.fingerprint and record.fingerprint != fingerprint:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a "
                f"{record.operation or operation} request made with different inputs; a repeat is a "
                "retry of the same request, not a new one"
            )
        return record

    # -- writes ----------------------------------------------------------------------------

    def claim(
        self, request_id: str, *, operation: str, fingerprint: str, now: float = 0.0
    ) -> tuple[SprintRequest, bool]:
        """The request this id owns, claiming it under the lock when it owns none yet.

        The boolean says whether this call claimed it. `False` is the idempotency contract: the
        second caller of the same request finds the first one's record, whether or not the first
        one ever got as far as a sprint.
        """
        self._prepare()
        with file_lock(self.lock_path):
            existing = self.by_request(request_id, operation=operation, fingerprint=fingerprint)
            if existing is not None:
                return existing, False
            record = SprintRequest(
                request_id=request_id,
                operation=operation,
                fingerprint=fingerprint,
                claimed_at=now,
            )
            self._write(record)
            return record, True

    def record_reference(self, request_id: str, reference: str) -> SprintRequest:
        """Name the sprint this request produced, once. A recorded reference is never replaced.

        Never replaced because a request owns one outcome: a second reference under the same id
        could only come from a create that made a second sprint, and this store would be the last
        place able to notice it.
        """
        self._prepare()
        with file_lock(self.lock_path):
            record = self.by_request(request_id)
            if record is None:
                raise RunStoreError(f"there is no claimed sprint request {request_id!r} to complete")
            if record.reference:
                return record
            completed = replace(record, reference=reference)
            self._write(completed)
            return completed

    def _write(self, record: SprintRequest) -> None:
        write_text_atomic(self._path(record.request_id), json.dumps(record.to_json(), sort_keys=True, indent=2))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
