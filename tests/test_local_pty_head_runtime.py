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
    DRAIN_AFTER_PARTIAL_DELIVERY,
    OBSERVE_HEAD_EXITED,
    OBSERVE_NO_RUN_DIRECTORY,
    OBSERVE_SUPERVISOR_UNREACHABLE,
    STOP_ACTIVITY_SINCE,
    STOP_TURN_IN_FLIGHT,
    AttachedStream,
    DeliveryReport,
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
    """Criterion 3 and 4: admission is not arrival, and a stall is never glued over."""

    def _stuck_head(self, **options) -> HeadRun:
        """A head that never reads its terminal, so a payload fills the pty and stops."""
        return self.live_run(
            command=f"{sys.executable} -u {SLOW_READER} --chunk 4096 --pause 600",
            delivery_seconds=options.pop("delivery_seconds", 1.0),
            **options,
        )

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

    def test_a_delivery_that_stalled_is_not_an_ok_receipt_with_a_delivery_on_it(self) -> None:
        """The wound this card exists not to re-open, one layer above where it was closed."""
        run = self._stuck_head()

        receipt = self.deliver_line(run, "x" * (protocol.INPUT_MAX_BYTES - 1))

        self.assertFalse(receipt.ok, "a stalled delivery reported as a success")
        self.assertFalse(receipt.arrived)
        self.assertEqual(receipt.status, HEAD_ALIVE)
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
        """
        run = self.live_run()
        lease = self.runtime.activity.grant(run.run_id, "a delivery that landed nothing")

        receipt = self.runtime._unfinished_delivery(  # noqa: SLF001 - the branch under test
            run,
            DeliveryReport(state=protocol.DELIVERY_STALLED, written=0, offered=64),
            lease,
            self.runtime.activity.epoch(run.run_id),
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertIsNone(receipt.lease, "no turn was started, so none is held")
        self.assertTrue(self.runtime.activity.admits(run.run_id), "the head was closed for nothing")
        self.assertEqual(self.deliver_line(run, "still fine").status, HEAD_OK)

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
