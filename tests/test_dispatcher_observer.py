"""Observer-head lifecycle: the production tick against the sprint board (secretary-793)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from secretary import dispatcher as dispatcher_module
from secretary.dispatcher import (
    CommandHostRuntime,
    CutoverState,
    DispatcherRuntime,
    InstanceCatalog,
)
from secretary.dispatcher_launcher import HeadLaunch
from secretary.dispatcher_tui import TuiDeliveryError
from secretary.dispatcher_observer import (
    EVENT_DEFERRED,
    EVENT_LAUNCHED,
    EVENT_RELAUNCHED,
    EVENT_STOPPED,
    OBSERVER_HEAD_FALLBACK,
    DeliveryStage,
    ObserverDelivery,
    ObserverLaunchAborted,
    ObserverRecord,
    load_observers,
    observer_alive,
    observer_pid_file,
    put_observers,
    observer_request_id,
    render_observer_prompt,
    stop_observer_head,
)
from secretary.sprints import SprintReader, SprintWriter
from secretary.dispatcher_types import HostError
from secretary.sprint_observer import head_choice
from secretary.dispatcher_production import _budget_event_type, _production_claim_ready, _reconcile_sprint_budget
from secretary.dispatcher_watchdog import initial_output_stall_seconds
from secretary.head_health import HeadReadiness
from secretary.head_registry import canonical_heads
from secretary.role_env import (
    OBSERVER_GENERATION_ENV,
    OBSERVER_SPRINT_ENV,
    ROLE_ALLOWLIST,
    ROLE_REQUIRED,
    declared_observer_sprint,
    observer_binding,
    runtime_env,
)
from secretary.status import _observers as status_observers
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter, _now

from tests.observer_identity import as_observer, bind_observer
from tests.test_dispatcher import (
    FakeCatalog,
    FakeHost,
    FakeKanboard,
    FakeLegacyPause,
    TwoOpenSprintAdmission,
)


# A pid that is real but not this process: `kill(pid, 0)` raises, so the watchdog reads the head as
# dead. Pid 2 is the kernel's kthreadd on Linux, hence a live pid nobody can be launched as; 999999
# is above the default pid_max and is reliably free.
DEAD_PID = 999999

# What the host raises when Orca refuses `terminal wait`. The bodies are the live CLI's: it exits
# non-zero for a condition it could not satisfy as well as for a failure, printing the answer as
# JSON on stdout, and the host carries that text into the failure it raises. Reading it is what
# tells a busy pane from a probe that was never answered.
TIMEOUT_WAIT_FAILURE = (
    "orca terminal wait --terminal observer:sprint:1 --for tui-idle --timeout-ms 6000 --json "
    'failed: {\n  "id": "0b1ba8ed",\n  "ok": false,\n  "error": {\n'
    '    "code": "timeout",\n    "message": "timeout"\n  }\n}'
)
# The live CLI exits non-zero for a pane it has looked at and found busy, printing an `ok: true`
# body with `satisfied: false`. Captured from the production audit log, `observer_launch_deferred`
# event `evt_24fb1640c4ea4a998f9f80e060d722fb` on `sprint:879`.
BLOCKED_PANE_WAIT_BODY = (
    '{\n  "id": "c5bb8352-65ff-4f8a-bd3a-fb0cdb97655c",\n  "ok": true,\n  "result": {\n'
    '    "wait": {\n      "handle": "term_c0755f85",\n      "condition": "tui-idle",\n'
    '      "satisfied": false,\n      "status": "running",\n      "exitCode": null,\n'
    '      "blockedReason": "codex-update-prompt"\n    }\n  }\n}'
)
STALE_HANDLE_WAIT_FAILURE = (
    "orca terminal wait --terminal observer:sprint:1 --for tui-idle --timeout-ms 6000 --json "
    'failed: {\n  "id": "7ea4ada1",\n  "ok": false,\n  "error": {\n'
    '    "code": "terminal_handle_stale",\n    "message": "terminal_handle_stale"\n  }\n}'
)


def install_skill_registry(root: Path, *, delivered: bool = True) -> Path:
    """A role-skill registry of this test's own, pointed at by SECRETARY_ROLE_SKILLS_MANIFEST.

    The launch gate reads the shell's skill directory, and the shells of the live installation are
    not a fixture: a test that let the tick look at them would pass or fail on whether somebody had
    run `role-skills sync` on this machine. The empty instance beside it does the same job for the
    other half of the registry: the overlay of the live installation is not a fixture either.
    Returns the observer skill's path in the fake shell, which `delivered=False` leaves absent.
    """
    manifest = root / "registry" / "manifest.toml"
    shell_root = root / "registry" / "codex-shell"
    claude_root = root / "registry" / "claude-shell"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        '[roles.observer]\nskills = ["observe-sprint"]\n\n'
        '[targets.codex-test]\nshell = "codex"\n'
        f'root = "{shell_root}"\nroles = ["observer"]\n\n'
        '[targets.claude-test]\nshell = "claude"\n'
        f'root = "{claude_root}"\nroles = ["observer"]\n',
        encoding="utf-8",
    )
    (root / "registry" / "instance").mkdir(parents=True, exist_ok=True)
    skill = shell_root / "observe-sprint" / "SKILL.md"
    if delivered:
        for target in (skill, claude_root / "observe-sprint" / "SKILL.md"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# canonical observer skill\n", encoding="utf-8")
    return skill


class ObserverLifecycleTests(TwoOpenSprintAdmission, unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        self.observer_skill = install_skill_registry(self.data_dir)
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
                "SECRETARY_ROLE_SKILLS_MANIFEST": str(self.data_dir / "registry" / "manifest.toml"),
                "SECRETARY_INSTANCE": str(self.data_dir / "registry" / "instance"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        # These tests call the writers as the head of `sprint:1`, which is the head the dispatcher
        # launches here; a test acting as another sprint's head binds its own.
        bind_observer(self, "sprint:1")
        (self.data_dir / "bodies").mkdir(parents=True, exist_ok=True)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.audit = TaskAudit(self.data_dir)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            self.audit,
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
        )
        self.runtime.state.save({
            "version": 1,
            "phase": "cutover_committed",
            "pilot_ref": "secretary-510-pilot",
            "old_owner_paused": True,
            "records": {},
        })

    # helpers -----------------------------------------------------------------

    def open_sprint(self, reference: str = "sprint:1", **metadata: object) -> None:
        self.board.add_sprint(reference, status="open", **metadata)

    def close_sprint(self, reference: str = "sprint:1") -> None:
        sprint = next(item for item in self.board.sprints if item["reference"] == reference)
        self.board.metadata[int(sprint["id"])]["sprint_status"] = "closed"

    def observers(self) -> dict:
        return load_observers(self.runtime.production_state.load())

    def actions(self, result: dict, step: str = "observer-reconcile") -> list[dict]:
        return [action for action in result["actions"] if action.get("step") == step]

    def expire_launch_grace(self, reference: str = "sprint:1") -> None:
        """Age a launch intent past the window in which a missing pid still counts as alive."""
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers[reference].launched_at = time.time() - initial_output_stall_seconds() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def kill_observer(self, reference: str = "sprint:1") -> None:
        record = self.observers()[reference]
        Path(record.pid_file).write_text(str(DEAD_PID), encoding="utf-8")

    def acknowledge_delivery(self, entry: dict[str, str], *, request_id: str) -> None:
        delivery = self.observers()["sprint:1"].delivery
        # The acknowledgement is this sprint's own head answering for what it was sent.
        with as_observer("sprint:1"):
            SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                reference="sprint:1",
                entry=entry,
                request_id=request_id,
                delivery_id=delivery.delivery_id,
                through_event=delivery.through_event,
            )

    def expire_wake_retry(self, reference: str = "sprint:1") -> None:
        """Age a deferred wake past its backoff so the next tick retries the delivery."""
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers[reference].delivery.next_at = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def age_delivery(
        self, seconds: float, reference: str = "sprint:1", *, deadline: bool = False
    ) -> None:
        """Put this delivery's send and hold that far in the past, as a slow head would.

        The acknowledgement deadline is left where it is unless `deadline` says otherwise, so a
        test can age a turn without also expiring the clock it is not about.
        """
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        delivery = observers[reference].delivery
        delivery.sent_at -= seconds
        delivery.held_since -= seconds
        if deadline:
            delivery.deadline -= seconds
            delivery.next_at -= seconds
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    def expire_launch_retry(self, reference: str = "sprint:1") -> None:
        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers[reference].launch_next_at = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

    # lifecycle ---------------------------------------------------------------

    def test_open_sprint_gets_one_observer_head(self) -> None:
        self.open_sprint()

        result = self.runtime.production_tick()

        actions = self.actions(result)
        self.assertEqual([action["action"] for action in actions], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.head, "codex-observer")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertTrue(record.workspace)
        self.assertTrue(record.handle)

    def test_the_head_is_launched_bound_to_its_own_record(self) -> None:
        """The binding a head writes with comes off the record it was brought up for.

        Sprint and generation together: the reference says whose cards this head may write, and
        the generation tells this lifecycle of that reference from the one before it, so a record
        rebuilt after a close and a reopen does not launch a head signing as the old one.
        """
        self.open_sprint()

        self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        self.assertEqual(
            self.host.observer_identities,
            [{OBSERVER_SPRINT_ENV: "sprint:1", OBSERVER_GENERATION_ENV: record.generation}],
        )
        self.assertTrue(record.generation)

        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.kill_observer()
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="replacement needed", request_id="replacement-event",
        )
        self.runtime.production_tick()

        relaunched = self.observers()["sprint:1"]
        self.assertEqual(relaunched.launches, 2)
        # A respawn is the same head of the same sprint, so it carries the same identity.
        self.assertEqual(
            self.host.observer_identities[-1],
            {OBSERVER_SPRINT_ENV: "sprint:1", OBSERVER_GENERATION_ENV: record.generation},
        )

    def _unbind_record(self, reference: str = "sprint:1") -> None:
        """Rewrite the record the way a state file written before the binding existed reads.

        The key is removed rather than set to False, because that is the on-disk shape a running
        installation is upgraded from, and `from_json` reading a missing key as bound is exactly
        the head that would be left running and refused.
        """
        payload = self.runtime.production_state.load()
        raw = payload["observers"][reference]
        raw.pop("bound", None)
        self.runtime.production_state.save(payload)
        self.assertFalse(self.observers()[reference].bound)

    def test_a_head_from_before_the_binding_is_retired_and_comes_back_bound(self) -> None:
        """The changeover: a live head nobody bound is stopped, and the next tick binds it.

        Its writes are all refused as `observer_identity_unbound`, and nothing it does can bind
        it: the binding is in the environment it started with. No probe of the process can ask,
        so the record is what says whether the head that is up carries one.
        """
        self.open_sprint()
        self.runtime.production_tick()
        self.assertTrue(self.observers()["sprint:1"].bound)
        first_generation = self.observers()["sprint:1"].generation
        self._unbind_record()
        # Alive, watched-looking on every other field, and unable to write: `status --json` says so
        # rather than leaving the operator to read the refusals.
        unbound_status = status_observers(self.runtime.production_state.load())[0]
        self.assertTrue(unbound_status["alive"])
        self.assertFalse(unbound_status["bound"])

        retired = self.runtime.production_tick()

        actions = self.actions(retired)
        self.assertEqual([action["action"] for action in actions], ["observer-stopped"])
        self.assertIn("predates the sprint binding", actions[0]["reason"])
        self.assertNotIn("sprint:1", self.observers())
        stopped = [event for event in self.audit.events() if event["kind"] == "observer_stopped"]
        self.assertIn("predates the sprint binding", stopped[-1]["payload"]["reason"])

        relaunched = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(relaunched)], ["observer-launched"],
        )
        record = self.observers()["sprint:1"]
        self.assertTrue(record.bound)
        self.assertNotEqual(record.generation, first_generation)
        self.assertEqual(
            self.host.observer_identities[-1],
            {OBSERVER_SPRINT_ENV: "sprint:1", OBSERVER_GENERATION_ENV: record.generation},
        )

    def test_a_bound_head_is_not_retired(self) -> None:
        """The changeover applies once. A head this dispatcher launched is left where it is."""
        self.open_sprint()
        self.runtime.production_tick()

        second = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(second)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(len(self.host.observer_identities), 1)

    def test_second_tick_does_not_launch_a_second_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        before = self.observers()["sprint:1"]

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].to_json(), before.to_json())

    def test_dead_pid_without_pending_card_work_is_not_relaunched(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-idle"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(self.host.stopped_observers, [])
        kinds = [event["kind"] for event in self.audit.events("sprint:1")]
        self.assertEqual(kinds, [EVENT_LAUNCHED])

    def test_finished_observer_queue_is_quiet_without_a_new_card_event(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 31 * 60,
            "idle": True,
        }

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-idle")
        self.assertIn("finished its turn", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, [])
        self.assertEqual(self.host.observer_nudges, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "idle-grace")
        self.assertTrue(record.idle_since)
        status = status_observers(self.runtime.production_state.load())[0]
        self.assertEqual(status["state"], "idle-grace")
        self.assertIsNotNone(status["idle_since"])
        self.assertIn("finished its turn", status["idle_reason"])
        sprint_status = self.runtime.sprints.status("sprint:1", observer=status)
        self.assertEqual(sprint_status["observer"]["state"], "idle-grace")

        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        resumed = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(resumed)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "running")

    def test_a_claude_head_is_nudged_and_acknowledges_with_its_causal_marker(self) -> None:
        """The gap this closes: a claude observer never showed Codex's completed-queue screen.

        Both halves run against the real host: readiness and delivery come from Orca's `tui-idle`,
        so the pane is never read for a vendor marker, and the batch is closed only by the
        observer's own resume naming this delivery.
        """
        self.catalog.profiles["claude-observer"] = {
            "adapter": "claude", "model": "opus", "resource": "claude-sub",
        }
        self.catalog.role_defaults["observer"] = "claude-observer"
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.assertEqual(self.observers()["sprint:1"].head, "claude-observer")
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")
        record = self.observers()["sprint:1"]
        sends: list[list[str]] = []
        busy = [False]

        def run_json(args: list[str]) -> dict:
            if args[1:3] == ["terminal", "list"]:
                return {"terminals": [{
                    "handle": record.handle, "leafId": record.leaf,
                    "connected": True, "lastOutputAt": int((time.time() - 2) * 1000),
                }]}
            if args[1:3] == ["terminal", "send"]:
                sends.append(args)
                busy[0] = True
                return {}
            if args[1:3] == ["terminal", "wait"]:
                # Ready for input until the wake lands, working on it afterwards.
                return {"wait": {"condition": "tui-idle", "satisfied": not busy[0]}}
            raise AssertionError(args)

        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="claude-observer-event",
        )

        with mock.patch.object(real_host, "_run_json", side_effect=run_json):
            self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]
            self.host.nudge_observer = real_host.nudge_observer  # type: ignore[method-assign]
            woke = self.runtime.production_tick()

            self.assertEqual([row["action"] for row in self.actions(woke)], ["observer-nudged"])
            delivery = self.observers()["sprint:1"].delivery
            self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)
            message = sends[0][sends[0].index("--text") + 1]
            self.assertIn(f"--delivery-id {delivery.delivery_id}", message)
            self.assertIn(f"--through-event {delivery.through_event}", message)

            entry = {
                "selected_step": "read board", "selected_why": "card changed",
                "rejected_alternatives": "wait", "current_task": "secretary-510-pilot",
                "dod_state": "open", "next_safe_step": "resume",
            }
            self.acknowledge_delivery(entry, request_id="claude-observer-ack")
            busy[0] = False
            acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.IDLE)
        self.assertTrue(delivery.acknowledged_delivery_id)
        self.assertTrue(delivery.acknowledged_resume_id)
        self.assertEqual(len(sends), 1)

    def test_a_wake_the_pane_never_took_is_an_explicit_refusal(self) -> None:
        """Retry exhaustion reaches the tick and the delivery record instead of being silent."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")
        record = self.observers()["sprint:1"]
        sends: list[list[str]] = []

        def run_json(args: list[str]) -> dict:
            if args[1:3] == ["terminal", "list"]:
                return {"terminals": [{
                    "handle": record.handle, "leafId": record.leaf,
                    "connected": True, "lastOutputAt": int((time.time() - 2) * 1000),
                }]}
            if args[1:3] == ["terminal", "send"]:
                sends.append(args)
                return {}
            if args[1:3] == ["terminal", "wait"]:
                # The pane stays ready however often the prompt is entered: it took none of them.
                return {"wait": {"condition": "tui-idle", "satisfied": True}}
            raise AssertionError(args)

        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="swallowed-wake-event",
        )

        with mock.patch.object(real_host, "_run_json", side_effect=run_json), \
             mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_TIMEOUT_S", 0.3), \
             mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_POLL_S", 0.01), \
             mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_RESEND_GRACE_S", 0), \
             mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_RETRIES", 2):
            self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]
            self.host.nudge_observer = real_host.nudge_observer  # type: ignore[method-assign]
            result = self.runtime.production_tick()

            action = self.actions(result)[0]
            self.assertEqual(action["action"], "observer-wake-deferred")
            self.assertEqual(action["status"], "degraded")
            self.assertIn("observer wake was not delivered", action["reason"])
            delivery = self.observers()["sprint:1"].delivery
            self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
            self.assertIn("observer wake was not delivered", delivery.reason)
            self.assertEqual(delivery.attempts, 1)
            self.assertEqual(self.observers()["sprint:1"].state, "wake-deferred")
            # The prompt and both retries were entered before the delivery was given up on.
            self.assertEqual(len(sends), 3)
            owed = (delivery.delivery_id, delivery.through_event)

            # Retries are bounded: the last one hands the batch to the replacement path instead of
            # growing the backoff on a head that takes none of its prompts.
            self.expire_wake_retry()
            second = self.runtime.production_tick()
            self.assertEqual(
                [row["action"] for row in self.actions(second)], ["observer-wake-deferred"]
            )
            self.assertEqual(self.observers()["sprint:1"].delivery.attempts, 2)

            self.expire_wake_retry()
            replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-relaunched")
        self.assertIn("replaced after 3 failed wakes", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.state, "running")
        # The replacement carries the batch that was owed, so a resume still acknowledges it.
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual((record.delivery.delivery_id, record.delivery.through_event), owed)
        self.assertEqual(record.delivery.method, "launch")
        self.assertEqual(record.delivery.attempts, 0)
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_RELAUNCHED],
        )

    def test_a_resume_for_a_refused_delivery_stops_the_retry(self) -> None:
        """A wake refused after its prompt landed is not sent twice.

        The delivery marker only exists in a prompt that reached the head, so a resume naming it
        is proof of the turn whatever the dispatcher saw of the send. Reachable now that a failure
        can happen after the prompt is in: the pane goes unanswerable while the head works on it.
        """
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")
        record = self.observers()["sprint:1"]
        sends: list[list[str]] = []
        unanswerable = [False]

        def run_json(args: list[str]) -> dict:
            if args[1:3] == ["terminal", "list"]:
                return {"terminals": [{
                    "handle": record.handle, "leafId": record.leaf,
                    "connected": True, "lastOutputAt": int((time.time() - 2) * 1000),
                }]}
            if args[1:3] == ["terminal", "send"]:
                sends.append(args)
                unanswerable[0] = True
                return {}
            if args[1:3] == ["terminal", "wait"]:
                if unanswerable[0]:
                    raise HostError(STALE_HANDLE_WAIT_FAILURE)
                return {"wait": {"condition": "tui-idle", "satisfied": True}}
            raise AssertionError(args)

        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="post-send-refusal-event",
        )

        with mock.patch.object(real_host, "_run_json", side_effect=run_json):
            self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]
            self.host.nudge_observer = real_host.nudge_observer  # type: ignore[method-assign]
            refused = self.runtime.production_tick()

            self.assertEqual(
                [row["action"] for row in self.actions(refused)], ["observer-wake-deferred"]
            )
            delivery = self.observers()["sprint:1"].delivery
            self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
            self.assertEqual(len(sends), 1)

            # The head had the prompt after all, and says so with this delivery's own markers.
            entry = {
                "selected_step": "read board", "selected_why": "card changed",
                "rejected_alternatives": "wait", "current_task": "secretary-510-pilot",
                "dod_state": "open", "next_safe_step": "resume",
            }
            self.acknowledge_delivery(entry, request_id="post-send-refusal-ack")
            unanswerable[0] = False
            self.expire_wake_retry()
            acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.IDLE)
        self.assertTrue(delivery.acknowledged_resume_id)
        # No second turn for a batch the observer has already answered.
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_readiness_probe_that_fails_is_not_read_as_a_busy_head(self) -> None:
        """Through the real `observer_status`: a probe Orca refuses must not read as ordinary work.

        The wake would otherwise sit in `waiting_for_idle` forever, counting no attempts and never
        reaching either the explicit refusal or the replacement.
        """
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")
        record = self.observers()["sprint:1"]

        def run_json(args: list[str]) -> dict:
            if args[1:3] == ["terminal", "list"]:
                return {"terminals": [{
                    "handle": record.handle, "leafId": record.leaf,
                    "connected": True, "lastOutputAt": int((time.time() - 2) * 1000),
                }]}
            if args[1:3] == ["terminal", "wait"]:
                raise HostError(STALE_HANDLE_WAIT_FAILURE)
            raise AssertionError(args)

        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="unprobeable-pane-event",
        )

        with mock.patch.object(real_host, "_run_json", side_effect=run_json):
            self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]
            first = self.runtime.production_tick()

            action = self.actions(first)[0]
            self.assertEqual(action["action"], "observer-wake-deferred")
            self.assertEqual(action["status"], "degraded")
            self.assertIn("observer terminal readiness could not be read", action["reason"])
            delivery = self.observers()["sprint:1"].delivery
            self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
            self.assertEqual(delivery.attempts, 1)

            self.expire_wake_retry()
            self.runtime.production_tick()
            self.expire_wake_retry()
            replaced = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replaced)], ["observer-relaunched"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        self.assertEqual(self.host.observer_nudges, [])

    def test_an_adopted_head_with_no_handle_is_replaced_not_waited_on(self) -> None:
        """A tick that died before recording the handle leaves a head nothing can be sent to.

        Its pid is alive, so the lifecycle keeps it, and a card event then has nowhere to go. That
        must reach the delivery's bounded failure path rather than waiting for a readiness the
        record can never be asked for.
        """
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()
        adopted = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(adopted)], ["observer-adopted"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.handle, "")
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")

        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="unaddressable-head-event",
        )
        self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]

        first = self.runtime.production_tick()

        action = self.actions(first)[0]
        self.assertEqual(action["action"], "observer-wake-deferred")
        self.assertEqual(action["status"], "degraded")
        self.assertIn("observer record names no terminal to read", action["reason"])
        self.assertEqual(self.observers()["sprint:1"].delivery.attempts, 1)

        self.expire_wake_retry()
        self.runtime.production_tick()
        self.expire_wake_retry()
        replaced = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replaced)], ["observer-relaunched"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertTrue(record.handle)
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(record.delivery.method, "launch")

    def test_a_readiness_probe_timeout_is_an_ordinary_busy_head(self) -> None:
        """The other half of the same distinction: a busy pane must not cost the sprint its head."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")
        record = self.observers()["sprint:1"]

        def run_json(args: list[str]) -> dict:
            if args[1:3] == ["terminal", "list"]:
                return {"terminals": [{
                    "handle": record.handle, "leafId": record.leaf,
                    "connected": True, "lastOutputAt": int((time.time() - 2) * 1000),
                }]}
            if args[1:3] == ["terminal", "wait"]:
                raise HostError(TIMEOUT_WAIT_FAILURE)
            raise AssertionError(args)

        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="busy-pane-event",
        )

        with mock.patch.object(real_host, "_run_json", side_effect=run_json):
            self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]
            waiting = self.runtime.production_tick()
            self.expire_wake_retry()
            still_waiting = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-waiting"])
        self.assertEqual(
            [row["action"] for row in self.actions(still_waiting)], ["observer-wake-waiting"]
        )
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.WAITING_FOR_IDLE)
        self.assertEqual(delivery.attempts, 0)
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_terminal_orca_will_not_answer_for_also_ends_in_a_replacement(self) -> None:
        """The other external failure of a wake: bounded retries, then the same replacement path."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="unreadable-terminal-event",
        )

        with mock.patch.object(
            self.host, "observer_status", side_effect=HostError("orca terminal list failed")
        ):
            first = self.runtime.production_tick()
            self.assertEqual(
                [row["action"] for row in self.actions(first)], ["observer-wake-deferred"]
            )
            self.expire_wake_retry()
            self.runtime.production_tick()
            self.expire_wake_retry()
            replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-relaunched")
        self.assertIn("observer terminal could not be read", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_a_replacement_that_cannot_come_up_keeps_both_reasons(self) -> None:
        """The escalation does not hide why the wake failed nor why its replacement did not launch."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="failed-replacement-event",
        )

        with mock.patch.object(
            self.host, "observer_status", side_effect=HostError("orca terminal list failed")
        ):
            self.runtime.production_tick()
            self.expire_wake_retry()
            self.runtime.production_tick()
            self.expire_wake_retry()
            self.host.fail_observer_reason = "orca worktree create failed"
            replaced = self.runtime.production_tick()

        action = self.actions(replaced)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertIn("observer terminal could not be read", action["reason"])
        self.assertIn("that replacement did not come up", action["reason"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        # The batch is not lost with the failed replacement: it goes back on the bounded retry.
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
        self.assertEqual(delivery.attempts, 1)

    def test_finished_observer_queue_is_nudged_once_for_a_linked_card_event(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 2,
            "idle": True,
        }
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="observer-event",
        )

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-nudged")
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertTrue(record.delivery.delivery_id)
        # The head took the prompt and is working the batch, which is what a pane reports after a
        # nudge: no second delivery goes out while it is busy.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        repeated = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(repeated)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_event_waiting_for_an_active_queue_is_nudged_when_that_queue_finishes(self) -> None:
        """The watchdog is not a delay between a normal turn completing and its event wake."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed while observer was working", request_id="event-during-active-turn",
        )

        waiting = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-waiting"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.WAITING_FOR_IDLE)
        self.assertTrue(record.delivery.pending_from)
        self.assertEqual(self.host.observer_nudges, [])

        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        delivered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(delivered)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(
            self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK
        )

    def test_resume_acknowledges_the_event_and_prevents_a_second_wake_after_restart(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="ack-event",
        )
        self.runtime.production_tick()
        delivery_id = self.observers()["sprint:1"].delivery.delivery_id
        event_id = self.observers()["sprint:1"].delivery.through_event
        entry = {
            "selected_step": "check board", "selected_why": "card changed", "rejected_alternatives": "wait",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "resume",
        }
        self.acknowledge_delivery(entry, request_id="ack-resume")

        acknowledged = self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.acknowledged_through, event_id)
        self.assertEqual(record.delivery.acknowledged_delivery_id, delivery_id)
        self.assertEqual(record.delivery.acknowledged_resume_id, self.audit.events()[-1]["event_id"])
        self.assertEqual(record.delivery.stage, DeliveryStage.IDLE)
        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        restarted = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(restarted)], ["observer-idle"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_only_the_active_delivery_marker_acknowledges_an_event(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="marker-event",
        )
        self.runtime.production_tick()
        delivery = self.observers()["sprint:1"].delivery
        entry = {
            "selected_step": "read board", "selected_why": "card changed", "rejected_alternatives": "wait",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "resume",
        }
        writer = SprintWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        writer.resume(
            role="observer", actor="observer", reference="sprint:1", entry=entry,
            request_id="unrelated-resume",
        )
        writer.resume(
            role="observer", actor="observer", reference="sprint:1", entry=entry,
            request_id="wrong-delivery", delivery_id="delivery-old", through_event=delivery.through_event,
        )
        writer.resume(
            role="observer", actor="observer", reference="sprint:1", entry=entry,
            request_id="wrong-through", delivery_id=delivery.delivery_id, through_event="evt-old",
        )
        # The head is mid-turn writing these resumes, so nothing here is idle evidence.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}

        pending = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(pending)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(self.observers()["sprint:1"].delivery.delivery_id, delivery.delivery_id)

        self.acknowledge_delivery(entry, request_id="matching-marker")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        self.assertEqual(self.observers()["sprint:1"].delivery.acknowledged_delivery_id, delivery.delivery_id)

    def test_crash_before_nudge_keeps_an_unrelated_resume_unacknowledged(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="crash-before-nudge-event",
        )

        with mock.patch.object(
            self.host, "nudge_observer", side_effect=KeyboardInterrupt("crash before nudge")
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "crash before nudge"):
                self.runtime.production_tick()

        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.DELIVERY_INTENT)
        self.assertEqual(self.host.observer_nudges, [])
        entry = {
            "selected_step": "wait", "selected_why": "the board is quiet", "rejected_alternatives": "relaunch",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "wait",
        }
        SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
            role="observer", actor="observer", reference="sprint:1", entry=entry,
            request_id="old-turn-resume",
        )

        recovered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(recovered)], ["observer-wake-pending"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.DELIVERY_INTENT)
        self.assertEqual(record.delivery.delivery_id, delivery.delivery_id)
        self.assertEqual(self.host.observer_nudges, [])

    def test_resume_before_a_same_second_event_does_not_acknowledge_it(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        same_second = _now()
        entry = {
            "selected_step": "wait", "selected_why": "board is quiet", "rejected_alternatives": "relaunch",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "wait",
            "recorded_at": same_second,
        }
        SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
            role="observer", actor="observer", reference="sprint:1", entry=entry, request_id="same-second-resume",
        )
        self.audit.append("same-second-card-event", {
            "event_id": "evt_same_second_card", "request_id": "same-second-card-event",
            "ref": "secretary-510-pilot", "kind": "commented", "outcome": "success",
            "occurred_at": same_second,
        })

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertNotEqual(
            self.observers()["sprint:1"].delivery.acknowledged_through, "evt_same_second_card"
        )

    def test_new_event_after_woken_turn_is_delivered_after_its_resume(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="first", request_id="first-wake-event",
        )
        self.runtime.production_tick()
        entry = {
            "selected_step": "read board", "selected_why": "first event", "rejected_alternatives": "wait",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "wait",
        }
        self.acknowledge_delivery(entry, request_id="first-wake-resume")
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="second", request_id="second-wake-event",
        )

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])

    def test_a_nudge_b_resume_a_c_delivers_a_second_batch_through_c(self) -> None:
        """A resume for A cannot absorb B or C appended after A's delivery intent."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="first", request_id="burst-first",
        )
        self.runtime.production_tick()
        first_id = self.observers()["sprint:1"].delivery.through_event
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="coalesced", request_id="burst-second",
        )
        # B lands while the head is still working A's batch.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        waiting = self.runtime.production_tick()
        second_id = self.audit.events()[-1]["event_id"]

        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-pending"])
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.observers()["sprint:1"].delivery.through_event, first_id)
        entry = {
            "selected_step": "read board", "selected_why": "coalesced burst", "rejected_alternatives": "wait",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "wait",
        }
        self.acknowledge_delivery(entry, request_id="burst-resume")
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="after resume", request_id="after-burst-resume",
        )
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}

        delivered = self.runtime.production_tick()
        record = self.observers()["sprint:1"]

        self.assertEqual([row["action"] for row in self.actions(delivered)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])
        self.assertEqual(record.delivery.acknowledged_through, first_id)
        self.assertEqual(record.delivery.through_event, self.audit.events()[-1]["event_id"])

    def test_replacement_launch_delivers_pending_event_without_a_second_nudge(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.kill_observer()
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="replacement needed", request_id="replacement-event",
        )
        pending_id = self.audit.events()[-1]["event_id"]

        replacement = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replacement)], ["observer-relaunched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(record.delivery.method, "launch")
        self.assertEqual(record.delivery.through_event, pending_id)
        launch_prompt = (Path(record.workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn(record.delivery.delivery_id, launch_prompt)
        self.assertIn(record.delivery.through_event, launch_prompt)
        # The replacement head is working the batch its launch prompt carried.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}

        repeated = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(repeated)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, [])

    def test_burst_of_card_events_coalesces_to_one_observer_nudge(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        for request_id in ("burst-one", "burst-two"):
            self.writer.comment(
                role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
                body=request_id, request_id=request_id,
            )

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(
            self.observers()["sprint:1"].delivery.through_event, self.audit.events()[-1]["event_id"]
        )
        entry = {
            "selected_step": "read board", "selected_why": "coalesced batch", "rejected_alternatives": "wait",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "wait",
        }
        self.acknowledge_delivery(entry, request_id="burst-ack")

        acknowledged = self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        self.assertEqual(record.delivery.stage, DeliveryStage.IDLE)
        self.assertEqual(record.delivery.acknowledged_through, self.audit.events()[-2]["event_id"])

    def test_persisted_nudge_intent_recovers_after_crash_without_a_duplicate_before_the_deadline(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="crash boundary", request_id="crash-boundary-event",
        )

        def crash_after_send(record, *, confirm=None) -> None:
            self.host.observer_nudges.append(str(record.sprint))
            raise KeyboardInterrupt("simulated dispatcher crash")

        with mock.patch.object(self.host, "nudge_observer", side_effect=crash_after_send):
            with self.assertRaisesRegex(KeyboardInterrupt, "simulated dispatcher crash"):
                self.runtime.production_tick()

        persisted = self.observers()["sprint:1"].delivery
        self.assertEqual(persisted.stage, DeliveryStage.DELIVERY_INTENT)
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

        recovered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(recovered)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers["sprint:1"].delivery.deadline = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

        redelivered = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(redelivered)], ["observer-redelivered"])
        self.assertIn("acknowledgement deadline expired", self.actions(redelivered)[0]["reason"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])

    def test_failed_nudge_retries_with_the_same_delivery_after_restart(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="retry delivery", request_id="retry-delivery-event",
        )
        self.host.fail_observer_reason = "terminal transport failed"

        failed = self.runtime.production_tick()

        delivery = self.observers()["sprint:1"].delivery
        delivery_id = delivery.delivery_id
        self.assertEqual([row["action"] for row in self.actions(failed)], ["observer-wake-deferred"])
        self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
        self.assertEqual(delivery.attempts, 1)
        self.assertIn("retry in 30s", delivery.reason)

        self.host.fail_observer_reason = ""
        deferred = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(deferred)], ["observer-wake-deferred"])
        self.assertEqual(self.host.observer_nudges, [])

        payload = self.runtime.production_state.load()
        observers = load_observers(payload)
        observers["sprint:1"].delivery.next_at = time.time() - 1
        put_observers(payload, observers)
        self.runtime.production_state.save(payload)

        retried = self.runtime.production_tick()

        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual([row["action"] for row in self.actions(retried)], ["observer-nudged"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(delivery.delivery_id, delivery_id)
        self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_an_old_event_alone_does_not_redeliver_a_live_delivery(self) -> None:
        """The acknowledgement deadline is armed by the delivery, never by the age of the event."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.audit.append("old-event", {
            "event_id": "evt_old_event", "request_id": "old-event", "ref": "secretary-510-pilot",
            "kind": "commented", "outcome": "success", "occurred_at": "2000-01-01T00:00:00Z",
        })

        first = self.runtime.production_tick()
        # The head is working the batch. The event it names is decades old, and that alone is
        # never a reason to send this delivery again.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        repeated = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(first)], ["observer-nudged"])
        self.assertEqual([row["action"] for row in self.actions(repeated)], ["observer-wake-pending"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_an_observer_that_becomes_idle_unacknowledged_is_redelivered_at_once(self) -> None:
        """No acknowledgement and a head back at its prompt: the tick that sees it redelivers."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="idle-redelivery-event",
        )
        self.runtime.production_tick()
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)
        # The turn ran and ended without a resume for this batch, seconds into a 30 minute
        # deadline. The pane is ready for input and its last output is current: nothing more is
        # required, and no quiet interval is waited out over either timestamp.
        self.age_delivery(5)
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}

        redelivered = self.runtime.production_tick()

        action = self.actions(redelivered)[0]
        self.assertEqual(action["action"], "observer-redelivered")
        self.assertIn("became idle without acknowledging", action["reason"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1", "sprint:1"])
        after = self.observers()["sprint:1"].delivery
        self.assertEqual(after.stage, DeliveryStage.AWAITING_ACK)
        # The batch is the same one: a redelivery never advances the high-water mark.
        self.assertEqual(after.through_event, delivery.through_event)
        self.assertLess(time.time(), after.deadline)

    def test_a_pane_without_readable_activity_is_not_read_as_idle_by_the_redelivery(self) -> None:
        """Missing or unreadable activity is no evidence a turn ended, and a busy head is none either.

        A head mid-sentence is covered by the second of these: it is not ready for input, so the
        not-idle branch keeps waiting on it. No quiet interval guards this path.
        """
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="mid-sentence-event",
        )
        self.runtime.production_tick()

        # Long quiet, but the pane's activity timestamp is missing, then unreadable, then the pane
        # is busy. None is evidence of an ended turn, so the delivery waits for its deadline.
        self.age_delivery(600)
        for status in (
            {"idle": True},
            {"idle": True, "last_activity": "never"},
            {"idle": False, "last_activity": time.time()},
        ):
            self.host.observer_status_result = status
            result = self.runtime.production_tick()
            self.assertEqual(
                [row["action"] for row in self.actions(result)], ["observer-wake-pending"]
            )
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)

    def test_a_head_that_never_returns_to_idle_ends_at_the_turn_ceiling(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed while the observer was working", request_id="turn-ceiling-event",
        )
        waiting = self.runtime.production_tick()
        self.assertEqual([row["action"] for row in self.actions(waiting)], ["observer-wake-waiting"])

        with mock.patch.dict(os.environ, {"SECRETARY_OBSERVER_TURN_CEILING_SECONDS": "1"}):
            self.age_delivery(60)
            ceiling = self.runtime.production_tick()

            action = self.actions(ceiling)[0]
            self.assertEqual(action["action"], "observer-wake-deferred")
            self.assertIn("turn ceiling", action["reason"])
            delivery = self.observers()["sprint:1"].delivery
            self.assertEqual(delivery.stage, DeliveryStage.RETRY_DEFERRED)
            self.assertEqual(delivery.attempts, 1)

            # The retries are bounded, so a head that stays busy costs the sprint a replacement
            # rather than holding the batch forever.
            for _ in range(2):
                self.expire_wake_retry()
                replaced = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(replaced)], ["observer-relaunched"])
        self.assertIn("turn ceiling", self.actions(replaced)[0]["reason"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])

    def test_a_long_card_mid_turn_is_not_torn_down_by_the_turn_ceiling(self) -> None:
        """The negative case: a busy head past its acknowledgement deadline is still working."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="a long card to work through", request_id="long-turn-event",
        )
        self.runtime.production_tick()
        delivery = self.observers()["sprint:1"].delivery
        self.assertEqual(delivery.stage, DeliveryStage.AWAITING_ACK)

        # An hour into the turn: the acknowledgement deadline is long gone, the head is busy the
        # whole time, and the default turn ceiling is nowhere near.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}
        self.age_delivery(60 * 60, deadline=True)

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-wake-waiting")
        self.assertIn("not ready for a prompt", action["reason"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, [])
        after = self.observers()["sprint:1"].delivery
        self.assertEqual(after.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(after.delivery_id, delivery.delivery_id)
        self.assertEqual(after.attempts, 0)

    def test_a_redelivered_batch_is_still_acknowledged_only_by_its_own_marker(self) -> None:
        """An idle-triggered redelivery does not loosen the causal acknowledgement."""
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        self.writer.comment(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            body="card changed", request_id="redelivery-ack-event",
        )
        self.runtime.production_tick()
        first = self.observers()["sprint:1"].delivery
        self.age_delivery(5)
        self.host.observer_status_result = {"last_activity": time.time(), "idle": True}
        self.runtime.production_tick()
        second = self.observers()["sprint:1"].delivery
        self.assertNotEqual(second.delivery_id, first.delivery_id)
        self.assertEqual(second.through_event, first.through_event)

        entry = {
            "selected_step": "read board", "selected_why": "card changed",
            "rejected_alternatives": "wait", "current_task": "secretary-510-pilot",
            "dod_state": "open", "next_safe_step": "resume",
        }
        # The first turn finishes after the second intent was persisted. It names the delivery it
        # was given, which is no longer the active one, so it credits nothing.
        SprintWriter(self.board, data_dir=self.data_dir).resume(  # type: ignore[arg-type]
            role="observer", actor="observer", reference="sprint:1", entry=entry,
            request_id="older-turn-resume", delivery_id=first.delivery_id,
            through_event=first.through_event,
        )
        # The head is working the redelivered batch, so this tick is only about the stale resume.
        self.host.observer_status_result = {"last_activity": time.time(), "idle": False}

        stale = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(stale)], ["observer-wake-pending"])
        self.assertEqual(self.observers()["sprint:1"].delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(self.observers()["sprint:1"].delivery.acknowledged_through, "")

        self.acknowledge_delivery(entry, request_id="active-marker-resume")
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        acknowledged = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(acknowledged)], ["observer-idle"])
        record = self.observers()["sprint:1"].delivery
        self.assertEqual(record.acknowledged_delivery_id, second.delivery_id)
        self.assertEqual(record.acknowledged_through, first.through_event)

    def test_routing_event_and_closed_sprint_do_not_wake_an_observer(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.runtime.production_tick()
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        # The dispatcher claim in the initial tick is a real card transition. Acknowledge its
        # delivery first, then prove the later routing-only audit line starts no additional turn.
        self.assertEqual(
            [row["action"] for row in self.actions(self.runtime.production_tick())], ["observer-nudged"]
        )
        entry = {
            "selected_step": "check board", "selected_why": "initial card claim", "rejected_alternatives": "wait",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "resume",
        }
        self.acknowledge_delivery(entry, request_id="routing-baseline")
        self.assertEqual(
            [row["action"] for row in self.actions(self.runtime.production_tick())], ["observer-idle"]
        )
        self.audit.append("routing-only", {
            "event_id": "evt_routing_only", "request_id": "routing-only", "ref": "secretary-510-pilot",
            "kind": "routing", "outcome": "success", "occurred_at": "2026-07-29T12:00:00Z",
        })

        routing = self.runtime.production_tick()
        self.close_sprint()
        closed = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(routing)], ["observer-idle"])
        self.assertEqual([row["action"] for row in self.actions(closed)], ["observer-stopped"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_denied_and_failed_card_events_do_not_wake_an_observer(self) -> None:
        self.open_sprint()
        self.board.tasks[0]["column_id"] = 6
        self.runtime.production_tick()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.host.observer_status_result = {"last_activity": time.time() - 2, "idle": True}
        for request_id, kind, outcome in (
            ("denied-card-event", "sprint_guard_denied", "denied"),
            ("guard-success-event", "sprint_guard_denied", "success"),
            ("failed-card-event", "commented", "failed"),
            ("missing-outcome-event", "commented", ""),
        ):
            self.audit.append(request_id, {
                "event_id": "evt_" + request_id,
                "request_id": request_id,
                "ref": "secretary-510-pilot",
                "kind": kind,
                "outcome": outcome,
                "occurred_at": "2099-01-01T00:00:00Z",
            })

        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-idle"])
        self.assertEqual(self.host.observer_nudges, [])

    def test_rotated_observer_handle_is_still_probed_for_readiness(self) -> None:
        """Orca may rotate the handle while retaining leafId, so status must read the alias."""
        self.open_sprint()
        self.runtime.production_tick()
        record = self.observers()["sprint:1"]
        self.assertEqual(record.leaf, "leaf:observer:sprint:1")
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real")
        calls: list[list[str]] = []
        stale_output = int((time.time() - 2) * 1000)

        def run_json(args: list[str]) -> dict:
            calls.append(args)
            if args[1:3] == ["terminal", "list"]:
                return {"terminals": [{
                    "handle": "observer:rotated", "leafId": record.leaf,
                    "connected": True, "lastOutputAt": stale_output,
                }]}
            if args[1:3] == ["terminal", "wait"]:
                return {"wait": {"condition": "tui-idle", "satisfied": True}}
            raise AssertionError(args)

        with mock.patch.object(real_host, "_run_json", side_effect=run_json):
            self.host.observer_status = real_host.observer_status  # type: ignore[method-assign]
            grace = self.runtime.production_tick()
            self.assertEqual([row["action"] for row in self.actions(grace)], ["observer-idle"])
            self.assertEqual(self.observers()["sprint:1"].state, "idle-grace")
        waits = [args for args in calls if args[1:3] == ["terminal", "wait"]]
        self.assertTrue(waits)
        self.assertEqual(waits[0][waits[0].index("--terminal") + 1], "observer:rotated")

    def test_active_card_does_not_relaunch_a_finished_observer_without_a_new_event(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.metadata[100]["sprint_current_task"] = "secretary-510-pilot"
        self.runtime.production_tick()
        self.host.observer_status_result = {
            "last_activity": time.time() - 2,
            "idle": True,
        }
        # The initial dispatcher tick claims the linked card after the observer comes up. That
        # transition gets one nudge; the completed turn below is then quiet despite the active
        # card remaining on the board.
        self.assertEqual(
            [row["action"] for row in self.actions(self.runtime.production_tick())], ["observer-nudged"]
        )
        entry = {
            "selected_step": "wait for card", "selected_why": "the card is active", "rejected_alternatives": "relaunch",
            "current_task": "secretary-510-pilot", "dod_state": "open", "next_safe_step": "wait for transition",
        }
        self.acknowledge_delivery(entry, request_id="active-card-baseline")
        result = self.runtime.production_tick()

        self.assertEqual([row["action"] for row in self.actions(result)], ["observer-idle"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.host.observer_nudges, ["sprint:1"])

    def test_closed_sprint_stops_the_head_and_drops_the_record(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_vanished_sprint_stops_the_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.sprints.clear()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.observers(), {})

    def test_a_reappeared_sprint_reference_is_a_second_audited_lifecycle(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.sprints.clear()
        self.runtime.production_tick()

        self.open_sprint()
        result = self.runtime.production_tick()
        self.board.sprints.clear()
        self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        events = self.audit.events("sprint:1")
        self.assertEqual(
            [event["kind"] for event in events],
            [EVENT_LAUNCHED, EVENT_STOPPED, EVENT_LAUNCHED, EVENT_STOPPED],
        )
        # The second lifecycle is its own request, not a retry of the first one.
        self.assertEqual(len({event["request_id"] for event in events}), 4)

    def test_no_open_sprint_changes_nothing(self) -> None:
        before = len(self.host.calls)

        result = self.runtime.production_tick()

        self.assertEqual(self.actions(result), [])
        self.assertNotIn("observers", self.runtime.production_state.load())
        self.assertNotIn("prepare_observer", self.host.calls[before:])
        self.assertNotIn("stop_observer", self.host.calls[before:])

    def test_budget_is_charged_from_card_events_once_and_hard_limit_stops_observer(self) -> None:
        self.catalog.instance = {"sprint_budget": {"signal": 1, "hard": 2}}
        self.runtime.sprints = SprintReader(self.board, data_dir=self.data_dir, thresholds={"signal": 1, "hard": 2})  # type: ignore[arg-type]
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot", kind="red",
            body="fix it", request_id="red-review",
        )
        first = self.runtime.production_tick()
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 1)
        self.assertTrue(self.runtime.sprints.show("sprint:1")["budget"]["signal_reached"])
        self.assertEqual(len([row for row in first["actions"] if row.get("step") == "sprint-budget"]), 1)
        repeated = self.runtime.production_tick()
        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 1)
        self.assertEqual([row for row in repeated["actions"] if row.get("step") == "sprint-budget"], [])
        self.writer.move(
            role="po", actor="operator", reference="secretary-510-pilot", target="blocked",
            reason="operator stop", sprint_override=True,
            sprint_override_reason="operator stop", request_id="blocked-card",
        )
        result = self.runtime.production_tick()
        sprint = self.runtime.sprints.show("sprint:1")
        self.assertEqual(sprint["status"], "stopped")
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 1)
        hard_stop_events = [
            event for event in self.audit.events("sprint:1")
            if event.get("kind") == "budget_hard_stopped"
        ]
        self.assertEqual(len(hard_stop_events), 1)
        self.assertEqual(hard_stop_events[0]["payload"]["reason"], "budget_hard_limit")
        self.assertIn("observer-stopped", [row.get("action") for row in self.actions(result)])

    def test_budget_event_classification_excludes_green_card_cycle(self) -> None:
        cases = {
            "red_review": {"kind": "verdict", "payload": {"marker": "review:red"}},
            "blocked": {"kind": "moved", "payload": {"to": "blocked"}},
            "red_ci": {"kind": "moved", "payload": {"to": "in_progress"}, "request_id": "gate-red"},
            "preempt": {"kind": "moved", "payload": {"from": "validate", "to": "ready"}},
            "recreated_task": {"kind": "created", "payload": {"budget_event": "recreated_task"}},
            "hotfix": {"kind": "created", "payload": {"budget_event": "hotfix"}},
        }
        self.assertEqual({name: _budget_event_type(event) for name, event in cases.items()}, {name: name for name in cases})
        self.assertIsNone(_budget_event_type({"kind": "verdict", "payload": {"marker": "review:green"}}))
        self.assertIsNone(_budget_event_type({"kind": "moved", "payload": {"to": "done"}}))

    def test_full_green_card_cycle_does_not_charge_the_sprint_budget(self) -> None:
        self.open_sprint()
        self.board.metadata[12]["sprint_ref"] = "sprint:1"

        self.writer.claim(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            worker="worker", request_id="green-claim",
        )
        self.writer.move(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            target="validate", reason="worker completed", request_id="green-validate",
        )
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot",
            kind="green", body="looks good", request_id="green-verdict",
        )
        self.writer.move(
            role="dispatcher", actor="dispatcher", reference="secretary-510-pilot",
            target="done", reason="review passed", request_id="green-done",
        )

        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show("sprint:1")["budget"]["total"], 0)
        self.assertEqual([row for row in result["actions"] if row.get("step") == "sprint-budget"], [])

    def test_unlinked_historical_budget_events_are_not_reread_on_every_tick(self) -> None:
        for index in range(20):
            task_id = 1000 + index
            reference = f"secretary-historical-{index}"
            self.board.tasks.append({
                "id": task_id, "reference": reference, "title": reference, "description": "",
                "column_id": 2, "position": task_id, "swimlane_id": 4,
                "date_creation": 1720000000, "date_modification": 1720000000,
            })
            self.board.metadata[task_id] = {"project": "secretary", "task_type": "code"}
            self.board.comments[task_id] = []
            self.audit.append(f"historical-red-{index}", {
                "event_id": f"evt_historical_red_{index}", "request_id": f"historical-red-{index}",
                "ref": reference, "kind": "verdict", "occurred_at": "2026-07-27T00:00:00Z",
                "payload": {"marker": "review:red"},
            })

        self.board.calls.clear()
        self.assertEqual(_reconcile_sprint_budget(self.runtime), [])
        first_reads = [
            params for method, params in self.board.calls
            if method == "getTaskByReference" and params.get("project_id") == 7
        ]
        self.assertEqual(len(first_reads), 20)
        self.assertEqual(
            len([event for event in self.audit.events() if event.get("kind") == "budget_unlinked"]), 20,
        )

        self.board.calls.clear()
        self.assertEqual(_reconcile_sprint_budget(self.runtime), [])
        repeated_reads = [
            params for method, params in self.board.calls
            if method == "getTaskByReference" and params.get("project_id") == 7
        ]
        self.assertEqual(repeated_reads, [])

    def test_unreadable_linked_sprint_skips_only_its_ready_cards_and_is_cached(self) -> None:
        ready = [
            {"ref": "broken-1", "sprint": "sprint:broken", "type": "code", "project": "one"},
            {"ref": "broken-2", "sprint": "sprint:broken", "type": "code", "project": "two"},
            {"ref": "claimable", "sprint": None, "type": "code", "project": "three"},
        ]
        with mock.patch("secretary.dispatcher_production._production_tasks", side_effect=[[], ready]), \
             mock.patch.object(
                 self.runtime.sprints, "show", side_effect=TaskError("backend_error", "sprint board is down", 1)
             ) as show, \
             mock.patch.object(self.runtime, "_claim", return_value={"action": "claimed"}) as claim:
            result = _production_claim_ready(self.runtime, {}, {})

        self.assertEqual(show.call_count, 1)
        self.assertEqual(claim.call_args.args[0]["ref"], "claimable")
        self.assertEqual([item["ref"] for item in result["skipped_ready"]], ["broken-1", "broken-2"])

    def test_unreadable_sprint_board_never_stops_a_live_head(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        def explode(**_kwargs):
            raise TaskError("backend_error", "kanboard is down", 1)

        with mock.patch.object(self.runtime.sprints, "list", explode):
            result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["sprint-board-unavailable"]
        )
        self.assertEqual(self.host.stopped_observers, [])
        self.assertIn("sprint:1", self.observers())

    def test_a_refused_stop_keeps_the_record_and_the_next_tick_retries(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-stop-failed"]
        )
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")
        # The head is still alive, so nothing may claim it was stopped.
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

        self.host.fail_stop_observer_reason = ""
        retry = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(retry)], ["observer-stopped"]
        )
        self.assertEqual(self.observers(), {})
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_a_refused_stop_never_yields_a_second_head_when_the_sprint_reopens(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"
        self.runtime.production_tick()

        self.board.metadata[
            int(next(item for item in self.board.sprints if item["reference"] == "sprint:1")["id"])
        ]["sprint_status"] = "open"
        self.host.fail_stop_observer_reason = ""
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "running")

    def test_a_dead_observer_without_pending_work_does_not_need_a_teardown(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.kill_observer()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-idle"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.handle, "observer:sprint:1")

    def test_a_bring_up_that_left_its_terminal_up_keeps_the_handle_on_the_record(self) -> None:
        self.open_sprint()
        pid_file = self.data_dir / "observers" / "aborted.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        # The abandoned head is a live process: only the record's own mark tells it apart from a
        # working observer.
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(pid_file),
        )

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-deferred"]
        )
        record = self.observers()["sprint:1"]
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertTrue(record.abandoned_handle)
        self.assertEqual(record.launches, 0)
        self.assertIn("terminal close failed", record.deferred_reason)
        # Nothing was launched, so no launch event may be in the log.
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])

    def test_an_abandoned_terminal_that_will_not_close_never_yields_a_second_head(self) -> None:
        self.open_sprint()
        pid_file = self.data_dir / "observers" / "aborted.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(pid_file),
        )
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        # Not "observer-live": the pid behind that handle belongs to a head that never got its
        # sprint. The tick retries the close and brings nothing new up until it succeeds.
        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-deferred"]
        )
        self.assertEqual(self.host.calls.count("prepare_observer"), 1)
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertTrue(record.abandoned_handle)
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertIn("could not be stopped", record.deferred_reason)

    def test_a_closed_abandoned_terminal_frees_the_sprint_for_a_launch(self) -> None:
        self.open_sprint()
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(self.data_dir / "observers" / "aborted.pid"),
        )
        self.runtime.production_tick()
        self.host.fail_observer_error = None

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertFalse(record.abandoned_handle)
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")][-1], EVENT_LAUNCHED)

    def test_a_freeze_takes_an_abandoned_terminal_down_with_the_rest(self) -> None:
        self.open_sprint()
        self.host.fail_observer_error = ObserverLaunchAborted(
            "TUI prompt was not delivered; observer terminal close failed: pane is busy",
            handle="observer:sprint:1",
            workspace=str(self.data_dir / "observers" / "sprint-1"),
            pid_file=str(self.data_dir / "observers" / "aborted.pid"),
        )
        self.runtime.production_tick()

        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        record = self.observers()["sprint:1"]
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(record.handle, "")
        self.assertFalse(record.abandoned_handle)

    # readiness ---------------------------------------------------------------

    def test_unready_resource_defers_the_launch_and_keeps_the_sprint(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unauthenticated", "resource authentication failed", 1.0
        )

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertIn("unauthenticated", record.deferred_reason)
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])

    # role skill delivery ------------------------------------------------------

    def test_an_undelivered_skill_defers_the_launch_instead_of_a_blind_head(self) -> None:
        self.observer_skill.unlink()
        self.open_sprint()

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertIn("observe-sprint", record.deferred_reason)
        self.assertIn(str(self.observer_skill), record.deferred_reason)
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])
        # The same reason has to be readable from outside, or the sprint just looks headless.
        self.assertIn("observe-sprint", status_observers(self.runtime.production_state.load())[0]["deferred_reason"])

    def test_a_delivered_skill_launches_after_the_deferred_retry_deadline(self) -> None:
        self.observer_skill.unlink()
        self.open_sprint()
        self.runtime.production_tick()

        install_skill_registry(self.data_dir)
        self.expire_launch_retry()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.observers()["sprint:1"].deferred_reason, "")

    def test_a_shell_without_an_observer_target_defers_the_launch(self) -> None:
        """The skill exists and is delivered somewhere, just not to the shell of this head."""
        manifest = self.data_dir / "registry" / "manifest.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('shell = "codex"', 'shell = "claude"'),
            encoding="utf-8",
        )
        self.open_sprint()

        result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launch-deferred")
        self.assertEqual(self.host.observers, [])
        self.assertIn("no codex target", self.observers()["sprint:1"].deferred_reason)

    def test_the_launched_prompt_points_at_the_delivered_skill(self) -> None:
        self.open_sprint()

        self.runtime.production_tick()

        prompt = (Path(self.observers()["sprint:1"].workspace) / "SPRINT.md").read_text(encoding="utf-8")
        self.assertIn(str(self.observer_skill), prompt)

    def test_deferred_launch_waits_for_a_persisted_retry_deadline(self) -> None:
        self.open_sprint()
        unready = HeadReadiness("openai-sub", "unavailable", "resource provider is unavailable", 1.0)
        self.runtime.head_readiness = lambda _head: unready
        self.runtime.production_tick()

        del self.runtime.head_readiness
        waiting = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(waiting)], ["observer-launch-deferred"])
        self.assertEqual(self.host.calls.count("prepare_observer"), 0)
        self.expire_launch_retry()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.deferred_reason, "")
        self.assertEqual(record.launches, 1)

    def test_a_deferral_streak_records_one_event(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )

        self.runtime.production_tick()
        self.runtime.production_tick()

        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_DEFERRED])

    def test_failed_bring_up_keeps_the_record_intact(self) -> None:
        self.open_sprint()
        self.host.fail_observer_reason = "orca refused the terminal"

        result = self.runtime.production_tick()

        self.assertEqual(self.actions(result)[0]["action"], "observer-launch-deferred")
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 0)
        self.assertIn("orca refused the terminal", record.deferred_reason)

    def test_failed_bring_up_is_not_retried_on_every_tick(self) -> None:
        self.open_sprint()
        self.host.fail_observer_reason = "orca refused the terminal"

        self.runtime.production_tick()
        repeated = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(repeated)], ["observer-launch-deferred"])
        self.assertEqual(self.host.calls.count("prepare_observer"), 1)
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launch_attempts, 1)
        self.assertGreater(record.launch_next_at, time.time())

    # audit durability --------------------------------------------------------

    def broken_append(self):
        """Audit storage that takes a staged event but refuses to commit it."""
        def explode(_request_id, _event):
            raise OSError("audit log is not writable")

        return mock.patch.object(self.audit, "append", explode)

    def broken_stage(self):
        """Audit storage that cannot even take the staged event."""
        def explode(_request_id, _event):
            raise OSError("pending audit directory is not writable")

        return mock.patch.object(self.audit, "stage", explode)

    def test_a_refused_audit_append_still_leaves_the_launched_head_on_the_books(self) -> None:
        self.open_sprint()

        with self.broken_append():
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-launched")
        self.assertEqual(action["audit"], "pending")
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.state, "running")
        self.assertTrue(record.handle)
        # The event is not lost: it waits on disk for the repair pass.
        pending = self.audit.pending_events()
        self.assertEqual([event["kind"] for event in pending], [EVENT_LAUNCHED])
        self.assertEqual(pending[0]["payload"]["workspace"], record.workspace)
        self.audit.reconcile()
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

    def test_a_refused_audit_append_does_not_yield_a_second_head(self) -> None:
        self.open_sprint()
        with self.broken_append():
            self.runtime.production_tick()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_an_unwritable_audit_defers_the_launch_instead_of_opening_a_head(self) -> None:
        self.open_sprint()

        with self.broken_stage():
            result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-deferred"]
        )
        self.assertEqual(self.host.observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 0)
        self.assertIn("could not be staged", record.deferred_reason)

        self.expire_launch_retry()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    # crash-safe launch intent ------------------------------------------------

    def failing_state_save(self, *, after: int = 0):
        """Production state that stops accepting writes after `after` of them landed.

        `after=0` is a data plane that is down before the tick touches the host; `after=1` lets
        the launch intent land and then kills every write after it, which is a tick that dies
        between opening the head and recording that it did.
        """
        real = self.runtime.production_state.save
        landed = {"count": 0}

        def save(payload):
            if landed["count"] >= after:
                raise OSError("production state is not writable")
            landed["count"] += 1
            real(payload)

        return mock.patch.object(self.runtime.production_state, "save", save)

    def test_the_launch_intent_is_on_disk_before_the_host_is_called(self) -> None:
        self.open_sprint()
        seen: list = []
        real = self.host.prepare_observer

        def spy(sprint, head, *, prompt, identity=None):
            seen.append(load_observers(self.runtime.production_state.load()).get("sprint:1"))
            return real(sprint, head, prompt=prompt, identity=identity)

        with mock.patch.object(self.host, "prepare_observer", spy):
            self.runtime.production_tick()

        intent = seen[0]
        self.assertIsNotNone(intent)
        self.assertEqual(intent.state, "launching")
        self.assertEqual(intent.pending_launch, 1)
        self.assertEqual(intent.launches, 0)
        # Workspace and pid file are the head's own, known before the head exists.
        self.assertEqual(intent.workspace, self.host.observer_workspace("sprint:1"))
        self.assertEqual(intent.pid_file, self.host.observer_pid_file("sprint:1"))

    def test_state_that_cannot_be_written_launches_no_head_at_all(self) -> None:
        self.open_sprint()

        with self.failing_state_save():
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        self.assertEqual(self.host.observers, [])
        self.assertEqual(self.observers(), {})

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].launches, 1)

    def test_a_launch_intent_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch, intent.handle), ("launching", 1, ""))
        self.assertEqual(self.host.observers, ["sprint:1"])

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-adopted"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.pending_launch, 0)
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

        live = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(live)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_no_tick_after_a_refused_confirmation_ever_opens_a_second_head(self) -> None:
        """The invariant the worker and reviewer contours were ported from (secretary-820).

        The head is up and the write that would have confirmed it refused. Every tick after that,
        not only the first, has to resolve the intent to the head that is already running.
        """
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "launching")

        actions = [
            action["action"]
            for _ in range(3)
            for action in self.actions(self.runtime.production_tick())
        ]

        self.assertEqual(actions, ["observer-adopted", "observer-live", "observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_an_adopted_head_is_stopped_through_its_workspace(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()
        self.runtime.production_tick()
        self.close_sprint()

        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-stopped"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})

    def test_a_freeze_takes_an_adopted_head_down_too(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()
        self.runtime.production_tick()

        status = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="host maintenance"
        )

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].state, "stopped-by-pause")

    def test_an_intent_whose_head_died_before_the_next_tick_is_a_relaunch(self) -> None:
        self.open_sprint()
        # The heartbeat writes a pid nobody is running under, so the head of the lost tick is dead.
        self.host.observer_pid = DEAD_PID
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()

        self.host.observer_pid = os.getpid()
        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-relaunched"]
        )
        # Whatever the lost tick opened is closed before the replacement head opens.
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.host.observers, ["sprint:1", "sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.state, "running")
        # The attempt the log already carries is spent, so the new head gets its own line.
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_RELAUNCHED],
        )

    def test_a_tick_killed_before_the_host_answered_retries_the_same_attempt(self) -> None:
        self.open_sprint()

        with mock.patch.object(self.host, "prepare_observer", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.production_tick()

        self.assertEqual(self.host.observers, [])
        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch), ("launching", 1))
        # The event of that attempt never made it out of pending, so nothing was observed to launch.
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_LAUNCHED])

        self.expire_launch_grace()
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-launched"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertEqual(self.observers()["sprint:1"].launches, 1)
        self.assertEqual(self.audit.pending_events(), [])
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

    def test_an_intent_without_a_pid_yet_waits_out_its_grace_window(self) -> None:
        """A head that has been launched but has not written its pid is not a dead head.

        Its own grace window says so, so the tick leaves the intent alone: no stop, no second
        prepare_observer. Cutting a head that is still coming up would break the one continuous
        session the sprint is supposed to have.
        """
        self.open_sprint()
        with mock.patch.object(self.host, "prepare_observer", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.production_tick()
        self.host.calls.clear()

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-launch-pending"]
        )
        self.assertNotIn("prepare_observer", self.host.calls)
        self.assertNotIn("stop_observer", self.host.calls)
        intent = self.observers()["sprint:1"]
        self.assertEqual((intent.state, intent.pending_launch, intent.launches), ("launching", 1, 0))
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_LAUNCHED])
        self.assertEqual(self.audit.events("sprint:1"), [])

    def test_a_waiting_intent_whose_head_appears_is_adopted(self) -> None:
        self.open_sprint()
        with self.failing_state_save(after=1):
            with self.assertRaises(OSError):
                self.runtime.production_tick()
        pid_file = Path(self.observers()["sprint:1"].pid_file)
        written = pid_file.read_text(encoding="utf-8")
        pid_file.unlink()

        waiting = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(waiting)], ["observer-launch-pending"]
        )

        pid_file.write_text(written, encoding="utf-8")
        result = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-adopted"])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_refused_audit_append_still_drops_the_stopped_record(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        with self.broken_append():
            result = self.runtime.production_tick()

        action = self.actions(result)[0]
        self.assertEqual(action["action"], "observer-stopped")
        self.assertEqual(action["audit"], "pending")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        # The terminal is gone, so no record may keep pointing at it.
        self.assertEqual(self.observers(), {})
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_STOPPED])
        self.audit.reconcile()
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_an_unwritable_audit_parks_the_stop_instead_of_performing_it(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.close_sprint()

        with self.broken_stage():
            result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in self.actions(result)], ["observer-stop-failed"]
        )
        self.assertEqual(self.host.stopped_observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")

        retry = self.runtime.production_tick()

        self.assertEqual([action["action"] for action in self.actions(retry)], ["observer-stopped"])
        self.assertEqual(self.observers(), {})

    def test_the_repair_pass_commits_a_staged_event_of_a_sprint_that_is_gone(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.board.sprints.clear()
        with self.broken_append():
            self.runtime.production_tick()

        repaired, unresolved = self.writer.reconcile()

        self.assertEqual((repaired, unresolved), (1, 0))
        self.assertEqual(self.audit.pending_events(), [])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_a_refused_audit_append_still_records_the_freeze_stop(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        with self.broken_append():
            status = self.runtime.pause_pipeline(
                mode="freeze", actor="operator", reason="host maintenance"
            )

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertEqual([event["kind"] for event in self.audit.pending_events()], [EVENT_STOPPED])

    def test_an_unwritable_audit_keeps_the_freeze_stop_pending(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        with self.broken_stage():
            status = self.runtime.pause_pipeline(
                mode="freeze", actor="operator", reason="host maintenance"
            )

        self.assertEqual(status["stopped_observer"], [])
        self.assertEqual(self.host.stopped_observers, [])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "pause-stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")

        result = self.runtime.production_tick()

        self.assertEqual(
            [row["action"] for row in result["observer_stops"]], ["observer-stopped-by-pause"]
        )
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])

    # pause -------------------------------------------------------------------

    def test_freeze_stops_the_observer_and_records_the_reason(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        status = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="host maintenance"
        )

        self.assertEqual(status["stopped_observer"], ["sprint:1"])
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertIn("host maintenance", record.stopped_reason)
        self.assertGreater(record.paused_at, 0)

    def test_a_refused_freeze_stop_is_reported_and_retried_by_the_frozen_tick(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        status = self.runtime.pause_pipeline(
            mode="freeze", actor="operator", reason="host maintenance"
        )

        self.assertEqual(status["stopped_observer"], [])
        self.assertTrue(any("sprint:1" in warning for warning in status["warnings"]))
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "pause-stop-pending")
        self.assertEqual(record.handle, "observer:sprint:1")
        self.assertEqual([event["kind"] for event in self.audit.events("sprint:1")], [EVENT_LAUNCHED])

        self.host.fail_stop_observer_reason = ""
        result = self.runtime.production_tick()

        self.assertEqual(
            [row["action"] for row in result["observer_stops"]], ["observer-stopped-by-pause"]
        )
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stopped-by-pause")
        self.assertEqual(record.handle, "")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:1")],
            [EVENT_LAUNCHED, EVENT_STOPPED],
        )

    def test_a_head_that_survived_a_refused_freeze_stop_is_not_relaunched_on_resume(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        resumed = self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        self.assertEqual(resumed["observers_resumed"], ["sprint:1"])
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-live"])
        self.assertEqual(self.host.observers, ["sprint:1"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.paused_at, 0.0)

    def test_resume_brings_the_observer_back_on_the_next_tick(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        resumed = self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        self.assertEqual(resumed["observers_resumed"], ["sprint:1"])
        self.assertEqual([action["action"] for action in self.actions(result)], ["observer-relaunched"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 2)
        self.assertEqual(record.paused_at, 0.0)

    def test_drain_leaves_a_live_observer_alone_and_launches_none(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()

        actions = {action["sprint"]: action["action"] for action in self.actions(result)}
        self.assertEqual(actions["sprint:1"], "observer-live")
        self.assertEqual(actions["sprint:2"], "observer-launch-skipped")
        self.assertEqual(self.host.stopped_observers, [])
        self.assertEqual(self.host.observers, ["sprint:1"])

    def test_a_sprint_opened_during_drain_is_still_visible_from_outside(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        self.runtime.production_tick()

        record = self.observers()["sprint:2"]
        self.assertEqual(record.head, "codex-observer")
        self.assertEqual(record.state, "deferred")
        self.assertEqual(record.launches, 0)
        self.assertEqual(record.deferred_reason, "pipeline is draining")
        self.assertTrue(record.last_action_at)
        observed = {row["sprint"]: row for row in self.runtime.production_observe()["observers"]}
        status = {row["sprint"]: row for row in status_observers(self.runtime.production_state.load())}
        for row in (observed["sprint:2"], status["sprint:2"]):
            self.assertEqual(row["head"], "codex-observer")
            self.assertFalse(row["alive"])
            self.assertIn("draining", row["deferred_reason"])
            self.assertTrue(row["last_action_at"])

    def test_the_drained_sprint_is_launched_from_its_record_after_resume(self) -> None:
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")
        self.runtime.production_tick()
        generation = self.observers()["sprint:2"].generation

        self.runtime.resume_pipeline(actor="operator")
        result = self.runtime.production_tick()

        actions = {action["sprint"]: action["action"] for action in self.actions(result)}
        self.assertEqual(actions["sprint:2"], "observer-launched")
        record = self.observers()["sprint:2"]
        self.assertEqual(record.generation, generation)
        self.assertEqual(record.state, "running")
        self.assertEqual(record.launches, 1)
        self.assertEqual(record.deferred_reason, "")
        self.assertEqual(self.host.observers, ["sprint:2"])
        kinds = [event["kind"] for event in self.audit.events("sprint:2")]
        self.assertEqual(kinds, [EVENT_DEFERRED, EVENT_LAUNCHED])

    def test_repeated_drain_ticks_write_one_deferral_event(self) -> None:
        self.open_sprint("sprint:2")
        self.runtime.pause_pipeline(mode="drain", actor="operator", reason="host maintenance")

        self.runtime.production_tick()
        before = self.observers()["sprint:2"]
        self.runtime.production_tick()

        self.assertEqual(self.observers()["sprint:2"].generation, before.generation)
        self.assertEqual(self.observers()["sprint:2"].launches, 0)
        self.assertEqual(self.host.observers, [])
        self.assertEqual(
            [event["kind"] for event in self.audit.events("sprint:2")], [EVENT_DEFERRED]
        )

    def test_frozen_tick_launches_nothing(self) -> None:
        self.open_sprint()
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.host.observers, [])

    # isolation ---------------------------------------------------------------

    def test_a_live_observer_does_not_block_a_card_claim(self) -> None:
        self.open_sprint()

        result = self.runtime.production_tick()

        claim = [action for action in result["actions"] if action.get("step") == "claim"]
        self.assertEqual(claim[0]["pilot_ref"], "secretary-510-pilot")
        self.assertEqual(self.reader.show("secretary-510-pilot")["state"], "in_progress")
        # The observer holds no card record and does not occupy the per-project claim gate.
        records = self.runtime.production_state.records(self.runtime.production_state.load())
        self.assertEqual(sorted(records), ["secretary-510-pilot"])
        self.assertNotIn("sprint:1", records)

    def test_the_observer_workspace_is_not_a_card_workspace(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        record = self.observers()["sprint:1"]
        card = self.runtime.production_state.records(self.runtime.production_state.load())
        self.assertNotEqual(record.workspace, card["secretary-510-pilot"].workspace)
        self.assertNotEqual(record.handle, card["secretary-510-pilot"].handle)

    # observability -----------------------------------------------------------

    def test_observer_state_is_visible_without_a_transcript(self) -> None:
        self.open_sprint()
        self.runtime.production_tick()

        observed = self.runtime.production_observe()["observers"]
        status = status_observers(self.runtime.production_state.load())

        self.assertEqual(observed[0]["sprint"], "sprint:1")
        self.assertEqual(observed[0]["head"], "codex-observer")
        self.assertTrue(observed[0]["alive"])
        self.assertEqual(status[0]["state"], "running")
        self.assertEqual(status[0]["launches"], 1)
        self.assertIsNotNone(status[0]["last_action_at"])
        self.assertIsNone(status[0]["deferred_reason"])

    def test_status_reports_the_deferred_reason(self) -> None:
        self.open_sprint()
        self.runtime.head_readiness = lambda _head: HeadReadiness(
            "openai-sub", "unavailable", "resource provider is unavailable", 1.0
        )
        self.runtime.production_tick()

        status = status_observers(self.runtime.production_state.load())

        self.assertEqual(status[0]["state"], "deferred")
        self.assertFalse(status[0]["alive"])
        self.assertIn("unavailable", status[0]["deferred_reason"])

    # two open sprints --------------------------------------------------------

    def open_disjoint_pair(self) -> None:
        """The admitted pair, with one card in each of the four reserved projects."""
        self.sprint_writer = self.admit_two_open_sprints(
            observer=head_choice("codex-observer")
        )
        self.link_pair_cards()

    def budget_of(self, reference: str) -> dict:
        return self.runtime.sprints.show(reference, include_cards=False)["budget"]

    def charge(self, reference: str, request_id: str) -> None:
        """Put one budget-shaped card event on the board card of `reference`'s sprint."""
        self.writer.move(
            role="po", actor="operator", reference=reference, target="blocked",
            reason="operator stop", sprint_override=True,
            sprint_override_reason="operator stop", request_id=request_id,
        )

    def claimed(self, result: dict) -> list[dict]:
        return [action for action in result["actions"] if action.get("step") == "claim"]

    def skipped(self, result: dict) -> list[dict]:
        for action in result["actions"]:
            if action.get("step") in {"claim", "production-claim"}:
                return list(action.get("skipped_ready") or [])
        return []

    def advanced(self, result: dict) -> list[str]:
        return [
            action["pilot_ref"] for action in result["actions"] if action["step"] == "advance"
        ]

    def with_thresholds(self, signal: int, hard: int) -> None:
        self.catalog.instance = {"sprint_budget": {"signal": signal, "hard": hard}}
        self.runtime.sprints = SprintReader(  # type: ignore[arg-type]
            self.board, data_dir=self.data_dir, thresholds={"signal": signal, "hard": hard},
        )

    def settled_pair(self) -> None:
        """Two ticks: the declared head is up, and one card of each sprint is in flight.

        The first tick fences `sprint:1` because its head has not been launched yet, so the
        card claimed there is the other sprint's; the second claims `sprint:1`'s own.
        """
        self.open_disjoint_pair()
        self.assertEqual(
            self.claimed(self.runtime.production_tick())[0]["pilot_ref"], "secretary-510-neighbor",
        )
        self.assertEqual(
            self.claimed(self.runtime.production_tick())[0]["pilot_ref"], "secretary-510-pilot",
        )

    def test_a_card_event_charges_the_sprint_it_is_linked_to_and_no_other(self) -> None:
        self.with_thresholds(1, 3)
        self.settled_pair()
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot", kind="red",
            body="fix it", request_id="red-review-first-sprint",
        )

        result = self.runtime.production_tick()

        charged = [action for action in result["actions"] if action.get("step") == "sprint-budget"]
        self.assertEqual([action["sprint"] for action in charged], ["sprint:1"])
        self.assertEqual(self.budget_of("sprint:1")["total"], 1)
        self.assertTrue(self.budget_of("sprint:1")["signal_reached"])
        self.assertEqual(self.budget_of("sprint:2")["total"], 0)
        self.assertFalse(self.budget_of("sprint:2")["signal_reached"])
        self.assertEqual(self.runtime.sprints.show("sprint:2")["status"], "open")

    def test_a_red_review_that_restarts_a_worker_charges_only_its_own_sprint(self) -> None:
        """The operational restart, not a synthesised event: review red, worker restarted.

        The card driven here belongs to the sprint that declares no observer, so the verdict
        acts at once rather than parking for a decision.
        """
        self.with_thresholds(1, 6)
        self.open_disjoint_pair()
        self.assertEqual(
            self.claimed(self.runtime.production_tick())[0]["pilot_ref"], "secretary-510-neighbor",
        )
        # Through the command in the checkout: that id is what attributes the report to the round
        # the dispatcher is waiting for (secretary-1063).
        workspace = (self.runtime.production_state.load()["records"]
                     ["secretary-510-neighbor"]["workspace"])
        document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
        done_command = next(
            line for line in document.splitlines() if "--kind done" in line
        )
        self.writer.report(
            role="worker", actor="worker", reference="secretary-510-neighbor", kind="done",
            body="ready for validation",
            request_id=done_command.split("--request-id ", 1)[1].split()[0],
        )
        self.assertEqual(self.runtime.production_tick()["status"], "ok")  # moved to validate
        self.assertIn("review-started", [action["action"] for action in self.runtime.production_tick()["actions"] if action["step"] == "review"])
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-neighbor", kind="red",
            body="needs work", request_id="rework-red-verdict",
        )

        result = self.runtime.production_tick()

        self.assertIn(
            "rework-started",
            [action["action"] for action in result["actions"] if action["step"] == "review"],
        )
        self.assertIn("restart_worker", self.host.calls)
        charged = [action for action in result["actions"] if action.get("step") == "sprint-budget"]
        self.assertEqual(
            [(action["sprint"], action["event_type"]) for action in charged],
            [("sprint:2", "red_review")],
        )
        self.assertEqual(self.budget_of("sprint:2")["total"], 1)
        self.assertTrue(self.budget_of("sprint:2")["signal_reached"])
        self.assertEqual(self.budget_of("sprint:1")["total"], 0)
        self.assertFalse(self.budget_of("sprint:1")["signal_reached"])
        self.assertEqual(self.runtime.sprints.show("sprint:1")["status"], "open")

    def test_a_hard_stop_stops_one_sprint_while_the_other_keeps_claiming(self) -> None:
        self.with_thresholds(1, 2)
        self.settled_pair()
        self.writer.verdict(
            role="reviewer", actor="reviewer", reference="secretary-510-pilot", kind="red",
            body="fix it", request_id="red-first-sprint",
        )
        self.charge("secretary-510-pilot", "blocked-first-sprint")

        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show("sprint:1")["status"], "stopped")
        self.assertEqual(self.runtime.sprints.show("sprint:2")["status"], "open")
        self.assertEqual(self.budget_of("sprint:1")["total"], 2)
        self.assertEqual(self.budget_of("sprint:2")["total"], 0)
        self.assertEqual(
            [
                event["ref"] for event in self.audit.events()
                if event["kind"] == "budget_hard_stopped"
            ],
            ["sprint:1"],
        )
        # The stopped sprint's head is stopped and its remaining Ready card is left alone; the
        # other sprint claims its own in the same tick.
        self.assertIn("observer-stopped", [action["action"] for action in self.actions(result)])
        self.assertEqual(self.claimed(result)[0]["pilot_ref"], "third-1")
        self.assertEqual(
            self.skipped(result),
            [{"ref": "fourth-1", "reason": "linked sprint is stopped or closed"}],
        )
        self.assertEqual(self.reader.show("third-1")["state"], "in_progress")
        self.assertEqual(self.reader.show("fourth-1")["state"], "ready")

    def test_closing_the_observed_sprint_leaves_the_other_live_and_claiming(self) -> None:
        self.settled_pair()
        self.assertEqual(self.host.observers, ["sprint:1"])

        self.sprint_writer.close(role="po", actor="operator", reference=self.FIRST)
        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show(self.FIRST)["status"], "closed")
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(self.observers(), {})
        self.assertEqual(self.runtime.sprints.show(self.SECOND)["status"], "open")
        self.assertEqual(self.claimed(result)[0]["pilot_ref"], "third-1")
        self.assertEqual(
            self.skipped(result),
            [{"ref": "fourth-1", "reason": "linked sprint is stopped or closed"}],
        )
        # The open sprint's card in flight keeps riding its cycle.
        self.assertIn("secretary-510-neighbor", self.advanced(result))

    def test_closing_the_second_sprint_leaves_the_first_live_and_claiming(self) -> None:
        """The other way round: the sprint closed here is not the first one opened."""
        self.settled_pair()

        self.sprint_writer.close(role="po", actor="operator", reference=self.SECOND)
        result = self.runtime.production_tick()

        self.assertEqual(self.runtime.sprints.show(self.SECOND)["status"], "closed")
        self.assertEqual(self.host.stopped_observers, [])
        # The head of the sprint that stayed open is untouched and still alive.
        self.assertEqual(self.host.observers, ["sprint:1"])
        self.assertTrue(observer_alive(self.observers()[self.FIRST])["alive"])
        self.assertEqual(self.runtime.sprints.show(self.FIRST)["status"], "open")
        self.assertEqual(self.claimed(result)[0]["pilot_ref"], "fourth-1")
        self.assertIn("secretary-510-pilot", self.advanced(result))
        # The closed sprint's own Ready card is the one left alone, on the next pass that
        # reaches it.
        self.assertEqual(
            self.skipped(self.runtime.production_tick()),
            [{"ref": "third-1", "reason": "linked sprint is stopped or closed"}],
        )
        self.assertEqual(self.reader.show("third-1")["state"], "ready")


class ObserverConfigurationTests(unittest.TestCase):
    def test_the_observer_head_comes_from_its_own_role_default(self) -> None:
        canonical = canonical_heads(Path(__file__).resolve().parents[1])
        role_defaults = canonical["role_defaults"]

        self.assertIn("observer", role_defaults)
        self.assertNotEqual(role_defaults["observer"], role_defaults["new_card"])
        self.assertIn(role_defaults["observer"], canonical["profiles"])
        self.assertIn(OBSERVER_HEAD_FALLBACK, canonical["profiles"])

    def test_a_registry_without_the_key_falls_back_to_the_named_profile(self) -> None:
        catalog = FakeCatalog()
        catalog.role_defaults.pop("observer")

        self.assertEqual(catalog.observer_head(), OBSERVER_HEAD_FALLBACK)

    def test_the_observer_runs_with_the_role_scoped_environment(self) -> None:
        self.assertIn("observer", ROLE_ALLOWLIST)
        # The worker's environment plus the two names that say which sprint this head observes.
        self.assertEqual(
            ROLE_ALLOWLIST["observer"],
            (*ROLE_ALLOWLIST["worker"], OBSERVER_SPRINT_ENV, OBSERVER_GENERATION_ENV),
        )
        self.assertIn("observer", ROLE_REQUIRED)

        env_file = Path(tempfile.mkdtemp()) / "runtime.env"
        env_file.write_text(
            "KANBOARD_URL=http://board\nKANBOARD_API_USER=u\nKANBOARD_API_TOKEN=t\n"
            "UNRELATED_SECRET_TOKEN=leak\n",
            encoding="utf-8",
        )

        env = runtime_env("observer", base_env={"PATH": "/usr/bin"}, env_file=env_file)

        self.assertEqual(env["BOARD_ROLE"], "observer")
        self.assertEqual(env["KANBOARD_URL"], "http://board")
        self.assertNotIn("UNRELATED_SECRET_TOKEN", env)

    def test_runtime_env_cannot_rename_the_sprint_a_head_observes(self) -> None:
        """`runtime.env` is a file inside an installation, and the binding is not its to give.

        A line there naming another sprint is how a head would come up able to write on work it
        was never launched for, so the launcher's value wins the way the installation binding
        does. A head the launcher bound to nothing takes nothing from the file either.
        """
        env_file = Path(tempfile.mkdtemp()) / "runtime.env"
        env_file.write_text(
            "KANBOARD_URL=http://board\nKANBOARD_API_USER=u\nKANBOARD_API_TOKEN=t\n"
            f"{OBSERVER_SPRINT_ENV}=sprint:somebody-else\n"
            f"{OBSERVER_GENERATION_ENV}=forged\n",
            encoding="utf-8",
        )

        bound = runtime_env(
            "observer",
            base_env={"PATH": "/usr/bin", **observer_binding("sprint:1126", "abc123")},
            env_file=env_file,
        )
        self.assertEqual(bound[OBSERVER_SPRINT_ENV], "sprint:1126")
        self.assertEqual(bound[OBSERVER_GENERATION_ENV], "abc123")

        unbound = runtime_env("observer", base_env={"PATH": "/usr/bin"}, env_file=env_file)
        self.assertNotIn(OBSERVER_SPRINT_ENV, unbound)
        self.assertNotIn(OBSERVER_GENERATION_ENV, unbound)

    def test_half_a_binding_is_no_identity(self) -> None:
        """The launcher renders both names or neither, so a lone sprint came from somewhere else."""
        self.assertEqual(observer_binding("sprint:1126", ""), {})
        self.assertEqual(observer_binding("", "abc123"), {})
        self.assertEqual(
            declared_observer_sprint({OBSERVER_SPRINT_ENV: "sprint:1126"}), "",
        )
        self.assertEqual(
            declared_observer_sprint(observer_binding("sprint:1126", "abc123")), "sprint:1126",
        )

    def test_the_prompt_is_rendered_from_the_live_sprint(self) -> None:
        prompt = render_observer_prompt(
            {
                "ref": "sprint:9",
                "goal": "make the pipeline autonomous",
                "definition_of_done": "an operator sleeps through a sprint",
                "repositories": ["secretary", "codegen"],
                "status": "open",
                "current_task": "secretary-800",
                "budget": {"total": 3},
            },
            skill_path="/shell/skills/observe-sprint/SKILL.md",
        )

        self.assertIn("sprint:9", prompt)
        self.assertIn("make the pipeline autonomous", prompt)
        self.assertIn("an operator sleeps through a sprint", prompt)
        self.assertIn("- secretary", prompt)
        self.assertIn("- codegen", prompt)
        self.assertIn("secretary-800", prompt)
        self.assertIn("/shell/skills/observe-sprint/SKILL.md", prompt)

    def test_the_prompt_points_at_the_skill_instead_of_restating_it(self) -> None:
        """The launch document carries data and one pointer; the instructions live in the skill."""
        prompt = render_observer_prompt(
            {"ref": "sprint:9", "goal": "goal", "definition_of_done": "dod"},
            skill_path="/shell/skills/observe-sprint/SKILL.md",
        )
        canonical = (
            Path(__file__).resolve().parents[1]
            / "skills" / "roles" / "observer" / "observe-sprint" / "SKILL.md"
        ).read_text(encoding="utf-8")

        # Every heading the skill owns is a section the prompt must not carry a second copy of.
        headings = [line for line in canonical.splitlines() if line.startswith("## ")]
        self.assertTrue(headings)
        self.assertEqual([heading for heading in headings if heading in prompt], [])
        self.assertNotIn("sprint resume", prompt)
        self.assertNotIn("task create", prompt)

    def test_request_ids_are_stable_per_launch_generation(self) -> None:
        self.assertEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-a", 1),
        )
        self.assertNotEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-a", 2),
        )
        # Two lifecycles of the same sprint reference are two different requests.
        self.assertNotEqual(
            observer_request_id("launch", "sprint:1", "gen-a", 1),
            observer_request_id("launch", "sprint:1", "gen-b", 1),
        )

    def test_each_record_gets_its_own_generation(self) -> None:
        self.assertNotEqual(
            ObserverRecord(sprint="sprint:1").generation,
            ObserverRecord(sprint="sprint:1").generation,
        )

    def test_legacy_wake_flags_migrate_to_one_fail_closed_delivery(self) -> None:
        record = ObserverRecord.from_json({
            "sprint": "sprint:1",
            "acknowledged_event_id": "evt_older",
            "wake_event_id": "evt_pending",
            "wake_sent": True,
            "wake_attempts": 2,
            "wake_next_at": 123.0,
            "wake_reason": "old wake",
        })

        self.assertEqual(record.delivery.stage, DeliveryStage.AWAITING_ACK)
        self.assertEqual(record.delivery.acknowledged_through, "evt_older")
        self.assertEqual(record.delivery.through_event, "evt_pending")
        self.assertFalse(record.delivery.resume_cursor_known)
        persisted = record.to_json()
        self.assertIn("delivery", persisted)
        self.assertNotIn("wake_sent", persisted)


class ObserverTerminalStatusTests(unittest.TestCase):
    def test_a_busy_pane_survives_the_hosts_non_zero_exit_path(self) -> None:
        """Through the real runner: Orca exits non-zero for a busy pane, and it is still busy.

        `_run` raises on the exit code before any JSON is parsed, so the only place the answer
        survives is the text of the failure. Reading it wrong would replace a working observer.
        """
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            record = ObserverRecord(
                sprint="sprint:1", workspace="/workspace", handle="observer:sprint:1"
            )
            listed = json.dumps({"result": {"terminals": [{
                "handle": "observer:sprint:1", "connected": True, "lastOutputAt": 1_753_456_789_123,
            }]}})

            def run(args, **kwargs):
                if args[1:3] == ["terminal", "list"]:
                    return subprocess.CompletedProcess(args, 0, stdout=listed, stderr="")
                if args[1:3] == ["terminal", "wait"]:
                    # Exactly what the CLI does with a pane it found working: non-zero exit, and
                    # the answer on stdout.
                    return subprocess.CompletedProcess(
                        args, 1, stdout=BLOCKED_PANE_WAIT_BODY, stderr=""
                    )
                raise AssertionError(args)

            with mock.patch.object(dispatcher_module.subprocess, "run", side_effect=run):
                status = host.observer_status(record)

        self.assertEqual(status, {"last_activity": 1_753_456_789.123, "idle": False})

    def test_a_pane_nothing_can_be_sent_to_refuses_instead_of_reading_busy(self) -> None:
        """A head that cannot be addressed is not a working one, whatever its pid says."""
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            adopted = ObserverRecord(sprint="sprint:1", workspace="/workspace")

            # A record whose handle died with the tick that launched it: nothing to read at all.
            with self.assertRaises(HostError):
                host.observer_status(adopted)

            record = ObserverRecord(
                sprint="sprint:1", workspace="/workspace", handle="observer:sprint:1"
            )
            for terminals in (
                [],
                [{"handle": "observer:sprint:1", "connected": False}],
            ):
                def run_json(args: list[str], answer=terminals) -> dict:
                    if args[1:3] == ["terminal", "list"]:
                        return {"terminals": answer}
                    raise AssertionError(args)

                with mock.patch.object(host, "_run_json", side_effect=run_json), \
                     self.assertRaises(HostError):
                    host.observer_status(record)

    def test_real_host_nudge_carries_the_active_delivery_marker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            record = ObserverRecord(
                sprint="sprint:1",
                workspace="/workspace",
                handle="observer:sprint:1",
                delivery=ObserverDelivery(
                    delivery_id="delivery-1", through_event="evt-card-1",
                ),
            )
            calls: list[list[str]] = []

            def run_json(args: list[str]) -> dict:
                calls.append(args)
                if args[1:3] == ["terminal", "list"]:
                    return {"terminals": [{"handle": "observer:sprint:1", "connected": True}]}
                if args[1:3] == ["terminal", "send"]:
                    return {}
                if args[1:3] == ["terminal", "wait"]:
                    # Ready before the send, working after it: the pane took the prompt.
                    sends = [call for call in calls if call[1:3] == ["terminal", "send"]]
                    return {"wait": {"condition": "tui-idle", "satisfied": not sends}}
                raise AssertionError(args)

            with mock.patch.object(host, "_run_json", side_effect=run_json):
                outcome = host.nudge_observer(record)

        sent = next(args for args in calls if args[1:3] == ["terminal", "send"])
        message = sent[sent.index("--text") + 1]
        self.assertIn("--delivery-id delivery-1", message)
        self.assertIn("--through-event evt-card-1", message)
        # The pane started a turn, and that alone does not close an observer delivery.
        self.assertEqual(outcome, "accepted")
        self.assertEqual(
            [call[1:3] for call in calls],
            [["terminal", "list"], ["terminal", "wait"], ["terminal", "send"], ["terminal", "wait"]],
        )

    def test_real_host_nudge_on_a_claude_head_needs_no_screen_marker(self) -> None:
        """The gap this closes: a claude pane shows none of Codex's screen forms.

        Nothing here answers `terminal read` at all. The wake still goes out, because readiness and
        acceptance both come from Orca's `tui-idle`, and it is refused only by a caller criterion.
        """
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            record = ObserverRecord(
                sprint="sprint:1",
                workspace="/workspace",
                handle="observer:sprint:1",
                delivery=ObserverDelivery(delivery_id="delivery-2", through_event="evt-card-2"),
            )
            calls: list[list[str]] = []
            acknowledged: list[bool] = [False]

            def run_json(args: list[str]) -> dict:
                calls.append(args)
                if args[1:3] == ["terminal", "list"]:
                    return {"terminals": [{"handle": "observer:sprint:1", "connected": True}]}
                if args[1:3] == ["terminal", "send"]:
                    # The head answers: its resume for this delivery reaches the audit log.
                    acknowledged[0] = True
                    return {}
                if args[1:3] == ["terminal", "wait"]:
                    return {"wait": {"condition": "tui-idle", "satisfied": True}}
                raise AssertionError(args)

            with mock.patch.object(host, "_run_json", side_effect=run_json):
                outcome = host.nudge_observer(record, confirm=lambda _sent_at: acknowledged[0])

        self.assertEqual(outcome, "confirmed")
        self.assertEqual([call for call in calls if call[1:3] == ["terminal", "read"]], [])

    def test_real_host_nudge_refuses_a_wake_the_pane_never_took(self) -> None:
        """A pane that stays idle swallowed the prompt: retries, then an explicit failure."""
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            record = ObserverRecord(
                sprint="sprint:1",
                workspace="/workspace",
                handle="observer:sprint:1",
                delivery=ObserverDelivery(delivery_id="delivery-3", through_event="evt-card-3"),
            )
            calls: list[list[str]] = []

            def run_json(args: list[str]) -> dict:
                calls.append(args)
                if args[1:3] == ["terminal", "list"]:
                    return {"terminals": [{"handle": "observer:sprint:1", "connected": True}]}
                if args[1:3] == ["terminal", "send"]:
                    return {}
                if args[1:3] == ["terminal", "wait"]:
                    return {"wait": {"condition": "tui-idle", "satisfied": True}}
                raise AssertionError(args)

            with mock.patch.object(host, "_run_json", side_effect=run_json), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_TIMEOUT_S", 0.3), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_POLL_S", 0.01), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_RESEND_GRACE_S", 0), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_RETRIES", 2), \
                 self.assertRaises(HostError) as raised:
                host.nudge_observer(record, confirm=lambda _sent_at: False)

        self.assertIn("observer wake was not delivered", str(raised.exception))
        self.assertIn("pane-stayed-ready", str(raised.exception))
        sends = [call for call in calls if call[1:3] == ["terminal", "send"]]
        self.assertEqual(len(sends), 3)
        self.assertEqual([call[call.index("--text") + 1] for call in sends[1:]], ["", ""])

    def test_real_host_reads_readiness_from_tui_idle_and_the_output_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            host = CommandHostRuntime(FakeCatalog(), Path(root), mode="real")
            record = ObserverRecord(
                sprint="sprint:1", workspace="/workspace", handle="observer:sprint:1"
            )
            calls: list[list[str]] = []

            def run_json(args: list[str]) -> dict:
                calls.append(args)
                if args[1:3] == ["terminal", "list"]:
                    return {
                        "terminals": [
                            {
                                "handle": "observer:sprint:1",
                                "connected": True,
                                "lastOutputAt": 1_753_456_789_123,
                            }
                        ]
                    }
                if args[1:3] == ["terminal", "wait"]:
                    return {"wait": {"condition": "tui-idle", "satisfied": True}}
                raise AssertionError(args)

            with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
                status = host.observer_status(record)

        self.assertEqual(status, {"last_activity": 1_753_456_789.123, "idle": True})
        self.assertEqual([args[1:3] for args in calls], [["terminal", "list"], ["terminal", "wait"]])


class RealHostStopObserverTests(unittest.TestCase):
    """The real host, not the fake: a refused stop has to reach the lifecycle."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.record = ObserverRecord(
            sprint="sprint:1", head="observer", handle="term-1",
            workspace="/ws/observers/sprint-1", head_possible=True,
        )
        self.calls: list[list[str]] = []

    def _run_json(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        return {}

    def _refusing(self, step: list[str], message: str):
        def run_json(args: list[str]) -> dict[str, object]:
            self.calls.append(args)
            if args[1:3] == step:
                raise HostError(message)
            return {}

        return run_json

    def test_the_head_and_the_workspace_it_was_given_are_both_stopped(self) -> None:
        """What the bring-up registered, the stop gives back: Orca is left with neither a terminal
        of this observer nor a worktree for it."""
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(self.record)

        self.assertEqual(
            self.calls,
            [
                [
                    "orca", "worktree", "show",
                    "--worktree", "path:/ws/observers/sprint-1", "--json",
                ],
                [
                    "orca", "terminal", "stop",
                    "--worktree", "path:/ws/observers/sprint-1", "--json",
                ],
                [
                    "orca", "worktree", "rm",
                    "--worktree", "path:/ws/observers/sprint-1", "--force", "--json",
                ],
            ],
        )

    def test_a_head_with_no_handle_is_stopped_through_its_workspace(self) -> None:
        """A head adopted from a launch intent: the handle died with the tick that opened it, and
        the observer workspace is the only pointer left to its terminals."""
        adopted = ObserverRecord(
            sprint="sprint:1", head="observer", workspace="/ws/observers/sprint-1",
            head_possible=True,
        )
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(adopted)

        self.assertEqual(
            [args[1:3] for args in self.calls],
            [["worktree", "show"], ["terminal", "stop"], ["worktree", "rm"]],
        )

    def test_a_session_wrapped_observer_is_confirmed_dead_before_its_worktree_is_removed(self) -> None:
        """Terminal stop cannot kill a `setsid` head by tty alone."""
        record = ObserverRecord(
            sprint="sprint:1", head="observer", handle="term-1",
            workspace="/ws/observers/sprint-1", head_possible=True, pid_file="/tmp/observer.pid",
        )
        confirmed: list[str] = []
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ), mock.patch.object(
            self.host, "_confirm_head_process_gone", lambda path: confirmed.append(path)
        ):
            self.host.stop_observer(record)

        self.assertEqual(confirmed, ["/tmp/observer.pid"])
        self.assertEqual(self.calls[-1][1:3], ["worktree", "rm"])

    def test_a_record_without_a_workspace_still_closes_its_pane(self) -> None:
        """Records written before the launch intent named a workspace: the handle is all there is."""
        legacy = ObserverRecord(sprint="sprint:1", head="observer", handle="term-1")
        with mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        ):
            self.host.stop_observer(legacy)

        self.assertEqual(
            self.calls, [["orca", "terminal", "close", "--terminal", "term-1", "--json"]]
        )

    def test_a_refused_stop_raises_instead_of_reporting_success(self) -> None:
        run_json = self._refusing(["terminal", "stop"], "orca terminal stop failed: pane is busy")
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            with self.assertRaises(HostError):
                self.host.stop_observer(self.record)

    def test_a_refused_stop_keeps_the_record_and_marks_stop_pending(self) -> None:
        runtime = mock.Mock()
        runtime.host = self.host
        run_json = self._refusing(["terminal", "stop"], "orca terminal stop failed: pane is busy")
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertTrue(self.record.head_possible)
        self.assertEqual(self.record.workspace, "/ws/observers/sprint-1")

    def test_a_terminal_that_is_stopped_but_a_worktree_that_will_not_go_is_a_failed_stop(
        self,
    ) -> None:
        """Otherwise the record is dropped while the worktree it named is still registered, and
        nothing is left pointing at it to clean it up."""
        runtime = mock.Mock()
        runtime.host = self.host
        run_json = self._refusing(["worktree", "rm"], "orca worktree rm failed: worktree is busy")
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertEqual(self.record.workspace, "/ws/observers/sprint-1")

    def test_a_workspace_orca_does_not_know_is_a_head_that_is_already_gone(self) -> None:
        """What makes the retry of a half-finished stop terminate."""
        run_json = self._refusing(
            ["worktree", "show"], "orca worktree show failed: selector_not_found"
        )
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.host.stop_observer(self.record)

        self.assertEqual([args[1:3] for args in self.calls], [["worktree", "show"]])

    def test_an_unreadable_answer_is_not_an_absent_workspace(self) -> None:
        """Orca down must not read as "nothing is running": that is how a live head loses its
        record, and the next time the sprint opens a second head is put beside it."""
        runtime = mock.Mock()
        runtime.host = self.host
        run_json = self._refusing(
            ["worktree", "show"], "orca worktree show failed: daemon is unreachable"
        )
        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            self.assertFalse(stop_observer_head(runtime, self.record))

        self.assertTrue(self.record.head_possible)
        self.assertEqual(self.record.workspace, "/ws/observers/sprint-1")

    def test_the_pid_file_is_named_before_the_head_exists(self) -> None:
        self.assertEqual(
            self.host.observer_pid_file("sprint:1"), observer_pid_file("sprint:1")
        )


class RealHostObserverWorkspaceTests(unittest.TestCase):
    """The real host on the bring-up path: how the observer workspace becomes known to Orca.

    A directory made with `mkdir` is not a worktree selector, so the live bring-up used to die on
    `selector_not_found`. The shape of the commands is what these tests pin, not the fake's answer.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces")}
        )
        env.start()
        self.addCleanup(env.stop)
        self.host = CommandHostRuntime(_ObserverCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self.shell: list[list[str]] = []
        self.registered = False
        self.created_path: str | None = None
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)
        run = mock.patch.object(
            CommandHostRuntime, "_run", lambda _self, args, label, **kwargs: self._run(args)
        )
        run.start()
        self.addCleanup(run.stop)

    @property
    def workspace(self) -> str:
        return self.host.observer_workspace("sprint:1")

    def _run(self, args: list[str]) -> mock.Mock:
        self.shell.append(args)
        if args[:2] == ["git", "-C"] and "init" in args:
            Path(args[2], ".git").mkdir(parents=True, exist_ok=True)
        return mock.Mock(stdout="", stderr="", returncode=0)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        if args[1:3] == ["worktree", "show"] and not self.registered:
            raise HostError("orca worktree show failed: selector_not_found")
        if args[1:3] == ["worktree", "create"]:
            path = self.created_path if self.created_path is not None else self.workspace
            Path(path).mkdir(parents=True, exist_ok=True)
            return {"worktree": {"path": path}}
        if args[1:3] == ["terminal", "create"]:
            return {"handle": "term-obs"}
        return {}

    def _prepare(self) -> dict[str, object]:
        return self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

    def test_the_workspace_is_registered_with_orca_before_the_terminal_is_asked_for(self) -> None:
        launched = self._prepare()

        repo = self.root / "data" / "dispatcher" / "observer-root" / "observers"
        self.assertEqual(
            [args[1:3] for args in self.calls],
            [
                ["worktree", "show"],
                ["repo", "add"],
                ["worktree", "create"],
                ["terminal", "create"],
            ],
        )
        self.assertEqual(
            self.calls[2],
            [
                "orca", "worktree", "create",
                "--repo", f"path:{repo}",
                "--name", Path(self.workspace).name,
                "--base-branch", "observers",
                "--setup", "skip",
                "--no-parent",
                "--json",
            ],
        )
        self.assertIn("--worktree", self.calls[3])
        self.assertEqual(
            self.calls[3][self.calls[3].index("--worktree") + 1], f"path:{self.workspace}"
        )
        self.assertEqual(launched["workspace"], self.workspace)
        self.assertEqual(launched["handle"], "term-obs")

    def test_the_observer_repo_is_its_own_and_not_a_checkout_of_the_project(self) -> None:
        """The observer reads the board and writes no code. Its workspace is cut from an empty
        standalone repo, so there is nothing there to commit the project from."""
        self._prepare()

        repo = self.root / "data" / "dispatcher" / "observer-root" / "observers"
        self.assertEqual([args[2] for args in self.shell], [str(repo), str(repo)])
        self.assertIn("init", self.shell[0])
        self.assertIn("--allow-empty", self.shell[1])
        project_repo = str(FakeCatalog().binding("secretary")["repo"])
        self.assertNotIn(project_repo, [args[2] for args in self.shell])
        self.assertNotIn(f"path:{project_repo}", self.calls[2])

    def test_a_workspace_orca_already_knows_is_reused_by_a_relaunch(self) -> None:
        self.registered = True
        Path(self.workspace).mkdir(parents=True, exist_ok=True)

        self._prepare()

        self.assertEqual(
            [args[1:3] for args in self.calls],
            [["worktree", "show"], ["terminal", "create"]],
        )
        self.assertEqual(self.shell, [])

    def test_a_directory_orca_never_learned_about_is_cleared_not_worked_around(self) -> None:
        """The state the live defect left behind: `worktree create` would otherwise place the
        workspace beside it, at a path no record points at."""
        stale = Path(self.workspace)
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "SPRINT.md").write_text("stale\n", encoding="utf-8")

        self._prepare()

        self.assertEqual(
            (stale / "SPRINT.md").read_text(encoding="utf-8").splitlines()[0], "# Sprint"
        )

    def test_a_workspace_placed_somewhere_else_fails_the_bring_up(self) -> None:
        """The launch intent already names the workspace, and a tick that dies now can only find
        the head through it."""
        self.created_path = str(self.root / "workspaces" / "observers" / "elsewhere")

        with self.assertRaises(HostError) as caught:
            self._prepare()

        self.assertIn("elsewhere", str(caught.exception))
        self.assertNotIn(["terminal", "create"], [args[1:3] for args in self.calls])

    def test_a_workspace_orca_refuses_to_create_leaves_no_terminal(self) -> None:
        def run_json(args: list[str]) -> dict[str, object]:
            if args[1:3] == ["worktree", "create"]:
                raise HostError("orca worktree create failed: repo is unavailable")
            return self._run_json(args)

        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            with self.assertRaises(HostError):
                self._prepare()

        self.assertNotIn(["terminal", "create"], [args[1:3] for args in self.calls])


class RealHostObserverTeardownTests(unittest.TestCase):
    """The lifecycle over the real host seam: what a closed sprint gives back to Orca.

    The bring-up registers the observer workspace before it asks for a terminal, so a failure after
    that point leaves a registration behind with no head at all. The record has to keep pointing at
    it, or the sprint closes and the worktree stays in Orca with nothing left to remove it.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        install_skill_registry(self.data_dir)
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
                "SECRETARY_ROLE_SKILLS_MANIFEST": str(self.data_dir / "registry" / "manifest.toml"),
                "SECRETARY_INSTANCE": str(self.data_dir / "registry" / "instance"),
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.data_dir / "workspaces"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (self.data_dir / "bodies").mkdir(parents=True, exist_ok=True)
        self.board = FakeKanboard()
        self.catalog = _ObserverCatalog(instance_dir=self.data_dir)
        self.host = CommandHostRuntime(self.catalog, self.data_dir / "host", mode="real")  # type: ignore[arg-type]
        self.audit = TaskAudit(self.data_dir)
        self.runtime = DispatcherRuntime(
            TaskReader(self.board),  # type: ignore[arg-type]
            TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir),  # type: ignore[arg-type]
            self.audit,
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
        )
        self.runtime.state.save({
            "version": 1,
            "phase": "cutover_committed",
            "pilot_ref": "secretary-510-pilot",
            "old_owner_paused": True,
            "records": {},
        })
        self.calls: list[list[str]] = []
        self.registered = False
        self.terminal_create_fails = False
        self.worktree_create_fails = False
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)
        run = mock.patch.object(
            CommandHostRuntime, "_run", lambda _self, args, label, **kwargs: self._run(args)
        )
        run.start()
        self.addCleanup(run.stop)

    @property
    def workspace(self) -> str:
        return self.host.observer_workspace("sprint:1")

    def _run(self, args: list[str]) -> mock.Mock:
        if args[:2] == ["git", "-C"] and "init" in args:
            Path(args[2], ".git").mkdir(parents=True, exist_ok=True)
        return mock.Mock(stdout="", stderr="", returncode=0)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        self.calls.append(args)
        step = args[1:3]
        if step == ["worktree", "show"] and not self.registered:
            raise HostError("orca worktree show failed: selector_not_found")
        if step == ["worktree", "create"]:
            if not any("observer" in arg for arg in args):
                # A card worktree of the same tick's pipeline pass, not the observer's.
                return {"worktree": {"path": str(self.data_dir / "workspaces" / args[6])}}
            if self.worktree_create_fails:
                raise HostError("orca worktree create failed: repo is unavailable")
            Path(self.workspace).mkdir(parents=True, exist_ok=True)
            self.registered = True
            return {"worktree": {"path": self.workspace}}
        if step == ["worktree", "rm"]:
            self.registered = False
            return {}
        if step == ["terminal", "create"]:
            if self.terminal_create_fails:
                raise HostError("orca terminal create failed: selector_not_found")
            return {"handle": "term-obs"}
        return {}

    def observer_calls(self) -> list[list[str]]:
        """The tick's calls about the observer. A tick also runs the card pipeline, whose own
        worktrees and terminals are not what these tests are about."""
        return [args for args in self.calls if any("observer" in arg for arg in args)]

    def steps(self) -> list[list[str]]:
        return [args[1:3] for args in self.observer_calls()]

    def observers(self) -> dict:
        return load_observers(self.runtime.production_state.load())

    def actions(self, result: dict) -> list[str]:
        return [
            action["action"]
            for action in result["actions"]
            if action.get("step") == "observer-reconcile"
        ]

    def close_sprint(self) -> None:
        sprint = next(item for item in self.board.sprints if item["reference"] == "sprint:1")
        self.board.metadata[int(sprint["id"])]["sprint_status"] = "closed"

    def test_a_bring_up_that_dies_after_the_worktree_still_gives_it_back_on_closure(self) -> None:
        self.board.add_sprint("sprint:1", status="open")
        self.terminal_create_fails = True

        deferred = self.runtime.production_tick()

        self.assertEqual(self.actions(deferred), ["observer-launch-deferred"])
        record = self.observers()["sprint:1"]
        self.assertFalse(record.head_possible)
        self.assertTrue(record.workspace_live)
        self.assertTrue(self.registered)

        self.close_sprint()
        self.calls.clear()
        stopped = self.runtime.production_tick()

        self.assertEqual(self.actions(stopped), ["observer-stopped"])
        self.assertEqual(
            self.observer_calls(),
            [
                ["orca", "worktree", "show", "--worktree", f"path:{self.workspace}", "--json"],
                ["orca", "terminal", "stop", "--worktree", f"path:{self.workspace}", "--json"],
                [
                    "orca", "worktree", "rm",
                    "--worktree", f"path:{self.workspace}", "--force", "--json",
                ],
            ],
        )
        self.assertFalse(self.registered)
        self.assertEqual(self.observers(), {})

    def test_a_bring_up_that_never_registered_a_workspace_leaves_nothing_to_remove(self) -> None:
        """The other side of it: the stop asks Orca and takes its answer, rather than removing a
        worktree on the strength of a path the record computed before the host was ever called."""
        self.board.add_sprint("sprint:1", status="open")
        self.worktree_create_fails = True

        self.runtime.production_tick()
        self.close_sprint()
        self.calls.clear()
        stopped = self.runtime.production_tick()

        self.assertEqual(self.actions(stopped), ["observer-stopped"])
        self.assertEqual(self.steps(), [["worktree", "show"]])
        self.assertEqual(self.observers(), {})

    def test_a_worktree_that_will_not_go_keeps_the_closed_sprint_on_the_books(self) -> None:
        """A refused teardown of a workspace with no head behind it is still a failed stop: the
        record survives as `stop-pending` and the next tick comes back to it."""
        self.board.add_sprint("sprint:1", status="open")
        self.terminal_create_fails = True
        self.runtime.production_tick()
        self.close_sprint()
        refusing = self._run_json

        def run_json(args: list[str]) -> dict[str, object]:
            if args[1:3] == ["worktree", "rm"]:
                self.calls.append(args)
                raise HostError("orca worktree rm failed: worktree is busy")
            return refusing(args)

        with mock.patch.object(CommandHostRuntime, "_run_json", lambda _self, args: run_json(args)):
            failed = self.runtime.production_tick()

        self.assertEqual(self.actions(failed), ["observer-stop-failed"])
        record = self.observers()["sprint:1"]
        self.assertEqual(record.state, "stop-pending")
        self.assertTrue(record.workspace_live)
        self.assertTrue(self.registered)

        retried = self.runtime.production_tick()

        self.assertEqual(self.actions(retried), ["observer-stopped"])
        self.assertFalse(self.registered)
        self.assertEqual(self.observers(), {})


class RealHostTuiObserverLaunchTests(unittest.TestCase):
    """The real host on the TUI bring-up path: what a failed prompt delivery hands back."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces")}
        )
        env.start()
        self.addCleanup(env.stop)
        self.host = CommandHostRuntime(_TuiCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.stops: list[str] = []
        self.stop_refused = False
        delivery = mock.patch.object(
            dispatcher_module,
            "_deliver_tui_prompt",
            mock.Mock(side_effect=TuiDeliveryError("TUI prompt was not delivered")),
        )
        delivery.start()
        self.addCleanup(delivery.stop)
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        if args[:3] == ["orca", "terminal", "create"]:
            return {"handle": "term-obs"}
        if args[:3] == ["orca", "terminal", "stop"]:
            self.stops.append(args[4])
            if self.stop_refused:
                raise HostError("orca terminal stop failed: pane is busy")
            return {}
        return {}

    def _prepare(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

    def test_a_stopped_terminal_reports_a_bring_up_with_nothing_left_running(self) -> None:
        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertEqual(self.stops, [f"path:{self.host.observer_workspace('sprint:1')}"])
        self.assertEqual(caught.exception.handle, "")

    def test_a_terminal_that_will_not_stop_hands_its_handle_back(self) -> None:
        self.stop_refused = True

        with self.assertRaises(ObserverLaunchAborted) as caught:
            self._prepare()

        self.assertEqual(caught.exception.handle, "term-obs")
        self.assertTrue(caught.exception.workspace)
        self.assertTrue(caught.exception.pid_file)
        self.assertIn("stop failed", str(caught.exception))


class ObserverCodexTrustTests(unittest.TestCase):
    """The bring-up answers codex's trust question for the observer workspace.

    A real `CommandHostRuntime` over a real `InstanceCatalog`: the launcher renders the command and
    prepares the workspace, only `orca` is replaced. The git worktree is a real one, cut from the
    real observer repo the runtime creates, because what codex asks about is the repository root
    that worktree hangs off and no fake reproduces that shape.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.codex_home = self.root / "codex-home"
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces"),
                "TA_CODEX_HOME": str(self.codex_home),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        catalog = object.__new__(InstanceCatalog)
        catalog._heads = canonical_heads(Path(__file__).resolve().parents[1])  # type: ignore[attr-defined]
        self.host = CommandHostRuntime(catalog, self.root / "data", mode="real")  # type: ignore[arg-type]
        self.commands: list[str] = []
        self.registered = False
        delivery = mock.patch.object(dispatcher_module, "_deliver_tui_prompt", mock.Mock())
        delivery.start()
        self.addCleanup(delivery.stop)
        run_json = mock.patch.object(
            CommandHostRuntime, "_run_json", lambda _self, args: self._run_json(args)
        )
        run_json.start()
        self.addCleanup(run_json.stop)

    def _run_json(self, args: list[str]) -> dict[str, object]:
        step = args[1:3]
        if step == ["worktree", "show"]:
            if self.registered:
                return {}
            raise HostError("orca worktree show failed: selector_not_found")
        if step == ["worktree", "create"]:
            repo = args[args.index("--repo") + 1].split(":", 1)[1]
            workspace = self.host.observer_workspace("sprint:1")
            # What `orca worktree create` leaves behind: a git worktree of that repo at that path.
            self.host._run(  # type: ignore[attr-defined]
                ["git", "-C", repo, "worktree", "add", "--quiet", "--detach", workspace],
                "worktree add",
            )
            self.registered = True
            return {"worktree": {"path": workspace}}
        if step == ["terminal", "create"]:
            self.commands.append(args[args.index("--command") + 1])
            return {"handle": "term-obs"}
        return {}

    def test_the_bring_up_trusts_the_repository_root_of_the_observer_workspace(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

        trusted = tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))
        workspace = Path(self.host.observer_workspace("sprint:1")).resolve()
        repo_root = (self.root / "data" / "dispatcher" / "observer-root" / "observers").resolve()
        self.assertEqual(trusted["projects"][str(repo_root)]["trust_level"], "trusted")
        self.assertEqual(trusted["projects"][str(workspace)]["trust_level"], "trusted")
        command = self.commands[0]
        self.assertIn(f"CODEX_HOME={self.codex_home} codex", command)
        self.assertNotIn("codex exec", command)
        self.assertIn("--role observer", command)

    def test_a_second_bring_up_leaves_the_recorded_trust_alone(self) -> None:
        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")
        first = (self.codex_home / "config.toml").read_text(encoding="utf-8")

        self.host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint\n")

        self.assertEqual((self.codex_home / "config.toml").read_text(encoding="utf-8"), first)

    def test_worker_and_reviewer_launches_never_touch_the_codex_config(self) -> None:
        """Trust is written for the observer alone.

        Worker and reviewer workspaces are worktrees of repositories the codex runtime already
        trusts, so their bring-up has no reason to rewrite the runtime's own `config.toml` and
        must not: it is installation state, shared by every codex head on the host.
        """
        workspace = self.root / "worker-workspace"
        workspace.mkdir()

        for head, role in (("codex", "worker"), ("codex-reviewer", "reviewer")):
            with self.subTest(role=role):
                launch = self.host.catalog.head_launch(
                    head, "TASK.md", workspace=str(workspace), role=role
                )
                self.assertIn(f"CODEX_HOME={self.codex_home} codex exec", launch.command)
                self.assertIn(f"--role {role}", launch.command)
                self.assertFalse(self.codex_home.exists())


class _ObserverCatalog(FakeCatalog):
    """An observer profile whose head takes its prompt on the command line."""

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ):
        return HeadLaunch(f"run-{role}")


class _TuiCatalog(FakeCatalog):
    """An observer profile whose head takes its prompt after the terminal is already up."""

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ):
        return HeadLaunch(f"run-{role}", prompt_after_start=True)


if __name__ == "__main__":
    unittest.main()
