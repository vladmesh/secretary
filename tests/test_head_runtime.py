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
"""

from __future__ import annotations

import unittest

from tests.fakes.host import FakeSessionHost
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
    HeadRuntime,
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
from triggered_agents.runtime.orca_legacy_head import OrcaLegacyHeadRuntime
from triggered_agents.runtime.pane_host import Pane, PaneHostError

CODEX = HeadSpec(profile_id="codex-worker", adapter="codex", effort="high", codex_mode="tui")
WORKSPACE = "/tmp/does-not-need-to-exist/secretary-1461"

# The six verbs, named once. A seventh added without a decision would fail `test_the_boundary_is_six
# _verbs` rather than quietly widening what every backend has to implement.
VERBS = ("start", "deliver", "observe", "request_drain", "stop", "attach")


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

    # the shape of the boundary --------------------------------------------
    def test_the_boundary_is_six_verbs(self) -> None:
        """The protocol is the contract a second backend has to meet, so its size is pinned."""
        declared = tuple(
            name for name in HeadRuntime.__dict__ if not name.startswith("_")
        )

        self.assertEqual(sorted(declared), sorted(VERBS))
        for verb in VERBS:
            self.assertTrue(callable(getattr(self.runtime, verb)), verb)

    def test_every_verb_answers_with_a_receipt_rather_than_a_bool_or_a_dict(self) -> None:
        run = self.live_run()

        for receipt in (
            self.runtime.observe(run),
            self.runtime.attach(run),
            self.runtime.request_drain(run, StopInitiator(actor="operator")),
        ):
            self.assertNotIsInstance(receipt, (bool, dict))
            self.assertIn(receipt.status, ("ok", "busy", "draining", "alive", "gone", "unsupported"))

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

        receipt = self.bring_up(
            pointer=NudgePointer.line("report now"), transport=RefusingTransport()
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertIsInstance(receipt.failure, HeadSpawnAborted)
        self.assertEqual(receipt.run.handle, "term:1")
        self.assertEqual(receipt.run.workspace, WORKSPACE)

    def test_a_bring_up_whose_pane_closed_cleanly_left_nothing(self) -> None:
        receipt = self.bring_up(
            pointer=NudgePointer.line("report now"), transport=RefusingTransport()
        )

        self.assertEqual(receipt.status, HEAD_GONE)
        self.assertIsInstance(receipt.failure, HeadSpawnFailed)
        self.assertNotIsInstance(receipt.failure, HeadSpawnAborted)
        self.assertEqual(self.host.closed, ["term:1"])

    def test_a_pane_that_was_busy_and_never_took_its_prompt_is_worth_retrying(self) -> None:
        self.host.idle = False

        receipt = self.bring_up(
            pointer=NudgePointer.line("report now"), transport=RefusingTransport()
        )

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertTrue(receipt.deferred)
        self.assertIsInstance(receipt.failure, HeadPaneBusy)
        self.assertEqual(receipt.failure.readiness, "busy")

    def test_a_session_manager_failing_on_its_own_terms_is_not_classified_here(self) -> None:
        """A boundary that invented a classification for it would be guessing about the head."""
        self.host.inventory_error = PaneHostError("orca terminal list failed")

        with self.assertRaises(PaneHostError):
            anchor = self.live_run()
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
        opened = self.runtime.activity.epoch

        delivered = self.runtime.deliver(
            run, NudgePointer.line("report now"), transport=CONFIRMING
        ).epoch
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
        granted = self.runtime.deliver(
            run, NudgePointer.line("report now"), transport=CONFIRMING
        ).lease
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

        self.assertEqual(activity.renew("run-1", "third").subject, "third")
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


if __name__ == "__main__":
    unittest.main()
