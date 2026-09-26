"""The PO service: the one owner of PO head turns on an installation (`secretary po-serve`).

`secretary-po.service` runs it. It holds the installation's only :class:`~secretary.po.runner.PoRunner`,
so every turn process is a child in this unit's control group: a restart of the web touches none of
them. Submitters (the web now, the dispatcher later) reach it only through its local socket
(`secretary.po.client`); none of them imports the runner.

**Inputs.** A message is written to the durable queue (`secretary.po.queue`, ``<data_dir>/po-queue/``)
before the submitter is answered. The service takes inputs FIFO per session and runs at most one turn per
session: an input for a session with a running turn waits in the queue, neither refused nor lost.
Sessions run in parallel. An input leaves the queue only after its turn row exists
(`PoStore.claim_turn` under the input's request id), so a crash between the claim and the removal is
repaired by the next hand-over answering the same turn and creating nothing.

**Start.** `PoRunner.recover(rerun=True)`: a turn left `running` whose process is gone is re-run once
over the same CLI conversation with the same prompt, recorded on the row; a re-run found `running`
again is settled `interrupted`; a still-living process with the recorded identity is killed first.
Queued inputs are taken after that.

**Restart for new process inputs.** An upgrade never kills a running turn: the PO itself runs
`secretary upgrade` inside a turn. The upgrade writes the restart marker and asks
(`secretary.po.client.request_restart`); :meth:`PoService.request_restart` is the rule. Idle, the
service exits at once and `Restart=always` starts the new code. Busy, it starts no new turn (inputs
keep queueing) and exits as soon as its last running turn settles.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import fcntl
import inspect
import json
import os
import socketserver
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from secretary.po.client import (
    LOCK_NAME,
    MAX_MESSAGE_BYTES,
    restart_marker_path,
    service_dir,
    socket_path,
)
from secretary.po.queue import SOURCES, PoQueue, QueuedInput, QueueError
from secretary.po.runner import PoRunner, RunnerError
from secretary.po.store import (
    CLIS,
    DEFAULT_EFFORT,
    SEND,
    SESSION_CLOSED,
    SESSION_CREATE,
    PoRequest,
    PoStoreError,
    RequestConflict,
    SessionClosed,
    SessionNotFound,
    TurnInProgress,
    send_fingerprint,
    session_fingerprint,
)

# How often the service looks at its queue, its restart marker and a recovery that did not run yet,
# besides being woken by a submit or a settled turn.
TICK_SECONDS = 1.0
# The longest wait between two recovery passes while a `running` row has no process here.
RECOVERY_MAX_DELAY_SECONDS = 30.0
# The longest `sun_path` Linux takes, with its terminating NUL.
MAX_SOCKET_PATH_BYTES = 107
STOP_ACTOR = "owner"


class ServiceStartError(RuntimeError):
    """The PO service cannot serve here: another one holds the lock, or the socket cannot be bound."""


class Refused(Exception):
    """A request refused before it reached the store; `code` is the endpoint's."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PoService:
    """Turns, the queue and the endpoint's operations of one installation. No socket here: see `listening`."""

    def __init__(
        self, runner: PoRunner, queue: PoQueue | None = None, *, data_dir: Path | str | None = None
    ) -> None:
        self.runner = runner
        self.store = runner.store
        self.data_dir = Path(data_dir) if data_dir is not None else runner.data_dir
        self.queue = queue or PoQueue(self.data_dir)
        self.marker = restart_marker_path(self.data_dir)
        runner.on_settled = self._settled
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._exit = threading.Event()
        # True only once every `running` row is live under this service or settled.
        self._recovered = False
        self._recovery_delay = 0.0
        self._next_recovery = 0.0

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> list[str]:
        """Settle what a previous run left, drop a restart this process fulfils, take the queue; journal lines."""
        lines = []
        try:
            if self.marker.exists():
                self.marker.unlink()
                lines.append("secretary po: a pending restart request is fulfilled by this start")
        except OSError as exc:
            lines.append(f"secretary po: could not clear the restart marker {self.marker}: {exc}")
        try:
            self.queue.ensure()
        except QueueError as exc:
            lines.append(f"secretary po: {exc}")
        lines.extend(self._recover())
        self.pump()
        return lines

    def _recover(self, *, tick: float = TICK_SECONDS) -> list[str]:
        """One recovery pass; complete only when no `running` row is left without a waiter here.

        An incomplete pass (a store that did not answer, a row that could not be prepared yet) is
        retried with a doubling delay from `tick` up to `RECOVERY_MAX_DELAY_SECONDS`. Meanwhile the
        sessions of the rows still `running` take no queued input — `pump` skips every session with a
        running row — and every other session goes on.
        """
        lines: list[str] = []
        with self._lock:
            try:
                recovered = self.runner.recover(rerun=True)
                orphaned = self.runner.orphaned_turns()
            except Exception as exc:  # noqa: BLE001 - retried with backoff until the store answers
                orphaned = None
                lines.append(f"secretary po: turn recovery did not complete: {type(exc).__name__}: {exc}")
            else:
                lines.extend(
                    f"secretary po: turn {turn.session_id}/{turn.seq} {turn.state}: {turn.reason}"
                    for turn in recovered
                )
            self._recovered = orphaned == []
            if self._recovered:
                self._recovery_delay = 0.0
            else:
                self._recovery_delay = min(max(self._recovery_delay * 2, tick), RECOVERY_MAX_DELAY_SECONDS)
                self._next_recovery = time.monotonic() + self._recovery_delay
                for turn in orphaned or []:
                    lines.append(
                        f"secretary po: turn {turn.session_id}/{turn.seq} still running without a process; "
                        f"recovery retries in {self._recovery_delay:g}s"
                    )
        return lines

    def run(self, *, tick: float = TICK_SECONDS, say: Callable[[str], None] | None = None) -> int:
        """Serve until a restart is due at idle (exit 0, `Restart=always` starts the new code)."""
        say = say or _say
        while not self._exit.is_set():
            self._wake.wait(tick)
            self._wake.clear()
            if not self._recovered and time.monotonic() >= self._next_recovery:
                for line in self._recover(tick=tick):
                    say(line)
            self.pump()
            if self.restart_due():
                say("secretary po: exiting for a pending restart; no turn is running")
                self._exit.set()
        return 0

    def stop(self) -> None:
        self._exit.set()
        self._wake.set()

    @property
    def exiting(self) -> bool:
        return self._exit.is_set()

    def _settled(self, _session_id: str, _seq: int) -> None:
        self._wake.set()

    # --- the queue --------------------------------------------------------------------------

    def pump(self) -> None:
        """Hand the oldest input of every idle session to the runner; hold everything while a restart is pending."""
        with self._lock:
            # Not gated on `_recovered`: a row recovery has not settled is `running`, so its session
            # is busy below and takes nothing until it is.
            if self._exit.is_set() or self.marker.exists():
                return
            try:
                heads = self.queue.heads()
                if not heads:
                    return
                busy = {turn.session_id for turn in self.store.running_turns()}
            except (QueueError, PoStoreError) as exc:
                _say(f"secretary po: the queue waits: {exc}")
                return
            for item in heads:
                if item.session_id not in busy:
                    self._hand_over(item)

    def _hand_over(self, item: QueuedInput) -> None:
        """One input becomes its turn, then leaves the queue; the claim's request id makes a repeat harmless."""
        try:
            self.runner.send_request(item.session_id, item.text, item.request_id)
        except TurnInProgress:
            return
        except (SessionNotFound, SessionClosed, RequestConflict) as exc:
            self._refuse(item, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - the claim may have committed before the launch failed
            try:
                known = self.store.request(item.request_id)
            except PoStoreError:
                _say(f"secretary po: {item.name} waits: {type(exc).__name__}: {exc}")
                return
            if known is None:
                if isinstance(exc, RunnerError):
                    self._refuse(item, str(exc))
                else:
                    _say(f"secretary po: {item.name} waits: {type(exc).__name__}: {exc}")
                return
            # Claimed: the turn exists (settled `failed` by the runner if its CLI never started).
        try:
            self.queue.remove(item)
        except QueueError as exc:
            _say(f"secretary po: {exc}; the next hand-over answers the same turn")

    def _refuse(self, item: QueuedInput, reason: str) -> None:
        _say(f"secretary po: {item.name} for session {item.session_id} set aside: {reason}")
        try:
            self.queue.refuse(item, reason)
        except QueueError as exc:
            _say(f"secretary po: {exc}")

    # --- the endpoint's operations ----------------------------------------------------------

    def create_session(
        self, *, cli: str, model: str, effort: str = DEFAULT_EFFORT, request_id: str
    ) -> dict[str, Any]:
        """One session per request id, reserved through :meth:`_reserve` like a message.

        Accepted at the `claim_session` commit, or earlier when `_reserve` finds the id already made
        this session; from then on the answer is the session (`handle`'s acceptance rule).
        """
        request_id = _required(request_id, "request_id")
        model = str(model or "").strip()
        effort = str(effort or "").strip() or DEFAULT_EFFORT
        if cli not in CLIS:
            raise Refused("validation", f"a PO session runs {' or '.join(CLIS)}, not {cli!r}")
        if not model:
            raise Refused("validation", "a PO session needs a model")
        fingerprint = session_fingerprint(cli, model, effort)
        with self._lock:
            known = self._reserve(request_id, SESSION_CREATE, fingerprint)
            if isinstance(known, PoRequest):
                self._accepted({"session_id": known.session_id, "effort": effort, "repeated": True})
                session = self.store.session(known.session_id)
                return {"session_id": session.session_id, "effort": session.effort, "repeated": True}
            self._accepting()
            session, created = self.runner.create_session_request(cli, model, request_id, effort)
            answer = {"session_id": session.session_id, "effort": session.effort, "repeated": not created}
            self._accepted(answer)
        return answer

    def submit(self, *, session_id: str, text: str, request_id: str, source: str = "web") -> dict[str, Any]:
        """Queue one message durably, then answer; a request id answers what it already made.

        The same id with the same session and text is a replay: the turn it became, or the input still
        queued. The id bound to anything else is `RequestConflict` (:meth:`_reserve`). A message into an
        unknown or closed session is refused with nothing queued. Otherwise it is queued and handed over
        at once when its session is idle, and the answer says which.
        """
        request_id = _required(request_id, "request_id")
        session_id = _required(session_id, "session_id")
        if not str(text or "").strip():
            raise Refused("validation", "an empty message starts no turn")
        if source not in SOURCES:
            raise Refused("validation", f"a PO input comes from {' or '.join(SOURCES)}, not {source!r}")
        with self._lock:
            known = self._reserve(
                request_id, SEND, send_fingerprint(session_id, text), session_id=session_id, text=text
            )
            if known is not None:
                self._accepted(_known_answer(session_id, known))
                return {**self._sent(session_id, request_id), "repeated": True}
            session = self.store.session(session_id)
            if session.state == SESSION_CLOSED:
                raise SessionClosed(f"PO session {session_id} is closed; open a new session to continue")
            self._accepting()
            self.queue.put(session_id=session_id, text=text, request_id=request_id, source=source)
            self._accepted(
                {"session_id": session_id, "queued": True, "seq": None, "state": None, "repeated": False}
            )
            # Best effort from here: the answer above stands if the hand-over or the lookup fails.
            self.pump()
            return {**self._sent(session_id, request_id), "repeated": False}

    def _reserve(
        self,
        request_id: str,
        operation: str,
        fingerprint: str,
        *,
        session_id: str | None = None,
        text: str | None = None,
    ) -> PoRequest | QueuedInput | None:
        """The one request-id check of the service: what already owns `request_id`, or None when nothing does.

        A request id belongs to exactly one operation with fixed inputs, installation-wide, from the
        moment it is acknowledged. Acknowledged means one of three places: a `po_requests` row (a
        session or a turn exists), a message still pending in the queue, or a message the service set
        aside in `refused/`. The same operation with the same inputs gets its record back (a replay);
        anything else is `RequestConflict`. Every operation that takes a request id calls this while
        holding the service lock, so nothing can take an id between this check and its own write.
        """
        known = self.store.request(request_id)
        if known is not None:
            if (known.operation, known.fingerprint) != (operation, fingerprint):
                raise RequestConflict(
                    f"request id {request_id!r} already belongs to another {known.operation} request; "
                    "a request id is repeated only with the same operation and inputs"
                )
            return known
        queued = self.queue.find(request_id)
        if queued is not None:
            if operation != SEND or (queued.session_id, queued.text) != (session_id, text):
                raise RequestConflict(
                    f"request id {request_id!r} already belongs to a message queued for PO session "
                    f"{queued.session_id}; a request id is repeated only with the same operation and inputs"
                )
            return queued
        refused = self.queue.find_refused(request_id)
        if refused is not None:
            raise RequestConflict(
                f"request id {request_id!r} belongs to a message the PO service set aside "
                f"({refused.get('reason') or 'no reason recorded'}); send it again with a new form"
            )
        return None

    def _sent(self, session_id: str, request_id: str) -> dict[str, Any]:
        """Where an acknowledged message is now: its turn, or still in the queue."""
        known = self.store.request(request_id)
        if known is not None and known.seq is not None:
            turn = self.store.turn(known.session_id, int(known.seq))
            return {"session_id": session_id, "queued": False, "seq": turn.seq, "state": turn.state}
        if self.queue.find(request_id) is not None:
            return {"session_id": session_id, "queued": True, "seq": None, "state": None}
        raise Refused(
            "unavailable", "the message was queued and then set aside; the service journal says why"
        )

    def stop_turn(self, *, session_id: str, seq: int) -> dict[str, Any]:
        """Stop turn `seq` only if it is the one running; queued inputs of the session then go on."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise Refused("validation", "seq names the running turn to stop, as a whole number")
        turn = self.runner.stop_turn(_required(session_id, "session_id"), seq)
        self._wake.set()
        return {"session_id": session_id, "seq": seq, "stopped": turn is not None}

    def close_session(self, *, session_id: str, actor: str = STOP_ACTOR) -> dict[str, Any]:
        """Close as `actor`; a running turn or a message still queued for the session refuses it."""
        session_id = _required(session_id, "session_id")
        with self._lock:
            waiting = self.queue.pending(session_id)
            if waiting:
                raise TurnInProgress(
                    f"{len(waiting)} message(s) queued in PO session {session_id} have not run yet; "
                    "wait for their answers, then close"
                )
            session = self.store.close_session(session_id, _required(actor, "actor"))
        return {"session_id": session.session_id, "state": session.state}

    def status(self) -> dict[str, Any]:
        return {
            "running": self.runner.live_count(),
            "queued": len(self.queue.pending()),
            "restart_pending": self.marker.exists(),
            "recovered": self._recovered,
        }

    def request_restart(self, *, reason: str = "") -> dict[str, Any]:
        """The upgrade rule: restart now only while no turn runs, else defer it to the first idle moment.

        The marker (written by the requester, `secretary.po.client.request_restart`) holds the queue
        from here on, so a deferred restart is not starved by new inputs; they stay queued and the new
        process takes them. Idle, the service exits once this answer is sent.
        """
        with self._lock:
            if not self.marker.exists():
                from secretary.po.client import write_restart_marker

                write_restart_marker(self.data_dir, reason or "restart requested")
            running = self.runner.live_count()
            if running:
                return {
                    "restart": "deferred",
                    "running": running,
                    "detail": f"PO service restart deferred: {running} turn(s) running",
                }
            self._exit.set()
            self._wake.set()
            return {"restart": "now", "running": 0, "detail": "PO service is idle and exits for the restart"}

    def restart_due(self) -> bool:
        return self.marker.exists() and self.runner.live_count() == 0

    # --- the wire ---------------------------------------------------------------------------

    def handle(self, request: Any) -> dict[str, Any]:
        """One decoded request to one answer document; never raises.

        An operation that takes a request id runs inside :meth:`_answer_id_operation`, the one place
        that decides what such a request is answered once it may have been accepted.
        """
        try:
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise Refused("validation", "a PO service request is a JSON object with an op")
            fields = {key: value for key, value in request.items() if key != "op"}
            operation = _OPERATIONS.get(request["op"])
            if operation is None:
                raise Refused("validation", f"the PO service has no operation {request['op']!r}")
            method = getattr(self, operation)
            try:
                inspect.signature(method).bind(**fields)
            except TypeError as exc:
                raise Refused("validation", f"{request['op']}: {exc}") from None
        except Refused as exc:
            return _error(exc.code, str(exc), nothing_written=True)
        if operation in ID_OPERATIONS:
            return self._answer_id_operation(method, fields)
        try:
            return {"ok": True, "result": method(**fields)}
        except Exception as exc:  # noqa: BLE001 - an answer, not a dead connection
            return _refusal(exc, nothing_written=False)

    def _answer_id_operation(
        self, method: Callable[..., dict[str, Any]], fields: dict[str, Any]
    ) -> dict[str, Any]:
        """The one wrapper around every operation that takes a request id.

        The operation reports its progress on an :class:`_Acceptance` (`_accepting` right before the
        write that accepts the request, `_accepted` with the answer it already knows right after). The
        answer then follows one rule, whatever failed:

        - nothing failed: the operation's own answer;
        - a failure after acceptance (an enrichment read, the queue pump): the answer known at
          acceptance — the message queued under its id, or the session — never a refusal;
        - a failure during the accepting write itself, which may have committed: `outcome_unknown`,
          whose client-side meaning is "repeat the same request";
        - a failure before it: a refusal, marked `nothing_written` only for the refusals known to
          have written nothing (validation, a request-id conflict, an unknown or closed session).
        """
        acceptance = _Acceptance()
        token = _ACCEPTANCE.set(acceptance)
        try:
            return {"ok": True, "result": method(**fields)}
        except Exception as exc:  # noqa: BLE001 - an answer, not a dead connection
            if acceptance.answer is not None:
                _say(
                    f"secretary po: request accepted, answered from acceptance after "
                    f"{type(exc).__name__}: {exc}"
                )
                return {"ok": True, "result": acceptance.answer}
            if acceptance.accepting:
                return _error(
                    "outcome_unknown",
                    f"the PO service may have accepted this request ({type(exc).__name__}: {exc}); "
                    "repeat it with the same request id",
                )
            definite = (
                isinstance(exc, _DEFINITE_REFUSALS) and getattr(exc, "code", "validation") == "validation"
            )
            return _refusal(exc, nothing_written=definite)
        finally:
            _ACCEPTANCE.reset(token)

    @staticmethod
    def _accepting() -> None:
        acceptance = _ACCEPTANCE.get()
        if acceptance is not None:
            acceptance.accepting = True

    @staticmethod
    def _accepted(answer: dict[str, Any]) -> None:
        acceptance = _ACCEPTANCE.get()
        if acceptance is not None:
            acceptance.accepting = True
            acceptance.answer = dict(answer)


class _Acceptance:
    """How far one id-taking request got: into its accepting write, and the answer known right after it."""

    def __init__(self) -> None:
        self.accepting = False
        self.answer: dict[str, Any] | None = None


_ACCEPTANCE: contextvars.ContextVar[_Acceptance | None] = contextvars.ContextVar(
    "po_acceptance", default=None
)
# The endpoint operations that take a request id; `handle` answers them through `_answer_id_operation`.
ID_OPERATIONS = frozenset({"create_session", "submit"})


# Refusals raised before acceptance that are known to have written nothing (`nothing_written`).
_DEFINITE_REFUSALS = (Refused, RequestConflict, SessionClosed, SessionNotFound, RunnerError)


def _known_answer(session_id: str, known: PoRequest | QueuedInput) -> dict[str, Any]:
    """What a replayed message is known to be without another read: its turn's seq, or still queued."""
    if isinstance(known, PoRequest):
        return {"session_id": session_id, "queued": False, "seq": known.seq, "state": None, "repeated": True}
    return {"session_id": session_id, "queued": True, "seq": None, "state": None, "repeated": True}


def _refusal(exc: Exception, *, nothing_written: bool) -> dict[str, Any]:
    """One exception to one error answer; `nothing_written` marks a refusal that wrote nothing."""
    if isinstance(exc, Refused):
        code = exc.code
    elif isinstance(exc, SessionNotFound):
        code = "session_not_found"
    elif isinstance(exc, SessionClosed):
        code = "session_closed"
    elif isinstance(exc, RequestConflict):
        code = "request_conflict"
    elif isinstance(exc, TurnInProgress):
        code = "turn_in_progress"
    elif isinstance(exc, RunnerError):
        code = "validation"
    elif isinstance(exc, (PoStoreError, QueueError)):
        return _error("unavailable", str(exc))
    else:
        return _error("unavailable", f"the PO service failed: {type(exc).__name__}: {exc}")
    return _error(code, str(exc), nothing_written=nothing_written)


_OPERATIONS = {
    "create_session": "create_session",
    "submit": "submit",
    "stop_turn": "stop_turn",
    "close_session": "close_session",
    "status": "status",
    "restart": "request_restart",
}


def _error(code: str, message: str, *, nothing_written: bool = False) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if nothing_written:
        error["nothing_written"] = True
    return {"ok": False, "error": error}


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise Refused("validation", f"{name} is required")
    return text


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# --- the socket -----------------------------------------------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        try:
            # Only a complete line runs: a sender whose write failed before its newline was
            # told "nothing written" (`secretary.po.client`), and that has to stay true.
            if not raw.endswith(b"\n"):
                raise ValueError("incomplete request line")
            request = json.loads(raw)
        except ValueError:
            answer = _error(
                "validation",
                "a PO service request is one complete JSON object on one line",
                nothing_written=True,
            )
        else:
            answer = self.server.service.handle(request)  # type: ignore[attr-defined]
        self.wfile.write(json.dumps(answer, ensure_ascii=False, default=str).encode("utf-8") + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, service: PoService) -> None:
        self.service = service
        previous = os.umask(0o177)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)


@contextlib.contextmanager
def listening(service: PoService) -> Iterator[Path]:
    """Hold the service lock and serve the socket for as long as the block runs."""
    directory = service_dir(service.data_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = socket_path(service.data_dir)
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        raise ServiceStartError(f"the PO service socket path is too long for a Unix socket: {path}")
    with open(directory / LOCK_NAME, "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceStartError(
                f"another PO service already serves this installation (it holds {directory / LOCK_NAME})"
            ) from None
        # Under the lock no other service is alive, so a socket file left here is a dead one's.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        try:
            server = _Server(path, service)
        except OSError as exc:
            raise ServiceStartError(f"could not listen on {path}: {exc}") from None
        thread = threading.Thread(target=server.serve_forever, name="po-service-socket", daemon=True)
        thread.start()
        try:
            yield path
        finally:
            server.shutdown()
            server.server_close()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


# --- `secretary po-serve` -------------------------------------------------------------------------


def add_po_serve_subcommands(subparsers) -> None:
    command = subparsers.add_parser(
        "po-serve",
        help="run the PO head service: the one owner of PO turns, fed by a durable queue over a local socket",
    )
    command.add_argument("--instance", required=True, help="path to an instance dir or instance.yaml")
    command.add_argument(
        "--data-dir",
        default=os.environ.get("SECRETARY_DATA_DIR"),
        help="override the instance's configured data directory",
    )
    command.set_defaults(handler=run_po_serve)


def run_po_serve(args: argparse.Namespace) -> int:
    from secretary.config import DataDirError, instance_data_dir

    try:
        data_dir = Path(args.data_dir) if args.data_dir else instance_data_dir(Path(args.instance))
    except DataDirError as exc:
        _say(f"secretary po-serve: {exc}")
        return 2
    service = PoService(PoRunner.for_instance(args.instance, data_dir), data_dir=data_dir)
    try:
        with listening(service) as path:
            _say(f"secretary po: serving {path}; queue {service.queue.directory}")
            for line in service.start():
                _say(line)
            return service.run()
    except ServiceStartError as exc:
        _say(f"secretary po-serve: {exc}")
        return 1


__all__ = [
    "TICK_SECONDS",
    "PoService",
    "Refused",
    "ServiceStartError",
    "add_po_serve_subcommands",
    "listening",
    "run_po_serve",
]
