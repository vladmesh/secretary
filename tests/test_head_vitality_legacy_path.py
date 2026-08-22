"""Legacy decision-path characterisation for the incident regression table (card S1-3).

Where ``test_head_vitality_regression.py`` pins what the reducer *should* conclude, these
tests pin what the watchdog's existing wait-tick / gate machinery *does* today for three of
the incidents. They exist so S1-4's switch can be reviewed as a diff against recorded
behaviour instead of memory:

* where today's behaviour already matches the plan (the busy-readiness deferral), the test
  is a real assertion;
* where it still contradicts the plan, the test is marked ``expectedFailure`` with the
  sprint that flips it named in the docstring -- none is silently skipped.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SECRETARY_DISPATCHER_BODY_DIR", tempfile.mkdtemp())

from secretary import dispatcher as dispatcher_module
from secretary.dispatcher_state import DispatcherRecord, now_rfc3339
from secretary.tasks import TaskAudit, TaskReader, TaskWriter
from tests.dispatcher_fixtures import ensure_attempt
from tests.fakes.dispatcher import (
    FakeCatalog,
    FakeHost,
    FakeKanboard,
    FakeSprints,
)
from tests.observer_identity import bind_observer

CARD_REF = "secretary-510-pilot"


class LegacyPathTests(unittest.TestCase):
    """Shared fixture: one card driven through ``_tick_task`` like the runtime tests do."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name) / "data"
        env = mock.patch.dict(
            os.environ, {"SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies")}
        )
        env.start()
        self.addCleanup(env.stop)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)
        self.writer = TaskWriter(
            self.board, data_dir=self.data_dir, workspace=self.data_dir
        )
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.sprints = FakeSprints()
        self.runtime = dispatcher_module.DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            self.data_dir,
            self.catalog,
            self.host,
            owner="secretary-pilot",
            sprints=self.sprints,
        )

    # -- fixture plumbing -------------------------------------------------------

    def observed_sprint(self) -> None:
        """Bind the pilot card to an open sprint with a declared observer head."""
        self.board.metadata[12]["sprint_ref"] = "sprint:1031"
        bind_observer(self, "sprint:1031")
        self.sprints.rows["sprint:1031"] = {
            "ref": "sprint:1031", "status": "open",
            "observer": {"kind": "head", "profile": "claude-observer"},
        }
        row = next(
            (r for r in self.board.sprints if r["reference"] == "sprint:1031"), None
        )
        if row is None:
            self.board.add_sprint(
                "sprint:1031", status="open", sprint_reservations='["secretary"]',
            )
        else:
            self.board.metadata[int(row["id"])]["sprint_status"] = "open"

    def start_dispatcher(self) -> None:
        self.observed_sprint()
        self.runtime.production_state.save({
            "version": 1, "mode": "production", "phase": "production",
            "owner": self.runtime.owner, "records": {},
        })

    def tick(self) -> dict:
        from secretary._fsutil import file_lock
        runtime = self.runtime
        with file_lock(runtime.production_state.tick_lock):
            payload = runtime.production_state.load()
            records = runtime.production_state.records(payload)
            attempt_id = ensure_attempt(payload, CARD_REF, runtime.owner, runtime.owner)
            outcome = runtime._tick_task(
                self.reader.show(CARD_REF), records, payload, attempt_id
            )
            runtime.production_state.put_records(payload, records)
            payload["last_tick_at"] = now_rfc3339()
            runtime.production_state.save(payload)
        return outcome

    def record_of(self) -> DispatcherRecord:
        return self.runtime.production_state.records(
            self.runtime.production_state.load()
        )[CARD_REF]

    def report_done(self) -> None:
        record = self.runtime.production_state.load()["records"][CARD_REF]
        document = (Path(record["workspace"]) / "TASK.md").read_text(encoding="utf-8")
        line = next(l for l in document.splitlines() if "--kind done" in l)
        request_id = line.split("--request-id ", 1)[1].split()[0]
        self.writer.report(
            role="worker", actor="worker", reference=CARD_REF,
            kind="done", body="done", request_id=request_id,
        )

    def run_worker_to_validate_and_review(self) -> None:
        """Claim, report done, pass the default green gate, start the reviewer."""
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")


class IssueB5195041LegacyIdlePathTests(LegacyPathTests):
    """issue:b5195041abbc3ec28243: the idle fence reads the screen, not the transcript.

    Today a pid-confirmed head whose pane reads idle for two confirmations past the idle
    window goes straight to the report prompt and then to a stop/respawn -- no provider
    evidence is consulted anywhere on that path. Per the plan ("nudge/respawn/block only
    on a dead transcript") that is the destructive mistake; S1-4 flips this to assert the
    episode's HealthyActive blocks the respawn.
    """

    @unittest.expectedFailure
    def test_flip_in_S1_4_a_working_transcript_blocks_the_legacy_idle_respawn(self) -> None:
        """The plan's demand: an advancing provider cursor forbids the idle respawn.

        Expected failure: the legacy idle fence still acts on pane readiness alone --
        ``_wait_watchdog`` consults neither the provider cursor nor the vitality episode
        before acting on a confirmed-idle head -- so a second idle episode on a head that
        provably works still ends in ``worker-respawned``. S1-4 flips this to assert the
        episode's HealthyActive blocks every rung of that ladder (prompt, respawn, block).
        """
        self.start_dispatcher()
        # The retained conversation is available, so the rework resumes the same worker.
        self.host.fail_resume_worker_reason = ""
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference=CARD_REF, kind="red",
            body="fix it", request_id="review-red",
        )
        parked = self.tick()
        self.assertEqual(parked["to"], "assessment")
        self.writer.decide(
            role="observer", actor="observer", reference=CARD_REF, kind="rework",
            body="decided", request_id="decision-rework",
        )
        resumed = self.tick()
        self.assertEqual(resumed["action"], "review-red-reused-worker")

        def idle_episode() -> dict:
            """One pane-idle episode to its acting tick, screen idle throughout."""
            first = self.tick()
            assert first["action"] == "waiting-worker-report"
            payload = self.runtime.production_state.load()
            record = payload["records"][CARD_REF]
            record["worker_idle_since"] -= 301  # past IDLE_STALL_DEFAULT
            self.runtime.production_state.save(payload)
            status["last_activity"] -= 400
            self.tick()  # worker-idle-unconfirmed: the window's two-tick separation
            return self.tick()

        # The screen reads idle forever while the transcript keeps advancing (the exact
        # b5195041 shape: rollout JSONL moves, nothing refreshes the pane).
        status = {
            "known": True, "live": True, "reason": "live",
            "last_activity": time.time(), "pid_confirmed": True,
            "idle": True,
        }
        self.host.worker_status_result = dict(status)
        prompted = idle_episode()
        self.assertEqual(prompted["action"], "worker-report-prompted")

        second = idle_episode()

        # What the plan demands of the switch (S1-4): a working transcript is never respawned.
        self.assertNotEqual(second["action"], "worker-respawned")
        self.assertNotIn("restart_worker", self.host.calls)


class Issue3e7abdf9LegacyBusyReadinessTests(LegacyPathTests):
    """issue:3e7abdf91b8cd8a16254: busy must not read as unavailability.

    The original defect -- a readiness timeout on a working head classified as
    ``transport-refused-wait-for-readiness`` and answered with a replacement -- was fixed
    by secretary-1425/secretary-1163: the refusal is now classified by what Orca said
    about the pane, and a busy answer defers the launch instead of failing the round.
    This characterisation pins that corrected behaviour as a REAL assertion.
    """

    def test_a_busy_pane_at_launch_defers_instead_of_replacing(self) -> None:
        """/proc alive + readiness busy => deferred launch, same claim, no replacement.

        The bring-up raises ``HeadPaneNotReady(readiness='busy')``, which the claim path
        turns into ``worker-launch-deferred``: the card keeps its claim and the identical
        bring-up is retried next tick. No head is stopped and nothing is replaced.
        """
        from secretary.dispatcher_types import HeadPaneNotReady

        self.start_dispatcher()
        self.host.fail_prepare_error = HeadPaneNotReady(
            "the head pane was busy and never took its launch prompt: "
            "orca terminal wait --for tui-idle timeout",
            readiness="busy", pane="term-head",
        )

        deferred = self.tick()

        self.assertEqual(deferred["status"], "skipped")
        self.assertEqual(deferred["action"], "worker-launch-deferred")
        self.assertIn("busy", deferred["reason"])
        task = self.reader.show(CARD_REF)
        self.assertEqual(task["state"], "in_progress", "a deferral is not a failed round")
        record = self.record_of()
        self.assertEqual(record.state, "claim_verified")
        self.assertEqual(record.launch_intent, {}, "no head came up, so no intent stays open")
        # Nothing was stopped or replaced behind the busy pane.
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)

        # And the retry is the very next tick once the pane frees up.
        self.host.fail_prepare_error = None
        launched = self.tick()
        self.assertEqual(launched["status"], "ok")
        self.assertEqual(launched["step"], "claim")
        self.assertEqual(self.host.prepared, [CARD_REF])


class IssueFe04011bLegacyGatePendingTests(LegacyPathTests):
    """issue:fe04011b3723df8d5c2c: the gate phase counts hours, not liveness.

    A card waiting in validate whose worker sat in ``T (stopped)`` kept writing
    ``gate-pending status: ok errors: []`` with ``worker_idle_since=0``; only the six-hour
    ``GATE_PENDING_STALL_SECONDS`` ceiling applied. The plan's Done-when says `T` must not
    wait out a six-hour ceiling; S1-4/S1-5 flip this test when the gate path learns to ask
    whether the process it waits on is alive.
    """

    @unittest.expectedFailure
    def test_flip_in_S1_4_S1_5_a_stopped_worker_ends_the_gate_wait_before_the_ceiling(
        self,
    ) -> None:
        """The plan's demand: /proc state `T` inside the gate wait is acted on in one tick.

        Expected failure: ``_gate_pending`` reads only the clock -- a suspended worker
        behind a pending gate is invisible until six hours pass, exactly the incident.
        """
        self.start_dispatcher()
        self.run_worker_to_validate_and_review()

        # CI hangs; the card parks in gate-pending.
        from secretary.dispatcher_gate import GateResult
        self.host.gate_results = [GateResult("pending", "CI still running")]

        pending_tick = self.tick()
        self.assertEqual(pending_tick["action"], "gate-pending")
        self.assertEqual(pending_tick["status"], "ok")

        # Meanwhile the worker's process is discovered suspended (its /proc state is `T`).
        stopped_status = {
            "known": True, "live": True, "reason": "live",
            "last_activity": time.time(), "pid_confirmed": True, "idle": False,
            "pid_status": {"known": True, "alive": True, "match": True,
                           "state": "live-match", "stopped": True},
        }
        self.host.worker_status_result = dict(stopped_status)

        # Age the pending window just past one minute: far below the six-hour ceiling.
        payload = self.runtime.production_state.load()
        payload["records"][CARD_REF]["gate_pending_since"] -= 61
        self.runtime.production_state.save(payload)

        noticed = self.tick()

        # What the plan demands: the suspension is seen within a tick, not after 6h.
        self.assertNotEqual(noticed["action"], "gate-pending")
