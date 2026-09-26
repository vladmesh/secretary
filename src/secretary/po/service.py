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
import fcntl
import inspect
import json
import os
import socketserver
import sys
import threading
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
    DEFAULT_EFFORT,
    SEND,
    SESSION_CLOSED,
    PoStoreError,
    RequestConflict,
    SessionClosed,
    SessionNotFound,
    TurnInProgress,
    send_fingerprint,
)

# How often the service looks at its queue, its restart marker and a recovery that did not run yet,
# besides being woken by a submit or a settled turn.
TICK_SECONDS = 1.0
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
        self._recovered = False

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

    def _recover(self) -> list[str]:
        with self._lock:
            try:
                recovered = self.runner.recover(rerun=True)
            except Exception as exc:  # noqa: BLE001 - retried every tick until the store answers
                return [f"secretary po: turn recovery did not run: {type(exc).__name__}: {exc}"]
            self._recovered = True
        return [
            f"secretary po: turn {turn.session_id}/{turn.seq} {turn.state}: {turn.reason}"
            for turn in recovered
        ]

    def run(self, *, tick: float = TICK_SECONDS, say: Callable[[str], None] | None = None) -> int:
        """Serve until a restart is due at idle (exit 0, `Restart=always` starts the new code)."""
        say = say or _say
        while not self._exit.is_set():
            self._wake.wait(tick)
            self._wake.clear()
            if not self._recovered:
                for line in self._recover():
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
            if not self._recovered or self._exit.is_set() or self.marker.exists():
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
        request_id = _required(request_id, "request_id")
        session, created = self.runner.create_session_request(
            cli, model, request_id, effort or DEFAULT_EFFORT
        )
        return {"session_id": session.session_id, "effort": session.effort, "repeated": not created}

    def submit(self, *, session_id: str, text: str, request_id: str, source: str = "web") -> dict[str, Any]:
        """Queue one message durably, then answer; a request id answers what it already made.

        The same id with the same session and text is a replay: the turn it became, or the input still
        queued. The id bound to anything else is `RequestConflict`. A message into an unknown or closed
        session is refused with nothing queued. Otherwise it is queued and handed over at once when its
        session is idle, and the answer says which.
        """
        request_id = _required(request_id, "request_id")
        session_id = _required(session_id, "session_id")
        if not str(text or "").strip():
            raise Refused("validation", "an empty message starts no turn")
        if source not in SOURCES:
            raise Refused("validation", f"a PO input comes from {' or '.join(SOURCES)}, not {source!r}")
        with self._lock:
            replay = self._replay(session_id, text, request_id)
            if replay is not None:
                return replay
            session = self.store.session(session_id)
            if session.state == SESSION_CLOSED:
                raise SessionClosed(f"PO session {session_id} is closed; open a new session to continue")
            self.queue.put(session_id=session_id, text=text, request_id=request_id, source=source)
            self.pump()
            answer = self._replay(session_id, text, request_id)
        if answer is None:
            raise Refused(
                "unavailable", "the message was queued and then set aside; the service journal says why"
            )
        return {**answer, "repeated": False}

    def _replay(self, session_id: str, text: str, request_id: str) -> dict[str, Any] | None:
        known = self.store.request(request_id)
        if known is not None:
            if (known.operation, known.fingerprint) != (SEND, send_fingerprint(session_id, text)):
                raise RequestConflict(
                    f"request id {request_id!r} already belongs to another {known.operation} request; "
                    "a request id is repeated only with the same operation and inputs"
                )
            turn = self.store.turn(known.session_id, int(known.seq))
            return {
                "session_id": session_id,
                "queued": False,
                "seq": turn.seq,
                "state": turn.state,
                "repeated": True,
            }
        queued = self.queue.find(request_id)
        if queued is not None:
            if (queued.session_id, queued.text) != (session_id, text):
                raise RequestConflict(
                    f"request id {request_id!r} is already queued for another message; "
                    "a request id is repeated only with the same operation and inputs"
                )
            return {"session_id": session_id, "queued": True, "seq": None, "state": None, "repeated": True}
        return None

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
        """One decoded request to one answer document; never raises."""
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
            return {"ok": True, "result": method(**fields)}
        except Refused as exc:
            return _error(exc.code, str(exc))
        except SessionNotFound as exc:
            return _error("session_not_found", str(exc))
        except SessionClosed as exc:
            return _error("session_closed", str(exc))
        except RequestConflict as exc:
            return _error("request_conflict", str(exc))
        except TurnInProgress as exc:
            return _error("turn_in_progress", str(exc))
        except RunnerError as exc:
            return _error("validation", str(exc))
        except (PoStoreError, QueueError) as exc:
            return _error("unavailable", str(exc))
        except Exception as exc:  # noqa: BLE001 - an answer, not a dead connection
            return _error("unavailable", f"the PO service failed: {type(exc).__name__}: {exc}")


_OPERATIONS = {
    "create_session": "create_session",
    "submit": "submit",
    "stop_turn": "stop_turn",
    "close_session": "close_session",
    "status": "status",
    "restart": "request_restart",
}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


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
            request = json.loads(raw)
        except ValueError:
            answer = _error("validation", "a PO service request is one JSON object on one line")
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
