"""The process that owns one head: its pty, its socket, its journal and its ending.

The supervisor is the answer to a question the sprint's first card exists to settle — can a
process the product starts itself outlive the dispatcher tick that started it and stay
addressable afterwards. It does four things and refuses to do a fifth:

  * it **starts the head on a pty of its own**, with `pty.fork`, which puts the head in a new
    session with the pty as its controlling terminal. A signal sent to the dispatcher's process
    group therefore cannot reach it, and an interactive adapter gets the terminal it expects,
    including `SIGWINCH` when the size changes;
  * it **holds the head's addressable surface**: a Unix socket at a predictable path, owner-only,
    with bounded input, bounded output and bounded attach. Every bound refuses by name;
  * it **narrates the run** into the versioned append-only journal beside the socket;
  * it **reaps the head**, tells its own death apart from the head's, and writes `run.exited` with
    the exit code or the signal. When the head is gone the socket goes with it: nothing holds an
    address for a process that no longer exists;
  * it does **not** implement `HeadRuntime`. It is a substrate, and the six verbs are a separate
    piece of work built on the surface above.

The head's identity is not this process's business either. The head's command is wrapped by
`with_pid_heartbeat`, so the record under `head.pid` is written by the head's own process and is
the same launch identity `secretary.dispatcher_watchdog` already reads.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import selectors
import signal
import socket
import struct
import sys
import termios
import time
from pathlib import Path
from typing import Any

from ..command import with_pid_heartbeat
from . import protocol
from .journal import (
    DRAIN_REQUESTED,
    INPUT_ACCEPTED,
    PROVIDER_PROGRESSED,
    RUN_EXITED,
    RUN_STARTED,
    RUN_STOPPING,
    TURN_FINISHED,
    TURN_STARTED,
    JournalWriter,
)

#: A turn is over when the head has said nothing for this long. The substrate cannot see a
#: provider's own end-of-turn marker — that is an adapter's knowledge, and inventing one here would
#: be a lie in the journal — so what it records is the fact it can actually observe: the head went
#: quiet.
TURN_QUIET_SECONDS = 2.0
#: One chatty second of output is one `provider.progressed` record, with the bytes it covered.
PROGRESS_COALESCE_SECONDS = 0.5
#: How long a stopping head is given before the signal is escalated.
STOP_GRACE_SECONDS = 5.0
#: The loop's own resolution: what bounds how late a quiet turn or a stop deadline is noticed.
LOOP_TICK_SECONDS = 0.1
#: How long the supervisor keeps trying to flush the last frames to attached clients before it
#: closes the socket for good.
FAREWELL_SECONDS = 0.5
_READ_CHUNK = 65536

EXIT_OK = 0
EXIT_STARTUP_FAILED = 2
EXIT_ALREADY_RUNNING = 3

START_ALREADY_RUNNING = "already_running"
START_FAILED = "startup_failed"


class SupervisorStartupError(RuntimeError):
    """The supervisor could not take ownership of this run, and took none of it."""

    def __init__(self, reason: str, detail: str, *, exit_code: int = EXIT_STARTUP_FAILED) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.exit_code = exit_code


class _Client:
    """One caller on the socket, and everything the supervisor owes it or withholds from it."""

    __slots__ = ("conn", "inbox", "pending", "attached", "dropped", "closing", "overflowed")

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.inbox = bytearray()
        self.pending = bytearray()
        self.attached = False
        self.dropped = 0
        self.closing = False
        self.overflowed = False


class Supervisor:
    """One run's owner. Constructed in the process that will *be* the supervisor, never elsewhere."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        role: str,
        task: str,
        command: str,
        rows: int = 24,
        cols: int = 80,
        term: str = "xterm-256color",
        quiet_seconds: float = TURN_QUIET_SECONDS,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.role = role
        self.task = task
        self.command = command
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        self.term = term
        self.quiet_seconds = float(quiet_seconds)

        self.socket_path = protocol.socket_path_for(self.run_dir)
        self.journal_path = self.run_dir / protocol.JOURNAL_NAME
        self.pid_file = self.run_dir / protocol.PID_FILE_NAME

        self._lock_fd = -1
        self._listener: socket.socket | None = None
        self._selector = selectors.DefaultSelector()
        self._clients: dict[socket.socket, _Client] = {}
        self._journal: JournalWriter | None = None

        self._master = -1
        self._head_pid = 0
        self._head_status: int | None = None
        self._to_head = bytearray()

        self._output = bytearray()
        self._output_dropped = 0
        self._output_total = 0

        self._turn_open = False
        self._turn_id = 0
        self._turn_bytes = 0
        self._last_output_at = 0.0
        self._progress_bytes = 0
        self._progress_at = 0.0

        self._draining = False
        self._stopping = False
        self._stop_deadline = 0.0
        self._signalled = 0
        self._wakeup_read = -1
        self._wakeup_write = -1

    # -- startup ---------------------------------------------------------------------------

    def claim(self) -> None:
        """Take exclusive ownership of the run directory, or refuse without touching anything.

        The lock is what makes a restart over an orphaned socket safe. A live supervisor holds it,
        so a second start is refused loudly rather than binding a second socket beside the first
        and bringing a second head up under the same run id. Only once the lock is *held* is a
        socket file left over from a dead supervisor treated as debris and removed.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        lock_path = self.run_dir / protocol.SUPERVISOR_LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise SupervisorStartupError(
                START_ALREADY_RUNNING,
                f"another supervisor already owns {self.run_dir} ({exc.strerror})",
                exit_code=EXIT_ALREADY_RUNNING,
            ) from exc
        self._lock_fd = fd
        os.write(fd, f"{os.getpid()}\n".encode())
        self._refuse_a_second_head()
        self._bind()

    def _refuse_a_second_head(self) -> None:
        """Refuse to start beside a head of this run that is still alive.

        Holding the lock proves no other *supervisor* owns the run; it does not prove the run has
        no head. A supervisor killed with `SIGKILL` leaves its head running and orphaned, and the
        one thing a restart must never do is bring a second head up under the same run id in
        silence. The check reads the head's own launch identity — the same record
        `secretary.dispatcher_watchdog` reads — and refuses only on the full triple, so a recycled
        pid cannot fence a run out.
        """
        alive = _live_head(self.pid_file, self.run_id)
        if alive:
            raise SupervisorStartupError(
                START_ALREADY_RUNNING,
                f"head {alive} of run {self.run_id} is still alive; refusing to start a second "
                f"head for the same run",
                exit_code=EXIT_ALREADY_RUNNING,
            )

    def _bind(self) -> None:
        if self.socket_path.exists():
            if self._socket_answers():
                raise SupervisorStartupError(
                    START_ALREADY_RUNNING,
                    f"{self.socket_path} still answers; refusing to start a second head",
                    exit_code=EXIT_ALREADY_RUNNING,
                )
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o077)
        try:
            listener.bind(str(self.socket_path))
        finally:
            os.umask(previous)
        os.chmod(self.socket_path, 0o600)
        listener.listen(protocol.ATTACH_MAX_CLIENTS + 4)
        listener.setblocking(False)
        self._listener = listener

    def _socket_answers(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def _head_argv(self) -> list[str]:
        identity = {"run_id": self.run_id, "role": self.role, "task": self.task}
        wrapped = with_pid_heartbeat(self.command, str(self.pid_file), identity=identity)
        return ["/bin/sh", "-c", wrapped]

    def start_head(self) -> int:
        """`pty.fork` the head: new session, controlling pty, and the launch identity it writes.

        Everything this process holds is already close-on-exec, so the head's `exec` drops the
        socket, the lock and the journal rather than carrying a copy of them into a process that
        would keep them open after the supervisor died.
        """
        argv = self._head_argv()
        environment = dict(os.environ)
        environment["TERM"] = self.term
        pid, master = pty.fork()
        if pid == 0:  # pragma: no cover - the child never returns to the test process
            try:
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH, signal.SIGHUP):
                    signal.signal(number, signal.SIG_DFL)
                os.execvpe(argv[0], argv, environment)
            except BaseException:
                os._exit(127)
        self._head_pid = pid
        self._master = master
        os.set_blocking(master, False)
        os.set_inheritable(master, False)
        self.set_winsize(self.rows, self.cols)
        return pid

    def set_winsize(self, rows: int, cols: int) -> None:
        """Set the pty's size. The kernel delivers `SIGWINCH` to the head from here."""
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        if self._master < 0:
            return
        packed = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(self._master, termios.TIOCSWINSZ, packed)

    # -- the loop --------------------------------------------------------------------------

    def run(self) -> int:
        """Own the head until it ends, and say how it ended."""
        self._journal = JournalWriter(self.journal_path, self.run_id).open()
        self._install_signals()
        self.start_head()
        (self.run_dir / protocol.SUPERVISOR_PID_NAME).write_text(f"{os.getpid()}\n", "utf-8")
        self._append(
            RUN_STARTED,
            head_pid=self._head_pid,
            supervisor_pid=os.getpid(),
            command=self.command,
            rows=self.rows,
            cols=self.cols,
            role=self.role,
            task=self.task,
            pid_file=str(self.pid_file),
            socket_path=str(self.socket_path),
            input_limit_bytes=protocol.INPUT_MAX_BYTES,
            output_buffer_bytes=protocol.OUTPUT_BUFFER_BYTES,
            attach_limit=protocol.ATTACH_MAX_CLIENTS,
        )
        assert self._listener is not None
        self._selector.register(self._listener, selectors.EVENT_READ, "listener")
        self._selector.register(self._master, selectors.EVENT_READ, "master")
        self._selector.register(self._wakeup_read, selectors.EVENT_READ, "wakeup")
        try:
            while self._head_status is None:
                for key, mask in self._selector.select(LOOP_TICK_SECONDS):
                    self._dispatch(key, mask)
                self._tick()
            return self._finish()
        finally:
            self._shutdown()

    def _dispatch(self, key: selectors.SelectorKey, mask: int) -> None:
        data = key.data
        if data == "listener":
            self._accept()
        elif data == "master":
            if mask & selectors.EVENT_WRITE:
                self._flush_to_head()
            if mask & selectors.EVENT_READ:
                self._read_head()
        elif data == "wakeup":
            try:
                os.read(self._wakeup_read, 4096)
            except OSError:
                pass
        else:
            client = data
            if mask & selectors.EVENT_WRITE:
                self._flush(client)
            if mask & selectors.EVENT_READ:
                self._read_client(client)

    def _tick(self) -> None:
        now = time.time()
        if self._signalled and not self._stopping:
            self._begin_stop(f"signal:{self._signalled}", signal.SIGTERM)
        if self._turn_open and self._last_output_at and now - self._last_output_at >= self.quiet_seconds:
            self._flush_progress()
            self._append(
                TURN_FINISHED,
                turn=self._turn_id,
                reason="quiet",
                quiet_seconds=self.quiet_seconds,
                output_bytes=self._turn_bytes,
            )
            self._turn_open = False
        elif self._turn_open and self._progress_bytes and now - self._progress_at >= PROGRESS_COALESCE_SECONDS:
            self._flush_progress()
        if self._stopping and self._stop_deadline and now >= self._stop_deadline:
            self._stop_deadline = 0.0
            self._signal_head(signal.SIGKILL)
        self._reap()

    def _reap(self) -> None:
        if self._head_pid <= 0 or self._head_status is not None:
            return
        try:
            pid, status = os.waitpid(self._head_pid, os.WNOHANG)
        except ChildProcessError:
            self._head_status = 0
            return
        if pid == self._head_pid:
            self._head_status = status

    # -- the head's pty --------------------------------------------------------------------

    def _read_head(self) -> None:
        while True:
            try:
                chunk = os.read(self._master, _READ_CHUNK)
            except BlockingIOError:
                return
            except OSError as exc:
                # A pty master reads EIO once the last slave end is gone: that is the head's exit
                # arriving as a read error rather than as an event of its own.
                if exc.errno in (errno.EIO, errno.EBADF):
                    self._master_closed()
                    return
                raise
            if not chunk:
                self._master_closed()
                return
            self._record_output(chunk)

    def _master_closed(self) -> None:
        try:
            self._selector.unregister(self._master)
        except (KeyError, ValueError):
            pass
        # EIO means every slave end of the pty is closed, which is usually the head's exit
        # arriving early. It is not proof of one: a head may close its terminal and keep running,
        # so the exit itself is still taken from `waitpid`, in the tick, when it really happens.
        self._reap()

    def _record_output(self, chunk: bytes) -> None:
        self._output_total += len(chunk)
        self._output += chunk
        if len(self._output) > protocol.OUTPUT_BUFFER_BYTES:
            excess = len(self._output) - protocol.OUTPUT_BUFFER_BYTES
            del self._output[:excess]
            self._output_dropped += excess
        self._last_output_at = time.time()
        if self._turn_open:
            self._turn_bytes += len(chunk)
            self._progress_bytes += len(chunk)
            if not self._progress_at:
                self._progress_at = self._last_output_at
        for client in list(self._clients.values()):
            if client.attached:
                self._push_output(client, chunk)

    def _flush_progress(self) -> None:
        if not self._progress_bytes:
            return
        self._append(
            PROVIDER_PROGRESSED,
            turn=self._turn_id,
            output_bytes=self._progress_bytes,
            total_output_bytes=self._output_total,
            dropped_bytes=self._output_dropped,
        )
        self._progress_bytes = 0
        self._progress_at = 0.0

    def _flush_to_head(self) -> None:
        while self._to_head:
            try:
                written = os.write(self._master, bytes(self._to_head[:_READ_CHUNK]))
            except BlockingIOError:
                break
            except OSError:
                self._to_head.clear()
                break
            del self._to_head[:written]
        self._update_master_interest()

    def _update_master_interest(self) -> None:
        if self._master < 0:
            return
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if self._to_head else 0)
        try:
            self._selector.modify(self._master, events, "master")
        except (KeyError, ValueError):
            pass

    def _signal_head(self, number: int) -> None:
        if self._head_pid <= 0:
            return
        try:
            # The head is its own session and process group leader, so its pid names its group.
            os.killpg(self._head_pid, number)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(self._head_pid, number)
            except OSError:
                pass

    # -- the socket ------------------------------------------------------------------------

    def _accept(self) -> None:
        assert self._listener is not None
        while True:
            try:
                conn, _ = self._listener.accept()
            except BlockingIOError:
                return
            except OSError:
                return
            conn.setblocking(False)
            client = _Client(conn)
            self._clients[conn] = client
            self._selector.register(conn, selectors.EVENT_READ, client)

    def _read_client(self, client: _Client) -> None:
        try:
            data = client.conn.recv(_READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            self._close_client(client)
            return
        if not data:
            self._close_client(client)
            return
        client.inbox += data
        while True:
            index = client.inbox.find(b"\n")
            if index < 0:
                if len(client.inbox) > protocol.FRAME_MAX_BYTES:
                    self._send(
                        client,
                        {
                            "ok": False,
                            "error": protocol.ERROR_FRAME_TOO_LARGE,
                            "limit_bytes": protocol.FRAME_MAX_BYTES,
                            "size_bytes": len(client.inbox),
                            "detail": (
                                f"an unterminated request of at least {len(client.inbox)} bytes "
                                f"exceeds the {protocol.FRAME_MAX_BYTES}-byte frame limit"
                            ),
                        },
                    )
                    client.inbox.clear()
                    client.closing = True
                return
            line = bytes(client.inbox[:index])
            del client.inbox[: index + 1]
            self._handle(client, line)
            if client.closing:
                return

    def _handle(self, client: _Client, line: bytes) -> None:
        try:
            request = protocol.decode_frame(line)
        except protocol.ProtocolError as exc:
            self._send(client, {"ok": False, "error": protocol.ERROR_MALFORMED, "detail": str(exc)})
            return
        op = str(request.get("op") or "")
        handler = {
            protocol.OP_STATUS: self._op_status,
            protocol.OP_INPUT: self._op_input,
            protocol.OP_OUTPUT: self._op_output,
            protocol.OP_ATTACH: self._op_attach,
            protocol.OP_RESIZE: self._op_resize,
            protocol.OP_DRAIN: self._op_drain,
            protocol.OP_STOP: self._op_stop,
        }.get(op)
        if handler is None:
            self._send(
                client,
                {
                    "ok": False,
                    "error": protocol.ERROR_UNKNOWN_OP,
                    "detail": f"unknown op {op!r} (known: {', '.join(protocol.OPS)})",
                },
            )
            return
        try:
            self._send(client, handler(client, request))
        except protocol.ProtocolError as exc:
            self._send(client, {"ok": False, "error": protocol.ERROR_MALFORMED, "detail": str(exc)})

    def _op_status(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client, request
        return {
            "ok": True,
            "run_id": self.run_id,
            "role": self.role,
            "task": self.task,
            "head_pid": self._head_pid,
            "supervisor_pid": os.getpid(),
            "alive": self._head_status is None,
            "draining": self._draining,
            "stopping": self._stopping,
            "turn_open": self._turn_open,
            "turn": self._turn_id,
            "rows": self.rows,
            "cols": self.cols,
            "journal_seq": self._journal.seq if self._journal else 0,
            "output_bytes": self._output_total,
            "dropped_bytes": self._output_dropped,
            "attached": sum(1 for other in self._clients.values() if other.attached),
            "attach_limit": protocol.ATTACH_MAX_CLIENTS,
            "input_limit_bytes": protocol.INPUT_MAX_BYTES,
            "output_buffer_bytes": protocol.OUTPUT_BUFFER_BYTES,
        }

    def _op_input(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        payload = protocol.decode_payload(request.get("data"))
        size = len(payload)
        if size > protocol.INPUT_MAX_BYTES:
            return protocol.input_refusal(size)
        if self._head_status is not None:
            return {"ok": False, "error": protocol.ERROR_HEAD_GONE, "detail": "the head has exited"}
        if self._draining:
            return {
                "ok": False,
                "error": protocol.ERROR_DRAINING,
                "detail": "this head's admission is closed; it takes no further input",
            }
        if len(self._to_head) + size > protocol.INPUT_BACKLOG_MAX_BYTES:
            return protocol.backlog_refusal(size, len(self._to_head))
        self._append(INPUT_ACCEPTED, bytes=size, subject=str(request.get("subject") or ""))
        if not self._turn_open:
            self._turn_id += 1
            self._turn_open = True
            self._turn_bytes = 0
            self._last_output_at = time.time()
            self._append(TURN_STARTED, turn=self._turn_id, subject=str(request.get("subject") or ""))
        self._to_head += payload
        self._flush_to_head()
        return {"ok": True, "accepted_bytes": size, "turn": self._turn_id}

    def _op_output(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        try:
            limit = int(request.get("max_bytes") or protocol.OUTPUT_BUFFER_BYTES)
        except (TypeError, ValueError) as exc:
            raise protocol.ProtocolError("max_bytes is a number") from exc
        limit = max(0, min(limit, protocol.OUTPUT_BUFFER_BYTES))
        tail = bytes(self._output[-limit:]) if limit else b""
        return {
            "ok": True,
            "data": protocol.encode_payload(tail),
            "bytes": len(tail),
            "total_bytes": self._output_total,
            "dropped_bytes": self._output_dropped + max(0, len(self._output) - len(tail)),
            "truncated": self._output_total > len(tail),
            "alive": self._head_status is None,
        }

    def _op_attach(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del request
        if client.attached:
            return {"ok": True, "attached": True, "already": True}
        attached = sum(1 for other in self._clients.values() if other.attached)
        if attached >= protocol.ATTACH_MAX_CLIENTS:
            return {
                "ok": False,
                "error": protocol.ERROR_ATTACH_LIMIT,
                "limit": protocol.ATTACH_MAX_CLIENTS,
                "attached": attached,
                "detail": (
                    f"{attached} callers already hold this head's stream, which is the "
                    f"{protocol.ATTACH_MAX_CLIENTS}-attachment limit"
                ),
            }
        client.attached = True
        return {
            "ok": True,
            "attached": True,
            "data": protocol.encode_payload(bytes(self._output)),
            "dropped_bytes": self._output_dropped,
            "total_bytes": self._output_total,
        }

    def _op_resize(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        try:
            rows = int(request["rows"])
            cols = int(request["cols"])
        except (KeyError, TypeError, ValueError) as exc:
            raise protocol.ProtocolError("a resize names rows and cols") from exc
        if self._head_status is not None:
            return {"ok": False, "error": protocol.ERROR_HEAD_GONE, "detail": "the head has exited"}
        self.set_winsize(rows, cols)
        return {"ok": True, "rows": self.rows, "cols": self.cols}

    def _op_drain(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        initiator = str(request.get("initiator") or "client")
        if not self._draining:
            self._draining = True
            self._append(DRAIN_REQUESTED, initiator=initiator, turn_open=self._turn_open)
        return {"ok": True, "draining": True, "turn_open": self._turn_open}

    def _op_stop(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        initiator = str(request.get("initiator") or "client")
        name = str(request.get("signal") or "TERM").upper()
        number = getattr(signal, f"SIG{name}", None) if not name.startswith("SIG") else getattr(signal, name, None)
        if not isinstance(number, signal.Signals):
            raise protocol.ProtocolError(f"unknown signal {name!r}")
        if self._head_status is not None:
            return {"ok": False, "error": protocol.ERROR_HEAD_GONE, "detail": "the head has exited"}
        self._begin_stop(initiator, number)
        return {"ok": True, "stopping": True, "signal": int(number)}

    def _begin_stop(self, initiator: str, number: int) -> None:
        if self._stopping:
            return
        self._stopping = True
        if not self._draining:
            self._draining = True
            self._append(DRAIN_REQUESTED, initiator=initiator, turn_open=self._turn_open)
        self._append(RUN_STOPPING, initiator=initiator, signal=int(number), turn_open=self._turn_open)
        self._signal_head(number)
        self._stop_deadline = time.time() + STOP_GRACE_SECONDS

    # -- client plumbing -------------------------------------------------------------------

    def _send(self, client: _Client, payload: dict[str, Any]) -> None:
        client.pending += protocol.encode_frame(payload)
        self._flush(client)

    def _push_output(self, client: _Client, chunk: bytes) -> None:
        """Hand an attached client its bytes, or count what a slow client could not take.

        A client that stops reading must not be able to grow the supervisor without bound, and it
        must not be told a partial stream is a whole one: the chunk is dropped, counted, and the
        count is sent as its own event once the client drains.
        """
        if len(client.pending) + len(chunk) > protocol.OUTPUT_BUFFER_BYTES:
            client.dropped += len(chunk)
            client.overflowed = True
            return
        if client.overflowed:
            client.pending += protocol.encode_frame(
                {"event": protocol.EVENT_DROPPED, "bytes": client.dropped}
            )
            client.overflowed = False
        client.pending += protocol.encode_frame(
            {"event": protocol.EVENT_OUTPUT, "data": protocol.encode_payload(chunk)}
        )
        self._flush(client)

    def _flush(self, client: _Client) -> None:
        while client.pending:
            try:
                sent = client.conn.send(bytes(client.pending[:_READ_CHUNK]))
            except BlockingIOError:
                break
            except OSError:
                self._close_client(client)
                return
            del client.pending[:sent]
        if client.closing and not client.pending:
            self._close_client(client)
            return
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if client.pending else 0)
        try:
            self._selector.modify(client.conn, events, client)
        except (KeyError, ValueError):
            pass

    def _close_client(self, client: _Client) -> None:
        """A caller going away is not an event in the head's life: it detaches, nothing else."""
        try:
            self._selector.unregister(client.conn)
        except (KeyError, ValueError):
            pass
        self._clients.pop(client.conn, None)
        try:
            client.conn.close()
        except OSError:
            pass

    # -- ending ----------------------------------------------------------------------------

    def _finish(self) -> int:
        status = int(self._head_status or 0)
        self._flush_progress()
        if self._turn_open:
            self._append(
                TURN_FINISHED, turn=self._turn_id, reason="head_exited", output_bytes=self._turn_bytes
            )
            self._turn_open = False
        exited: dict[str, Any] = {
            "head_pid": self._head_pid,
            "output_bytes": self._output_total,
            "dropped_bytes": self._output_dropped,
            "stopping": self._stopping,
        }
        if os.WIFSIGNALED(status):
            exited["signal"] = os.WTERMSIG(status)
            exited["exit_code"] = None
        else:
            exited["signal"] = None
            exited["exit_code"] = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
        record = self._append(RUN_EXITED, **exited)
        for client in list(self._clients.values()):
            if client.attached:
                client.pending += protocol.encode_frame(
                    {"event": protocol.EVENT_EXITED, "record": record}
                )
            self._flush(client)
        deadline = time.time() + FAREWELL_SECONDS
        while time.time() < deadline and any(c.pending for c in self._clients.values()):
            for _key, _mask in self._selector.select(0.05):
                pass
            for client in list(self._clients.values()):
                self._flush(client)
        return EXIT_OK

    def _shutdown(self) -> None:
        """Let go of everything, in the order that leaves nothing addressable behind."""
        if self._listener is not None:
            try:
                self._selector.unregister(self._listener)
            except (KeyError, ValueError):
                pass
            self._listener.close()
            self._listener = None
        for client in list(self._clients.values()):
            self._close_client(client)
        self.socket_path.unlink(missing_ok=True)
        (self.run_dir / protocol.SUPERVISOR_PID_NAME).unlink(missing_ok=True)
        if self._master >= 0:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = -1
        if self._journal is not None:
            self._journal.close()
            self._journal = None
        self._selector.close()
        if self._lock_fd >= 0:
            os.close(self._lock_fd)
            self._lock_fd = -1

    def _append(self, kind: str, **fields: Any) -> dict[str, Any]:
        assert self._journal is not None
        return self._journal.append(kind, **fields)

    def _install_signals(self) -> None:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        os.set_inheritable(read_fd, False)
        os.set_inheritable(write_fd, False)
        self._wakeup_read, self._wakeup_write = read_fd, write_fd
        signal.set_wakeup_fd(write_fd)

        def remember(number: int, _frame: Any) -> None:
            self._signalled = number

        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, remember)


def _live_head(pid_file: Path, run_id: str) -> int:
    """The pid of this run's head when it is still running, and 0 otherwise.

    Deliberately the same three-part test the watchdog applies: a live pid alone means nothing
    after a reboot or a pid recycle, so the boot id and the process start ticks have to agree too.
    """
    try:
        record = json.loads(pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(record, dict) or str(record.get("run_id") or "") != run_id:
        return 0
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    close = stat.rfind(")")
    fields = stat[close + 2:].split()
    if close < 0 or len(fields) <= 19:
        return 0
    if fields[0] == "Z":
        return 0
    if str(record.get("boot_id") or "") != boot_id:
        return 0
    if str(record.get("proc_starttime_ticks") or "") != fields[19]:
        return 0
    return pid


def _write_startup_error(run_dir: Path, reason: str, detail: str) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / protocol.STARTUP_ERROR_NAME).write_text(
            json.dumps({"reason": reason, "detail": detail}, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-pty-supervisor", description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("--term", default="xterm-256color")
    parser.add_argument("--quiet-seconds", type=float, default=TURN_QUIET_SECONDS)
    parser.add_argument(
        "--daemonize",
        action="store_true",
        help=(
            "fork once and let the launching process reap the intermediate immediately, so the "
            "supervisor is never a child of the tick that started it"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = Path(args.run_dir)
    if run_dir.exists():
        (run_dir / protocol.STARTUP_ERROR_NAME).unlink(missing_ok=True)
    if args.daemonize and os.fork() != 0:
        # The intermediate exits at once. Its parent reaps it, and the supervisor below is
        # reparented to init: addressable through its socket and its pid file, owned by nobody.
        os._exit(EXIT_OK)
    if args.cwd:
        os.chdir(args.cwd)
    supervisor = Supervisor(
        run_dir=run_dir,
        run_id=args.run_id,
        role=args.role,
        task=args.task,
        command=args.command,
        rows=args.rows,
        cols=args.cols,
        term=args.term,
        quiet_seconds=args.quiet_seconds,
    )
    try:
        supervisor.claim()
    except (SupervisorStartupError, OSError) as exc:
        reason = getattr(exc, "reason", START_FAILED)
        _write_startup_error(run_dir, reason, str(exc))
        print(f"supervisor startup refused ({reason}): {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", EXIT_STARTUP_FAILED)
    # A failure after this point is not a startup failure: the run directory already says the head
    # is up, and the traceback belongs in the supervisor's log where the launcher reports it from.
    return supervisor.run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
