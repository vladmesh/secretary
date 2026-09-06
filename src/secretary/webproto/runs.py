"""What a product run *is*, and the durable place Secretary keeps one.

The read layer of secretary-1561 answers what the installation is doing. This is the record behind
the half of that answer the product itself produces: a run is one head this product raised for one
card, and everything about it that outlives the process that raised it — its workspace, its run
directory, its pid file, its log, its result file and the head record the backend handed back.

Three decisions are the whole module:

**The run is owned here, not in the dispatcher's production state.** A product run is not a
pipeline attempt: it takes no claim, moves no card and holds no attempt id, and writing it into
`dispatcher/production-state.json` would make it look like one to every reader of that file — the
watchdog, the reconciler, the orphan sweep. So it lives under its own directory of the same data
plane, and the dispatcher's state is read (by :mod:`secretary.webproto.admission`) and never
written.

**A request id owns one run of one operation, and it owns it from before the head exists.** The record is written
under the store's lock the moment a request id is accepted, with the run id and the workspace path
already decided, and only then is anything spawned. That ordering is what makes a repeat of the
same start command return the same run rather than a second process beside the first: the second
caller finds the record and stops, whether the first caller had finished spawning or not. What the
record owns is the *request* and not the id alone: it carries the operation it was made under and a
fingerprint of the inputs, so a repeat that disagrees with either is refused as a
:class:`RequestMismatch` instead of handing back a document about somebody else's run.

**Nothing here decides what a run's state is.** The record holds evidence — a pid file, a run
directory, a result path — and :mod:`secretary.webproto.run_state` reads that evidence when it is
asked. A stored `state` field would be a second answer to a question the process itself answers,
and it would be stale the moment the head ended.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from secretary._fsutil import file_lock, write_text_atomic

#: Which side of the demonstration scenario a run is. Two, and there is no third: this card covers
#: exactly the scenario it executes.
WORKER = "worker"
REVIEWER = "reviewer"
RUN_ROLES = (WORKER, REVIEWER)

#: Where the whole product runtime keeps its own state inside the installation's data plane.
RUNS_RELATIVE = Path("webproto") / "runs"
WORKSPACES_RELATIVE = Path("webproto") / "workspaces"
HEADS_RELATIVE = Path("webproto") / "heads"

#: The file a head writes its own result into, inside its run directory. The head is told the path
#: in its environment; nothing infers it from the transcript, and there is no second place a result
#: may appear.
RESULT_NAME = "result.json"

#: How long a run may take before the product ends the head that is holding it. A run is a real
#: agent turn, so this is generous; it exists so that a head which never finishes is a run that
#: ends rather than one that is `running` forever.
DEFAULT_DEADLINE_SECONDS = 60.0 * 60.0


#: The two operations a request id may own. A request id is an idempotency key *of one operation*,
#: never a name for "whatever this caller asked for last": see :class:`RequestMismatch`.
START_OPERATION = "run_start"
REVIEW_OPERATION = "run_review"


class RunStoreError(RuntimeError):
    """The store could not be read or written. Never a statement about a run."""


class RequestMismatch(RunStoreError):
    """This request id already owns a different operation, or the same one over a different request.

    Idempotency is a promise about a *retry*: the same operation, made again with the same inputs,
    produces the run the first attempt produced instead of a second one. It is not a promise that
    any later command carrying that id inherits the first one's run -- that would let a review
    request return the worker's own document, with `role == "worker"` and no parent run, while no
    reviewer was ever raised and the caller was told one was. So the record a request id owns
    carries the operation it was made under and a fingerprint of the request itself, and a repeat
    that disagrees with either is refused here rather than silently aliased.
    """


def request_fingerprint(operation: str, request: dict[str, Any]) -> str:
    """The immutable identity of one request: its operation and the inputs it was made with.

    A digest rather than the values themselves, for the same reason the request index is digested:
    nothing a caller supplies becomes a path or a readable field of this installation.
    """
    payload = json.dumps(
        {"operation": operation, "request": {key: _text(value) for key, value in request.items()}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProductRun:
    """One head this product raised for one card, as everything after the raise has to see it.

    Frozen and JSON, like `HeadRun`: the process that started a run is not the process that reads
    it back, and every field here is something a later reader needs and cannot recompute.
    """

    run_id: str
    request_id: str
    ref: str
    project: str
    role: str
    profile: str
    adapter: str = ""
    runtime: str = ""
    #: The worker run a review answers. Empty for a worker run, and the link criterion 1 means by
    #: "by its result": a review exists only for a worker run that has one.
    parent_run_id: str = ""
    workspace: str = ""
    run_dir: str = ""
    pid_file: str = ""
    #: The supervisor's versioned journal for this head: `run.started`, every delivery, and the
    #: `run.exited` that carries the exit status. This is what says how a run ended.
    journal_path: str = ""
    #: The supervisor's own stderr log, for a failure that never reached the journal.
    log_path: str = ""
    result_path: str = ""
    #: The pid the substrate reported for the head at spawn. Diagnostic only: liveness is decided
    #: from the launch-identity heartbeat, never from a number remembered here.
    head_pid: int = 0
    supervisor_pid: int = 0
    started_at: float = 0.0
    deadline_at: float = 0.0
    #: The backend's own record of the head, as `HeadRun.to_json` wrote it. Absent while the
    #: request id has been claimed and the head has not been raised yet.
    head_run: dict[str, Any] = field(default_factory=dict)
    #: Set once, by whoever first observed this run reach a terminal process state.
    settled_at: float = 0.0
    settled_state: str = ""
    settled_reason: str = ""
    #: The evidence that ending was read off, recorded with it. A settled run is history, and
    #: history that is re-derived from files a sweep may since have removed is not history: it is a
    #: run that was `finished` with exit 0 at noon and reads as finished with no exit at midnight.
    #: Keeping the two here is also what makes this run's `product_run.finished` event
    #: deterministic, so republishing it after a journal failure rebuilds the identical record.
    settled_exit: dict[str, Any] = field(default_factory=dict)
    settled_result: dict[str, Any] = field(default_factory=dict)

    @property
    def raised(self) -> bool:
        """Whether a head was ever spawned under this record."""
        return bool(self.head_run)

    @property
    def settled(self) -> bool:
        return bool(self.settled_state)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "ref": self.ref,
            "project": self.project,
            "role": self.role,
            "profile": self.profile,
            "adapter": self.adapter,
            "runtime": self.runtime,
            "parent_run_id": self.parent_run_id,
            "workspace": self.workspace,
            "run_dir": self.run_dir,
            "pid_file": self.pid_file,
            "journal_path": self.journal_path,
            "log_path": self.log_path,
            "result_path": self.result_path,
            "head_pid": self.head_pid,
            "supervisor_pid": self.supervisor_pid,
            "started_at": self.started_at,
            "deadline_at": self.deadline_at,
            "head_run": dict(self.head_run),
            "settled_at": self.settled_at,
            "settled_state": self.settled_state,
            "settled_reason": self.settled_reason,
            "settled_exit": dict(self.settled_exit),
            "settled_result": dict(self.settled_result),
        }

    @classmethod
    def from_json(cls, payload: Any) -> ProductRun:
        if not isinstance(payload, dict):
            raise RunStoreError("a product run record is an object, and this is not one")
        return cls(
            run_id=_text(payload.get("run_id")),
            request_id=_text(payload.get("request_id")),
            ref=_text(payload.get("ref")),
            project=_text(payload.get("project")),
            role=_text(payload.get("role")),
            profile=_text(payload.get("profile")),
            adapter=_text(payload.get("adapter")),
            runtime=_text(payload.get("runtime")),
            parent_run_id=_text(payload.get("parent_run_id")),
            workspace=_text(payload.get("workspace")),
            run_dir=_text(payload.get("run_dir")),
            pid_file=_text(payload.get("pid_file")),
            journal_path=_text(payload.get("journal_path")),
            log_path=_text(payload.get("log_path")),
            result_path=_text(payload.get("result_path")),
            head_pid=_int(payload.get("head_pid")),
            supervisor_pid=_int(payload.get("supervisor_pid")),
            started_at=_float(payload.get("started_at")),
            deadline_at=_float(payload.get("deadline_at")),
            head_run=payload.get("head_run") if isinstance(payload.get("head_run"), dict) else {},
            settled_at=_float(payload.get("settled_at")),
            settled_state=_text(payload.get("settled_state")),
            settled_reason=_text(payload.get("settled_reason")),
            settled_exit=_mapping(payload.get("settled_exit")),
            settled_result=_mapping(payload.get("settled_result")),
        )

    def with_head(self, head_run: dict[str, Any], *, head_pid: int, supervisor_pid: int) -> ProductRun:
        return replace(self, head_run=dict(head_run), head_pid=head_pid, supervisor_pid=supervisor_pid)

    def settled_as(
        self,
        state: str,
        reason: str,
        *,
        now: float,
        exit_status: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> ProductRun:
        """The same run, marked as having reached a terminal state, once and never again.

        Idempotent by construction: a run that already carries a settlement keeps the first one. The
        settlement is what makes the run's one terminal event publishable exactly once, and a second
        writer that could overwrite it would be a second ending for the same run.
        """
        if self.settled:
            return self
        return replace(
            self,
            settled_state=state,
            settled_reason=reason,
            settled_at=now,
            settled_exit=dict(exit_status or {}),
            settled_result=dict(result or {}),
        )


class RunStore:
    """Every product run of one installation, keyed by run id and by the request that asked for it.

    Two directories and one lock. `runs/<run_id>.json` is the record; `runs/requests/<digest>.json`
    names the run a request id owns, digested rather than used verbatim so that a caller's request
    id never becomes a path in this installation (the same rule `TaskAudit` keeps for its pending
    records). The lock is one file for the whole store, held across read-decide-write, because the
    decision this store exists to make — "does this request id already own a run" — is only a
    decision if nobody can answer it twice at once.
    """

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.root = Path(os.fspath(data_dir)) / RUNS_RELATIVE
        self.requests = self.root / "requests"
        self.lock_path = self.root / ".runs.lock"

    # -- paths -----------------------------------------------------------------------------

    def _run_path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or run_id in (".", ".."):
            raise RunStoreError(f"a product run is named by a run id, not by {run_id!r}")
        return self.root / f"{run_id}.json"

    def _request_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.requests / f"{digest}.json"

    def _prepare(self) -> None:
        try:
            self.requests.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunStoreError(f"the product run store at {self.root} could not be opened: {exc}") from None

    # -- reads -----------------------------------------------------------------------------

    def get(self, run_id: str) -> ProductRun | None:
        """One run, or nothing. An unreadable record is a store failure, never a missing run."""
        path = self._run_path(run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the product run record at {path} could not be read: {exc}") from None
        return ProductRun.from_json(payload)

    def by_request(
        self, request_id: str, *, operation: str = "", fingerprint: str = ""
    ) -> ProductRun | None:
        """The run a request id already owns, if it owns one *and this is the same request*.

        `operation` and `fingerprint` are what make the answer a retry rather than an alias: a
        caller that names them gets the run back only when the record agrees with both, and a
        :class:`RequestMismatch` otherwise. Naming neither reads the record as it stands, which is
        what a reader that is not making a request -- a listing, a repair -- wants.
        """
        path = self._request_path(request_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"the product run request index at {path} could not be read: {exc}") from None
        if not isinstance(payload, dict):
            raise RunStoreError(f"the product run request index at {path} is not an object")
        owned_operation = _text(payload.get("operation"))
        owned_fingerprint = _text(payload.get("fingerprint"))
        if operation and owned_operation and owned_operation != operation:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a {owned_operation} run; a request id is "
                f"the idempotency key of one operation and cannot be reused for {operation}"
            )
        if fingerprint and owned_fingerprint and owned_fingerprint != fingerprint:
            raise RequestMismatch(
                f"request id {request_id!r} already owns a {owned_operation or operation} run made "
                "with different inputs; a repeat is a retry of the same request, not a new one"
            )
        run_id = _text(payload.get("run_id"))
        return self.get(run_id) if run_id else None

    def for_ref(self, ref: str) -> list[ProductRun]:
        """Every run of one card, oldest first. What a card page shows, and what admission reads."""
        runs: list[ProductRun] = []
        try:
            names = sorted(path for path in self.root.glob("*.json"))
        except OSError as exc:
            raise RunStoreError(f"the product run store at {self.root} could not be listed: {exc}") from None
        for path in names:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                raise RunStoreError(f"the product run record at {path} could not be read: {exc}") from None
            run = ProductRun.from_json(payload)
            if run.ref == ref:
                runs.append(run)
        return sorted(runs, key=lambda run: (run.started_at, run.run_id))

    # -- writes ----------------------------------------------------------------------------

    def claim(
        self,
        request_id: str,
        build: Any,
        *,
        operation: str = "",
        fingerprint: str = "",
    ) -> tuple[ProductRun, bool]:
        """The run this request id owns, creating it under the lock when it owns none yet.

        `build` is called with a fresh run id only when this request id is new, and it returns the
        record to write. Everything a later reader needs to find the run's debris — its workspace,
        its run directory, its result path — is therefore durable *before* any process exists, so a
        caller that dies between this call and the spawn leaves a run that can be found rather than
        an orphan nobody recorded.

        The boolean says whether this call created the run. `False` is the whole of the idempotency
        contract: the same request id twice gets the same run, and the second caller spawns nothing.
        """
        self._prepare()
        with file_lock(self.lock_path):
            existing = self.by_request(request_id, operation=operation, fingerprint=fingerprint)
            if existing is not None:
                return existing, False
            run = build(new_run_id())
            if not isinstance(run, ProductRun):
                raise RunStoreError("a product run record is built as a ProductRun")
            self._write(run)
            write_text_atomic(
                self._request_path(request_id),
                json.dumps(
                    {
                        "request_id": request_id,
                        "run_id": run.run_id,
                        "operation": operation,
                        "fingerprint": fingerprint,
                    },
                    sort_keys=True,
                ),
            )
            return run, True

    def save(self, run: ProductRun) -> ProductRun:
        """Replace one run's record. Taken under the same lock every other write is."""
        self._prepare()
        with file_lock(self.lock_path):
            self._write(run)
        return run

    def settle(
        self,
        run_id: str,
        state: str,
        reason: str,
        *,
        now: float,
        exit_status: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> tuple[ProductRun, bool]:
        """Record the terminal state of a run, exactly once, and say whether this call did it.

        The one place a run stops being open, and the reason the terminal event can be published
        without a second history: whoever gets `True` back is the caller that owes that event, and
        every later observer of the same ending gets `False` and publishes nothing.
        """
        self._prepare()
        with file_lock(self.lock_path):
            current = self.get(run_id)
            if current is None:
                raise RunStoreError(f"there is no product run {run_id!r} to settle")
            if current.settled:
                return current, False
            settled = current.settled_as(
                state, reason, now=now, exit_status=exit_status, result=result
            )
            self._write(settled)
            return settled, True

    def _write(self, run: ProductRun) -> None:
        write_text_atomic(self._run_path(run.run_id), json.dumps(run.to_json(), sort_keys=True, indent=2))


def new_run_id() -> str:
    """A product run's identity. Prefixed so a run directory is never mistaken for a pipeline one."""
    return "pr-" + uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
