"""secretary-1465: `LocalPtyHeadRuntime`, against real processes, real ptys and a real socket.

Nothing here is faked, for the same reason nothing in `test_local_pty_supervisor` is: the facts
this backend exists to establish — a delivery that is admitted before it arrives, a head that
stopped reading its terminal, an attachment a caller can drop without hurting the head — are facts
about a kernel, and a fake substrate would settle none of them. Every test starts a real
supervisor, which forks a real trivial child onto a real pty, and every test takes back what it
started on success and on failure alike.

`LocalPtyHeadRuntimeContractTests` is the boundary's own suite from `tests.support.
head_runtime_contract`, run here against this backend and in `test_head_runtime` against the
legacy one. What is in this file beside it is what only this backend can be asked:

  * a delivery that stalls half-way, and the next one *not* being glued to the prefix it left;
  * `attach` being a stream rather than an address, and detaching being harmless;
  * `request_drain` reaching the process that owns the head, with `head_signalled` read back
    rather than assumed;
  * a supervisor that died being classified by the head's launch identity and not by the socket;
  * `stop_if_quiescent` in the order secretary-1462 fixed, with liveness outranking the terminal.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from secretary.dispatcher_watchdog import head_process_status
from tests.support.head_runtime_contract import HeadRuntimeContract
from triggered_agents.runtime.head import (
    EXITED,
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    HeadRun,
    HeadSpec,
    NudgePointer,
    StopInitiator,
    TaskRef,
    TurnLease,
)
from triggered_agents.runtime.head.local_pty import protocol
from triggered_agents.runtime.head.local_pty.client import SupervisorClient
from triggered_agents.runtime.head.local_pty.journal import (
    DRAIN_REQUESTED,
    INPUT_ACCEPTED,
    read_events,
)
from triggered_agents.runtime.local_pty_head import (
    DELIVERY_ARRIVED,
    DELIVERY_LANDED_NOTHING,
    DELIVERY_LEFT_A_PREFIX,
    DELIVERY_STATE_UNKNOWN,
    DELIVERY_STILL_GOING,
    DELIVERY_UNESTABLISHED,
    DRAIN_AFTER_PARTIAL_DELIVERY,
    DRAIN_AFTER_UNESTABLISHED_DELIVERY,
    OBSERVE_HEAD_EXITED,
    OBSERVE_NO_RUN_DIRECTORY,
    OBSERVE_SUPERVISOR_UNREACHABLE,
    STOP_ACTIVITY_SINCE,
    STOP_TURN_IN_FLIGHT,
    AttachedStream,
    LocalPtyHeadRuntime,
    LocalPtyRuntimeError,
)

REPO = Path(__file__).resolve().parents[1]
CHILD = REPO / "tests" / "fixtures" / "local_pty_child.py"
SLOW_READER = REPO / "tests" / "fixtures" / "local_pty_slow_reader.py"
CHILD_COMMAND = f"{sys.executable} -u {CHILD}"
#: A head that keeps running when its terminal is hung up and its supervisor is gone. It is the
#: orphan case: the one a socket that stopped answering must never be reported as a head that ended.
ORPHAN = REPO / "tests" / "fixtures" / "local_pty_orphan.py"
#: A head that reports what its own terminal handed it, record by record. The only witness that
#: can say whether two payloads reached the head as one sentence; every other one is a receipt.
LINE_READER = REPO / "tests" / "fixtures" / "local_pty_line_reader.py"
DEAF_COMMAND = f"{sys.executable} -u {ORPHAN}"
CODEX = HeadSpec(profile_id="codex-worker", adapter="codex", effort="high", codex_mode="tui")
#: Long enough that a mid-turn assertion is never racing the child's own silence, short enough
#: that a test which has to see the turn end waits about two seconds for it.
TURN_SECONDS = 2.0


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    close = stat.rfind(")")
    fields = stat[close + 2:].split()
    return bool(fields) and fields[0] != "Z"


def _kill(pid: int, number: int = signal.SIGKILL, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, number) if group else os.kill(pid, number)
    except OSError:
        pass


class LocalPtyRuntimeTestCase(unittest.TestCase):
    """A runtime over a temporary run root, and every process it started given back."""

    def setUp(self) -> None:
        # /tmp rather than the workspace: a Unix socket address is bounded at about a hundred
        # bytes and a workspace path with a run id under it does not fit inside one.
        self.root = Path(tempfile.mkdtemp(prefix="lp-runtime-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="lp-workspace-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.addCleanup(self._reap_everything)
        self.runtime = LocalPtyHeadRuntime(self.root, head_process_status=head_process_status)
        self.task = TaskRef.card("secretary-1465", document=f"{self.workspace}/TASK.md")

    # -- processes -----------------------------------------------------------------------------

    def _pids(self) -> list[tuple[int, int]]:
        """Every (head, supervisor) pair this test's run root knows about."""
        pairs = []
        for run_dir in sorted(self.root.glob("*")):
            if not run_dir.is_dir():
                continue
            pairs.append((self._head_pid(run_dir), self._supervisor_pid(run_dir)))
        return pairs

    def _head_pid(self, run_dir: Path) -> int:
        try:
            record = json.loads((run_dir / protocol.PID_FILE_NAME).read_text(encoding="utf-8"))
            return int(record.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            return 0

    def _supervisor_pid(self, run_dir: Path) -> int:
        try:
            return int((run_dir / protocol.SUPERVISOR_PID_NAME).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _reap_everything(self) -> None:
        for head, supervisor in self._pids():
            _kill(head, group=True)
            _kill(head)
            _kill(supervisor)
        for head, _supervisor in self._pids():
            self._await(lambda pid=head: not _alive(pid), timeout=5.0, soft=True)

    def _await(self, predicate, *, timeout: float = 15.0, message: str = "", soft: bool = False):
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

    # -- the runtime ---------------------------------------------------------------------------

    def bring_up(self, *, pointer: NudgePointer | None = None, command: str = CHILD_COMMAND,
                 **options):
        receipt = self.runtime.start(
            CODEX,
            str(self.workspace),
            self.task,
            command=command,
            title="secretary-1465 worker",
            pointer=pointer,
            role="worker",
            quiet_seconds=options.pop("quiet_seconds", 0.4),
            **options,
        )
        return receipt

    def live_run(self, **options) -> HeadRun:
        receipt = self.bring_up(**options)
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.run

    def deliver_line(self, run: HeadRun, text: str, *, subject: str = ""):
        return self.runtime.deliver(run, NudgePointer.line(text), subject=subject)

    def begin_turn(self, run: HeadRun, *, subject: str = "") -> TurnLease:
        receipt = self.deliver_line(run, f"busy {TURN_SECONDS}", subject=subject)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        return receipt.lease

    def end_turn(self, run: HeadRun) -> None:
        """Wait until the supervisor itself says the turn has closed.

        Deliberately read from the substrate rather than through `observe`: the tests that call
        this go on to make their own observation, and an observation made here would be the one
        that closed the lease.
        """
        address = self.runtime._address(run)  # noqa: SLF001 - the test is the backend's own
        self._await(
            lambda: not self._status(address.socket_path).get("turn_open", True),
            message="the head's turn never closed",
        )

    def _status(self, socket_path: Path) -> dict:
        try:
            with SupervisorClient.connect(socket_path, timeout=5.0) as client:
                return client.status()
        except Exception:  # noqa: BLE001 - a supervisor that cannot be asked answers nothing
            return {}

    def payloads_delivered(self) -> int:
        """Everything every head under this root has actually been given, as its journal counts."""
        total = 0
        for run_dir in sorted(self.root.glob("*")):
            journal = run_dir / protocol.JOURNAL_NAME
            total += len(read_events(journal).of_kind(INPUT_ACCEPTED))
        return total

    def output_of(self, run: HeadRun) -> bytes:
        """What the head has printed, read straight from its supervisor."""
        address = self.runtime._address(run)  # noqa: SLF001 - the test is the backend's own
        try:
            with SupervisorClient.connect(address.socket_path, timeout=5.0) as client:
                return bytes(client.read_output()["bytes_data"])
        except Exception:  # noqa: BLE001 - a supervisor that cannot be asked has printed nothing
            return b""

    def orphan_head(self) -> HeadRun:
        """A head that will outlive its supervisor, waited for until it really will.

        The waiting is the point. `start` returns as soon as the head's shell has `exec`ed, which
        is before the interpreter behind that `exec` has installed anything: a supervisor killed in
        that window hangs the terminal up while `SIGHUP` is still the kernel's default, and the
        head dies of the very thing this fixture exists not to die of. Its first line is what says
        the disposition is in place.
        """
        run = self.live_run(command=DEAF_COMMAND)
        self._await(
            lambda: b"ORPHAN" in self.output_of(run),
            message="the orphan head never said it was up",
        )
        return run

    def adrift_run(self) -> HeadRun:
        return HeadRun(
            run_id="a-head-this-runtime-never-started",
            spec=CODEX,
            workspace=str(self.workspace),
            task_ref=self.task,
            role="worker",
        )

    def events(self, run: HeadRun):
        return read_events(self.root / run.run_id / protocol.JOURNAL_NAME)

    def head_pid_of(self, run: HeadRun) -> int:
        return self._head_pid(self.root / run.run_id)


class LocalPtyHeadRuntimeContractTests(LocalPtyRuntimeTestCase, HeadRuntimeContract):
    """The boundary's own suite, run against a backend made of real processes."""


class LocalPtyStartTests(LocalPtyRuntimeTestCase):
    def test_a_head_comes_up_addressed_by_its_socket_and_carrying_its_launch_identity(self) -> None:
        receipt = self.bring_up()

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        run = receipt.run
        self.assertEqual(run.handle, str(self.root / run.run_id / protocol.SOCKET_NAME))
        self.assertEqual(run.leaf, run.run_id)
        self.assertEqual(run.pid_file, str(self.root / run.run_id / protocol.PID_FILE_NAME))
        identity = json.loads(Path(run.pid_file).read_text(encoding="utf-8"))
        self.assertEqual(identity["run_id"], run.run_id)
        self.assertEqual(
            head_process_status(run.pid_file)["state"], "live-match", "the head is not running"
        )

    def test_a_bring_up_that_cannot_be_made_is_a_receipt_and_not_an_exception(self) -> None:
        """A supervisor refusing to start a second head for a run that already has one."""
        run = self.live_run()
        self.runtime.activity.forget(run.run_id)

        receipt = self.bring_up(run_id=run.run_id, timeout=5.0)

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.status, HEAD_ALIVE, "something is still running under that run id")
        self.assertIsNotNone(receipt.failure, "the refusal travels as the operation's own type")
        self.assertTrue(_alive(self.head_pid_of(run)), "the refused bring-up took the head with it")

    def test_this_runtime_is_not_built_without_the_products_own_liveness_reader(self) -> None:
        """A second pid-file reader invented here is the failure this argument exists to prevent."""
        with self.assertRaises(LocalPtyRuntimeError):
            LocalPtyHeadRuntime(self.root, head_process_status=None)  # type: ignore[arg-type]

    def test_a_bring_up_over_a_head_that_is_running_a_turn_is_a_receipt_not_an_exception(self) -> None:
        run = self.live_run()
        self.begin_turn(run)

        second = self.bring_up(run_id=run.run_id)

        self.assertEqual(second.status, HEAD_BUSY)
        self.assertIsNotNone(second.lease)


class LocalPtyDeliveryTests(LocalPtyRuntimeTestCase):
    """Criterion 3 and 4: admission is not arrival, and a stall is never glued over.

    One test per name in the backend's delivery vocabulary, and every one of them produced by a
    real supervisor writing into a real pty rather than by handing the backend a report it made
    up: arrived, still going, a prefix left behind, nothing landed at all, and a fate that could
    not be established because the supervisor stopped answering after it had admitted the payload.
    The last two are the states that used to be reachable only by argument, which is exactly how
    both of them came to be wrong.
    """

    def _stuck_head(self, **options) -> HeadRun:
        """A head that never reads its terminal, so a payload fills the pty and stops."""
        return self.live_run(
            command=f"{sys.executable} -u {SLOW_READER} --chunk 4096 --pause 600",
            delivery_seconds=options.pop("delivery_seconds", 1.0),
            **options,
        )

    def _slow_head(self, **options) -> HeadRun:
        """A head that reads its terminal steadily, so a large payload takes real time to land."""
        return self.live_run(
            command=f"{sys.executable} -u {SLOW_READER} --chunk 4096 --pause 0.2",
            delivery_seconds=options.pop("delivery_seconds", 60.0),
            **options,
        )

    def _rebuild_runtime(self, **options) -> None:
        """The same backend over the same run root, with the delivery knobs a test needs.

        `delivery_timeout` and `delivery_poll` are how long this runtime is willing to watch a
        delivery and how often it looks; the supervisor's own `delivery_seconds` is a separate
        knob with a separate default, and the branches below are what happens when the two do not
        line up. Nothing about the substrate changes with them.
        """
        self.runtime = LocalPtyHeadRuntime(
            self.root, head_process_status=head_process_status, **options
        )

    def _fill_the_terminal(self, run: HeadRun) -> int:
        """Stall one payload against this head's pty from outside the runtime, and say how much landed.

        Straight at the socket on purpose: it leaves the pty's buffer full and the head's terminal
        carrying a fragment, without the runtime having made the delivery and so without the
        runtime knowing anything about it. That is the only way to reach the next delivery's
        "the kernel took nothing at all" with a real kernel.
        """
        address = self.runtime._address(run)  # noqa: SLF001 - the test is the backend's own
        with SupervisorClient.connect(address.socket_path, timeout=5.0) as client:
            answer = client.send_input(b"x" * (protocol.INPUT_MAX_BYTES - 1))
            self.assertTrue(answer["ok"], answer)
            final = client.wait_for_delivery(answer["delivery"]["id"], timeout=15.0)
        self.assertEqual(final["state"], protocol.DELIVERY_STALLED, final)
        self.assertGreater(final["written_bytes"], 0, "the pty took nothing, so it is not full")
        return int(final["written_bytes"])

    def _stop_supervisor_once(self, run: HeadRun, ready) -> None:
        """Stop this head's supervisor with `SIGSTOP` the moment `ready()` holds, from a thread.

        A supervisor that has admitted a payload and then cannot answer is a real thing — a loaded
        host, an fsync stall on a journal every record of which is fsynced, a process somebody
        stopped — and this makes it happen on purpose instead of waiting for a bad day. What is
        stopped is the supervisor's own process: the socket, the journal, the pty and the head are
        all still exactly what they were.
        """
        supervisor = self._supervisor_pid(self.root / run.run_id)
        self.assertGreater(supervisor, 0, "the run directory names no supervisor")
        self.addCleanup(_kill, supervisor, signal.SIGCONT)

        def watch() -> None:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if ready():
                    _kill(supervisor, signal.SIGSTOP)
                    return
                time.sleep(0.005)

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)

    def _delivery_in_flight(self, run: HeadRun, size: int):
        """A predicate that holds once the substrate is carrying a payload of exactly this size."""
        address = self.runtime._address(run)  # noqa: SLF001 - the test is the backend's own

        def ready() -> bool:
            delivery = self._status(address.socket_path).get("delivery") or {}
            return (
                delivery.get("state") == protocol.DELIVERY_IN_FLIGHT
                and int(delivery.get("size_bytes") or 0) == size
            )

        return ready

    def test_a_delivery_that_arrived_says_so_with_the_numbers_that_prove_it(self) -> None:
        run = self.live_run()

        receipt = self.deliver_line(run, "hello", subject="worker-nudge")

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.arrived)
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_COMPLETE)
        self.assertEqual(receipt.delivered_bytes, receipt.offered_bytes)
        self.assertEqual(receipt.delivered_bytes, len("hello\n"))
        # The journal says the same thing, with the two counts that make a partial arrival
        # impossible to read as a whole one.
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertTrue(accepted["complete"])
        self.assertEqual(accepted["bytes"], accepted["offered_bytes"])
        self.assertTrue(receipt.evidence.journalled, "status and the journal both saw it")
        self.assertEqual(receipt.evidence.outcome, DELIVERY_ARRIVED)

    def test_a_delivery_that_stalled_is_not_an_ok_receipt_with_a_delivery_on_it(self) -> None:
        """The wound this card exists not to re-open, one layer above where it was closed."""
        run = self._stuck_head()

        receipt = self.deliver_line(run, "x" * (protocol.INPUT_MAX_BYTES - 1))

        self.assertFalse(receipt.ok, "a stalled delivery reported as a success")
        self.assertFalse(receipt.arrived)
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.evidence.outcome, DELIVERY_LEFT_A_PREFIX)
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_STALLED)
        self.assertGreater(receipt.delivered_bytes, 0, "nothing landed, so nothing stalled")
        self.assertLess(receipt.delivered_bytes, receipt.offered_bytes)
        self.assertIsNone(receipt.delivery, "there is no delivery outcome for a payload that stuck")
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertFalse(accepted["complete"])
        self.assertEqual(accepted["bytes"], receipt.delivered_bytes)

    def test_the_next_payload_is_never_glued_to_the_prefix_a_stall_left(self) -> None:
        run = self._stuck_head()
        stalled = self.deliver_line(run, "x" * (protocol.INPUT_MAX_BYTES - 1))
        self.assertEqual(stalled.delivery_state, protocol.DELIVERY_STALLED)
        landed = self.payloads_delivered()

        second = self.deliver_line(run, "and now this")

        self.assertEqual(second.status, HEAD_DRAINING, second.reason)
        self.assertIn(DRAIN_AFTER_PARTIAL_DELIVERY, second.reason)
        self.assertEqual(self.payloads_delivered(), landed, "a second payload was written anyway")
        # And it is not only this runtime's bookkeeping: the head's own supervisor refuses too, so
        # nothing else that can reach the socket can glue a payload onto the fragment either.
        address = self.runtime._address(run)  # noqa: SLF001 - the test is the backend's own
        with SupervisorClient.connect(address.socket_path) as client:
            self.assertTrue(client.status()["draining"])
            refused = client.send_input(b"straight at the socket\n")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], protocol.ERROR_DRAINING)

    def test_a_delivery_of_which_nothing_landed_leaves_the_head_worth_delivering_to(self) -> None:
        """The other side of the same decision, and the reason it is not "any stall is fatal".

        A payload that reached the terminal in part cannot be taken back, so the head is closed.
        A payload of which the kernel took *nothing* left the terminal exactly as it was: there is
        no fragment for a later payload to be glued to, the turn it would have started never
        started, and closing the head over it would be throwing away a working head.

        The zero is produced rather than asserted about: the head's pty is filled from outside
        this runtime first, so the delivery the runtime then makes finds a terminal with no room
        in it and the kernel takes none of it.
        """
        run = self._stuck_head()
        self._fill_the_terminal(run)

        receipt = self.deliver_line(run, "not one byte of this can land")

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.evidence.outcome, DELIVERY_LANDED_NOTHING)
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_STALLED)
        self.assertEqual(receipt.delivered_bytes, 0, "something landed, so this is not the case")
        self.assertEqual(receipt.offered_bytes, len("not one byte of this can land\n"))
        self.assertIsNone(receipt.lease, "no turn was started, so none is held")
        self.assertTrue(self.runtime.activity.admits(run.run_id), "the head was closed for nothing")
        # The head's own journal carries the delivery that landed nothing, with both counts.
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertEqual(accepted["bytes"], 0)
        self.assertEqual(accepted["offered_bytes"], receipt.offered_bytes)
        # And the head is still one this runtime will deliver to: the refusal is not permanent.
        self.assertNotEqual(self.deliver_line(run, "and again").status, HEAD_DRAINING)

    def test_a_delivery_still_in_flight_is_not_a_head_this_runtime_condemns(self) -> None:
        """A delivery that is merely still landing is an answer, never a death sentence.

        The supervisor's delivery bound and this runtime's willingness to watch are two knobs with
        two defaults, and a head that redraws a terminal is exactly where an operator raises the
        first one. When the wait runs out under a delivery that is going perfectly well, the head
        must be left alone: admission open, no drain, nothing in the journal about it, and the
        payload free to finish landing. This is the branch that used to condemn the head.
        """
        self._rebuild_runtime(delivery_timeout=0.5)
        run = self._slow_head()
        text = "x" * (protocol.INPUT_MAX_BYTES - 1)

        receipt = self.deliver_line(run, text)

        self.assertFalse(receipt.ok, "a delivery that has not finished is not a success")
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.evidence.outcome, DELIVERY_STILL_GOING)
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_IN_FLIGHT)
        self.assertLess(receipt.delivered_bytes, receipt.offered_bytes)
        self.assertIsNotNone(receipt.lease, "the head was given something and is working on it")
        # Nothing about the head was closed, here or at the process that owns it.
        self.assertTrue(self.runtime.activity.admits(run.run_id), "a live head was condemned")
        address = self.runtime._address(run)  # noqa: SLF001 - the test is the backend's own
        self.assertFalse(self._status(address.socket_path)["draining"])
        self.assertEqual(self.events(run).of_kind(DRAIN_REQUESTED), ())
        # A second delivery is refused because one turn is running, not because the head is over.
        second = self.deliver_line(run, "behind it")
        self.assertEqual(second.status, HEAD_BUSY, second.reason)
        # And the payload goes on landing until all of it has: the head was never interrupted.
        self._await(
            lambda: (self._status(address.socket_path).get("delivery") or {}).get("state")
            == protocol.DELIVERY_COMPLETE,
            timeout=30.0,
            message="the delivery this runtime stopped watching never finished",
        )
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertTrue(accepted["complete"])
        self.assertEqual(accepted["bytes"], len(text) + 1)
        self.assertTrue(self.runtime.activity.admits(run.run_id), "the head was closed after all")

    def _reporting_head(self, *, pause: float, delivery_seconds: float) -> HeadRun:
        """A head too slow to be delivered to in time, that says what its terminal handed it.

        Both halves are needed. Reading slowly is what makes a large payload stop part-way and
        leave a fragment on the terminal; going on reading is what leaves room in the pty for a
        second payload to be written behind that fragment and taken in with it, in one read. A
        head that stopped reading altogether would keep its terminal full, and nothing could land
        behind the fragment whether this runtime allowed it or not — which would prove nothing.
        """
        run = self.live_run(
            command=f"{sys.executable} -u {LINE_READER} --chunk 4096 --pause {pause:g}",
            delivery_seconds=delivery_seconds,
        )
        self._await(
            lambda: b"UP" in self.output_of(run), message="the head never said it was up"
        )
        return run

    def _terminal_records(self, run: HeadRun) -> list[bytes]:
        """What the head said its own terminal handed it: one record per line or fragment."""
        return [
            line
            for line in self.output_of(run).splitlines()
            if line.startswith(b"LINE ") or line.startswith(b"FRAG ")
        ]

    def test_a_delivery_this_runtime_stopped_watching_cannot_glue_the_next_payload(self) -> None:
        """The head's own terminal, read record by record, is the witness this test believes.

        `DELIVERY_STILL_GOING` is the outcome of a delivery this runtime stopped watching before
        it ended, and the substrate's own bound ends it afterwards and out of sight. When it ends
        as a stall, the supervisor stops refusing input, the turn the stall opened closes on its
        own, and a second payload written then is read by the head as the tail of the first one's
        sentence — the gluing criterion 4 forbids by name, arriving one delivery after the receipt
        that reported the first one.

        So the unfinished delivery is carried on the head and settled before anything else is
        admitted. The receipts below say the second payload was refused; the head's own records
        say the only thing that actually matters, which is that nothing of it ever reached its
        terminal to be glued to anything.
        """
        # A runtime that gives up watching well before the substrate gives up writing. The two
        # knobs are independent by design, and this is the direction that makes the outcome
        # `DELIVERY_STILL_GOING` reachable at all.
        self._rebuild_runtime(delivery_timeout=0.4)
        run = self._reporting_head(pause=0.5, delivery_seconds=1.5)
        journal = self.root / run.run_id / protocol.JOURNAL_NAME
        address = self.runtime._address(run)  # noqa: SLF001 - the test is the backend's own

        first = self.deliver_line(run, "A" * (protocol.INPUT_MAX_BYTES - 1))

        self.assertEqual(first.evidence.outcome, DELIVERY_STILL_GOING)
        self.assertFalse(first.ok, "a delivery that has not finished is not a success")
        # The substrate ends it on its own bound, with a fragment on the terminal, and then goes
        # back to admitting input: nothing at that layer prevents the next payload.
        self._await(
            lambda: bool(read_events(journal).of_kind(INPUT_ACCEPTED)),
            timeout=20.0,
            message="the substrate never ended the delivery this runtime stopped watching",
        )
        accepted = read_events(journal).of_kind(INPUT_ACCEPTED)[-1]
        self.assertEqual(accepted["state"], protocol.DELIVERY_STALLED, accepted)
        self.assertGreater(accepted["bytes"], 0, "nothing landed, so there is no fragment")
        self.assertFalse(accepted["complete"])
        self.assertFalse(self._status(address.socket_path)["draining"], "the substrate refuses")
        # The turn those landed bytes opened closes on its own while the head reads, so the lease
        # is handed back too: this runtime's own register is the only thing left between the next
        # payload and the fragment.
        self.end_turn(run)
        landed = self.payloads_delivered()

        second = self.deliver_line(run, "B" * 40)

        # Settled before anything was admitted: the fragment closes the head here, and the second
        # payload meets that refusal instead of the terminal.
        self.assertEqual(second.status, HEAD_DRAINING, second.reason)
        self.assertIn(DRAIN_AFTER_PARTIAL_DELIVERY, second.reason)
        self.assertEqual(self.payloads_delivered(), landed, "a second payload was offered anyway")
        self.assertFalse(self.runtime.activity.admits(run.run_id))
        self.assertTrue(self._status(address.socket_path)["draining"], "the substrate still admits")
        # And now the head itself, off its own descriptor: it takes in what its terminal kept for
        # it and says what that was. A fragment of the first payload is unavoidable — those bytes
        # cannot be taken back — but nothing of the second may be anywhere in it, and the fragment
        # must still be a fragment: a `LINE` here would be that fragment finished by something.
        self._await(
            lambda: bool(self._terminal_records(run)),
            timeout=20.0,
            message="the head never said what its terminal handed it",
        )
        records = self._terminal_records(run)
        for record in records:
            kinds = record.split(b"kinds=")[1].split(b" ")[0]
            self.assertEqual(kinds, b"A", f"the head was handed something else: {record!r}")
        self.assertTrue(
            all(record.startswith(b"FRAG ") for record in records),
            f"the first payload's line was completed by something: {records!r}",
        )

    def test_a_delivery_this_runtime_stopped_watching_and_that_arrived_costs_the_head_nothing(
        self,
    ) -> None:
        """The other side of settling, and the reason it is not "stopped watching is fatal".

        A delivery this runtime stopped watching usually goes on to arrive whole. Settled, it is
        an arrival like any other: the head keeps its admission, the register is emptied, and the
        next payload is delivered normally.
        """
        self._rebuild_runtime(delivery_timeout=0.5)
        text = "x" * (protocol.INPUT_MAX_BYTES - 1)
        # A head that reads steadily and says so once it has taken the whole payload, so that the
        # second delivery below is made to a terminal this one has provably finished with.
        run = self.live_run(
            command=(
                f"{sys.executable} -u {SLOW_READER} --chunk 4096 --pause 0.1 "
                f"--total {len(text) + 1}"
            ),
            delivery_seconds=60.0,
        )

        first = self.deliver_line(run, text)

        self.assertEqual(first.evidence.outcome, DELIVERY_STILL_GOING)
        self.assertIn(run.run_id, self.runtime._unfinished)  # noqa: SLF001 - the backend's own
        self._await(
            lambda: b"READ " in self.output_of(run),
            timeout=30.0,
            message="the head never took the delivery this runtime stopped watching",
        )
        self.end_turn(run)

        second = self.deliver_line(run, "and now this")

        self.assertEqual(second.status, HEAD_OK, second.reason)
        self.assertTrue(second.arrived)
        self.assertNotIn(run.run_id, self.runtime._unfinished)  # noqa: SLF001 - the backend's own
        self.assertTrue(self.runtime.activity.admits(run.run_id), "an arrival closed the head")

    def test_a_delivery_nobody_could_account_for_is_not_reported_as_one_that_never_started(self) -> None:
        """Admitted, then unanswerable: the one case where the fate cannot be established.

        `send_input` said `ok`, so the payload is the supervisor's and may be on the terminal
        already. When that supervisor then stops answering and its journal has nothing to say
        about the delivery either, the honest report is `unknown` — not `delivered 0 of 0`, which
        is what "refused before anything reached the terminal" means on this boundary and which
        would let the next payload be written straight behind a fragment.
        """
        self._rebuild_runtime(connect_timeout=1.0, delivery_timeout=30.0)
        run = self._stuck_head(delivery_seconds=60.0)
        text = "x" * (protocol.INPUT_MAX_BYTES - 1)
        self._stop_supervisor_once(run, self._delivery_in_flight(run, len(text) + 1))

        receipt = self.deliver_line(run, text)

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.evidence.outcome, DELIVERY_UNESTABLISHED)
        self.assertEqual(receipt.delivery_state, DELIVERY_STATE_UNKNOWN)
        self.assertEqual(receipt.offered_bytes, len(text) + 1, "the payload is reported as offered")
        self.assertEqual(receipt.status, HEAD_ALIVE, "an unanswered socket is not a dead head")
        # Admission is closed rather than left open over bytes that may be sitting on the terminal.
        self.assertFalse(self.runtime.activity.admits(run.run_id))
        landed = self.payloads_delivered()
        second = self.deliver_line(run, "and now this")
        self.assertEqual(second.status, HEAD_DRAINING, second.reason)
        self.assertIn(DRAIN_AFTER_UNESTABLISHED_DELIVERY, second.reason)
        self.assertEqual(self.payloads_delivered(), landed, "a second payload was offered anyway")

    def test_what_the_journal_still_knows_is_read_when_the_supervisor_cannot_be_asked(self) -> None:
        """The other witness. A supervisor that stops answering does not take the facts with it.

        Here the delivery really did end — the journal has its `input.accepted`, with a prefix on
        the terminal — and only the socket is gone. The report is that stall, from the journal,
        with the head closed for the prefix it left; reporting `unknown` here would be throwing
        away a fact that is on this host's disk.
        """
        self._rebuild_runtime(connect_timeout=1.0, delivery_timeout=30.0, delivery_poll=5.0)
        run = self._stuck_head(delivery_seconds=1.0)
        journal = self.root / run.run_id / protocol.JOURNAL_NAME
        self._stop_supervisor_once(run, lambda: bool(read_events(journal).of_kind(INPUT_ACCEPTED)))
        text = "x" * (protocol.INPUT_MAX_BYTES - 1)

        receipt = self.deliver_line(run, text)

        accepted = read_events(journal).of_kind(INPUT_ACCEPTED)[-1]
        self.assertGreater(accepted["bytes"], 0, "nothing landed, so this is a different case")
        self.assertEqual(receipt.evidence.outcome, DELIVERY_LEFT_A_PREFIX)
        self.assertTrue(receipt.evidence.journalled, "the journal was not the witness it is")
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_STALLED)
        self.assertEqual(receipt.delivered_bytes, accepted["bytes"])
        self.assertEqual(receipt.offered_bytes, len(text) + 1)
        self.assertFalse(self.runtime.activity.admits(run.run_id))
        self.assertIn(DRAIN_AFTER_PARTIAL_DELIVERY, self.deliver_line(run, "glue").reason)

    def test_a_payload_over_the_declared_limit_is_refused_and_the_head_is_untouched(self) -> None:
        run = self.live_run()

        receipt = self.deliver_line(run, "x" * (protocol.INPUT_MAX_BYTES + 1))

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(self.payloads_delivered(), 0, "an oversized payload was written anyway")
        self.assertIsNone(receipt.lease, "a refused delivery started no turn")
        self.assertEqual(self.deliver_line(run, "a payload that fits").status, HEAD_OK)


class LocalPtyDrainTests(LocalPtyRuntimeTestCase):
    """Criterion 5: the drain reaches the process that owns the head, and says so truthfully."""

    def test_a_drain_reaches_the_head_s_own_supervisor_and_is_read_back(self) -> None:
        run = self.live_run()

        receipt = self.runtime.request_drain(run, StopInitiator(actor="operator"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertNotEqual(receipt.status, HEAD_UNSUPPORTED, "this backend owns the process")
        self.assertTrue(receipt.draining)
        self.assertTrue(receipt.head_signalled, "the supervisor was told and confirmed it")
        self.assertTrue(self.events(run).of_kind(DRAIN_REQUESTED), "the head's journal says so")

    def test_a_drain_this_runtime_could_not_deliver_says_that_instead_of_claiming_it(self) -> None:
        """`head_signalled` is read back from the supervisor, never assumed from the request."""
        run = self.orphan_head()
        supervisor = self._supervisor_pid(self.root / run.run_id)
        head = self.head_pid_of(run)
        _kill(supervisor)
        self._await(lambda: not _alive(supervisor), message="the supervisor never died")

        receipt = self.runtime.request_drain(run, StopInitiator(actor="operator"))

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(receipt.draining, "this runtime's own gate is still real")
        self.assertFalse(receipt.head_signalled)
        self.assertEqual(
            self.deliver_line(run, "more work").status, HEAD_DRAINING, "the local gate holds"
        )
        _kill(head, group=True)

    def test_a_drain_does_not_interrupt_the_turn_the_head_is_running(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        head = self.head_pid_of(run)

        self.runtime.request_drain(run, StopInitiator(actor="operator"))

        self.assertTrue(_alive(head), "the drain killed the head it was supposed to wind down")
        self.assertIs(self.runtime.observe(run).busy, True, "the running turn was taken away")


class LocalPtyAttachTests(LocalPtyRuntimeTestCase):
    """Criterion 6: a stream rather than an address, bounded, and safe to walk away from."""

    def test_attach_hands_over_the_head_s_own_stream_addressed_by_its_socket(self) -> None:
        run = self.live_run()

        self._await(lambda: b"SIZE" in self.output_of(run), message="the head never printed")

        receipt = self.runtime.attach(run)

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertNotEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertIsInstance(receipt.evidence, AttachedStream)
        self.addCleanup(receipt.evidence.close)
        self.assertEqual(receipt.handle, str(self.root / run.run_id / protocol.SOCKET_NAME))
        self.assertEqual(receipt.evidence.socket_path, receipt.handle)
        self.assertIn(b"SIZE", receipt.evidence.backlog, "the head's output so far came with it")

    def test_an_attached_caller_is_pushed_what_the_head_prints(self) -> None:
        run = self.live_run()
        receipt = self.runtime.attach(run)
        self.addCleanup(receipt.evidence.close)

        self.assertEqual(self.deliver_line(run, "hello").status, HEAD_OK)

        seen = b""
        deadline = time.monotonic() + 10.0
        while b"ECHO hello" not in seen and time.monotonic() < deadline:
            frame = receipt.evidence.client.next_event(timeout=1.0)
            if frame is not None:
                seen += bytes(frame.get("bytes_data") or b"")
        self.assertIn(b"ECHO hello", seen)

    def test_detaching_does_not_touch_the_head_and_loses_none_of_its_output(self) -> None:
        run = self.live_run()
        first = self.runtime.attach(run)
        head = self.head_pid_of(run)

        first.evidence.close()

        self.assertTrue(_alive(head), "a caller walking away took the head with it")
        self.assertEqual(self.deliver_line(run, "after the detach").status, HEAD_OK)
        self._await(
            lambda: b"ECHO after the detach" in self.output_of(run),
            message="the head stopped printing once nobody was attached",
        )
        second = self.runtime.attach(run)
        self.addCleanup(second.evidence.close)
        self.assertIn(
            b"ECHO after the detach",
            second.evidence.backlog,
            "output produced while nobody held the stream was lost",
        )

    def test_attach_is_bounded_by_the_substrate_s_own_limit_and_the_refusal_is_deferred(self) -> None:
        run = self.live_run()
        held = []
        for _ in range(protocol.ATTACH_MAX_CLIENTS):
            receipt = self.runtime.attach(run)
            self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
            held.append(receipt.evidence)
            self.addCleanup(receipt.evidence.close)

        refused = self.runtime.attach(run)

        self.assertEqual(refused.status, HEAD_BUSY)
        self.assertTrue(refused.deferred, "somebody else's stream ending makes this worth retrying")
        self.assertEqual(refused.evidence["error"], protocol.ERROR_ATTACH_LIMIT)
        held[0].close()

        def rejoined() -> bool:
            receipt = self.runtime.attach(run)
            if not receipt.ok:
                return False
            self.addCleanup(receipt.evidence.close)
            return True

        self._await(rejoined, message="the freed slot was never reusable")

    def test_a_live_head_at_the_connection_bound_is_not_reported_as_one_that_ended(self) -> None:
        """The collapse this sprint exists to remove, in the one verb that could still make it.

        Both of the substrate's bounds are refusals worth making again the moment somebody lets
        go. Reporting either of them as `HEAD_GONE` tells a caller that a head which is running,
        answering and merely popular has ended — and what a caller does with a head that has ended
        is open another one beside it.
        """
        run = self.live_run()
        head = self.head_pid_of(run)
        held = []
        for _ in range(protocol.CONNECTION_MAX_CLIENTS):
            client = SupervisorClient.connect(self.root / run.run_id / protocol.SOCKET_NAME)
            held.append(client)
            self.addCleanup(client.close)

        refused = self.runtime.attach(run)

        self.assertEqual(refused.evidence["error"], protocol.ERROR_CONNECTION_LIMIT)
        self.assertEqual(refused.status, HEAD_BUSY, "a live head at a bound is not a head that ended")
        self.assertTrue(refused.deferred, "somebody else letting go makes this worth retrying")
        self.assertTrue(_alive(head))
        held.pop().close()

        def rejoined() -> bool:
            receipt = self.runtime.attach(run)
            if not receipt.ok:
                return False
            receipt.evidence.close()
            return True

        self._await(rejoined, message="the freed connection was never reusable")



class LocalPtyObserveTests(LocalPtyRuntimeTestCase):
    """Criterion 7: built on the journal and the substrate's state, never on a guess."""

    def test_a_head_with_no_run_directory_is_not_a_head_that_is_not_busy(self) -> None:
        receipt = self.runtime.observe(self.adrift_run())

        self.assertEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertEqual(receipt.reason, OBSERVE_NO_RUN_DIRECTORY)
        self.assertIsNone(receipt.busy)

    def test_a_supervisor_that_died_is_not_reported_as_a_head_that_ended(self) -> None:
        """The collapse that opens a replacement beside a head that is still running."""
        run = self.orphan_head()
        supervisor = self._supervisor_pid(self.root / run.run_id)
        head = self.head_pid_of(run)
        _kill(supervisor)
        self._await(lambda: not _alive(supervisor), message="the supervisor never died")
        self.assertTrue(_alive(head), "the head did not outlive its supervisor, so this asks nothing")

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, OBSERVE_SUPERVISOR_UNREACHABLE)
        self.assertIsNone(receipt.busy, "not knowing is not knowing the head is idle")
        self.assertTrue(_alive(head))
        _kill(head, group=True)

    def test_a_head_whose_process_ended_is_gone_and_its_turn_with_it(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        self.assertTrue(self.runtime.activity.busy(run.run_id))
        with SupervisorClient.connect(self.root / run.run_id / protocol.SOCKET_NAME) as client:
            client.stop("test")

        self._await(
            lambda: self.runtime.observe(run).status in (HEAD_GONE,),
            message="the head's exit was never observed",
        )
        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_GONE)
        self.assertEqual(receipt.reason, OBSERVE_HEAD_EXITED)
        self.assertIsNone(receipt.lease, "a dead head runs no turn")
        self.assertIs(receipt.busy, False)

    def test_the_epoch_moves_on_new_output_and_not_on_the_fact_of_looking(self) -> None:
        run = self.live_run()
        self.deliver_line(run, "hello")
        self._await(
            lambda: b"ECHO hello" in self.output_of(run), message="the head never echoed"
        )
        self.end_turn(run)
        printed = self.runtime.observe(run).epoch

        again = self.runtime.observe(run).epoch

        self.assertEqual(again, printed, "looking twice at a quiet head is not activity")
        self.assertEqual(self.deliver_line(run, "again").status, HEAD_OK)
        self._await(lambda: b"ECHO again" in self.output_of(run), message="the head never echoed")
        self.assertGreater(self.runtime.observe(run).epoch, printed)



class LocalPtyStopTests(LocalPtyRuntimeTestCase):
    """Criterion 8: the stop is confirmed against the launch identity, and nothing else."""

    def test_stop_ends_the_process_and_confirms_it_against_the_launch_identity(self) -> None:
        run = self.live_run()
        head = self.head_pid_of(run)

        receipt = self.runtime.stop(run, StopInitiator(actor="operator", reason="rotation"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.lifecycle, EXITED)
        self.assertEqual(receipt.run.stopped_by.actor, "operator")
        self.assertFalse(_alive(head))
        self.assertEqual(head_process_status(run.pid_file)["state"], "dead")

    def test_a_head_mid_turn_is_stopped_unconditionally_when_a_stop_says_so(self) -> None:
        run = self.live_run()
        self.begin_turn(run)

        receipt = self.runtime.stop(run, StopInitiator(actor="reviewer-freeze"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertFalse(_alive(self.head_pid_of(run)))

    def test_a_conditional_stop_refuses_a_head_that_is_mid_turn(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        head = self.head_pid_of(run)

        receipt = self.runtime.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=self.runtime.activity.epoch(run.run_id),
            head_process_alive=True,
        )

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.reason, STOP_TURN_IN_FLIGHT)
        self.assertTrue(_alive(head), "an active turn was interrupted")
        self.assertTrue(self.runtime.activity.admits(run.run_id), "a refusal left it out of service")

    def test_a_conditional_stop_refuses_a_head_that_has_done_something_since(self) -> None:
        run = self.live_run()
        stale = self.runtime.activity.epoch(run.run_id)
        self.deliver_line(run, "hello")

        receipt = self.runtime.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher"),
            expected_activity_epoch=stale,
            head_process_alive=True,
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, STOP_ACTIVITY_SINCE)
        self.assertTrue(_alive(self.head_pid_of(run)))

    def test_a_dead_head_is_rotated_without_the_terminal_being_asked_about_its_turn(self) -> None:
        """Liveness outranks terminal readiness, exactly as it does on the legacy backend.

        The head is killed mid-turn, which takes its supervisor's socket with it, so the probe
        below would answer "I could not ask" — a refusal — for a head that is provably gone. The
        caller's own launch-identity evidence is what decides instead, and the rotation happens.
        """
        run = self.live_run()
        self.begin_turn(run)
        head = self.head_pid_of(run)
        _kill(head, group=True)
        self._await(lambda: not _alive(head), message="the head never died")

        receipt = self.runtime.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=self.runtime.activity.epoch(run.run_id),
            head_process_alive=False,
        )

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.lifecycle, EXITED)
        self.assertFalse(self.runtime.activity.busy(run.run_id))


class TheBackendIsNotWiredInTests(unittest.TestCase):
    """Criterion 10: this card builds a backend. Nothing selects it, and nothing runs on it."""

    def test_the_substrate_has_exactly_one_consumer_and_it_is_this_backend(self) -> None:
        package = REPO / "src" / "triggered_agents" / "runtime" / "head" / "local_pty"
        backend = REPO / "src" / "triggered_agents" / "runtime" / "local_pty_head.py"
        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            if package in path.parents or path == backend:
                continue
            if "local_pty" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "the substrate is reached from outside its one backend")

    def test_nothing_in_the_product_imports_this_backend(self) -> None:
        backend = REPO / "src" / "triggered_agents" / "runtime" / "local_pty_head.py"
        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            if path == backend:
                continue
            if "local_pty_head" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "a card that only builds a backend wired it in")

    def test_the_dispatcher_still_builds_the_legacy_backend_for_every_head(self) -> None:
        source = (REPO / "src" / "secretary" / "dispatcher.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        built = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.endswith("HeadRuntime")
        }
        self.assertEqual(built, {"OrcaLegacyHeadRuntime"})

    def test_no_profile_and_no_registry_names_this_backend(self) -> None:
        offenders = []
        for path in (REPO / "src").rglob("*"):
            if path.is_dir() or path.suffix not in (".yaml", ".yml", ".json", ".toml"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "local-pty" in text or "local_pty" in text:
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "a profile selects a backend this card does not wire in")


if __name__ == "__main__":
    unittest.main()
