"""The process that owns one head: its pty, its socket, its journal and its ending.

The supervisor is the answer to a question the sprint's first card exists to settle — can a
process the product starts itself outlive the dispatcher tick that started it and stay
addressable afterwards. It does four things and refuses to do a fifth:

  * it **starts the head on a pty of its own**, in a new session with the pty as its controlling
    terminal. A signal sent to the dispatcher's process group therefore cannot reach it, and an
    interactive adapter gets the terminal it expects, including `SIGWINCH` when the size changes.
    The terminal is sized and taken out of canonical mode before the head exists, so the head never
    observes a half-configured one;
  * it **holds the head's addressable surface**: a Unix socket at a predictable path, owner-only,
    with bounded input, bounded output and bounded attach. Every bound refuses by name, and no
    request on that socket ever waits for the head: a delivery is admitted or refused on the spot
    and then written by this loop, so a head that has stopped reading its terminal changes what
    `status` says about the delivery and changes nothing about how long anybody is answered in;
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
import traceback
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
#: Indices into the list `termios.tcgetattr` returns, named rather than counted at the call site.
_IFLAG = 0
_LFLAG = 3
_CC = 6

EXIT_OK = 0
EXIT_STARTUP_FAILED = 2
EXIT_ALREADY_RUNNING = 3
#: A run that was up and then lost its supervisor. Distinct from a startup failure on purpose.
EXIT_RUN_FAILED = 4

START_ALREADY_RUNNING = "already_running"
START_FAILED = "startup_failed"
RUN_FAILED = "run_failed"


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


class _Delivery:
    """One admitted payload on its way to the head's terminal, and everything said about it.

    A delivery outlives the request that admitted it. That is the whole point: the socket answers
    "accepted" within its tick, the loop writes the bytes as the head takes them, and how far it
    got is state — reported by `status`, written down in the journal when it ends — rather than a
    caller held on the wire.
    """

    __slots__ = ("id", "payload", "subject", "written", "deadline", "state", "why", "seconds")

    def __init__(self, identifier: int, payload: bytes, subject: str, seconds: float) -> None:
        self.id = identifier
        self.payload = payload
        self.subject = subject
        self.written = 0
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds
        self.state = protocol.DELIVERY_IN_FLIGHT
        self.why = ""

    @property
    def size(self) -> int:
        return len(self.payload)

    @property
    def in_flight(self) -> bool:
        return self.state == protocol.DELIVERY_IN_FLIGHT

    def view(self) -> dict[str, Any]:
        """What a reader is told about this delivery, whether it is running, done or abandoned."""
        return {
            "id": self.id,
            "state": self.state,
            "size_bytes": self.size,
            "written_bytes": self.written,
            "complete": self.state == protocol.DELIVERY_COMPLETE,
            "subject": self.subject,
            "timeout_seconds": self.seconds,
            "detail": protocol.delivery_detail(self.state, self.size, self.written, self.why, self.seconds),
        }


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
        delivery_seconds: float = protocol.INPUT_DELIVERY_SECONDS,
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
        self.delivery_seconds = float(delivery_seconds)

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

        self._output = bytearray()
        self._output_dropped = 0
        self._output_total = 0

        self._turn_open = False
        self._turn_id = 0
        self._turn_bytes = 0
        self._last_output_at = 0.0
        self._progress_bytes = 0
        self._progress_at = 0.0

        self._delivery: _Delivery | None = None
        self._delivery_seq = 0

        self.started = False
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
        listener.listen(protocol.CONNECTION_MAX_CLIENTS + 4)
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
        """Fork the head onto a pty of its own: new session, controlling terminal, launch identity.

        This is `pty.fork` written out rather than called, for one reason: the terminal has to be
        **configured before the head exists**, not after. Its size and its line discipline are
        properties of the pty, so setting them on the slave before the fork means the head cannot
        observe anything else — no window where the first `TIOCGWINSZ` sees 0x0, and no window
        where a payload arrives while the discipline is still the kernel's default.

        Everything this process holds is already close-on-exec, so the head's `exec` drops the
        socket, the lock and the journal rather than carrying a copy of them into a process that
        would keep them open after the supervisor died.
        """
        argv = self._head_argv()
        environment = dict(os.environ)
        environment["TERM"] = self.term
        master, slave = pty.openpty()
        self._prepare_terminal(slave)
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never returns to the test process
            try:
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                for target in (0, 1, 2):
                    os.dup2(slave, target)
                if slave > 2:
                    os.close(slave)
                signal.set_wakeup_fd(-1)
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH, signal.SIGHUP):
                    signal.signal(number, signal.SIG_DFL)
                os.execvpe(argv[0], argv, environment)
            except BaseException:
                os._exit(127)
        os.close(slave)
        self._head_pid = pid
        self._master = master
        os.set_blocking(master, False)
        os.set_inheritable(master, False)
        return pid

    def _prepare_terminal(self, slave: int) -> None:
        """Size the pty and set the line discipline the head inherits, before the head runs.

        The canonical discipline the kernel sets by default is the whole reason this exists. In it
        `N_TTY` buffers a *line*, caps that line at 4095 bytes and **silently discards** everything
        past the cap: the writer is not blocked, is given no `EAGAIN`, and is told nothing. A
        supervisor that declares a 64 KiB input limit on top of that discipline is declaring a
        limit it does not have, and losing the tail of a delivery in silence is precisely the
        legacy wound (`issue:d9d049eaad39d02bbb1e`) this backend exists not to repeat.

        Non-canonical is therefore the substrate's default, and it is what makes the declared limit
        real: the kernel gives back-pressure through `EAGAIN` instead of dropping bytes, so a
        payload of any size up to the limit arrives whole.

        What is turned off, precisely, and what is left on:

          * **off: `ICANON` and every echo flag**, plus `IEXTEN`. These are what buffer, cap and
            re-emit a delivery, and they are what the 4095-byte silent truncation lives in;
          * **off: `IXON`**. Software flow control is not a byte-preserving discipline either: it
            eats `0x11` and `0x13` out of a delivery, and `0x13` additionally stops the head's
            output until a `0x11` arrives. A payload with one stray byte in it would then produce
            a head that answers nothing and looks dead — the exact class of wrong diagnosis this
            sprint exists to remove — so a payload's bytes are never allowed to mean this;
          * **on, deliberately: `ICRNL`**, so a carriage return in a delivery reaches the head as a
            newline. This one *does* rewrite a byte, and it stays because it is what a terminal
            does and what an interactive adapter reading lines expects; a caller that needs a
            literal `0x0D` on the head's terminal cannot have it through this mode. It is the only
            input translation left on;
          * **on: `ISIG`**, so a `^C` in a delivery still interrupts the head, and **`OPOST`**, so
            the head's output keeps the line endings a terminal gives it.

        An interactive adapter that wants a mode of its own sets one for itself; this is the mode
        it inherits until it does, not one imposed on it.
        """
        packed = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, packed)
        attributes = termios.tcgetattr(slave)
        attributes[_IFLAG] &= ~termios.IXON
        attributes[_LFLAG] &= ~(
            termios.ICANON | termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL | termios.IEXTEN
        )
        # With ICANON off a read returns as soon as one byte is there, and never waits on a timer.
        attributes[_CC][termios.VMIN] = 1
        attributes[_CC][termios.VTIME] = 0
        termios.tcsetattr(slave, termios.TCSANOW, attributes)

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
        """Own the head until it ends, and say how it ended.

        Everything after `claim` is inside the one `finally`: a failure on the way up — a pty that
        cannot be opened, a journal that cannot be written — must let go of the socket and the lock
        it already took, so that what a launcher finds is a named refusal rather than debris that
        answers nothing.
        """
        try:
            try:
                self._begin()
            except BaseException:
                # The window between forking the head and finishing the bring-up. Everything the
                # `finally` below does is about letting go, and letting go of a pty master hangs
                # the head up — which is a request, not an ending: a head that ignores `SIGHUP`
                # survives its supervisor's failure, orphaned, holding this run id against the
                # restart that would otherwise take the run over. So the head this process forked
                # and never took ownership of is ended here, by the supervisor that forked it.
                self._abandon_head()
                raise
            while self._head_status is None:
                for key, mask in self._selector.select(LOOP_TICK_SECONDS):
                    self._dispatch(key, mask)
                self._tick()
            return self._finish()
        finally:
            self._shutdown()

    def _begin(self) -> None:
        """Bring the head up and say so, in the order a reader of the run directory needs."""
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
        self.started = True
        assert self._listener is not None
        self._selector.register(self._listener, selectors.EVENT_READ, "listener")
        self._selector.register(self._master, selectors.EVENT_READ, "master")
        self._selector.register(self._wakeup_read, selectors.EVENT_READ, "wakeup")

    def _abandon_head(self) -> None:
        """End a head this supervisor forked and then failed to take ownership of.

        Reached only from a failed `_begin`, where there is no loop to escalate a stop through and
        nobody else who knows the pid: the process group is signalled, given the grace a stop gets,
        and killed. Reaping it here is what keeps a failed bring-up from leaving a zombie behind
        for init to collect after this process has already written its refusal.
        """
        if self._head_pid <= 0 or self._head_status is not None:
            return
        self._signal_head(signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            self._reap()
            if self._head_status is not None:
                return
            time.sleep(LOOP_TICK_SECONDS)
        self._signal_head(signal.SIGKILL)
        try:
            _pid, status = os.waitpid(self._head_pid, 0)
        except (ChildProcessError, OSError):
            self._head_status = 0
            return
        self._head_status = status

    def _dispatch(self, key: selectors.SelectorKey, mask: int) -> None:
        data = key.data
        if data == "listener":
            self._accept()
        elif data == "master":
            if mask & selectors.EVENT_READ:
                self._read_head()
            if mask & selectors.EVENT_WRITE:
                # The head's terminal has room: put more of the admitted payload into it. Reading
                # first is deliberate — a head blocked writing its own output cannot deadlock
                # against a supervisor with a payload to place.
                self._pump_delivery()
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
        elif (
            self._turn_open and self._progress_bytes and now - self._progress_at >= PROGRESS_COALESCE_SECONDS
        ):
            self._flush_progress()
        # A terminal that never becomes writable raises no event, so the delivery bound is a thing
        # the tick notices rather than a thing the selector reports.
        self._expire_delivery()
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
        self._finish_delivery(
            protocol.DELIVERY_FAILED, "the head's terminal was closed before the delivery finished"
        )
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

    # -- delivery: admitted by the socket, written by the loop -----------------------------

    def _admit(self, payload: bytes, subject: str) -> _Delivery:
        """Take one payload on, and hand it to the loop rather than to the caller's patience."""
        self._delivery_seq += 1
        delivery = _Delivery(self._delivery_seq, payload, subject, self.delivery_seconds)
        self._delivery = delivery
        self._arm_delivery()
        return delivery

    def _arm_delivery(self) -> None:
        """Ask the loop to wake when the head's terminal has room, not on a timer.

        A pty master is writable exactly while its buffer has space, so the delivery advances as
        fast as the head reads and costs nothing at all while the head does not. Watching for it
        this way is what lets the loop keep answering everybody else in between.
        """
        if self._master < 0:
            return
        try:
            self._selector.modify(self._master, selectors.EVENT_READ | selectors.EVENT_WRITE, "master")
        except (KeyError, ValueError):
            pass

    def _disarm_delivery(self) -> None:
        if self._master < 0:
            return
        try:
            self._selector.modify(self._master, selectors.EVENT_READ, "master")
        except (KeyError, ValueError):
            pass

    def _pump_delivery(self) -> None:
        """Write as much of the admitted payload as the terminal will take right now, and no more.

        Every call is bounded by the kernel's own back-pressure: writes continue while they succeed
        and stop at the first `EAGAIN`. Nothing here waits for the head — the loop returns to the
        selector and comes back when the pty says there is room — so a head that reads slowly, or
        not at all, costs the supervisor one non-blocking write attempt per wake-up and costs every
        other caller nothing.
        """
        delivery = self._delivery
        if delivery is None or not delivery.in_flight:
            return
        if self._head_status is not None:
            self._finish_delivery(protocol.DELIVERY_FAILED, "the head exited")
            return
        while delivery.written < delivery.size:
            chunk = delivery.payload[delivery.written : delivery.written + _READ_CHUNK]
            try:
                delivery.written += os.write(self._master, chunk)
            except BlockingIOError:
                break
            except OSError as exc:
                self._finish_delivery(
                    protocol.DELIVERY_FAILED,
                    f"the head's terminal refused the write ({exc.strerror})",
                )
                return
        if delivery.written >= delivery.size:
            self._finish_delivery(protocol.DELIVERY_COMPLETE, "")
            return
        self._expire_delivery()

    def _expire_delivery(self) -> None:
        """Give up on a payload the head has not taken within the delivery bound, and say so.

        The bound belongs to the supervisor's own memory, not to anybody's patience: it decides how
        long a payload for a head that stopped reading is carried before the fact is written down.
        """
        delivery = self._delivery
        if delivery is None or not delivery.in_flight:
            return
        if self._head_status is not None:
            # A pty master stays writable after the head is gone, so the end of a delivery to a
            # dead head is a thing the tick notices rather than one the kernel reports.
            self._finish_delivery(protocol.DELIVERY_FAILED, "the head exited")
            return
        if time.monotonic() >= delivery.deadline:
            self._finish_delivery(protocol.DELIVERY_STALLED, "the head stopped reading its terminal")

    def _finish_delivery(self, state: str, why: str) -> None:
        """Close a delivery out, and write down what actually reached the head's terminal.

        `input.accepted` is written here and only here: its `bytes` is what the kernel took from
        this process, never what a client handed over. That is the honest accounting the previous
        round owed — and it is bought by writing the record when the bytes land, rather than by
        making a caller wait for them to.

        A delivery of which **no** byte landed gets a record too, with `bytes` of zero. It is a
        real thing that happened to a payload this supervisor admitted, and leaving it only in
        `status` — which the next delivery overwrites — meant a run whose journal could not say
        that a delivery had been made at all. What it does not do is open a turn: a head that
        received nothing has not been given anything to work on, and a `turn.started` there would
        be the journal inventing work.

        Retry is not this substrate's business: a delivery that stalled leaves a prefix on the
        terminal that cannot be taken back, and what a caller should do about that belongs to
        `deliver` on the backend built above this, not here.
        """
        delivery = self._delivery
        if delivery is None or not delivery.in_flight:
            return
        delivery.state = state
        delivery.why = why
        self._disarm_delivery()
        self._append(
            INPUT_ACCEPTED,
            bytes=delivery.written,
            offered_bytes=delivery.size,
            complete=state == protocol.DELIVERY_COMPLETE,
            delivery=delivery.id,
            state=state,
            subject=delivery.subject,
            detail=protocol.delivery_detail(state, delivery.size, delivery.written, why, delivery.seconds),
        )
        if delivery.written and not self._turn_open:
            self._turn_id += 1
            self._turn_open = True
            self._turn_bytes = 0
            self._last_output_at = time.time()
            self._append(TURN_STARTED, turn=self._turn_id, subject=delivery.subject)

    def _delivery_view(self) -> dict[str, Any] | None:
        return self._delivery.view() if self._delivery is not None else None

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
            if len(self._clients) >= protocol.CONNECTION_MAX_CLIENTS:
                self._refuse_connection(conn)
                continue
            conn.setblocking(False)
            client = _Client(conn)
            self._clients[conn] = client
            self._selector.register(conn, selectors.EVENT_READ, client)

    def _refuse_connection(self, conn: socket.socket) -> None:
        """Say no to a caller the supervisor will not hold, rather than holding it silently."""
        try:
            conn.settimeout(0.2)
            conn.sendall(protocol.encode_frame(protocol.connection_refusal(len(self._clients))))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

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
        """Answer one request, entirely from state this process already holds.

        Every handler below returns within this call: `status`, `output` and `attach` read state,
        `resize` is one ioctl on a descriptor this process owns, `drain` and `stop` set a flag and
        signal, and `input` admits or refuses. None of them asks the head for anything, so the
        longest a caller waits is one pass of the loop.
        """
        try:
            request = protocol.decode_frame(line)
        except protocol.ProtocolError as exc:
            # Bytes this malformed have no id to answer with, so the frame carries none: a client
            # reading it can see it is uncorrelated rather than mistake it for its own answer.
            self._send(client, {"ok": False, "error": protocol.ERROR_MALFORMED, "detail": str(exc)})
            return
        request_id = request.get(protocol.REQUEST_ID)
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
            self._answer(
                client,
                request_id,
                {
                    "ok": False,
                    "error": protocol.ERROR_UNKNOWN_OP,
                    "detail": f"unknown op {op!r} (known: {', '.join(protocol.OPS)})",
                },
            )
            return
        try:
            self._answer(client, request_id, handler(client, request))
        except protocol.ProtocolError as exc:
            self._answer(
                client,
                request_id,
                {"ok": False, "error": protocol.ERROR_MALFORMED, "detail": str(exc)},
            )

    def _answer(self, client: _Client, request_id: Any, payload: dict[str, Any]) -> None:
        """Send one answer, carrying back the id of the question it answers.

        Without this a response is identified only by its position in the stream, and a caller that
        stopped waiting for one leaves it to be mistaken for the answer to its next question. The
        id makes that mistake impossible to make silently.
        """
        if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
            payload = {**payload, protocol.REQUEST_ID: request_id}
        self._send(client, payload)

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
            "delivery": self._delivery_view(),
            "rows": self.rows,
            "cols": self.cols,
            "journal_seq": self._journal.seq if self._journal else 0,
            "output_bytes": self._output_total,
            "dropped_bytes": self._output_dropped,
            "attached": sum(1 for other in self._clients.values() if other.attached),
            "attach_limit": protocol.ATTACH_MAX_CLIENTS,
            "connections": len(self._clients),
            "connection_limit": protocol.CONNECTION_MAX_CLIENTS,
            "input_limit_bytes": protocol.INPUT_MAX_BYTES,
            "output_buffer_bytes": protocol.OUTPUT_BUFFER_BYTES,
        }

    def _op_input(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        """Admit one payload, or refuse it by name. Either way, within this tick.

        Admission is the whole of what this handler decides, and it decides it from state the
        supervisor already holds: whether the head is gone, whether admission is closed, whether a
        payload is already in flight, and whether this one is inside the declared limit. None of
        those questions is about how fast the head reads its terminal, which is why none of them
        can make a caller wait on it. `ok` here means *accepted*, and the answer says so; what
        happened to the bytes afterwards is `status`'s `delivery` and the journal's
        `input.accepted`, both of which count what the terminal actually took.
        """
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
        if self._delivery is not None and self._delivery.in_flight:
            return protocol.in_flight_refusal(self._delivery.view())
        delivery = self._admit(payload, str(request.get("subject") or ""))
        return {
            "ok": True,
            "accepted": True,
            "accepted_bytes": size,
            "delivery": delivery.view(),
            "turn": self._turn_id,
        }

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
        number = (
            getattr(signal, f"SIG{name}", None) if not name.startswith("SIG") else getattr(signal, name, None)
        )
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
        self._announce_dropped(client)
        client.pending += protocol.encode_frame(
            {"event": protocol.EVENT_OUTPUT, "data": protocol.encode_payload(chunk)}
        )
        self._flush(client)

    def _announce_dropped(self, client: _Client) -> None:
        """Tell an attached client what it missed, before anything else it is told.

        A count that is only ever sent alongside the next chunk that fits is a count that is lost
        when the overflow happens on the last one, so the same notice is emitted here from the
        stream's end as well as from the middle of it.
        """
        if not client.overflowed:
            return
        client.pending += protocol.encode_frame({"event": protocol.EVENT_DROPPED, "bytes": client.dropped})
        client.overflowed = False

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
        self._finish_delivery(protocol.DELIVERY_FAILED, "the head exited before the delivery finished")
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
                self._announce_dropped(client)
                client.pending += protocol.encode_frame({"event": protocol.EVENT_EXITED, "record": record})
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
    fields = stat[close + 2 :].split()
    if close < 0 or len(fields) <= 19:
        return 0
    if fields[0] == "Z":
        return 0
    if str(record.get("boot_id") or "") != boot_id:
        return 0
    if str(record.get("proc_starttime_ticks") or "") != fields[19]:
        return 0
    return pid


def failure_of(started: bool) -> tuple[str, str, int]:
    """Which file a failure is left in, what it is called, and what the supervisor exits with.

    A failure before the run was up and a failure after it are different facts about a run
    directory, and the only honest way to say so is to name them differently: a head that ran for
    an hour and then lost its supervisor did not fail to *start*, and a reader who finds
    `startup.error` beside its journal is being told something untrue.
    """
    if started:
        return protocol.SUPERVISOR_ERROR_NAME, RUN_FAILED, EXIT_RUN_FAILED
    return protocol.STARTUP_ERROR_NAME, START_FAILED, EXIT_STARTUP_FAILED


def _write_failure(run_dir: Path, name: str, reason: str, detail: str) -> None:
    """Leave the reason in the run directory, so a launcher learns it rather than timing out."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_text(
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
    parser.add_argument("--delivery-seconds", type=float, default=protocol.INPUT_DELIVERY_SECONDS)
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
        (run_dir / protocol.SUPERVISOR_ERROR_NAME).unlink(missing_ok=True)
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
        delivery_seconds=args.delivery_seconds,
    )
    try:
        supervisor.claim()
    except (SupervisorStartupError, OSError) as exc:
        reason = getattr(exc, "reason", START_FAILED)
        _write_failure(run_dir, protocol.STARTUP_ERROR_NAME, reason, str(exc))
        print(f"supervisor startup refused ({reason}): {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", EXIT_STARTUP_FAILED)
    try:
        return supervisor.run()
    except Exception as exc:  # noqa: BLE001 - the launcher is owed the reason, whatever it is
        # A launcher still waiting is owed a name for this, or it learns nothing until its own
        # timeout, having been left a socket file that answers nobody. Which name depends on what
        # actually happened: a failure before `run.started` is a run that never came up, and one
        # after it is a run that was up and lost its supervisor. Calling the second a *startup*
        # error would leave a head that worked for an hour described by a file that says it never
        # started, so the two get different names and different exit codes.
        name, reason, code = failure_of(supervisor.started)
        where = "after the run was up" if supervisor.started else "on the way up"
        _write_failure(run_dir, name, reason, f"the supervisor failed {where}: {exc!r}")
        traceback.print_exc()
        return code


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
