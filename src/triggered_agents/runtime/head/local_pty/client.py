"""How anything else addresses a supervised head: start one, then speak to its socket.

`spawn_head` is the launcher side of the independence property. It starts an intermediate process
in a new session, that intermediate forks the supervisor and exits, and the launcher reaps the
intermediate immediately. So the supervisor is never the launcher's child: a dispatcher tick that
ends — or is killed with its whole process group — leaves the head running and still addressable,
and there is no descriptor of the launcher's left to turn the supervisor into a zombie.

Readiness is read from the run directory rather than from the pipe of a process that has already
exited: the socket answers, the journal has `run.started`, and the head has written its own launch
identity. A refusal on the way up is left behind as `startup.error`, so a launcher can tell "the
supervisor is still coming up" from "another supervisor already owns this run"; a failure after the
run was up is `supervisor.error` instead, because a head that ran for an hour and then lost its
supervisor did not fail to start and must not be described as if it had.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import protocol
from .journal import RUN_STARTED, JournalReadResult, read_events

SUPERVISOR_MODULE = "triggered_agents.runtime.head.local_pty.supervisor"
#: How long `spawn_head` waits for the run directory to say the head is up.
SPAWN_TIMEOUT_SECONDS = 20.0
_POLL_SECONDS = 0.02


class LocalPtyError(RuntimeError):
    """Something about a supervised head could not be done."""


class LocalPtySpawnError(LocalPtyError):
    """A head did not come up, and the reason the run directory gave for it."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class HeadHandle:
    """Everything a caller needs to reach a head it started, all derived from the run directory."""

    run_dir: Path
    run_id: str
    role: str
    task: str
    socket_path: Path
    journal_path: Path
    pid_file: Path
    supervisor_pid: int
    head_pid: int

    def connect(self, timeout: float = 5.0) -> SupervisorClient:
        return SupervisorClient.connect(self.socket_path, timeout=timeout)

    def events(self) -> JournalReadResult:
        """Read the journal from outside the supervisor, alive or dead."""
        return read_events(self.journal_path)

    def identity(self) -> dict[str, Any]:
        """The head's own launch-identity record, as `with_pid_heartbeat` wrote it."""
        try:
            record = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return record if isinstance(record, dict) else {}


def _supervisor_environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    """The supervisor imports the product from the checkout this module was loaded from.

    An ambient `PYTHONPATH` naming another installation is exactly the way a workspace's supervisor
    would come up running somebody else's code, so this checkout's root goes in front of it.
    """
    environment = dict(os.environ)
    environment.update(extra or {})
    root = str(Path(__file__).resolve().parents[4])
    parts = [part for part in environment.get("PYTHONPATH", "").split(os.pathsep) if part]
    environment["PYTHONPATH"] = os.pathsep.join([root, *[p for p in parts if p != root]])
    return environment


def spawn_head(
    *,
    root: str | os.PathLike[str],
    run_id: str,
    role: str,
    task: str,
    command: str,
    cwd: str | os.PathLike[str] = "",
    rows: int = 24,
    cols: int = 80,
    term: str = "xterm-256color",
    quiet_seconds: float | None = None,
    delivery_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = SPAWN_TIMEOUT_SECONDS,
) -> HeadHandle:
    """Bring one head up under a supervisor that outlives this process, and wait until it answers."""
    run_dir = protocol.run_dir_for(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    socket_path = protocol.socket_path_for(run_dir)
    error_paths = (
        run_dir / protocol.STARTUP_ERROR_NAME,
        run_dir / protocol.SUPERVISOR_ERROR_NAME,
    )
    for error_path in error_paths:
        error_path.unlink(missing_ok=True)
    journal_path = run_dir / protocol.JOURNAL_NAME
    already = len(read_events(journal_path).events)

    argv = [
        sys.executable,
        "-P",
        "-m",
        SUPERVISOR_MODULE,
        "--run-dir",
        str(run_dir),
        "--run-id",
        run_id,
        "--role",
        role,
        "--task",
        task,
        "--command",
        command,
        "--rows",
        str(rows),
        "--cols",
        str(cols),
        "--term",
        term,
        "--daemonize",
    ]
    if cwd:
        argv += ["--cwd", str(cwd)]
    if quiet_seconds is not None:
        argv += ["--quiet-seconds", str(quiet_seconds)]
    if delivery_seconds is not None:
        argv += ["--delivery-seconds", str(delivery_seconds)]
    log_path = run_dir / protocol.SUPERVISOR_LOG_NAME
    with open(log_path, "ab", buffering=0) as log:
        intermediate = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            env=_supervisor_environment(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    # The intermediate has already forked the supervisor and is on its way out; reaping it here is
    # what guarantees this process leaves no child behind, whatever it does next.
    status = intermediate.wait()

    deadline = time.monotonic() + timeout
    while True:
        for error_path in error_paths:
            failure = _startup_error(error_path)
            if failure is not None:
                raise LocalPtySpawnError(
                    str(failure.get("reason") or "startup_failed"),
                    str(failure.get("detail") or ""),
                )
        result = read_events(journal_path)
        started = [event for event in result.events[already:] if event.get("kind") == RUN_STARTED]
        if started and socket_path.exists() and _identity_written(run_dir, run_id) and _answers(socket_path):
            record = started[-1]
            return HeadHandle(
                run_dir=run_dir,
                run_id=run_id,
                role=role,
                task=task,
                socket_path=socket_path,
                journal_path=journal_path,
                pid_file=run_dir / protocol.PID_FILE_NAME,
                supervisor_pid=int(record.get("supervisor_pid") or 0),
                head_pid=int(record.get("head_pid") or 0),
            )
        if time.monotonic() >= deadline:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2048:]
            except OSError:
                pass
            raise LocalPtySpawnError(
                "timeout",
                f"the supervisor for {run_id} did not answer within {timeout:g}s "
                f"(intermediate exit {status}); log tail: {tail!r}",
            )
        time.sleep(_POLL_SECONDS)


def _identity_written(run_dir: Path, run_id: str) -> bool:
    """Whether the head has published its own launch identity yet.

    The heartbeat is written by the head's shell before it `exec`s, so the supervisor's
    `run.started` can land first. A handle is only honest once the record exists and names this
    run: until then a reader pointed at the pid file would see a head that is up as one that is
    not yet written.
    """
    try:
        record = json.loads((run_dir / protocol.PID_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and str(record.get("run_id") or "") == run_id


def _startup_error(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _answers(socket_path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(socket_path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class SupervisorClient:
    """One connection to one supervisor. Requests are answered in order; attach pushes after that.

    Deliberately synchronous and deliberately small: this is the substrate's surface, and the
    backend that will wear `HeadRuntime` is what turns these answers into receipts.
    """

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn
        self._inbox = bytearray()
        self._request_seq = 0
        #: Answers to questions this client stopped waiting for, discarded rather than returned.
        self.stale_frames = 0
        self.attached = False

    @classmethod
    def connect(cls, socket_path: str | os.PathLike[str], *, timeout: float = 5.0) -> SupervisorClient:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(str(socket_path))
        except OSError as exc:
            conn.close()
            raise LocalPtyError(f"no supervisor answers at {socket_path}: {exc}") from exc
        return cls(conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass

    def __enter__(self) -> SupervisorClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- frames ----------------------------------------------------------------------------

    def _next_frame(self) -> dict[str, Any]:
        while True:
            index = self._inbox.find(b"\n")
            if index >= 0:
                line = bytes(self._inbox[:index])
                del self._inbox[: index + 1]
                return protocol.decode_frame(line)
            chunk = self._conn.recv(65536)
            if not chunk:
                raise LocalPtyError("the supervisor closed the connection")
            self._inbox += chunk

    def _refusal_already_sent(self) -> dict[str, Any] | None:
        """A frame this connection was given before it could be written to, or `None`.

        Only ever consulted when a request could not be sent at all. A stream event is not an
        answer to anything and is skipped, and so is a frame that carries an id: an id names the
        request it answers, and the request that just failed to be sent is not that one. Only an
        uncorrelated frame — a connection refused before anything was asked on it, a refusal of
        bytes too malformed to carry an id — is this caller's news, and it is the whole of it.

        Skipping the correlated ones is the same rule `request` keeps below, kept here as well
        because this is the one path that reads the queue without having asked anything: without
        it, an answer to an abandoned earlier request would become this request's answer, which
        is exactly the desynchronisation the id exists to prevent.
        """
        try:
            while True:
                frame = self._next_frame()
                if "event" in frame:
                    continue
                if frame.get(protocol.REQUEST_ID) is not None:
                    self.stale_frames += 1
                    continue
                return frame
        except (LocalPtyError, OSError):
            return None

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one request and return *its* answer, discarding anything that answers something else.

        Three kinds of frame can arrive on this connection, and each is handled by what it is
        rather than by when it arrived: a pushed stream event, which belongs to `next_event`; the
        answer to this request, matched by id; and the answer to an earlier request this client
        stopped waiting for — a timeout, an interrupt — which is discarded and counted here.

        That last case is why the id exists. Without it a stale frame silently becomes the answer
        to the next question asked, and a connection stays one answer out of step for the rest of
        its life: a caller asking `status` would be handed the previous `input`'s reply and read a
        missing `alive` key as a dead head. Nothing in this substrate blocks long enough to make
        that the normal case any more, but a caller's timeout can fire for reasons of its own, so
        the recovery is part of the protocol rather than a property of it being fast.
        """
        self._request_seq += 1
        request_id = self._request_seq
        try:
            self._conn.sendall(protocol.encode_frame({**payload, protocol.REQUEST_ID: request_id}))
        except OSError:
            # The supervisor may have answered this connection *before* anything was asked on it
            # and closed it: that is what happens at the connection bound, where the refusal is
            # written and the socket is let go. A write that then loses the race is `EPIPE`, and
            # the refusal is still sitting in this end's receive queue. Losing it would turn a
            # live head at a bound into an exception out of a verb, so the queue is read before
            # the failure is passed on.
            refusal = self._refusal_already_sent()
            if refusal is None:
                raise
            return refusal
        while True:
            frame = self._next_frame()
            if "event" in frame:
                continue
            answered = frame.get(protocol.REQUEST_ID)
            if answered == request_id:
                return frame
            if answered is None:
                # A frame that answers no particular request: a connection refused before anything
                # was asked, or a refusal of bytes too malformed to carry an id. It is this
                # caller's news either way.
                return frame
            self.stale_frames += 1

    # -- verbs -----------------------------------------------------------------------------

    def set_timeout(self, timeout: float) -> None:
        """Rebound how long one request on this connection may take to be answered.

        The connect bound is a bound on reaching a supervisor that has not spoken yet, and it is
        the right number for that. It is the wrong number for a caller that has since learned how
        long the thing it is watching may take: a socket left on the connect bound turns a
        supervisor that is merely slower than that into a supervisor that stopped answering. So the
        bound is settable, and a caller that knows the substrate's own bound for what it is
        watching sets it from that rather than from anything of its own.
        """
        self._conn.settimeout(timeout)

    def status(self) -> dict[str, Any]:
        return self.request({"op": protocol.OP_STATUS})

    def send_input(self, data: bytes | str, *, subject: str = "") -> dict[str, Any]:
        """Offer one bounded payload for the head's pty. Oversize is refused, never truncated.

        The answer is about **admission**, and comes back within the supervisor's own tick: `ok`
        means the payload was taken on and is being written, and it carries the `delivery` — id,
        size, and the state to ask about later. A refusal names its reason: over the declared limit
        (with the limit and the actual size), admission closed, the head gone, or another delivery
        still holding the floor.

        What happened to the bytes afterwards is not in this answer and deliberately so: it is
        `status()["delivery"]` and the journal's `input.accepted`, both of which count what the
        head's terminal actually took. `wait_for_delivery` is the convenience for a caller that
        wants to stand and watch that happen.
        """
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return self.request(
            {"op": protocol.OP_INPUT, "data": protocol.encode_payload(payload), "subject": subject}
        )

    def wait_for_delivery(
        self,
        delivery_id: int | None = None,
        *,
        timeout: float = protocol.INPUT_DELIVERY_SECONDS + 5.0,
        poll: float = 0.02,
    ) -> dict[str, Any]:
        """Ask `status` until the delivery is no longer in flight, and return what became of it.

        The waiting is here, in the caller, where it can be given up on: the supervisor is answering
        every question in the meantime, including this one. A delivery always leaves the in-flight
        state — it completes, it stalls at its bound, or it fails with the head — so this returns.
        """
        deadline = time.monotonic() + timeout
        while True:
            delivery = self.status().get("delivery")
            if isinstance(delivery, dict) and (delivery_id is None or delivery.get("id") == delivery_id):
                if delivery.get("state") != protocol.DELIVERY_IN_FLIGHT:
                    return delivery
            if time.monotonic() >= deadline:
                raise LocalPtyError(
                    f"delivery {delivery_id} was still in flight after {timeout:g}s: {delivery}"
                )
            time.sleep(poll)

    def read_output(self, max_bytes: int | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"op": protocol.OP_OUTPUT}
        if max_bytes is not None:
            request["max_bytes"] = int(max_bytes)
        answer = self.request(request)
        if answer.get("ok"):
            answer["bytes_data"] = protocol.decode_payload(answer.get("data") or "")
        return answer

    def resize(self, rows: int, cols: int) -> dict[str, Any]:
        return self.request({"op": protocol.OP_RESIZE, "rows": int(rows), "cols": int(cols)})

    def drain(self, initiator: str = "client") -> dict[str, Any]:
        return self.request({"op": protocol.OP_DRAIN, "initiator": initiator})

    def stop(self, initiator: str = "client", signal_name: str = "TERM") -> dict[str, Any]:
        return self.request({"op": protocol.OP_STOP, "initiator": initiator, "signal": signal_name})

    def attach(self) -> dict[str, Any]:
        answer = self.request({"op": protocol.OP_ATTACH})
        if answer.get("ok"):
            self.attached = True
            answer["bytes_data"] = protocol.decode_payload(answer.get("data") or "")
        return answer

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next pushed frame, or `None` when none arrived within `timeout`.

        A reader that cannot tell "nothing was pushed" from "the stream ended" would have to
        guess, so the two are different values: `None` for the quiet wait, `StopIteration` from
        `stream` for the end.
        """
        if timeout is not None:
            self._conn.settimeout(timeout)
        while True:
            try:
                frame = self._next_frame()
            except TimeoutError:
                return None
            except (LocalPtyError, OSError):
                return None
            if "event" not in frame:
                continue
            if frame.get("data") is not None:
                frame["bytes_data"] = protocol.decode_payload(frame["data"])
            return frame

    def stream(self, *, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """Yield pushed events for an attached client until the connection ends.

        Detaching is closing the connection: it is not a message, and it does nothing to the head.
        """
        while True:
            frame = self.next_event(timeout)
            if frame is None:
                return
            yield frame
            if frame.get("event") == protocol.EVENT_EXITED:
                return
