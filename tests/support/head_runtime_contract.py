"""What every `HeadRuntime` backend owes, expressed once and run against all of them.

secretary-1461 pinned the boundary against the only backend there was, and secretary-1462 pinned
the order of its critical section the same way. With a second backend those expectations stop
being a suite about `OrcaLegacyHeadRuntime` and become the definition of the boundary: a mixin a
backend's own suite inherits, so that a new backend cannot be accepted with weaker behaviour than
the one that is already in production.

The mixin knows nothing about panes, sockets, ptys or session managers. Everything a backend has
of its own arrives through the hooks below, and each of them is a thing every backend can do:

  * `bring_up` and `deliver_line` perform the two verbs whose arguments differ per backend — the
    legacy one takes a transport, the local-pty one takes none;
  * `begin_turn` leaves the head **mid-turn** and `end_turn` returns once the backend can see that
    turn has ended. Both exist because "a turn is running" is not something a test can arrange by
    sleeping: on a fake session manager it is a flag, and on a real pty it is a child that is
    genuinely busy;
  * `payloads_delivered` counts what the head really received — in whatever unit the backend can
    count, since a session manager's writes and a pty's payloads are not the same thing, so it is
    only ever compared against its own earlier value. It is how a refusal is checked to have
    delivered nothing rather than merely to have said no;
  * `adrift_run` is a run this backend cannot address at all.

Tests that belong to one backend — Orca having no stream to attach to, the local-pty backend's
partial delivery — stay in that backend's own file. What is here is what neither of them may
weaken.
"""
from __future__ import annotations

from typing import Any

from triggered_agents.runtime.head import (
    EXITED,
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    RECEIPT_STATUSES,
    HeadRun,
    HeadRuntime,
    NudgePointer,
    StopInitiator,
    TurnLease,
)

#: The six verbs, named once. A seventh added without a decision fails `test_the_boundary_is_six_
#: verbs` rather than quietly widening what every backend has to implement.
VERBS = ("start", "deliver", "observe", "request_drain", "stop", "attach")


class HeadRuntimeContract:
    """The boundary's own expectations. Mixed into a `TestCase` beside a backend's setUp."""

    runtime: Any

    # -- what a backend supplies ---------------------------------------------------------------

    def bring_up(self, *, pointer: NudgePointer | None = None, **options: Any):
        """Perform `start` on this backend and hand back the receipt."""
        raise NotImplementedError

    def deliver_line(self, run: HeadRun, text: str, *, subject: str = ""):
        """Perform `deliver` of one line on this backend and hand back the receipt."""
        raise NotImplementedError

    def begin_turn(self, run: HeadRun, *, subject: str = "") -> TurnLease:
        """Leave this head mid-turn, and hand back the lease it is running under."""
        raise NotImplementedError

    def end_turn(self, run: HeadRun) -> None:
        """Return once the backend can see that the head's turn has ended."""
        raise NotImplementedError

    def payloads_delivered(self) -> int:
        """How many payloads this head has actually been given, as the backend can count them."""
        raise NotImplementedError

    def adrift_run(self) -> HeadRun:
        """A run this backend has no way to address."""
        raise NotImplementedError

    def attached(self, run: HeadRun):
        """`attach`, with whatever stream it hands over given back at the end of the test.

        A backend whose attach is real hands out a socket, and a suite that leaks one of those per
        test is a suite that leaks descriptors into every test after it.
        """
        receipt = self.runtime.attach(run)
        stream = receipt.evidence
        if hasattr(stream, "close"):
            self.addCleanup(stream.close)
        return receipt

    def live_run(self) -> HeadRun:
        receipt = self.bring_up()
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.run

    # -- the shape of the boundary -------------------------------------------------------------

    def test_the_boundary_is_six_verbs(self) -> None:
        declared = tuple(name for name in HeadRuntime.__dict__ if not name.startswith("_"))

        self.assertEqual(sorted(declared), sorted(VERBS))
        for verb in VERBS:
            self.assertTrue(callable(getattr(self.runtime, verb)), verb)

    def test_every_verb_answers_with_a_receipt_rather_than_a_bool_or_a_dict(self) -> None:
        run = self.live_run()

        for receipt in (
            self.runtime.observe(run),
            self.attached(run),
            self.runtime.request_drain(run, StopInitiator(actor="operator")),
        ):
            self.assertNotIsInstance(receipt, (bool, dict))
            self.assertIn(receipt.status, RECEIPT_STATUSES)

    def test_what_a_receipt_says_about_itself_is_read_off_its_status_and_nothing_else(self) -> None:
        """`ok`, `deferred`, `left_alive` and `unsupported` are each exactly one status class.

        Strengthened in secretary-1467, and the previous version is why. It asked only for "at most
        one flag" and it asked it of three receipts against a live head, every one of which is
        `ok` — so it held for any backend that set `ok` and no other flag, which is every backend
        that can bring a head up at all. It could not have failed. What is asked here instead is
        the classification itself, against the same statuses *and* against the refusals a head this
        backend cannot address produces: each status maps to exactly the flags named below, so a
        backend that reported a refusal as deferred-and-alive, or a `gone` head as merely deferred,
        fails — and both halves have to actually occur, or the test says so rather than passing on
        the half it saw.
        """
        expected = {
            HEAD_OK: {"ok"},
            HEAD_BUSY: {"deferred"},
            HEAD_DRAINING: {"deferred"},
            HEAD_ALIVE: {"left_alive"},
            HEAD_UNSUPPORTED: {"unsupported"},
            # A refusal that left nothing behind is the one status with no flag of its own: there
            # is nothing to come back to and nothing to account for.
            HEAD_GONE: set(),
        }
        live = self.live_run()
        adrift = self.adrift_run()
        receipts = [
            self.runtime.observe(live),
            self.attached(live),
            self.runtime.request_drain(live, StopInitiator(actor="operator")),
            self.deliver_line(live, "and this, after the drain"),
            self.runtime.observe(adrift),
            self.runtime.attach(adrift),
            self.runtime.request_drain(adrift, StopInitiator(actor="operator")),
        ]

        seen = set()
        for receipt in receipts:
            flags = {
                name
                for name in ("ok", "deferred", "left_alive", "unsupported")
                if getattr(receipt, name)
            }
            self.assertEqual(flags, expected[receipt.status], f"{receipt.status}: {receipt.reason}")
            seen.add(receipt.status)
        self.assertIn(HEAD_OK, seen, "no receipt in this test was an ok one")
        self.assertTrue(seen - {HEAD_OK}, "no refusal was exercised, so nothing was classified")

    # -- start ----------------------------------------------------------------------------------

    def test_start_brings_a_head_up_and_hands_back_its_run(self) -> None:
        receipt = self.bring_up()

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertIsInstance(receipt.run, HeadRun)
        self.assertIsNone(receipt.lease, "no pointer was delivered, so no turn was handed out")
        self.assertGreater(receipt.epoch, 0, "a head that came up did something")

    def test_a_start_that_delivers_a_pointer_hands_the_head_the_turn_it_started(self) -> None:
        before = self.payloads_delivered()

        receipt = self.bring_up(pointer=NudgePointer.line("report now"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertIsInstance(receipt.lease, TurnLease)
        self.assertEqual(receipt.lease.run_id, receipt.run.run_id)
        self.assertTrue(self.runtime.activity.busy(receipt.run.run_id))
        self.assertGreater(self.payloads_delivered(), before, "the pointer never reached the head")

    # -- deliver --------------------------------------------------------------------------------

    def test_a_delivery_that_says_ok_is_a_prompt_the_head_actually_received(self) -> None:
        """`ok` is about the head, not about the attempt, on every backend that can say it."""
        run = self.live_run()
        before = self.payloads_delivered()

        receipt = self.deliver_line(run, "report now", subject="worker-nudge")

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.arrived, "an ok delivery is one that arrived")
        self.assertGreater(self.payloads_delivered(), before, "ok, and the head got nothing")
        self.assertEqual(receipt.lease.subject, "worker-nudge")

    def test_a_delivery_on_top_of_a_running_turn_is_refused_and_never_delivered(self) -> None:
        run = self.live_run()
        lease = self.begin_turn(run)
        delivered = self.payloads_delivered()

        second = self.deliver_line(run, "and this too")

        self.assertEqual(second.status, HEAD_BUSY, second.reason)
        self.assertTrue(second.deferred, "a busy head is worth delivering to again, later")
        self.assertEqual(self.payloads_delivered(), delivered, "it was delivered anyway")
        self.assertEqual(second.lease, lease, "the turn that was running is the one still running")

    def test_a_delivery_after_a_drain_is_refused_as_draining_and_not_as_busy(self) -> None:
        run = self.live_run()
        self.runtime.request_drain(run, StopInitiator(actor="operator"))
        delivered = self.payloads_delivered()

        refused = self.deliver_line(run, "more work")

        self.assertEqual(refused.status, HEAD_DRAINING, refused.reason)
        self.assertNotEqual(
            refused.status, HEAD_BUSY, "a head that takes no more work is not a head mid-turn"
        )
        self.assertEqual(self.payloads_delivered(), delivered, "a refusal delivers nothing")

    # -- request_drain --------------------------------------------------------------------------

    def test_a_drain_closes_admission_and_leaves_the_running_turn_alone(self) -> None:
        run = self.live_run()
        lease = self.begin_turn(run)

        drained = self.runtime.request_drain(run, StopInitiator(actor="operator"))
        refused = self.deliver_line(run, "more")

        self.assertTrue(drained.draining, "the runtime's own gate is real on every backend")
        self.assertEqual(drained.lease, lease, "the drain did not take the turn away")
        self.assertFalse(drained.rotation_ready, "a head still mid-turn is not ready to be replaced")
        self.assertEqual(refused.status, HEAD_DRAINING)
        self.assertEqual(
            self.runtime.activity.lease(run.run_id), lease, "the refusal left the turn alone too"
        )

    def test_a_drained_head_is_ready_to_rotate_when_its_last_turn_closes(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        self.runtime.request_drain(run, StopInitiator(actor="operator"))
        self.assertFalse(self.runtime.observe(run).rotation_ready)

        self.end_turn(run)
        rotated = self.runtime.observe(run)

        self.assertIsNone(rotated.lease, "the last turn closed")
        self.assertTrue(rotated.rotation_ready, "and the head is observably ready for its successor")

    def test_a_drain_names_who_requested_it(self) -> None:
        run = self.live_run()

        with self.assertRaises(TypeError):
            self.runtime.request_drain(run, "operator")  # type: ignore[arg-type]

    # -- observe --------------------------------------------------------------------------------

    def test_a_head_mid_turn_is_busy_and_a_head_whose_turn_ended_is_not(self) -> None:
        run = self.live_run()
        self.begin_turn(run)

        self.assertIs(self.runtime.observe(run).busy, True)

        self.end_turn(run)
        closed = self.runtime.observe(run)

        self.assertIs(closed.busy, False)
        self.assertIsNone(closed.lease, "the turn this runtime handed out has been closed")
        self.assertFalse(self.runtime.activity.busy(run.run_id))

    def test_a_head_this_backend_cannot_address_is_not_a_head_that_is_not_busy(self) -> None:
        receipt = self.runtime.observe(self.adrift_run())

        self.assertFalse(receipt.ok)
        self.assertIsNone(receipt.busy, "an unobservable head is not a head that is not busy")

    def test_a_head_that_is_durably_working_is_not_therefore_busy(self) -> None:
        """`HeadRun.working` is the history of a delivery, never a statement about right now."""
        run = self.live_run()
        self.begin_turn(run)
        self.end_turn(run)
        stored = HeadRun.from_json(run.working().to_json())

        self.assertIs(self.runtime.observe(stored).busy, False)

    # -- the activity epoch ---------------------------------------------------------------------

    def test_the_epoch_grows_when_this_head_is_made_to_do_something(self) -> None:
        run = self.live_run()
        opened = self.runtime.activity.epoch(run.run_id)

        delivered = self.deliver_line(run, "report now").epoch

        self.assertGreater(delivered, opened)

    def test_an_observation_that_saw_nothing_new_is_not_activity(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        self.end_turn(run)
        first = self.runtime.observe(run).epoch

        again = self.runtime.observe(run).epoch

        self.assertEqual(again, first, "a head that has been quiet is not a head that acted")

    def test_one_head_s_epoch_does_not_move_when_another_head_works(self) -> None:
        quiet = self.live_run()
        busy = self.live_run()
        before = self.runtime.activity.epoch(quiet.run_id)

        self.deliver_line(busy, "report now")

        self.assertEqual(self.runtime.activity.epoch(quiet.run_id), before)

    # -- stop -----------------------------------------------------------------------------------

    def test_stop_ends_the_head_and_records_who_ended_it(self) -> None:
        run = self.live_run()

        receipt = self.runtime.stop(
            run, StopInitiator(actor="reviewer-freeze", reason="review took the tree")
        )

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.lifecycle, EXITED)
        self.assertEqual(receipt.run.stopped_by.actor, "reviewer-freeze")

    def test_a_stopped_head_leaves_no_turn_behind_it(self) -> None:
        receipt = self.bring_up(pointer=NudgePointer.line("report now"))
        run = receipt.run
        self.assertTrue(self.runtime.activity.busy(run.run_id))

        self.runtime.stop(run, StopInitiator(actor="operator"))

        self.assertFalse(self.runtime.activity.busy(run.run_id))
