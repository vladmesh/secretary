"""The local-pty substrate, exercised against real processes, real ptys and a real socket.

Nothing here is faked. The card this suite belongs to exists to settle one architectural
uncertainty — whether a process the product starts itself can outlive the dispatcher tick that
started it and stay addressable — and a fake supervisor would settle nothing. So every test starts
a real supervisor, which `pty.fork`s a real child, and every test takes back what it started, on
success and on failure alike.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from secretary.dispatcher_watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_LIVE_MATCH,
    head_process_status,
)
from triggered_agents.runtime.head.local_pty import protocol
from triggered_agents.runtime.head.local_pty.client import (
    HeadHandle,
    LocalPtySpawnError,
    SupervisorClient,
    spawn_head,
)
from triggered_agents.runtime.head.local_pty.journal import (
    DRAIN_REQUESTED,
    EVENT_KINDS,
    INPUT_ACCEPTED,
    JOURNAL_SCHEMA_VERSION,
    PROVIDER_PROGRESSED,
    RUN_EXITED,
    RUN_STARTED,
    RUN_STOPPING,
    TURN_FINISHED,
    TURN_STARTED,
    JournalError,
    JournalWriter,
    read_events,
)

REPO = Path(__file__).resolve().parents[1]
CHILD = REPO / "tests" / "fixtures" / "local_pty_child.py"
LAUNCHER = REPO / "tests" / "fixtures" / "local_pty_launcher.py"
CHILD_COMMAND = f"{sys.executable} -u {CHILD}"


def _identity_of(pid: int, run_id: str) -> dict[str, object]:
    """A launch-identity record for a process that really is running, in the shape the head writes."""
    return {
        "version": 1,
        "pid": pid,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
        "proc_starttime_ticks": _proc_field(pid, 22),
        "run_id": run_id,
        "role": "worker",
        "task": "secretary-1463",
    }


def _proc_field(pid: int, index: int) -> str:
    """One field of `/proc/<pid>/stat`, counting the way the kernel numbers them (1-based)."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    close = stat.rfind(")")
    fields = stat[close + 2:].split()
    return fields[index - 3]


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return "Z" not in _proc_field(pid, 3)
    except (OSError, IndexError):
        return False


def _kill(pid: int, number: int = signal.SIGKILL, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, number) if group else os.kill(pid, number)
    except OSError:
        pass


class LocalPtySubstrateTests(unittest.TestCase):
    """Every test owns its processes and its files, and gives all of them back."""

    def setUp(self) -> None:
        # /tmp rather than the workspace: a Unix socket address is bounded at ~100 bytes and a
        # workspace path plus a run id does not fit inside it.
        self.root = Path(tempfile.mkdtemp(prefix="local-pty-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._started: list[HeadHandle] = []
        self.addCleanup(self._reap_everything)

    def _reap_everything(self) -> None:
        for handle in self._started:
            _kill(handle.head_pid, group=True)
            _kill(handle.head_pid)
            _kill(handle.supervisor_pid)
        for handle in self._started:
            self._await(lambda handle=handle: not _alive(handle.head_pid), timeout=5.0, soft=True)

    def _start(self, *, run_id: str = "run", command: str = CHILD_COMMAND, **options) -> HeadHandle:
        handle = spawn_head(
            root=self.root,
            run_id=run_id,
            role="worker",
            task="secretary-1463",
            command=command,
            quiet_seconds=options.pop("quiet_seconds", 0.4),
            **options,
        )
        self._started.append(handle)
        return handle

    def _client(self, handle: HeadHandle) -> SupervisorClient:
        client = handle.connect()
        self.addCleanup(client.close)
        return client

    def _await(self, predicate, *, timeout: float = 5.0, message: str = "", soft: bool = False) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        if predicate():
            return True
        if not soft:
            self.fail(message or f"condition never held within {timeout:g}s")
        return False

    def _await_output(self, client: SupervisorClient, marker: bytes, *, timeout: float = 8.0) -> bytes:
        seen = b""

        def arrived() -> bool:
            nonlocal seen
            seen = client.read_output()["bytes_data"]
            return marker in seen

        self._await(arrived, timeout=timeout, message=f"{marker!r} never appeared in {seen[-400:]!r}")
        return seen

    # -- ownership of the process ----------------------------------------------------------

    def test_the_head_gets_its_own_session_and_the_launchers_group_cannot_signal_it(self) -> None:
        launcher, handle = self._launch_from_another_group(run_id="own-session", linger=30.0)
        self.addCleanup(_kill, launcher.pid, signal.SIGKILL, group=True)

        head_pid = handle.head_pid
        self.assertEqual(_proc_field(head_pid, 6), str(head_pid), "the head leads its own session")
        self.assertEqual(_proc_field(head_pid, 5), str(head_pid), "the head leads its own group")
        self.assertNotEqual(_proc_field(head_pid, 6), _proc_field(launcher.pid, 6))
        self.assertNotEqual(_proc_field(head_pid, 5), _proc_field(launcher.pid, 5))
        self.assertNotEqual(_proc_field(handle.supervisor_pid, 5), _proc_field(launcher.pid, 5))

        os.killpg(launcher.pid, signal.SIGTERM)
        launcher.wait(timeout=10)
        time.sleep(0.4)
        self.assertTrue(_alive(head_pid), "a signal to the launcher's group reached the head")
        self.assertTrue(_alive(handle.supervisor_pid))
        self.assertTrue(self._client(handle).status()["alive"])

    def test_the_supervisor_outlives_the_process_that_started_it(self) -> None:
        launcher, handle = self._launch_from_another_group(run_id="outlives", linger=0.0)
        launcher.wait(timeout=15)
        self.assertFalse(_alive(launcher.pid), "the launcher is gone")

        self.assertTrue(_alive(handle.head_pid), "the head died with its launcher")
        self.assertTrue(_alive(handle.supervisor_pid), "the supervisor died with its launcher")
        self.assertNotIn("Z", _proc_field(handle.supervisor_pid, 3), "the supervisor is a zombie")
        self.assertNotEqual(
            _proc_field(handle.supervisor_pid, 4), str(launcher.pid),
            "the supervisor is still parented to the launcher that exited",
        )
        client = self._client(handle)
        status = client.status()
        self.assertTrue(status["alive"])
        self.assertEqual(status["head_pid"], handle.head_pid)
        self.assertTrue(client.send_input("hello\n")["ok"], "the head is no longer addressable")
        self.assertIn(b"ECHO hello", self._await_output(client, b"ECHO hello"))

    def _launch_from_another_group(self, *, run_id: str, linger: float):
        """Start a head from a process that is neither this one nor in this one's group."""
        handle_file = self.root / f"{run_id}-handle.json"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(REPO / "src"), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        launcher = subprocess.Popen(
            [
                sys.executable, "-P", str(LAUNCHER),
                "--root", str(self.root),
                "--run-id", run_id,
                "--command", CHILD_COMMAND,
                "--handle-file", str(handle_file),
                "--linger", str(linger),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.addCleanup(launcher.wait)
        self.addCleanup(launcher.stdout.close)
        self.addCleanup(_kill, launcher.pid, signal.SIGKILL)
        self._await(
            handle_file.exists, timeout=30.0, message="the launcher never published a handle"
        )
        record = json.loads(handle_file.read_text(encoding="utf-8"))
        handle = HeadHandle(
            run_dir=Path(record["run_dir"]),
            run_id=record["run_id"],
            role=record["role"],
            task=record["task"],
            socket_path=Path(record["socket_path"]),
            journal_path=Path(record["journal_path"]),
            pid_file=Path(record["pid_file"]),
            supervisor_pid=int(record["supervisor_pid"]),
            head_pid=int(record["head_pid"]),
        )
        self._started.append(handle)
        return launcher, handle

    # -- the terminal ----------------------------------------------------------------------

    def test_the_head_runs_on_a_pty_that_can_be_sized_and_resized(self) -> None:
        handle = self._start(run_id="terminal", rows=24, cols=80)
        client = self._client(handle)
        self.assertIn(b"SIZE 24x80", self._await_output(client, b"SIZE 24x80"))

        self.assertEqual(client.resize(41, 132), {"ok": True, "rows": 41, "cols": 132})
        self.assertIn(b"WINCH 41x132", self._await_output(client, b"WINCH 41x132"))

    # -- bounded input ---------------------------------------------------------------------

    def test_an_oversized_input_is_refused_by_name_rather_than_truncated(self) -> None:
        handle = self._start(run_id="input-limit")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")

        payload = b"z" * (protocol.INPUT_MAX_BYTES + 1)
        answer = client.send_input(payload)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"], protocol.ERROR_INPUT_TOO_LARGE)
        self.assertEqual(answer["limit_bytes"], protocol.INPUT_MAX_BYTES)
        self.assertEqual(answer["size_bytes"], len(payload))
        self.assertIn(str(protocol.INPUT_MAX_BYTES), answer["detail"])
        self.assertIn(str(len(payload)), answer["detail"])

        # Nothing of the refused payload reached the head, in whole or in part.
        self.assertTrue(client.send_input("hello\n")["ok"])
        seen = self._await_output(client, b"ECHO hello")
        self.assertNotIn(b"zzzz", seen)
        self.assertEqual(handle.events().of_kind(INPUT_ACCEPTED)[0]["bytes"], 6)

    def test_the_input_limit_is_far_above_the_legacy_path_that_lost_a_nudge(self) -> None:
        # issue:d9d049eaad39d02bbb1e — a continuation nudge did not fit in the legacy 256-byte cap
        # and was silently truncated. A limit is only a real answer to that if a continuation
        # payload fits inside it with room to spare.
        self.assertGreaterEqual(protocol.INPUT_MAX_BYTES, 64 * 1024)
        self.assertGreater(protocol.FRAME_MAX_BYTES, protocol.INPUT_MAX_BYTES)
        handle = self._start(run_id="big-input")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        payload = ("continuation " * 800)[:8192]
        self.assertTrue(client.send_input(payload + "\n")["ok"])
        self.assertIn(b"ECHO continuation", self._await_output(client, b"ECHO continuation"))

    def test_a_frame_without_an_end_is_refused_with_its_limit_and_its_size(self) -> None:
        handle = self._start(run_id="frame-limit")
        # A foreign client, spoken to the socket directly: the bound belongs to the supervisor, not
        # to the convenience of this repository's own client class.
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        raw.settimeout(5.0)
        raw.connect(str(handle.socket_path))
        raw.sendall(b"{" + b"a" * (protocol.FRAME_MAX_BYTES + 16))
        answer = json.loads(raw.recv(65536).split(b"\n")[0])
        self.assertEqual(answer["error"], protocol.ERROR_FRAME_TOO_LARGE)
        self.assertEqual(answer["limit_bytes"], protocol.FRAME_MAX_BYTES)
        self.assertGreater(answer["size_bytes"], protocol.FRAME_MAX_BYTES)
        # The head is untouched by a client that misbehaved.
        self.assertTrue(_alive(handle.head_pid))
        self.assertTrue(self._client(handle).status()["alive"])

    # -- bounded output --------------------------------------------------------------------

    def test_output_is_bounded_and_the_reader_is_told_what_was_dropped(self) -> None:
        handle = self._start(run_id="output-bound")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        lines = protocol.OUTPUT_BUFFER_BYTES // 1000 + 200
        self.assertTrue(client.send_input(f"spew {lines}\n")["ok"])
        self._await_output(client, b"SPEWDONE", timeout=30.0)

        answer = client.read_output()
        self.assertLessEqual(answer["bytes"], protocol.OUTPUT_BUFFER_BYTES)
        self.assertTrue(answer["truncated"])
        self.assertGreater(answer["dropped_bytes"], 0)
        self.assertGreater(answer["total_bytes"], answer["bytes"])
        tail = answer["bytes_data"]
        self.assertIn(b"SPEWDONE", tail, "the reader did not get the freshest tail")
        self.assertNotIn(b"SIZE 24x80", tail, "the oldest output survived a bounded buffer")

    # -- bounded attach --------------------------------------------------------------------

    def test_attach_is_bounded_and_detaching_neither_kills_the_head_nor_loses_its_output(self) -> None:
        handle = self._start(run_id="attach")
        commander = self._client(handle)
        self._await_output(commander, b"SIZE ")

        attached = []
        for _ in range(protocol.ATTACH_MAX_CLIENTS):
            client = handle.connect()
            self.addCleanup(client.close)
            answer = client.attach()
            self.assertTrue(answer["ok"], answer)
            attached.append(client)

        surplus = handle.connect()
        self.addCleanup(surplus.close)
        refusal = surplus.attach()
        self.assertFalse(refusal["ok"])
        self.assertEqual(refusal["error"], protocol.ERROR_ATTACH_LIMIT)
        self.assertEqual(refusal["limit"], protocol.ATTACH_MAX_CLIENTS)
        self.assertEqual(refusal["attached"], protocol.ATTACH_MAX_CLIENTS)

        self.assertTrue(commander.send_input("first\n")["ok"])
        for client in attached:
            self._await_event(client, b"ECHO first")

        # A caller leaving is not an event in the head's life.
        for client in attached[1:]:
            client.close()
        self._await(
            lambda: commander.status()["attached"] == 1,
            message="the supervisor never noticed the detachments",
        )
        self.assertTrue(_alive(handle.head_pid), "detaching killed the head")
        self.assertTrue(commander.send_input("second\n")["ok"])
        self._await_event(attached[0], b"ECHO second")
        self.assertIn(b"ECHO second", self._await_output(commander, b"ECHO second"))

        # The attach slots the departed clients held are given back, not leaked.
        self.assertTrue(surplus.attach()["ok"])

    def _await_event(self, client: SupervisorClient, marker: bytes, *, timeout: float = 8.0) -> None:
        seen = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = client.next_event(0.5)
            if event is None:
                continue
            seen += event.get("bytes_data") or b""
            if marker in seen:
                return
        self.fail(f"{marker!r} never arrived on the attached stream; saw {bytes(seen[-300:])!r}")

    # -- the socket itself -----------------------------------------------------------------

    def test_the_socket_is_owner_only_at_a_predictable_path(self) -> None:
        handle = self._start(run_id="socket")
        self.assertEqual(handle.socket_path, self.root / "socket" / protocol.SOCKET_NAME)
        self.assertEqual(handle.socket_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(handle.run_dir.stat().st_mode & 0o777, 0o700)

    def test_a_second_start_over_a_live_run_is_refused_rather_than_doubling_the_head(self) -> None:
        handle = self._start(run_id="double")
        with self.assertRaises(LocalPtySpawnError) as caught:
            spawn_head(
                root=self.root,
                run_id="double",
                role="worker",
                task="secretary-1463",
                command=CHILD_COMMAND,
                timeout=10.0,
            )
        self.assertEqual(caught.exception.reason, "already_running")
        self.assertEqual(
            len(handle.events().of_kind(RUN_STARTED)), 1, "a second head came up for one run"
        )
        self.assertTrue(_alive(handle.head_pid))

    def test_a_supervisor_killed_outright_hangs_its_head_up_and_leaves_only_debris(self) -> None:
        handle = self._start(run_id="orphan")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        client.close()

        _kill(handle.supervisor_pid, signal.SIGKILL)
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor survived")
        self.assertTrue(handle.socket_path.exists(), "the debris this test is about is missing")
        # Closing the last master end of a pty hangs the terminal up, and the kernel sends SIGHUP
        # to the head's foreground group. So a killed supervisor does not leave a head running
        # behind it: the ownership is real in both directions.
        self._await(
            lambda: not _alive(handle.head_pid),
            timeout=10.0,
            message="the head survived its supervisor being killed",
        )

        restarted = self._start(run_id="orphan")
        self.assertNotEqual(restarted.head_pid, handle.head_pid)
        self.assertEqual(restarted.socket_path.stat().st_mode & 0o777, 0o600)
        events = restarted.events()
        self.assertTrue(events.ordered)
        self.assertEqual(len(events.of_kind(RUN_STARTED)), 2, "the journal restarted its sequence")

    def test_a_head_of_this_run_that_is_still_alive_refuses_a_second_one(self) -> None:
        """The guard that makes a restart over debris safe, with a real live process behind it.

        Modelled rather than orphaned on purpose: a head that survives its supervisor is exactly
        what the pty hangup above prevents, so the only deterministic way to hold the guard is to
        hand it a launch identity that names a process which really is alive — this one.
        """
        run_dir = protocol.run_dir_for(self.root, "still-alive")
        run_dir.mkdir(parents=True)
        (run_dir / protocol.PID_FILE_NAME).write_text(
            json.dumps(_identity_of(os.getpid(), "still-alive")), encoding="utf-8"
        )
        with self.assertRaises(LocalPtySpawnError) as caught:
            spawn_head(
                root=self.root, run_id="still-alive", role="worker", task="secretary-1463",
                command=CHILD_COMMAND, timeout=10.0,
            )
        self.assertEqual(caught.exception.reason, "already_running")
        self.assertIn(str(os.getpid()), caught.exception.detail)
        self.assertFalse((run_dir / protocol.SOCKET_NAME).exists(), "the refusal bound a socket")
        self.assertEqual(read_events(run_dir / protocol.JOURNAL_NAME).events, ())

    # -- the journal -----------------------------------------------------------------------

    def test_the_journal_is_versioned_sequenced_and_readable_by_another_process(self) -> None:
        handle = self._start(run_id="journal")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        self.assertTrue(client.send_input("hello\n", subject="a nudge")["ok"])
        self._await_output(client, b"ECHO hello")
        self._await(
            lambda: bool(handle.events().of_kind(TURN_FINISHED)),
            message="a quiet head never closed its turn",
        )
        # Read from a process that is not the supervisor, while the supervisor is still alive.
        reader = subprocess.run(
            [sys.executable, "-P", "-c",
             "import sys;sys.path.insert(0, sys.argv[1]);"
             "from triggered_agents.runtime.head.local_pty.journal import read_events;"
             "import json;result=read_events(sys.argv[2]);"
             "print(json.dumps({'kinds': list(result.kinds), 'ordered': result.ordered}))",
             str(REPO / "src"), str(handle.journal_path)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        outside = json.loads(reader.stdout)
        self.assertTrue(outside["ordered"])
        self.assertEqual(
            outside["kinds"][:5],
            [RUN_STARTED, INPUT_ACCEPTED, TURN_STARTED, PROVIDER_PROGRESSED, TURN_FINISHED],
        )

        client.drain("observer")
        client.stop("observer")
        self._await(
            lambda: bool(handle.events().of_kind(RUN_EXITED)), message="no run.exited was written"
        )
        result = handle.events()
        self.assertTrue(result.ordered)
        self.assertFalse(result.truncated_tail)
        self.assertEqual(result.malformed, 0)
        self.assertEqual([event["seq"] for event in result.events], list(range(1, len(result.events) + 1)))
        for event in result.events:
            self.assertEqual(event["schema_version"], JOURNAL_SCHEMA_VERSION)
            self.assertEqual(event["run_id"], "journal")
            self.assertIn(event["kind"], EVENT_KINDS)
        self.assertEqual(result.kinds[-3:], (DRAIN_REQUESTED, RUN_STOPPING, RUN_EXITED))

    def test_a_journal_torn_by_sigkill_stays_readable_and_ordered(self) -> None:
        handle = self._start(run_id="torn")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        self.assertTrue(client.send_input("hello\n")["ok"])
        self._await_output(client, b"ECHO hello")

        _kill(handle.supervisor_pid, signal.SIGKILL)
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor survived")
        intact = read_events(handle.journal_path)
        self.assertTrue(intact.ordered)
        self.assertFalse(intact.truncated_tail)
        self.assertGreaterEqual(len(intact.events), 3)

        # A kill *during* a write leaves a record with no newline. Modelling it here is the only
        # way to make the timing deterministic; the property under test is the reader's.
        with open(handle.journal_path, "ab") as journal:
            journal.write(b'{"schema_version":1,"seq":99,"run_id":"torn","kind":"run.exi')
        torn = read_events(handle.journal_path)
        self.assertTrue(torn.truncated_tail)
        self.assertEqual(torn.events, intact.events)
        self.assertTrue(torn.ordered)
        self.assertEqual(torn.malformed, 0)

    def test_the_journal_refuses_a_kind_it_does_not_define(self) -> None:
        path = self.root / "hand-written.jsonl"
        with JournalWriter(path, "hand") as journal:
            journal.append(RUN_STARTED, head_pid=1)
            with self.assertRaises(JournalError):
                journal.append("provider.invented", detail="not a kind")
            with self.assertRaises(JournalError):
                journal.append(RUN_STARTED, seq=4)
        self.assertEqual(read_events(path).kinds, (RUN_STARTED,))

    # -- identity --------------------------------------------------------------------------

    def test_the_head_carries_the_existing_launch_identity_and_the_watchdog_reads_it(self) -> None:
        handle = self._start(run_id="identity")
        record = handle.identity()
        self.assertEqual(record["pid"], handle.head_pid, "the identity belongs to the supervisor")
        for name in ("pid", "boot_id", "proc_starttime_ticks", "run_id", "role", "task"):
            self.assertTrue(str(record.get(name) or ""), f"{name} is missing from the identity")
        self.assertEqual(record["run_id"], "identity")
        self.assertEqual(record["role"], "worker")
        self.assertEqual(record["task"], "secretary-1463")

        expected = {"run_id": "identity", "role": "worker", "task": "secretary-1463"}
        status = head_process_status(str(handle.pid_file), expected=expected)
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
        self.assertEqual(status["pid"], handle.head_pid)

        foreign = head_process_status(
            str(handle.pid_file), expected={**expected, "run_id": "somebody-else"}
        )
        self.assertNotEqual(foreign["state"], HEARTBEAT_LIVE_MATCH)

        client = self._client(handle)
        client.stop("observer")
        self._await(lambda: not _alive(handle.head_pid), message="the head never stopped")
        self.assertEqual(
            head_process_status(str(handle.pid_file), expected=expected)["state"], HEARTBEAT_DEAD
        )

    # -- endings ---------------------------------------------------------------------------

    def test_a_heads_own_exit_is_recorded_with_its_code_and_frees_the_socket(self) -> None:
        handle = self._start(run_id="exit-code")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        self.assertTrue(client.send_input("exit 7\n")["ok"])

        self._await(
            lambda: bool(handle.events().of_kind(RUN_EXITED)), timeout=15.0,
            message="the supervisor never recorded the head's exit",
        )
        exited = handle.events().of_kind(RUN_EXITED)[-1]
        self.assertEqual(exited["exit_code"], 7)
        self.assertIsNone(exited["signal"])
        self.assertFalse(exited["stopping"], "an exit of the head's own is not a stop")
        self.assertEqual(exited["head_pid"], handle.head_pid)

        self._await(
            lambda: not _alive(handle.supervisor_pid), timeout=15.0,
            message="the supervisor held the socket after its head died",
        )
        self.assertFalse(handle.socket_path.exists(), "a dead head left an address behind")
        self.assertFalse((handle.run_dir / protocol.SUPERVISOR_PID_NAME).exists())

    def test_a_stop_is_recorded_as_a_signal_and_the_head_is_not_left_behind(self) -> None:
        handle = self._start(run_id="stopped", command=f"{sys.executable} -u -c "
                             "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_DFL);"
                             "print(\"UP\",flush=True);time.sleep(600)'")
        client = self._client(handle)
        self._await_output(client, b"UP")
        self.assertTrue(client.drain("observer")["ok"])
        self.assertTrue(client.stop("observer")["ok"])

        self._await(
            lambda: bool(handle.events().of_kind(RUN_EXITED)), timeout=15.0,
            message="a stopped head was never reaped",
        )
        exited = handle.events().of_kind(RUN_EXITED)[-1]
        self.assertEqual(exited["signal"], int(signal.SIGTERM))
        self.assertIsNone(exited["exit_code"])
        self.assertTrue(exited["stopping"])
        self.assertEqual(handle.events().of_kind(RUN_STOPPING)[-1]["initiator"], "observer")
        self._await(lambda: not _alive(handle.head_pid), message="the stopped head survived")
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor survived")


class SubstrateIsNotWiredInTests(unittest.TestCase):
    """This card builds a substrate. Nothing may be standing on it yet."""

    def test_no_module_outside_the_package_reaches_for_it(self) -> None:
        package = REPO / "src" / "triggered_agents" / "runtime" / "head" / "local_pty"
        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            if package in path.parents:
                continue
            if "local_pty" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "the substrate is not meant to be wired in by this card")

    def test_the_substrate_implements_none_of_the_six_verbs_as_a_boundary(self) -> None:
        """Prose about `HeadRuntime` is fine; an implementation of it is what this card excludes."""
        package = REPO / "src" / "triggered_agents" / "runtime" / "head" / "local_pty"
        verbs = {"start", "deliver", "observe", "request_drain", "attach"}
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(
                        "runtime", (node.module or "").split("."),
                        f"{path.name} imports the boundary this card does not implement",
                    )
                if isinstance(node, ast.ClassDef):
                    methods = {
                        child.name
                        for child in node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    self.assertFalse(
                        verbs.issubset(methods),
                        f"{path.name}:{node.name} wears the six verbs already",
                    )


if __name__ == "__main__":
    unittest.main()
