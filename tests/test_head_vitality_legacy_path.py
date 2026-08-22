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
from secretary.dispatch.head_vitality_episode import VitalityVerdict
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
    """issue:b5195041abbc3ec28243: the idle fence read the screen, not the transcript.

    The original defect -- a pid-confirmed head whose pane reads idle for two
    confirmations past the idle window goes straight to the report prompt and then to a
    stop/respawn with no provider evidence consulted -- is fixed by S1-4: the wait tick's
    decision is the persisted episode's verdict, and an advancing provider cursor keeps
    that verdict at HealthyActive, which refuses every destructive rung. These are REAL
    assertions now.
    """

    def test_a_working_transcript_blocks_the_legacy_idle_respawn(self) -> None:
        """The plan's demand, flipped (S1-4): an advancing provider cursor forbids the respawn.

        The screen reads idle forever while the transcript keeps advancing (the exact
        b5195041 shape: rollout JSONL moves, nothing refreshes the pane). The reduction
        sees Turn=Active on every tick, so no episode ever confirms a stall: the head is
        prompted once by the first quiet crossing and then simply waited on.
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
            # The transcript advances every tick -- the exact thing the pane cannot show.
            self.host.provider_cursor = f"rollout:{time.time()}"
            payload = self.runtime.production_state.load()
            record = payload["records"][CARD_REF]
            episode = record.get("worker_vitality_episode")
            if episode:
                # Age the persisted quiet reference the way an operator clock-rewind
                # would; the transcript itself never stops advancing.
                for name in ("started_at", "updated_at"):
                    if episode.get(name):
                        episode[name] -= 1000
                record["worker_vitality_episode"] = episode
                self.runtime.production_state.save(payload)
            return self.tick()

        status = {
            "known": True, "live": True, "reason": "live",
            "last_activity": time.time(), "pid_confirmed": True,
            "idle": True,
        }
        self.host.worker_status_result = dict(status)
        # A working head is never acted on destructively: with the transcript advancing
        # every tick, no episode ever leaves HealthyActive, so there is not even a
        # prompt -- just waits, forever, on evidence of life.
        first = idle_episode()
        self.assertEqual(first["action"], "waiting-worker-report")
        second = idle_episode()

        # What the plan demands of the switch (S1-4): a working transcript is never
        # respawned -- the advancing cursor keeps every verdict at HealthyActive, so
        # the ladder cannot act no matter how the pane reads or how old the wait clock is.
        self.assertNotEqual(second["action"], "worker-respawned")
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self.host.calls.count("prompt_worker_report"), 0)
        record = self.record_of()
        self.assertIsNotNone(record.worker_vitality_episode)
        self.assertIn(
            "advancing@provider_cursor", " ".join(record.worker_vitality_episode.basis),
            "the working head's own evidence says its transcript advances",
        )


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
    ``GATE_PENDING_STALL_SECONDS`` ceiling applied. Flipped by S1-5 (SIGCONT / response
    window): the gate-pending tick now runs the same vitality reduction + recovery policy
    for the worker head as the report wait does, so `/proc` state `T` is acted on within
    one tick and the six-hour ceiling remains only the outer bound for the CI rollup.
    """

    def test_a_stopped_worker_ends_the_gate_wait_before_the_ceiling(
        self,
    ) -> None:
        """The plan's demand, flipped (S1-5): `T` inside the gate wait is acted on in one tick.

        The original defect: ``_gate_pending`` read only the clock -- a suspended worker
        behind a pending gate was invisible until six hours passed, exactly the incident.
        The fixture ordering matters (S1-3 review MAJOR 2): the pending gate answers must be
        queued BEFORE the report tick, so the second validate-side tick reaches
        ``_gate_pending`` -- the machinery the incident is about.
        """
        from secretary.dispatcher_gate import GateResult

        self.start_dispatcher()
        self.host.gate_results = [
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
        ]
        self.tick()
        self.report_done()
        # The report's own move lands in validate; the gate has not been asked yet.
        self.assertEqual(self.tick()["to"], "validate")

        gated = self.tick()
        self.assertEqual(gated["action"], "gate-pending")
        self.assertEqual(gated["status"], "ok")

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

        # What the plan demands: the suspension is seen within a tick, not after 6h --
        # one identity-fenced SIGCONT, rung state on file, nothing stopped.
        self.assertEqual(noticed["action"], "worker-sigcont-sent")
        record = self.record_of()
        episode = record.worker_vitality_episode
        assert episode is not None
        self.assertEqual(episode.verdict, VitalityVerdict.SUSPENDED)
        self.assertEqual(episode.recovery_rung, 3)
        self.assertEqual(record.worker_respawns, 0)
        self.assertNotIn("restart_worker", self.host.calls)
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "stop signal" in str(comment.get("body") or "")
        ]
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("identity-fenced SIGCONT", comments[0])

    def test_an_expired_response_window_mid_gate_escalates_without_stopping(self) -> None:
        """The second rung works inside the gate wait too: operator, never a stop."""
        from secretary.dispatcher_gate import GateResult
        from secretary.dispatcher_watchdog import suspension_response_window_seconds

        self.start_dispatcher()
        self.host.gate_results = [
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
        ]
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()  # first pending stamp

        stopped_status = {
            "known": True, "live": True, "reason": "live",
            "last_activity": time.time(), "pid_confirmed": True, "idle": False,
            "pid_status": {"known": True, "alive": True, "match": True,
                           "state": "live-match", "stopped": True},
        }
        self.host.worker_status_result = dict(stopped_status)

        sent = self.tick()
        self.assertEqual(sent["action"], "worker-sigcont-sent")

        # Age BOTH span stamps past the response window: the head stayed parked.
        payload = self.runtime.production_state.load()
        episode = payload["records"][CARD_REF]["worker_vitality_episode"]
        for name in ("stall_frozen_since", "recovery_span_started_at"):
            if episode.get(name):
                episode[name] -= suspension_response_window_seconds() + 60
        payload["records"][CARD_REF]["worker_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

        escalated = self.tick()

        self.assertEqual(escalated["action"], "worker-suspension-escalated")
        record = self.record_of()
        self.assertEqual(record.worker_respawns, 0)
        # The six-hour gate ceiling was nowhere near elapsed; only the policy spoke.
        self.assertLess(
            time.time() - record.gate_pending_since, 600,
            "the escalation must come from the response window, not the gate clock",
        )
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)

    def test_a_deterministic_refusal_mid_gate_escalates_fast(self) -> None:
        """The 1194 contract holds while CI is pending: N identical refusals reach a human.

        A reviewer spawn refusing deterministically behind a pending gate must not sit out
        the six-hour rollup ceiling either; three sightings are enough for the policy.
        """
        from secretary.dispatch.head_vitality_policy import (
            DEFAULT_DETERMINISTIC_REFUSAL_LIMIT,
        )
        from secretary.dispatcher_gate import GateResult

        self.start_dispatcher()
        self.host.gate_results = [GateResult("pending", "CI still running")] * 8
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()  # first pending stamp (no scripted worker status yet: plain wait)

        refusal_status = {
            "known": True, "live": True, "reason": "live",
            "last_activity": time.time(), "pid_confirmed": False,
            "provider_progress": {"state": "unavailable",
                                  "reason": "terminal_split_source_not_found"},
        }
        self.host.worker_status_result = dict(refusal_status)
        for _ in range(DEFAULT_DETERMINISTIC_REFUSAL_LIMIT):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-deterministic-refusal-escalated")
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "authoritative refusal" in str(comment.get("body") or "")
        ]
        self.assertEqual(len(comments), 1, comments)
        record = self.record_of()
        self.assertEqual(record.worker_respawns, 0)
        self.assertNotIn("restart_worker", self.host.calls)
