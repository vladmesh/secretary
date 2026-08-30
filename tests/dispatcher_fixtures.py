"""Helpers for tests that drive one card through the tick's per-card decision.

The production tick opens an attempt per claim (`dispatcher_production`) and reads the running
one off the record for a card already in flight. A test that calls `_tick_task` directly has
neither, so it opens one here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary._fsutil import file_lock
from secretary.dispatcher import CommandHostRuntime, DispatcherRuntime, HostError
from secretary.dispatcher_heartbeat import heartbeat_identity
from secretary.dispatcher_state import new_attempt_id, now_rfc3339, record_attempt
from secretary.dispatcher_watchdog import idle_stall_seconds
from secretary.dispatcher_worker_lifecycle import head_run_binding
from secretary.tasks import TaskAudit, TaskReader, TaskWriter
from tests.fakes.dispatcher import FakeCatalog, FakeHost, FakeKanboard, FakeSprints
from tests.fanout_fixtures import accepted_transport_run
from tests.observer_identity import bind_observer
from triggered_agents.runtime.head import operations as head_ops

CARD_REF = "secretary-510-pilot"
STOPPED_STATUS = {
    "known": True,
    "live": True,
    "reason": "live",
    "pid_status": {"known": True, "alive": True, "match": True, "state": "live-match", "stopped": True},
}
RUNNING_STATUS = {
    "known": True,
    "live": True,
    "reason": "live",
    "pid_status": {"known": True, "alive": True, "match": True, "state": "live-match", "stopped": False},
}


def clear_env(test: unittest.TestCase, *names: str) -> None:
    """Drop dispatcher env overrides for one test and restore them at cleanup."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in names:
        os.environ.pop(name, None)


def write_heartbeat(path: Path, pid: int, *, identity: dict[str, str] | None = None) -> None:
    """Write a pid heartbeat matching the runtime's identity-fence format."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    record = dict(identity or heartbeat_identity(run_id="test-run", role="worker", task="card:secretary-751"))
    record.update(
        {
            "version": 1,
            "pid": pid,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
            "proc_starttime_ticks": stat[stat.rfind(")") + 2 :].split()[19],
        }
    )
    path.write_text(json.dumps(record), encoding="utf-8")


def ensure_attempt(payload: dict[str, Any], reference: str, actor: str, owner: str) -> str:
    """The attempt the payload already carries, or a freshly recorded one."""
    attempt_id = str(payload.get("attempt_id") or "")
    if attempt_id:
        return attempt_id
    attempt_id = new_attempt_id()
    payload["attempt_id"] = attempt_id
    record_attempt(payload, attempt_id, reference, actor, owner)
    return attempt_id


# The one card these tests drive through the tick.
CARD_REF = "secretary-510-pilot"


class DispatcherRuntimeFixture:
    """Shared runtime fixture for tests that tick one card through the dispatcher.

    A plain mixin (not a ``TestCase``), so the loader discovers each test exactly once:
    classes that only need the fixture inherit this, and ``DispatcherRuntimeTests``
    adds ``(DispatcherRuntimeFixture, unittest.TestCase)``. Before S1-4's review this
    setup lived directly on the TestCase, and every subclass re-ran the whole class's
    tests again.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        # Head heartbeats are keyed on the card reference alone, so without this every test in the
        # process would read and overwrite the same /tmp pid files.
        env = mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies")})
        env.start()
        self.addCleanup(env.stop)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        # workspace is pinned off the repo checkout: these tests stand in for a worker
        # report, and the done gate would otherwise read this repo's own working tree.
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        # The fixture card stands in for a card created through TaskWriter. Seed its durable
        # creation revision so review/decision packets exercise the production selector.
        self.writer.audit.append(
            "fixture-created",
            {
                "event_id": "fixture-created",
                "kind": "created",
                "ref": CARD_REF,
                "request_id": "fixture-created",
                "payload": {
                    "description_sha256": hashlib.sha256(
                        self.reader.show(CARD_REF)["description"].encode("utf-8")
                    ).hexdigest()
                },
            },
        )
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.sprints = FakeSprints()
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            sprints=self.sprints,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _confirmed_idle(self, *, at_prompt: bool = True) -> dict:
        """Drive one stall episode to the tick that acts on it, and hand that tick back.

        The verdict ladder replaces the old two-tick idle fence: one stamping tick, then
        a quiet age past ``confirm_after`` (the confirmed boundary, which spends the
        round's one report prompt on the way). Every acting tick here is degraded: this
        is the pipeline failing to move a card.

        `at_prompt=False` leaves the caller's own worker status alone, for a test that models a
        readiness this fixture's plain idle head does not have.
        """
        if at_prompt:
            self._head_at_its_prompt()
        first = self.tick()
        self.assertEqual(
            first["action"],
            "waiting-worker-report",
            "one reading of an idle pane is a head between turns, not a stalled one",
        )
        self._rewind_idle()
        bounced = self.tick()
        self.assertEqual(bounced["status"], "degraded")
        return bounced

    @staticmethod
    def _bound_provider_progress(
        record, cursor: str, *, source: str = "fake-bound-session"
    ) -> dict[str, str]:
        run_id, fingerprint = head_run_binding(record.worker_head_run)
        return {
            "state": "observed",
            "admission": "accepted",
            "source": source,
            "source_fingerprint": "e" * 32,
            "cursor": cursor,
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }

    def _task_document(self) -> str:
        return (Path(self._pilot_record()["workspace"]) / "TASK.md").read_text(encoding="utf-8")

    def _decide(
        self, kind: str, reason: str = "the observer looked and decided", *, request_id: str = ""
    ) -> None:
        """The observer's decision on a parked card, the only thing that releases it."""
        self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-510-pilot",
            kind=kind,
            body=reason,
            request_id=request_id or f"decision-{kind}",
        )

    def _assert_one_generation(self, expected: int) -> None:
        """Dispatcher state and the worker's own TASK.md name one round, not two numbers that
        happen to match: every report command in the document is read back and compared."""
        self.assertEqual(self._pilot_record()["report_generation"], expected)
        document = self._task_document()
        request_ids = [
            line.split("--request-id ", 1)[1].split()[0]
            for line in document.splitlines()
            if "--request-id" in line
        ]
        self.assertEqual(len(request_ids), 3, "one done and one blocked id per classification")
        self.assertEqual(len(set(request_ids)), 3, "the two classifications share an id")
        for request_id in request_ids:
            self.assertTrue(request_id.endswith(f"-{expected}"), request_id)
        self.assertIn(f"secretary-report-secretary-510-pilot-{expected}.md", document)

    def _park_and_decide(
        self,
        kind: str,
        *,
        request_id: str = "",
        reason: str = "the observer looked and decided",
    ) -> dict:
        """Tick the parked verdict through the seam and hand back the tick that acted on it."""
        parked = self.tick()
        self.assertEqual(parked["to"], "assessment")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "assessment")
        self._decide(kind, reason, request_id=request_id)
        return self.tick()

    def _report_done(self, body: str = "done") -> None:
        """Report through the command the worker actually holds in its TASK.md."""
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body=body,
            request_id=self._worker_report_request_id(),
        )

    def _review_red(self, request_id: str = "review-red", body: str = "fix the hermetic test") -> None:
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference="secretary-510-pilot",
            kind="red",
            body=body,
            request_id=request_id,
        )

    def _worker_report_request_id(self, kind: str = "done", classification: str = "") -> str:
        """The report request-id the worker in the checkout is actually holding, read out of its
        TASK.md rather than recomputed here: a test that recomputes it cannot catch the document
        and the dispatcher's own state naming different report rounds.

        The record may be gone (a dispatcher restart), which changes nothing about what the live
        worker is holding: the document is in the checkout either way.
        """
        record = self.runtime.production_state.load()["records"].get("secretary-510-pilot") or {}
        workspace = record.get("workspace") or (self.data_dir / "workspaces" / "secretary-510-pilot-pilot")
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
        wanted = f"--kind {kind}"
        if classification:
            wanted = f"{wanted} --classification {classification}"
        line = next(line for line in document.splitlines() if wanted in line)
        return line.split("--request-id ", 1)[1].split()[0]

    def observed_sprint(self, *, profile: str = "claude-observer", status: str = "open") -> None:
        """Put the pilot card in a sprint that declares a concrete observer head.

        That declaration is what makes a substantive verdict park for a decision: a card with
        nobody to release it keeps the immediate behaviour, which `unobserved_card` restores.

        The sprint goes onto the sprint board as well, reserving the pilot's project, because the
        observer's decision is guarded by that reservation: an observer decides only about a card
        whose project its own open sprint holds.
        """
        self.board.metadata[12]["sprint_ref"] = "sprint:1031"
        # The decisions these tests make are this sprint's head deciding about its own card, so
        # the caller carries the binding the dispatcher gives a head it launches.
        bind_observer(self, "sprint:1031")
        self.sprints.rows["sprint:1031"] = {
            "ref": "sprint:1031",
            "status": status,
            "observer": {"kind": "head", "profile": profile},
        }
        row = next((row for row in self.board.sprints if row["reference"] == "sprint:1031"), None)
        if row is None:
            self.board.add_sprint(
                "sprint:1031",
                status=status,
                sprint_reservations='["secretary"]',
            )
        else:
            self.board.metadata[int(row["id"])]["sprint_status"] = status

    def start_dispatcher(self) -> None:
        """Put the production state where a running dispatcher leaves it, with an observed card."""
        self.observed_sprint()
        self.runtime.production_state.save(
            {
                "version": 1,
                "mode": "production",
                "phase": "production",
                "owner": self.runtime.owner,
                "records": {},
            }
        )

    def tick(self, runtime: DispatcherRuntime | None = None) -> dict:
        """Drive one card through the tick's per-card decision.

        `production_tick` reaches the same `_tick_task` through its whole-board pass; these tests
        drive it for the one card under test, so an assertion is about that decision and not about
        everything else the board happens to hold.
        """
        runtime = runtime or self.runtime
        with file_lock(runtime.production_state.tick_lock):
            payload = runtime.production_state.load()
            records = runtime.production_state.records(payload)
            attempt_id = ensure_attempt(payload, CARD_REF, runtime.owner, runtime.owner)
            outcome = runtime._tick_task(self.reader.show(CARD_REF), records, payload, attempt_id)
            runtime.production_state.put_records(payload, records)
            payload["last_tick_at"] = now_rfc3339()
            runtime.production_state.save(payload)
        return outcome

    def _run_worker_to_validate(self) -> None:
        """Claim, drive the worker to report:done, and advance the card into validate.

        The report goes through the command the worker was actually handed, because that id is
        what attributes the report to the round the dispatcher is waiting for (secretary-1063).
        """
        self.tick()
        self.writer.report(
            role="worker",
            actor="worker",
            reference="secretary-510-pilot",
            kind="done",
            body="done",
            request_id=self._worker_report_request_id(),
        )
        advanced = self.tick()
        self.assertEqual(advanced["to"], "validate")

    def _pilot_record(self) -> dict:
        return self.runtime.production_state.load()["records"]["secretary-510-pilot"]

    def _head_at_its_prompt(self, kind: str = "worker", *, idle: bool = True) -> None:
        """The live incident's head: its process is alive and it is not working on anything.

        A pid-confirmed head is exempt from every timing ceiling, so this is the only state that
        distinguishes a finished or wedged head from one that is thinking.
        """
        status = {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": time.time(),
            "pid_confirmed": True,
            "idle": idle,
        }
        if kind == "review":
            self.host.review_status_result = status
        else:
            self.host.worker_status_result = status

    def _rewind_idle(self, kind: str = "worker") -> None:
        """Age the current quiet past the whole verdict ladder, to confirmation.

        S1-4: the idle fence is gone from the decision, so this ages the vitality
        episode's quiet reference instead -- far enough past ``confirm_after`` that the
        next reduction lands on ``ConfirmedStall``, the state the old double-tick
        rewind used to reach. For the finer-grained suspicion step the S1-4 decision
        tests age the episode directly.
        """
        self._age_vitality_quiet(kind, 3 * idle_stall_seconds() + 60)
        # The aged window models a head that has remained quiet.  A fresh last_activity would
        # instead be precisely the progress that restarts the continuous-idle clock.
        status = self.host.review_status_result if kind == "review" else self.host.worker_status_result
        if status is not None and status.get("last_activity") is not None:
            status["last_activity"] = time.time() - (3 * idle_stall_seconds() + 60)

    def _open_the_second_round(self) -> str:
        """Round 1 reported under its generation, round 2 opened and owns the next one.

        Hands back the report command of the round that is over, read out of the document the
        first worker was actually given: that is what a retained conversation still holds.
        """
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        stale_id = self._worker_report_request_id()
        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()
        self._review_red()
        self._park_and_decide("rework")
        self._assert_one_generation(2)
        self.assertNotEqual(stale_id, self._worker_report_request_id())
        return stale_id

    def _age_vitality_quiet(self, kind: str, seconds: float) -> None:
        """Age the persisted episode's quiet reference so the next reduction sees a stall.

        The verdict ladder measures quiet from the episode's reference (``started_at``
        before any progress), not from any fence field, so an episode is aged by moving
        that reference back -- the same operator clock-rewind the old helpers applied to
        ``worker_idle_since``, applied to the state the decision now actually reads. A
        run whose reduction is patched out has no episode to age; that is the shape the
        no-episode tests drive, and aging is simply skipped.
        """
        payload = self.runtime.production_state.load()
        record = payload["records"]["secretary-510-pilot"]
        episode = record.get(f"{kind}_vitality_episode")
        if episode is None:
            return
        for name in ("started_at", "updated_at"):
            if episode.get(name):
                episode[name] -= seconds
        record[f"{kind}_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

    def _bounce_the_idle_worker(self) -> dict:
        """Drive an idle head to the watchdog's destructive step, whatever precedes it.

        An addressable head spends the round's one report prompt at its first confirmed-idle
        boundary (secretary-1172), so for those the step this returns is the episode after it. A
        head nothing can type into — the fixture's ordinary exec profile — reaches it at the first.
        """
        bounced = self._confirmed_idle()
        if bounced["action"] == "worker-report-prompted":
            self._head_at_its_prompt()
            self._age_vitality_quiet("worker", idle_stall_seconds() + 660)
            bounced = self.tick()
        return bounced


class ReviewCatalog(FakeCatalog):
    """FakeCatalog plus the head-launch surface the real bring-up path calls into."""

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str = "") -> None:
        return None

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ):
        from triggered_agents.runtime.head import HeadCommand

        return HeadCommand(f"run-{role}", prompt_after_start=False)


class RecordingReviewHost(CommandHostRuntime):
    """CommandHostRuntime with the orca CLI and git stubbed, so the reviewer bring-up runs for
    real: anchor pick, split, label, worker freeze, pinned commit."""

    def __init__(
        self,
        root: Path,
        *,
        catalog=None,
        terminals: list[dict] | None = None,
        fail_ops: set[str] | None = None,
        split_pane_key: str = "",
        split_source_missing: bool = False,
        split_source_missing_after_open: bool = False,
    ) -> None:
        super().__init__(catalog or ReviewCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.preflight_codex_run = self._transport_preflight  # type: ignore[method-assign]
        self.calls: list[list[str]] = []
        self.fail_ops = fail_ops or set()
        self.split_pane_key = split_pane_key
        self.split_source_missing = split_source_missing
        self.split_source_missing_after_open = split_source_missing_after_open
        self.terminals = (
            [{"handle": "term-worker", "leafId": "leaf-worker", "title": "codex", "connected": True}]
            if terminals is None
            else terminals
        )
        # What Orca answers a `tui-idle` probe with. The default is a satisfied wait, which is a
        # pane ready for input.
        self.wait_answer: dict = {}

    def _transport_preflight(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: head_ops.TaskRef,
        pid_file: str,
        run_id: str,
    ) -> head_ops.HeadRun:
        """Cross only the Codex policy seam for transport tests.

        `NudgingReviewHost` also exercises a Claude worker.  Its normal HeadRun remains the real
        non-Codex one; fabricating a Codex attestation for it would be an identity mismatch, not a
        meaningful provider-policy fixture.
        """
        if self.catalog.head_profile(head).get("adapter") != "codex":
            return CommandHostRuntime.preflight_codex_run(
                self,
                head,
                role=role,
                workspace=workspace,
                task_ref=task_ref,
                pid_file=pid_file,
                run_id=run_id,
            )
        return accepted_transport_run(
            head,
            role=role,
            workspace=workspace,
            task_ref=task_ref,
            pid_file=pid_file,
            run_id=run_id,
        )

    def _run_json(self, args: list[str]) -> dict:
        self.calls.append(args)
        op = args[2] if args[:2] == ["orca", "terminal"] else ""
        if op in self.fail_ops:
            raise HostError(f"orca terminal {op} failed")
        if op == "list":
            return {"terminals": self.terminals}
        if op == "split":
            if self.split_source_missing:
                if self.split_source_missing_after_open:
                    self.terminals.append(
                        {"handle": "term-review", "leafId": "leaf-review", "title": None, "connected": True}
                    )
                raise HostError("orca terminal split failed: terminal_split_source_not_found")
            # The new pane joins the worktree's inventory, which is how the caller resolves its
            # leafId afterwards.
            self.terminals.append(
                {"handle": "term-review", "leafId": "leaf-review", "title": None, "connected": True}
            )
            split = {
                "handle": "term-review",
                "tabId": "tab-1",
                "paneRuntimeId": -1,
            }
            if self.split_pane_key:
                split["paneKey"] = self.split_pane_key
            return {"split": split}
        if op == "create":
            return {"terminal": {"handle": "term-created", "paneKey": "tab-1:leaf-created"}}
        if op == "wait":
            if isinstance(self.wait_answer, Exception):
                raise self.wait_answer
            return self.wait_answer
        return {}

    def _run(self, args: list[str], label: str, *, cwd: Path | None = None):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="deadbeefcafe0000\n", stderr="")

    def ops(self) -> list[str]:
        return [call[2] for call in self.calls if call[:2] == ["orca", "terminal"]]

    def call_for(self, op: str) -> list[str]:
        return next(call for call in self.calls if call[:3] == ["orca", "terminal", op])


class PromptAfterStartCatalog(ReviewCatalog):
    """A catalog whose heads take their prompt after the pane is up, the way a TUI provider does."""

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ):
        from triggered_agents.runtime.head import HeadCommand

        return HeadCommand(f"run-{role}", prompt_after_start=True)
