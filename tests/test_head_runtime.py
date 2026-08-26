"""secretary-1461: the `HeadRuntime` boundary, run against a fake session manager.

The contract suite of the six verbs, and — like `test_head_operations` beside it — the point is what
it does not need: no Orca, no dispatcher, no installation. `OrcaLegacyHeadRuntime` is driven here
through `FakeSessionHost`, which is possible because the runtime reaches a session manager through
`SessionHost` and nothing else. A second backend is expected to satisfy the same file.

What is pinned here:

  * every verb answers with a receipt, and the four things a receipt can say — it worked, it was
    refused because the head was busy or draining, it was refused with something still alive, it was
    refused and nothing is left — stay distinguishable. The operation's own refusal travels on the
    receipt unchanged, so `HeadSpawnAborted` cannot arrive looking like `HeadSpawnFailed`;
  * `attach` and `request_drain` say `unsupported` on this backend rather than inventing an answer,
    and `observe` says `unsupported` for every question Orca genuinely cannot answer;
  * a turn is a `TurnLease` and an activity epoch held by the runtime, not `HeadRun.working`: a run
    that is durably `working` is not busy once the turn it was given has ended.

secretary-1462 adds the order to it, and `SerialisedHeadTests` at the bottom is that: delivery,
drain and stop for one head are serialised by the runtime itself, a delivery over a running turn or
a drained head is refused with a value rather than queued, a drain closes admission without touching
the turn, and `stop_if_quiescent` is a check and a stop that cannot be observed apart. Concurrency is
pinned by wedging into the critical section through the fake host — `on_call` fires inside the
session-manager call — never by sleeping and hoping.
"""

from __future__ import annotations

import threading
import unittest
from typing import Any

from tests.fakes.host import FakeSessionHost
from tests.support.head_runtime_contract import HeadRuntimeContract
from triggered_agents.runtime.head import (
    EXITED,
    FINISHING,
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    OBSERVE_INVENTORY_UNREADABLE,
    OBSERVE_NO_ADDRESS,
    OBSERVE_PANE_ABSENT,
    OBSERVE_PANE_DISCONNECTED,
    OBSERVE_READINESS_UNKNOWN,
    WORKING,
    HeadActivity,
    HeadPaneBusy,
    HeadRun,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadSpec,
    HeadStopFailed,
    NudgePointer,
    StopInitiator,
    TaskRef,
    TurnLease,
    TurnLeaseError,
)
from triggered_agents.runtime.head import operations as head_operations
from triggered_agents.runtime.orca_legacy_head import (
    STOP_ACTIVITY_SINCE,
    STOP_TURN_IN_FLIGHT,
    OrcaLegacyHeadRuntime,
)
from triggered_agents.runtime.pane_host import Pane, PaneHostError

CODEX = HeadSpec(profile_id="codex-worker", adapter="codex", effort="high", codex_mode="tui")
WORKSPACE = "/tmp/does-not-need-to-exist/secretary-1461"


def confirmed(_sent_at: float) -> bool:
    return True


CONFIRMING = head_operations.HostTransport(confirm=confirmed)


class RefusingTransport(head_operations.HostTransport):
    """A transport whose delivery never reaches its confirmation, and nothing else changed."""

    def __init__(self, reason: str = "the head never took the prompt") -> None:
        super().__init__(confirm=confirmed)
        object.__setattr__(self, "reason", reason)

    def deliver(self, run, pointer, *, host, subject):
        raise RuntimeError(self.reason)


class ProbeHost(FakeSessionHost):
    """`FakeSessionHost` with the two answers an observation depends on made settable.

    A pane's idle probe and a workspace inventory are the whole of what Orca can say about a head,
    so a suite about `observe` has to be able to make each of them say every thing it can say.
    """

    def __init__(self) -> None:
        super().__init__()
        # None is the probe answering nothing at all, which is `READINESS_UNKNOWN`.
        self.idle: bool | None = True
        self.inventory_error: Exception | None = None
        self.connected = True
        self.last_output_at = 0.0

    def wait_idle(self, handle: str, *, timeout_ms: int):
        self._note("wait_idle")
        if self.idle is None:
            raise RuntimeError("the probe itself failed")
        return {"satisfied": self.idle}

    def panes(self, workspace: str):
        if self.inventory_error is not None:
            self.calls.append(("panes", workspace))
            raise self.inventory_error
        return [
            Pane(
                handle=pane.handle,
                leaf=pane.leaf,
                connected=self.connected,
                last_output_at=self.last_output_at,
            )
            for pane in super().panes(workspace)
        ]


class HeadRuntimeContractTests(unittest.TestCase):
    """Every verb, on the Orca-legacy backend, against a session manager made of dictionaries."""

    def setUp(self) -> None:
        self.host = ProbeHost()
        self.runtime = OrcaLegacyHeadRuntime(self.host)
        self.task = TaskRef.card("secretary-1461", document=f"{WORKSPACE}/TASK.md")

    def bring_up(self, **kwargs):
        kwargs.setdefault("transport", CONFIRMING)
        return self.runtime.start(
            CODEX,
            WORKSPACE,
            self.task,
            command="run-worker",
            title="secretary-1461 worker",
            **kwargs,
        )

    def live_run(self) -> HeadRun:
        receipt = self.bring_up()
        self.assertTrue(receipt.ok)
        return receipt.run

    # The shape of the boundary — that it is six verbs, and that every one of them answers with a
    # receipt — is not asserted here any more. It is not a fact about Orca, and secretary-1465
    # moved it verbatim into `tests.support.head_runtime_contract`, which this file runs against
    # this backend at the bottom and `test_local_pty_head_runtime` runs against the other one.

    # start -----------------------------------------------------------------
    def test_start_brings_a_head_up_through_the_host_and_hands_back_its_run(self) -> None:
        receipt = self.bring_up()

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(self.host.calls[0], ("open_pane", WORKSPACE, "secretary-1461 worker"))
        self.assertEqual(self.host.commands[receipt.run.handle], "run-worker")
        self.assertIsNone(receipt.lease, "no pointer was delivered, so no turn was handed out")
        self.assertEqual(receipt.epoch, 1)

    def test_a_start_that_delivers_a_pointer_hands_the_head_the_turn_it_started(self) -> None:
        receipt = self.bring_up(pointer=NudgePointer.at_document(self.task.document))

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertIsInstance(receipt.lease, TurnLease)
        self.assertEqual(receipt.lease.run_id, receipt.run.run_id)
        self.assertTrue(self.runtime.activity.busy(receipt.run.run_id))

    def test_a_bring_up_that_left_a_live_pane_is_alive_and_keeps_its_run(self) -> None:
        """The distinction the product has killed live heads by collapsing."""
        self.host.refuse_close = True

        receipt = self.bring_up(pointer=NudgePointer.line("report now"), transport=RefusingTransport())

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertIsInstance(receipt.failure, HeadSpawnAborted)
        self.assertEqual(receipt.run.handle, "term:1")
        self.assertEqual(receipt.run.workspace, WORKSPACE)

    def test_a_bring_up_whose_pane_closed_cleanly_left_nothing(self) -> None:
        receipt = self.bring_up(pointer=NudgePointer.line("report now"), transport=RefusingTransport())

        self.assertEqual(receipt.status, HEAD_GONE)
        self.assertIsInstance(receipt.failure, HeadSpawnFailed)
        self.assertNotIsInstance(receipt.failure, HeadSpawnAborted)
        self.assertEqual(self.host.closed, ["term:1"])

    def test_a_pane_that_was_busy_and_never_took_its_prompt_is_worth_retrying(self) -> None:
        self.host.idle = False

        receipt = self.bring_up(pointer=NudgePointer.line("report now"), transport=RefusingTransport())

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertTrue(receipt.deferred)
        self.assertIsInstance(receipt.failure, HeadPaneBusy)
        self.assertEqual(receipt.failure.readiness, "busy")

    def test_a_session_manager_failing_on_its_own_terms_is_not_classified_here(self) -> None:
        """A boundary that invented a classification for it would be guessing about the head."""
        anchor = self.live_run()
        self.host.inventory_error = PaneHostError("orca terminal list failed")

        with self.assertRaises(PaneHostError):
            self.bring_up(split_from=anchor.handle)

    # deliver ---------------------------------------------------------------
    def test_deliver_puts_one_prompt_in_front_of_a_running_head(self) -> None:
        run = self.live_run()

        receipt = self.runtime.deliver(
            run, NudgePointer.line("report now"), transport=CONFIRMING, subject="worker-nudge"
        )

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(self.host.sent[0][0], run.handle)
        self.assertEqual(receipt.run.run_id, run.run_id)
        self.assertEqual(receipt.lease.subject, "worker-nudge")

    def test_a_refused_delivery_leaves_the_head_the_caller_still_owns(self) -> None:
        run = self.live_run().finishing(StopInitiator(actor="watchdog"))

        receipt = self.runtime.deliver(run, NudgePointer.line("report now"), transport=CONFIRMING)

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertIsInstance(receipt.failure, head_operations.HeadNudgeFailed)
        self.assertEqual(self.host.sent, [], "a refused delivery wrote nothing into the pane")

    def test_a_drained_head_is_handed_no_further_work(self) -> None:
        run = self.live_run()
        self.runtime.request_drain(run, StopInitiator(actor="operator"))

        receipt = self.runtime.deliver(run, NudgePointer.line("report now"), transport=CONFIRMING)

        self.assertEqual(receipt.status, HEAD_DRAINING)
        self.assertTrue(receipt.deferred)
        self.assertEqual(self.host.sent, [])

    # observe ---------------------------------------------------------------
    def test_observe_reads_the_pane_and_its_probe(self) -> None:
        run = self.live_run()
        self.host.last_output_at = 1000.0

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(receipt.handle, run.handle)
        self.assertEqual(receipt.readiness, "ready")
        self.assertIs(receipt.connected, True)
        self.assertEqual(receipt.last_output_at, 1000.0)
        self.assertIs(receipt.busy, False)

    def test_a_working_head_is_busy_while_its_pane_is(self) -> None:
        run = self.live_run()
        self.host.idle = False

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(receipt.readiness, "busy")
        self.assertIs(receipt.busy, True)

    def test_a_head_with_no_address_is_not_observable_and_says_so(self) -> None:
        run = HeadRun(run_id="run-adrift", spec=CODEX, workspace="", task_ref=self.task)

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertEqual(receipt.reason, OBSERVE_NO_ADDRESS)
        self.assertIsNone(receipt.busy, "an unobservable head is not a head that is not busy")

    def test_an_unreadable_inventory_is_not_evidence_that_the_pane_is_gone(self) -> None:
        run = self.live_run()
        self.host.inventory_error = PaneHostError("orca terminal list failed")

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertEqual(receipt.reason, OBSERVE_INVENTORY_UNREADABLE)
        self.assertNotEqual(receipt.status, HEAD_GONE)

    def test_a_pane_absent_from_its_workspace_leaves_nothing_of_the_pane(self) -> None:
        run = self.live_run()
        self.host.close_pane(run.handle)

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_GONE)
        self.assertEqual(receipt.reason, OBSERVE_PANE_ABSENT)

    def test_a_disconnected_pane_is_not_an_observation_and_not_a_dead_head(self) -> None:
        run = self.live_run()
        self.host.connected = False

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, OBSERVE_PANE_DISCONNECTED)
        self.assertIs(receipt.connected, False)
        self.assertIsNone(receipt.busy)

    def test_a_probe_that_failed_is_not_a_busy_head_and_not_an_idle_one(self) -> None:
        run = self.live_run()
        self.host.idle = None

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertEqual(receipt.reason, OBSERVE_READINESS_UNKNOWN)
        self.assertIsNone(receipt.busy)

    # the turn lease and the activity epoch ---------------------------------
    def test_the_epoch_grows_when_the_head_is_seen_doing_something(self) -> None:
        run = self.live_run()
        opened = self.runtime.activity.epoch(run.run_id)

        delivered = self.runtime.deliver(run, NudgePointer.line("report now"), transport=CONFIRMING).epoch
        self.host.last_output_at = 2000.0
        printed = self.runtime.observe(run).epoch

        self.assertGreater(delivered, opened)
        self.assertGreater(printed, delivered)

    def test_a_pane_that_keeps_reporting_the_same_clock_has_been_quiet(self) -> None:
        run = self.live_run()
        self.host.last_output_at = 2000.0
        first = self.runtime.observe(run).epoch

        again = self.runtime.observe(run).epoch

        self.assertEqual(again, first, "the same output clock twice is not new activity")

    def test_the_turn_ends_when_the_backend_sees_the_pane_take_input_again(self) -> None:
        run = self.live_run()
        self.host.idle = False
        granted = self.runtime.deliver(run, NudgePointer.line("report now"), transport=CONFIRMING).lease
        self.assertIsInstance(granted, TurnLease)
        self.assertIs(self.runtime.observe(run).busy, True)

        self.host.idle = True
        closed = self.runtime.observe(run)

        self.assertIsNone(closed.lease)
        self.assertIs(closed.busy, False)
        self.assertFalse(self.runtime.activity.busy(run.run_id))

    def test_a_head_that_is_durably_working_is_not_therefore_busy(self) -> None:
        """`HeadRun.working` is the history of a delivery, never a statement about right now."""
        run = self.live_run()
        self.runtime.deliver(run, NudgePointer.line("report now"), transport=CONFIRMING)
        stored = HeadRun.from_json(run.working().to_json())
        self.assertEqual(stored.lifecycle, WORKING)

        after_the_turn = self.runtime.observe(stored)

        self.assertIs(after_the_turn.busy, False)

    def test_a_head_runs_one_turn_at_a_time(self) -> None:
        activity = HeadActivity()
        activity.grant("run-1", "first")

        with self.assertRaises(TurnLeaseError):
            activity.grant("run-1", "second")

        self.assertFalse(
            hasattr(activity, "renew"),
            "a grant that evicts the turn it interrupts is the silent queue this card removed",
        )
        self.assertIsNotNone(activity.release("run-1"))
        self.assertEqual(activity.grant("run-1", "third").subject, "third")
        self.assertIsNotNone(activity.release("run-1"))
        self.assertIsNone(activity.release("run-1"))

    # request_drain ---------------------------------------------------------
    def test_orca_cannot_ask_a_head_to_wind_down_and_the_receipt_says_so(self) -> None:
        run = self.live_run()

        receipt = self.runtime.request_drain(run, StopInitiator(actor="operator"))

        self.assertEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertTrue(receipt.draining, "the runtime's own gate is real")
        self.assertFalse(receipt.head_signalled, "the head itself was told nothing")

    def test_a_drain_names_who_requested_it(self) -> None:
        run = self.live_run()

        with self.assertRaises(TypeError):
            self.runtime.request_drain(run, "operator")  # type: ignore[arg-type]

    # stop ------------------------------------------------------------------
    def test_stop_ends_the_head_and_records_who_ended_it(self) -> None:
        run = self.live_run()

        receipt = self.runtime.stop(
            run, StopInitiator(actor="reviewer-freeze", reason="review took the tree")
        )

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(receipt.run.lifecycle, EXITED)
        self.assertEqual(receipt.run.stopped_by.actor, "reviewer-freeze")
        self.assertEqual(self.host.closed, [run.handle])

    def test_a_stop_that_could_not_be_confirmed_leaves_a_head_the_caller_owns(self) -> None:
        run = self.live_run()
        self.host.refuse_close = True

        receipt = self.runtime.stop(run, StopInitiator(actor="operator"))

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(receipt.left_alive)
        self.assertIsInstance(receipt.failure, HeadStopFailed)
        self.assertEqual(receipt.run.lifecycle, FINISHING)
        self.assertEqual(receipt.run.stopped_by.actor, "operator")

    def test_a_stopped_head_leaves_no_turn_behind_it(self) -> None:
        receipt = self.bring_up(pointer=NudgePointer.at_document(self.task.document))
        run = receipt.run
        self.assertTrue(self.runtime.activity.busy(run.run_id))

        self.runtime.stop(run, StopInitiator(actor="operator"))

        self.assertFalse(self.runtime.activity.busy(run.run_id))

    # attach ----------------------------------------------------------------
    def test_orca_has_no_stream_to_attach_to_and_hands_over_the_address_instead(self) -> None:
        run = self.live_run()
        before = list(self.host.calls)

        receipt = self.runtime.attach(run)

        self.assertEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertTrue(receipt.unsupported)
        self.assertEqual(receipt.handle, run.handle)
        self.assertEqual(receipt.leaf, run.leaf)
        self.assertEqual(self.host.calls, before, "an unsupported verb asks the host nothing")


class SerialisedHeadTests(unittest.TestCase):
    """secretary-1462: one owner, one lock, and no silent queue behind any of the three verbs."""

    def setUp(self) -> None:
        self.host = ProbeHost()
        self.runtime = OrcaLegacyHeadRuntime(self.host)
        self.task = TaskRef.card("secretary-1462", document=f"{WORKSPACE}/TASK.md")
        self.stopper = StopInitiator(actor="dispatcher", reason="rotation")

    def bring_up(self) -> HeadRun:
        receipt = self.runtime.start(
            CODEX,
            WORKSPACE,
            self.task,
            command="run-worker",
            title="secretary-1462 worker",
            transport=CONFIRMING,
        )
        self.assertTrue(receipt.ok)
        return receipt.run

    def working(self) -> tuple[HeadRun, TurnLease]:
        """A head that is mid-turn: it took a prompt and its pane is working on it."""
        run = self.bring_up()
        self.host.idle = False
        receipt = self.runtime.deliver(
            run, NudgePointer.line("do the work"), transport=CONFIRMING, subject="worker-nudge"
        )
        self.assertTrue(receipt.ok)
        return run, receipt.lease

    def epoch(self, run: HeadRun) -> int:
        return self.runtime.activity.epoch(run.run_id)

    # delivery is refused, never queued -------------------------------------
    def test_a_delivery_on_top_of_a_running_turn_is_refused_and_never_typed(self) -> None:
        run, lease = self.working()
        typed = len(self.host.sent)

        second = self.runtime.deliver(run, NudgePointer.line("and this too"), transport=CONFIRMING)

        self.assertEqual(second.status, HEAD_BUSY)
        self.assertTrue(second.deferred, "a busy head is worth delivering to again, later")
        self.assertEqual(len(self.host.sent), typed, "nothing was typed on top of the running turn")
        self.assertEqual(second.lease, lease, "the turn that was running is the one still running")

    def test_a_delivery_after_a_drain_is_refused_as_draining_and_not_as_busy(self) -> None:
        run = self.bring_up()
        self.runtime.request_drain(run, StopInitiator(actor="operator"))
        typed = len(self.host.sent)

        refused = self.runtime.deliver(run, NudgePointer.line("more work"), transport=CONFIRMING)

        self.assertEqual(refused.status, HEAD_DRAINING)
        self.assertNotEqual(
            refused.status, HEAD_BUSY, "a head that takes no more work is not a head mid-turn"
        )
        self.assertEqual(len(self.host.sent), typed, "a refusal delivers nothing, then or later")

    def test_a_drain_closes_admission_and_leaves_the_running_turn_alone(self) -> None:
        run, lease = self.working()

        drained = self.runtime.request_drain(run, StopInitiator(actor="operator"))
        refused = self.runtime.deliver(run, NudgePointer.line("more"), transport=CONFIRMING)

        self.assertEqual(drained.lease, lease, "the drain did not take the turn away")
        self.assertFalse(drained.rotation_ready, "a head still mid-turn is not ready to be replaced")
        self.assertEqual(refused.status, HEAD_DRAINING)
        self.assertEqual(
            self.runtime.activity.lease(run.run_id), lease, "the refusal left the turn alone too"
        )

    def test_a_drained_head_is_ready_to_rotate_when_its_last_turn_closes(self) -> None:
        run, _ = self.working()
        self.runtime.request_drain(run, StopInitiator(actor="operator"))
        self.assertFalse(self.runtime.observe(run).rotation_ready)

        self.host.idle = True
        rotated = self.runtime.observe(run)

        self.assertIsNone(rotated.lease, "the last turn closed")
        self.assertTrue(rotated.rotation_ready, "and the head is observably ready for its successor")

    # the epoch is per head -------------------------------------------------
    def test_one_head_s_epoch_does_not_move_when_another_head_works(self) -> None:
        quiet = self.bring_up()
        busy = self.bring_up()
        unchanged = self.epoch(quiet)

        self.runtime.deliver(busy, NudgePointer.line("work"), transport=CONFIRMING)

        self.assertEqual(self.epoch(quiet), unchanged, "another head's turn is not this head's")
        self.assertGreater(
            self.runtime.activity.ticks, unchanged, "the runtime-wide counter is a separate value"
        )
        self.assertTrue(
            self.runtime.stop_if_quiescent(
                quiet, self.stopper, expected_activity_epoch=unchanged, head_process_alive=True
            ).ok,
            "a head nobody touched is still stoppable on the epoch its caller last read",
        )

    def test_an_observation_of_a_pane_with_no_output_clock_is_not_activity(self) -> None:
        """ "I looked and the pane cannot say when it last printed" is not "the head printed"."""
        run = self.bring_up()
        self.host.last_output_at = 0.0
        quiet = self.epoch(run)

        seen = self.runtime.observe(run)

        self.assertEqual(seen.epoch, quiet, "an observation with no clock moved nothing")
        self.assertEqual(self.epoch(run), quiet)
        self.assertTrue(
            self.runtime.stop_if_quiescent(
                run, self.stopper, expected_activity_epoch=quiet, head_process_alive=True
            ).ok,
            "and a head that was only looked at is still quiet",
        )

    def test_a_pane_that_printed_is_activity(self) -> None:
        run = self.bring_up()
        quiet = self.epoch(run)
        self.host.last_output_at = 2000.0

        self.assertGreater(self.runtime.observe(run).epoch, quiet)

    # stop_if_quiescent -----------------------------------------------------
    def test_a_stop_if_quiescent_ends_a_head_nothing_has_happened_to(self) -> None:
        run = self.bring_up()

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=self.epoch(run), head_process_alive=True
        )

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(self.host.closed, [run.handle])

    def test_a_stop_if_quiescent_refuses_a_stale_epoch_and_closes_nothing(self) -> None:
        run = self.bring_up()
        stale = self.epoch(run)
        self.host.last_output_at = 2000.0
        self.runtime.observe(run)

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=stale, head_process_alive=True
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, STOP_ACTIVITY_SINCE)
        self.assertEqual(self.host.closed, [], "nothing was stopped")
        self.assertTrue(
            self.runtime.deliver(run, NudgePointer.line("carry on"), transport=CONFIRMING).ok,
            "and admission was left exactly as it was",
        )

    def test_a_stop_if_quiescent_refuses_a_head_that_is_mid_turn(self) -> None:
        """A live head that is working is not interrupted: the sprint's own definition of done."""
        run, lease = self.working()

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=self.epoch(run), head_process_alive=True
        )

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.reason, STOP_TURN_IN_FLIGHT)
        self.assertEqual(receipt.lease, lease)
        self.assertEqual(self.host.closed, [], "a head mid-turn keeps its pane")
        self.assertTrue(self.runtime.activity.admits(run.run_id))

    def test_a_stop_if_quiescent_reclaims_a_lease_whose_turn_the_backend_says_has_ended(self) -> None:
        """A lease nobody saw close must not refuse the rotation of a live but finished head.

        Only `deliver`, `observe` and `stop` ever release a lease, and on the path that rotates a
        head none of the first two runs. For a head whose process the caller found alive, the pane
        is the honest place to ask, and a pane that will take input again is a turn that has ended:
        the lease is reclaimed and the stop goes through. What must never depend on that answer is
        the head whose process is gone — `..._rotates_a_head_that_died_mid_turn` is that one.
        """
        run, _ = self.working()
        self.host.idle = True

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=self.epoch(run), head_process_alive=True
        )

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(self.host.closed, [run.handle])
        self.assertIsNone(self.runtime.activity.lease(run.run_id))

    def test_a_stop_if_quiescent_refuses_a_turn_the_probe_could_not_say_had_ended(self) -> None:
        """ "I could not tell" is not permission for a *live* head: it keeps its pane.

        The refusal is bounded by the head being alive, and that is what makes it a refusal a caller
        can wait out: this head is running and will finish its turn. The same pane answer about a
        head whose process is gone is not read at all, because it would be a refusal nothing could
        ever lift.
        """
        run, lease = self.working()
        self.host.idle = None

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=self.epoch(run), head_process_alive=True
        )

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.reason, STOP_TURN_IN_FLIGHT)
        self.assertEqual(receipt.lease, lease)
        self.assertEqual(self.host.closed, [], "nothing was stopped")
        self.assertTrue(self.runtime.activity.admits(run.run_id))

    def test_a_stop_if_quiescent_rotates_a_head_that_died_mid_turn(self) -> None:
        """The invariant: a head that is not alive never stops its own replacement happening.

        This is the ordinary way a head fails — it dies while it is working, so its lease is still
        outstanding and Orca is left holding the wrapper shell `with_pid_heartbeat` puts around it.
        That shell answers the idle probe exactly as a working agent does, `busy`, because
        `terminal_readiness` asks about a pane and not about a turn. Reading it here refused the
        rotation in the one state rotation exists for, and refused it forever: nothing else on this
        path moves the epoch or closes the lease, so every following tick got the same answer.

        The caller's pid-heartbeat evidence outranks the pane, so the probe is not made at all.
        """
        run, _ = self.working()
        # What the real Orca answers for a bare shell with no agent TUI on it, measured.
        self.host.idle = False
        probed: list[str] = []
        self.host.on_call = probed.append

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=self.epoch(run), head_process_alive=False
        )
        self.host.on_call = None

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(self.host.closed, [run.handle], "the ghost pane went with it")
        self.assertIsNone(
            self.runtime.activity.lease(run.run_id),
            "a lease held by a process that is gone named a turn that ended with it",
        )
        self.assertNotIn(
            "wait_idle", probed, "a dead head's pane is not asked to authorise its own replacement"
        )

    def test_a_dead_head_is_rotated_however_many_ticks_have_asked_before(self) -> None:
        """The refusal was permanent, so its repair is checked over more than one tick.

        Nothing between two ticks of the rotation changes: the epoch only moves for `acted` and
        `observed`, and neither runs for a head the tick has judged dead. A repair that only made
        the first attempt succeed would leave the same trap one tick further along.
        """
        run, _ = self.working()
        self.host.idle = False
        epoch = self.epoch(run)

        outcomes = [
            self.runtime.stop_if_quiescent(
                run, self.stopper, expected_activity_epoch=epoch, head_process_alive=False
            ).status
            for _ in range(3)
        ]

        self.assertEqual(outcomes[0], HEAD_OK, "the first tick already rotated it")
        self.assertEqual(self.host.closed, [run.handle], "and it was stopped exactly once")

    def test_a_stop_if_quiescent_still_refuses_a_live_head_whose_pane_it_could_not_read(
        self,
    ) -> None:
        """Liveness outranking readiness is not readiness being ignored.

        The pane says nothing useful in both of these, and the difference is entirely the process:
        the head that is alive keeps its turn, and the head that is gone is replaced.
        """
        alive, _ = self.working()
        self.host.idle = None

        refused = self.runtime.stop_if_quiescent(
            alive, self.stopper, expected_activity_epoch=self.epoch(alive), head_process_alive=True
        )

        self.assertEqual(refused.status, HEAD_BUSY)
        self.assertEqual(self.host.closed, [], "a live head is never interrupted")

        dead, _ = self.working()
        allowed = self.runtime.stop_if_quiescent(
            dead, self.stopper, expected_activity_epoch=self.epoch(dead), head_process_alive=False
        )

        self.assertEqual(allowed.status, HEAD_OK)
        self.assertEqual(self.host.closed, [dead.handle])

    def test_a_dead_head_that_moved_since_the_judgement_is_still_refused_on_its_epoch(self) -> None:
        """The liveness fact skips the pane probe. It does not skip the epoch.

        A judgement that has expired has expired whatever it said about the process — the caller
        reads both at the same moment, so a stale liveness reading travels with a stale epoch and
        the epoch is what catches it. This refusal is exitable: the next tick judges again.
        """
        run = self.bring_up()
        stale = self.epoch(run)
        self.host.idle = False
        self.assertTrue(self.runtime.deliver(run, NudgePointer.line("one more"), transport=CONFIRMING).ok)

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=stale, head_process_alive=False
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, STOP_ACTIVITY_SINCE)
        self.assertEqual(self.host.closed, [], "nothing was stopped")
        self.assertTrue(
            self.runtime.stop_if_quiescent(
                run,
                self.stopper,
                expected_activity_epoch=self.epoch(run),
                head_process_alive=False,
            ).ok,
            "and a caller that looks again gets its stop",
        )

    def test_a_stop_if_quiescent_reads_the_epoch_before_it_asks_about_the_turn(self) -> None:
        """The epoch is what guards this stop, so it is what decides it, and it decides it first.

        The lease can be reclaimed from the backend, so it is not a barrier a caller may lean on.
        A head that has done something since the caller formed its judgement is refused on that
        alone — its pane is never even probed, because the answer could not matter.
        """
        run = self.bring_up()
        # The epoch the caller read when it judged this head finished.
        stale = self.epoch(run)
        # A delivery lands between that judgement and the stop, and takes a turn.
        self.host.idle = False
        self.assertTrue(
            self.runtime.deliver(run, NudgePointer.line("one more thing"), transport=CONFIRMING).ok
        )
        self.assertNotEqual(self.epoch(run), stale)
        probed: list[str] = []
        self.host.on_call = probed.append

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=stale, head_process_alive=True
        )
        self.host.on_call = None

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, STOP_ACTIVITY_SINCE)
        self.assertEqual(self.host.closed, [], "nothing was stopped")
        self.assertEqual(
            probed,
            [],
            "a head that moved is refused without asking its pane anything",
        )

    def test_a_stop_that_could_not_be_confirmed_puts_the_head_back_in_service(self) -> None:
        run = self.bring_up()
        self.host.refuse_close = True

        receipt = self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=self.epoch(run), head_process_alive=True
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(
            self.runtime.activity.admits(run.run_id),
            "nothing was stopped, so nothing was taken out of service either",
        )

    def test_a_refused_stop_does_not_re_open_an_admission_a_drain_had_closed(self) -> None:
        run = self.bring_up()
        self.runtime.request_drain(run, StopInitiator(actor="operator"))
        self.host.refuse_close = True

        self.runtime.stop_if_quiescent(
            run, self.stopper, expected_activity_epoch=self.epoch(run), head_process_alive=True
        )

        self.assertFalse(self.runtime.activity.admits(run.run_id), "the drain outlives the stop that failed")

    def test_a_teardown_owns_its_own_failure_and_the_head_stays_in_service(self) -> None:
        """The observer's stop is Orca's worktree teardown, and it runs inside the same section."""
        run = self.bring_up()
        torn: list[str] = []

        def teardown() -> None:
            torn.append("tried")
            raise RuntimeError("the worktree would not go")

        with self.assertRaises(RuntimeError):
            self.runtime.stop_if_quiescent(
                run,
                self.stopper,
                expected_activity_epoch=self.epoch(run),
                head_process_alive=True,
                teardown=teardown,
            )

        self.assertEqual(torn, ["tried"])
        self.assertTrue(self.runtime.activity.admits(run.run_id))

    def test_a_teardown_is_not_reached_at_all_by_a_refused_stop_if_quiescent(self) -> None:
        run, _ = self.working()
        torn: list[str] = []

        receipt = self.runtime.stop_if_quiescent(
            run,
            self.stopper,
            expected_activity_epoch=self.epoch(run),
            head_process_alive=True,
            teardown=lambda: torn.append("tried"),
        )

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(torn, [], "the check and the teardown are one thing, in that order")

    def test_a_teardown_stop_reports_the_epoch_this_head_actually_ended_on(self) -> None:
        """The observer's teardown forgets the head on its way out, so the epoch is taken first.

        `stop_observer` calls `forget_head`, which is right — it is the cleanup the `stop` verb does
        for itself. Counting the stop afterwards started from nothing and put `1` on the receipt of
        a head that had been through several turns.
        """
        run = self.bring_up()
        self.host.idle = True
        self.assertTrue(self.runtime.deliver(run, NudgePointer.line("work"), transport=CONFIRMING).ok)
        before = self.epoch(run)
        self.assertGreater(before, 1)

        receipt = self.runtime.stop_if_quiescent(
            run,
            self.stopper,
            expected_activity_epoch=before,
            head_process_alive=True,
            teardown=lambda: self.runtime.forget_head(run.run_id),
        )

        self.assertEqual(receipt.status, HEAD_OK)
        self.assertEqual(receipt.epoch, before + 1)

    # what the boundary owes rather than its callers ------------------------
    def test_a_bring_up_over_a_head_that_is_running_a_turn_is_a_receipt_not_an_exception(
        self,
    ) -> None:
        """A caller that mints its own run id gets the boundary's refusal, not a `TurnLeaseError`.

        The observer bring-up passes a run id it persisted. A second bring-up on it while a turn is
        outstanding used to reach the grant and raise, which is the one thing every other refusal on
        this boundary does not do. Nothing is opened: the session manager is not reached at all.
        """
        run, lease = self.working()
        opened = len(self.host.panes_by_workspace.get(WORKSPACE, []))

        receipt = self.runtime.start(
            CODEX,
            WORKSPACE,
            self.task,
            command="run-worker",
            title="secretary-1462 worker",
            run=run,
            transport=CONFIRMING,
        )

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.lease, lease)
        self.assertEqual(
            len(self.host.panes_by_workspace.get(WORKSPACE, [])),
            opened,
            "nothing was opened for a head that is already up",
        )

    def test_a_head_ended_by_a_stop_this_runtime_did_not_perform_can_still_be_forgotten(
        self,
    ) -> None:
        """An observer's real stop is Orca's worktree teardown, and it owes the runtime this.

        Without it the epoch, the output mark and the admission of every head the production loop
        ever launched stay in a runtime that lives as long as the loop does.
        """
        run, _ = self.working()
        self.runtime.request_drain(run, StopInitiator(actor="operator"))
        self.assertTrue(self.runtime.activity.epoch(run.run_id))

        self.runtime.forget_head(run.run_id)

        self.assertEqual(self.runtime.activity.epoch(run.run_id), 0)
        self.assertIsNone(self.runtime.activity.lease(run.run_id))
        self.assertTrue(self.runtime.activity.admits(run.run_id))

    # the lock --------------------------------------------------------------
    def test_no_other_thread_is_inside_the_critical_section_of_a_delivery(self) -> None:
        """Deterministic, and no sleep: the wedge runs *inside* the session-manager call."""
        run = self.bring_up()
        got_in: list[bool] = []

        def probe_from_another_thread(call: str) -> None:
            if call != "send":
                return

            def attempt() -> None:
                entered = self.runtime._lock.acquire(blocking=False)
                got_in.append(entered)
                if entered:
                    self.runtime._lock.release()

            other = threading.Thread(target=attempt)
            other.start()
            other.join()

        self.host.on_call = probe_from_another_thread
        receipt = self.runtime.deliver(run, NudgePointer.line("work"), transport=CONFIRMING)
        self.host.on_call = None

        self.assertTrue(receipt.ok)
        self.assertTrue(got_in, "the wedge never ran, so this proved nothing")
        self.assertEqual(set(got_in), {False}, "the delivery held the lock across every host call it made")

    def test_a_workspace_teardown_is_inside_the_same_critical_section_as_the_verbs(self) -> None:
        """For an observer head the worktree teardown *is* the stop, so it is serialised too."""
        self.bring_up()
        got_in: list[bool] = []

        def probe_from_another_thread(call: str) -> None:
            if call != "stop_workspace":
                return

            def attempt() -> None:
                entered = self.runtime._lock.acquire(blocking=False)
                got_in.append(entered)
                if entered:
                    self.runtime._lock.release()

            other = threading.Thread(target=attempt)
            other.start()
            other.join()

        self.host.on_call = probe_from_another_thread
        self.runtime.stop_workspace(WORKSPACE)
        self.host.on_call = None

        self.assertEqual(got_in, [False], "the teardown held the lock across the host call")

    def test_a_delivery_that_arrives_mid_delivery_is_refused_by_the_state_it_finds(self) -> None:
        """The lock orders them; the turn taken before the host call is what refuses the second."""
        run = self.bring_up()
        self.host.idle = False
        wedged: list[Any] = []

        def deliver_again(call: str) -> None:
            if call != "send" or wedged:
                return
            wedged.append(None)
            wedged[0] = self.runtime.deliver(run, NudgePointer.line("second"), transport=CONFIRMING)

        self.host.on_call = deliver_again
        first = self.runtime.deliver(run, NudgePointer.line("first"), transport=CONFIRMING)
        self.host.on_call = None

        self.assertTrue(first.ok)
        self.assertEqual(wedged[0].status, HEAD_BUSY)
        self.assertEqual(wedged[0].lease.lease_id, first.lease.lease_id)


class OrcaLegacyContractTests(HeadRuntimeContract, unittest.TestCase):
    """The boundary's own suite, run against the backend this product has always run.

    secretary-1465 is where these expectations stopped being a suite about `OrcaLegacyHeadRuntime`
    and became the definition of `HeadRuntime`: the same file runs against the local-pty backend in
    `test_local_pty_head_runtime`, so neither backend can be accepted with weaker behaviour than
    the other. What stays above is what only Orca can be asked — a pane inventory, an idle probe,
    the two verbs it has no honest answer for.
    """

    def setUp(self) -> None:
        self.host = ProbeHost()
        self.runtime = OrcaLegacyHeadRuntime(self.host)
        self.task = TaskRef.card("secretary-1465", document=f"{WORKSPACE}/TASK.md")

    def bring_up(self, *, pointer=None, **options):
        options.setdefault("transport", CONFIRMING)
        return self.runtime.start(
            CODEX,
            WORKSPACE,
            self.task,
            command="run-worker",
            title="secretary-1465 worker",
            pointer=pointer,
            **options,
        )

    def deliver_line(self, run, text, *, subject=""):
        return self.runtime.deliver(run, NudgePointer.line(text), transport=CONFIRMING, subject=subject)

    def begin_turn(self, run, *, subject=""):
        """A pane that is working on the prompt it was just given."""
        self.host.idle = False
        receipt = self.deliver_line(run, "do the work", subject=subject)
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.lease

    def end_turn(self, run) -> None:
        """The pane will take input again, which is the only end of a turn Orca can show."""
        self.host.idle = True

    def payloads_delivered(self) -> int:
        return len(self.host.sent)

    def adrift_run(self):
        return HeadRun(run_id="run-adrift", spec=CODEX, workspace="", task_ref=self.task)


if __name__ == "__main__":
    unittest.main()
