"""Background-role telemetry read from the live data plane (secretary-833).

The incident: `python3 -m triggered_agents health` was RED with `no runs.jsonl yet` for curator,
steward and retro while all three were ticking fine — it looked for their state inside each
agent's git worktree, and the units write it under the installation's data dir. The pipeline line
and the steward's pipeline signal had the same shape of defect one layer down: both read the
pipeline worktree's `state/pipeline/runs.jsonl`, which the production dispatcher does not write,
so a failing production tick was invisible to both.

These tests pin all three ends: what the dispatcher records, what the readers make of it, and that
neither reader can be answered with a stale or missing source as if it were a healthy one.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from secretary import host
from secretary.dispatcher import CutoverState, DispatcherRuntime
from secretary.dispatcher_production import (
    TICK_TELEMETRY_UNHEALTHY_KEPT,
    record_tick_telemetry,
)
from secretary.dispatcher import default_data_dir
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter
from tests.test_dispatcher import FakeCatalog, FakeHost, FakeKanboard, FakeLegacyPause

from triggered_agents.agents.steward import cli as steward_cli
from triggered_agents.agents.steward import signals as steward_signals
from triggered_agents.runtime import health, production_telemetry, role_env
from triggered_agents.runtime.state import PRECHECK_SKIP, AgentState


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _instance(root: Path, data_dir: Path) -> Path:
    """A valid instance dir whose data_dir is `data_dir`, as the dispatcher unit is started with.

    The packaged unit passes `--instance` and no `--data-dir`, so this file is the only thing that
    binds the dispatcher to a data plane — and therefore the only thing that can bind a reader in
    another process to the same one.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "instance.yaml").write_text(
        "version: 1\n"
        "name: telemetry-test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n"
        "  instance_remote: https://example.invalid/instance.git\n",
        encoding="utf-8",
    )
    return root


def _telemetry_state(path: Path, telemetry: dict | None, **payload) -> None:
    body = {"version": 1, "mode": "production", "phase": "production", **payload}
    if telemetry is not None:
        body["tick_telemetry"] = telemetry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _tick(seq: int, *, healthy: bool, at: str | None = None, **fields) -> dict:
    return {
        "seq": seq,
        "at": at or _ts(1),
        "status": "ok" if healthy else "degraded",
        "step": "production-tick",
        "healthy": healthy,
        "reason": "",
        "actions": 0,
        "error_count": 0,
        "errors": [],
        **fields,
    }


class ProductionTickTelemetryTests(unittest.TestCase):
    """What the production dispatcher durably records about how its ticks ended."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
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

    def read_through_the_agent_reader(self) -> production_telemetry.TickTelemetry:
        """The record as the health line and the steward actually see it.

        Reading it back through `triggered_agents` rather than out of the payload is the point:
        writer and reader live in different processes on the host, and the file is the only
        contract between them.
        """
        with mock.patch.dict(
            os.environ,
            {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)},
        ):
            return production_telemetry.read()

    def test_healthy_tick_records_its_outcome_and_freshness(self) -> None:
        result = self.runtime.production_tick()
        self.assertEqual(result["status"], "ok")

        telemetry = self.read_through_the_agent_reader()

        self.assertTrue(telemetry.available)
        self.assertEqual(telemetry.tick_seq, 1)
        self.assertTrue(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["status"], "ok")
        self.assertEqual(telemetry.last["step"], "production-tick")
        self.assertEqual(telemetry.last_healthy_at, telemetry.last["at"])
        self.assertEqual(telemetry.unhealthy, ())
        self.assertEqual(telemetry.unhealthy_total, 0)

    def test_degraded_tick_records_the_diagnostic_and_leaves_the_healthy_stamp_behind(self) -> None:
        self.runtime.production_tick()
        healthy_at = self.read_through_the_agent_reader().last_healthy_at

        with mock.patch.object(self.runtime, "_tick_task", side_effect=RuntimeError("board is gone")):
            result = self.runtime.production_tick()
        self.assertEqual(result["status"], "degraded")

        telemetry = self.read_through_the_agent_reader()
        self.assertFalse(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["status"], "degraded")
        self.assertEqual(telemetry.last["error_count"], 1)
        self.assertEqual(telemetry.last["errors"][0]["code"], "unexpected_error")
        self.assertEqual(telemetry.unhealthy_total, 1)
        self.assertEqual(telemetry.unhealthy[-1]["seq"], telemetry.last["seq"])
        # The healthy stamp is not advanced by a failing tick, and not erased by it either: it is
        # the freshness evidence, not a verdict on the current tick.
        self.assertEqual(telemetry.last_healthy_at, healthy_at)

    def test_a_tick_that_dies_before_its_own_save_is_recorded_as_a_failure(self) -> None:
        """The Kanboard outage case: the first board read raises and the tick never finishes.

        Nothing inside the tick catches it, so without a record of its own the previous healthy
        tick would answer for the pipeline for the whole freshness window while no card moves.
        """
        self.runtime.production_tick()
        healthy = self.read_through_the_agent_reader()

        with mock.patch(
            "secretary.dispatcher_production._production_tasks",
            side_effect=TaskError("backend_unavailable", "board is down", 1),
        ):
            with self.assertRaises(TaskError):
                self.runtime.production_tick()

        telemetry = self.read_through_the_agent_reader()
        self.assertFalse(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["status"], "failed")
        self.assertEqual(telemetry.last["errors"][0]["code"], "backend_unavailable")
        self.assertEqual(telemetry.unhealthy_total, 1)
        self.assertEqual(telemetry.unhealthy[-1]["seq"], telemetry.last["seq"])
        self.assertEqual(telemetry.last_healthy_at, healthy.last_healthy_at)

        with mock.patch.dict(
            os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)}
        ):
            problems, _ = health._pipeline_status()
        self.assertTrue(any("last tick unhealthy" in problem for problem in problems))
        self.assertTrue(any("backend_unavailable" in problem for problem in problems))

    def test_a_failed_tick_records_telemetry_without_persisting_its_half_done_work(self) -> None:
        """Only the record of the failure lands: the payload it died holding is half applied."""
        self.runtime.production_tick()
        before = json.loads(self.runtime.production_state.path.read_text(encoding="utf-8"))

        with mock.patch(
            "secretary.dispatcher_production._reconcile_production",
            side_effect=RuntimeError("host is gone"),
        ):
            with self.assertRaises(RuntimeError):
                self.runtime.production_tick()

        after = json.loads(self.runtime.production_state.path.read_text(encoding="utf-8"))
        self.assertEqual(
            {key: value for key, value in after.items() if key != "tick_telemetry"},
            {key: value for key, value in before.items() if key != "tick_telemetry"},
        )
        self.assertEqual(self.read_through_the_agent_reader().last["status"], "failed")

    def test_telemetry_is_found_through_the_instance_the_unit_passes(self) -> None:
        """No TA_PRODUCTION_STATE, no SECRETARY_DATA_DIR: only `--instance`, as the unit runs it."""
        instance = _instance(self.data_dir / "instance", self.data_dir)
        self.runtime.production_tick()

        with mock.patch.dict(
            os.environ, {"SECRETARY_INSTANCE": str(instance)}, clear=False
        ):
            os.environ.pop("TA_PRODUCTION_STATE", None)
            os.environ.pop("SECRETARY_DATA_DIR", None)
            telemetry = production_telemetry.read()

        self.assertEqual(telemetry.path, self.runtime.production_state.path)
        self.assertTrue(telemetry.available)
        self.assertTrue(telemetry.last["healthy"])

    def test_frozen_tick_is_recorded_as_a_healthy_terminal_tick(self) -> None:
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()
        self.assertEqual(result["status"], "skipped")

        telemetry = self.read_through_the_agent_reader()
        self.assertTrue(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["reason"], "pipeline is frozen by pause")
        self.assertTrue(telemetry.last_healthy_at)

    def test_guard_blocked_tick_writes_nothing(self) -> None:
        """A dispatcher that is not allowed to touch its own state must not touch it here either.

        The evidence of a blocked dispatcher is that it stops producing healthy ticks; writing
        across the ownership fence to say so would be worse than the silence.
        """
        self.runtime.state.save({"version": 1, "phase": "new"})

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.read_through_the_agent_reader().available)

    def test_unhealthy_ring_is_bounded_while_the_counter_keeps_growing(self) -> None:
        payload: dict = {}
        for _ in range(TICK_TELEMETRY_UNHEALTHY_KEPT + 5):
            record_tick_telemetry(payload, {"status": "degraded", "step": "production-tick",
                                            "errors": [{"ref": "x", "code": "boom", "message": ""}]})

        telemetry = payload["tick_telemetry"]
        self.assertEqual(len(telemetry["unhealthy"]), TICK_TELEMETRY_UNHEALTHY_KEPT)
        self.assertEqual(telemetry["unhealthy_total"], TICK_TELEMETRY_UNHEALTHY_KEPT + 5)
        self.assertEqual(telemetry["unhealthy"][-1]["seq"], TICK_TELEMETRY_UNHEALTHY_KEPT + 5)

    def test_errors_recorded_per_tick_are_capped_but_counted_in_full(self) -> None:
        payload: dict = {}
        errors = [{"ref": f"ref-{i}", "code": "boom", "message": "m"} for i in range(9)]

        record_tick_telemetry(payload, {"status": "degraded", "step": "production-tick", "errors": errors})

        entry = payload["tick_telemetry"]["last"]
        self.assertEqual(entry["error_count"], 9)
        self.assertLess(len(entry["errors"]), 9)
        self.assertIn("+", production_telemetry.describe(entry))


class ProductionStatePathTests(unittest.TestCase):
    """Where the reader looks for the dispatcher's records, on a host that is not the default one."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "srv" / "secretary-data"
        self.instance = _instance(self.root / "instance", self.data_dir)
        env = mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        for name in ("TA_PRODUCTION_STATE", "SECRETARY_DATA_DIR", "SECRETARY_INSTANCE",
                     "TA_RUNTIME_ENV_FILE"):
            os.environ.pop(name, None)

    def test_instance_data_dir_is_the_dispatchers_own(self) -> None:
        os.environ["SECRETARY_INSTANCE"] = str(self.instance)

        # default_data_dir is what `secretary dispatcher production-tick --instance ...` resolves
        # with no --data-dir: the reader must land on the same directory, not on a home default.
        self.assertEqual(production_telemetry.data_dir(), default_data_dir(self.instance))
        self.assertEqual(
            production_telemetry.state_path(),
            self.data_dir / "dispatcher" / "production-state.json",
        )

    def test_a_relative_instance_data_dir_resolves_against_the_instance(self) -> None:
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: telemetry-test\ndata_dir: ./data\n"
            "offsite:\n  instance_remote: https://example.invalid/instance.git\n",
            encoding="utf-8",
        )
        os.environ["SECRETARY_INSTANCE"] = str(self.instance)

        self.assertEqual(production_telemetry.data_dir(), self.instance / "data")

    def test_explicit_env_overrides_win_in_the_dispatchers_order(self) -> None:
        os.environ["SECRETARY_INSTANCE"] = str(self.instance)
        os.environ["SECRETARY_DATA_DIR"] = str(self.root / "elsewhere")
        self.assertEqual(production_telemetry.data_dir(), self.root / "elsewhere")

        os.environ["TA_PRODUCTION_STATE"] = str(self.root / "state.json")
        self.assertEqual(production_telemetry.state_path(), self.root / "state.json")

    def test_an_unusable_instance_falls_back_to_the_home_default(self) -> None:
        """A broken or absent instance file must not crash the command that reports host trouble."""
        os.environ["SECRETARY_INSTANCE"] = str(self.root / "missing")
        self.assertEqual(production_telemetry.data_dir(), self.root / "home" / "secretary-data")

        (self.instance / "instance.yaml").write_text("data_dir: [not, a, path\n", encoding="utf-8")
        os.environ["SECRETARY_INSTANCE"] = str(self.instance)
        self.assertEqual(production_telemetry.data_dir(), self.root / "home" / "secretary-data")

    def test_a_missing_record_names_the_instance_resolved_path(self) -> None:
        os.environ["SECRETARY_INSTANCE"] = str(self.instance)

        telemetry = production_telemetry.read()

        self.assertEqual(telemetry.unavailable, "production-state-missing")
        self.assertEqual(telemetry.path, self.data_dir / "dispatcher" / "production-state.json")

    def test_the_role_units_env_file_path_carries_the_instance(self) -> None:
        """A role process handed only TA_RUNTIME_ENV_FILE still lands on the dispatcher's file."""
        os.environ["TA_RUNTIME_ENV_FILE"] = str(self.instance / "runtime.env")

        self.assertEqual(production_telemetry.data_dir(), self.data_dir)

        # An explicit instance still wins over the env-file's directory.
        other = _instance(self.root / "other-instance", self.root / "other-data")
        os.environ["SECRETARY_INSTANCE"] = str(other)
        self.assertEqual(production_telemetry.data_dir(), self.root / "other-data")


class PackagedStewardUnitEnvTests(unittest.TestCase):
    """The env the packaged steward units actually give the process that reads production state.

    Setting SECRETARY_INSTANCE in a test proves the reader, not the installation: the steward runs
    with whatever its rendered unit exports, through role_env's allowlist. So this renders the
    shipped templates and builds the role env exactly that way (secretary-833 review, round 2).
    """

    UNITS = ("secretary-steward.service", "secretary-steward-deep-sweep.service")

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "srv" / "secretary-data"
        self.instance = _instance(self.root / "instance", self.data_dir)
        # The live runtime.env carries board credentials and no instance path — the case the
        # reviewer found: role_env has nothing to forward unless the unit itself exports it.
        (self.instance / "runtime.env").write_text(
            "KANBOARD_URL=https://board.invalid/jsonrpc.php\n"
            "KANBOARD_API_USER=steward\n"
            "KANBOARD_API_TOKEN=secret\n",
            encoding="utf-8",
        )
        self.layout = host.SystemdLayout(
            product_root=self.root / "product",
            instance_path=self.instance,
            data_dir=self.data_dir,
            runtime_user="dev",
            runtime_home=self.root / "home",
        )

    def unit_env(self, name: str) -> dict[str, str]:
        rendered = host.render_systemd_unit(
            (host.default_packaging_root() / name).read_bytes(), self.layout
        ).decode()
        env = {}
        for line in rendered.splitlines():
            if line.startswith("Environment="):
                key, value = line[len("Environment="):].split("=", 1)
                env[key] = value
        return env

    def test_the_steward_process_resolves_the_installations_production_state(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                env = role_env.runtime_env(
                    "steward", base_env=self.unit_env(name),
                    env_file=self.instance / "runtime.env", require=True,
                )

                self.assertEqual(env["SECRETARY_INSTANCE"], str(self.instance))
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(
                        production_telemetry.state_path(),
                        self.data_dir / "dispatcher" / "production-state.json",
                    )


class HealthAgentStateTests(unittest.TestCase):
    """The health line for curator/steward/retro reads the state root the units actually write."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_root = Path(self.tmpdir.name) / "automation-state"
        patcher = mock.patch("triggered_agents.runtime.state.STATE_ROOT", self.state_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        timer = mock.patch.object(health, "_timer_active", return_value=True)
        timer.start()
        self.addCleanup(timer.stop)

    def write_runs(self, agent: str, records: list[dict]) -> None:
        state = AgentState(agent)
        state.ensure_dir()
        (state.dir / "runs.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    def test_runs_are_read_from_the_unit_state_root_not_the_worktree(self) -> None:
        self.write_runs("steward", [{"ts": _ts(5), "event": "precheck", "result": "no-change"}])

        problems, detail = health._runs_status("steward")

        self.assertEqual(problems, [])
        self.assertIn("last tick", detail)

    def test_state_root_override_is_honored(self) -> None:
        # TA_STATE is the same knob the units would use if they set one; health must follow it
        # rather than resolving a path of its own.
        elsewhere = Path(self.tmpdir.name) / "elsewhere"
        with mock.patch("triggered_agents.runtime.state.STATE_ROOT", elsewhere):
            state = AgentState("curator")
            state.ensure_dir()
            (state.dir / "runs.jsonl").write_text(
                json.dumps({"ts": _ts(5), "event": "dispatch"}) + "\n", encoding="utf-8"
            )
            problems, _ = health._runs_status("curator")

        self.assertEqual(problems, [])

    def test_missing_runs_is_still_reported(self) -> None:
        problems, _ = health._runs_status("steward")

        self.assertEqual(problems, ["no runs.jsonl yet"])

    def test_stale_healthy_tick_is_red(self) -> None:
        self.write_runs("steward", [{"ts": _ts(60 * 24), "event": "precheck", "result": "no-change"}])

        problems, _ = health._runs_status("steward")

        self.assertTrue(problems)
        self.assertIn("last healthy tick", problems[0])

    def test_a_fresh_error_does_not_pass_as_a_healthy_tick(self) -> None:
        self.write_runs("steward", [
            {"ts": _ts(60 * 24), "event": "precheck", "result": "no-change"},
            {"ts": _ts(1), "event": "precheck", "result": "error"},
        ])

        problems, _ = health._runs_status("steward")

        self.assertTrue(problems)
        self.assertIn("last healthy tick", problems[0])


class HealthPipelineLineTests(unittest.TestCase):
    """The pipeline line comes from production dispatcher telemetry, and never flatters it."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "dispatcher" / "production-state.json"
        env = mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def test_fresh_healthy_tick_is_green(self) -> None:
        _telemetry_state(self.path, {
            "tick_seq": 7,
            "last": _tick(7, healthy=True),
            "last_healthy_at": _ts(1),
            "unhealthy": [],
            "unhealthy_total": 0,
        })

        problems, detail = health._pipeline_status()

        self.assertEqual(problems, [])
        self.assertIn("last healthy", detail)

    def test_fresh_degraded_tick_is_red_even_with_a_recent_healthy_one(self) -> None:
        _telemetry_state(self.path, {
            "tick_seq": 8,
            "last": _tick(8, healthy=False, reason="",
                          error_count=1,
                          errors=[{"ref": "secretary-1", "code": "backend_unavailable", "message": "x"}]),
            "last_healthy_at": _ts(2),
            "unhealthy": [],
            "unhealthy_total": 1,
        })

        problems, _ = health._pipeline_status()

        self.assertTrue(problems)
        self.assertIn("last tick unhealthy", problems[0])
        self.assertIn("backend_unavailable", problems[0])

    def test_stale_healthy_tick_is_red(self) -> None:
        old = _ts(60 * 6)
        _telemetry_state(self.path, {
            "tick_seq": 3,
            "last": _tick(3, healthy=True, at=old),
            "last_healthy_at": old,
            "unhealthy": [],
            "unhealthy_total": 0,
        })

        problems, _ = health._pipeline_status()

        self.assertTrue(any("last healthy tick" in problem for problem in problems))

    def test_missing_state_file_is_red_and_names_the_path(self) -> None:
        problems, _ = health._pipeline_status()

        self.assertEqual(len(problems), 1)
        self.assertIn("production-state-missing", problems[0])
        self.assertIn(str(self.path), problems[0])

    def test_state_without_telemetry_is_red_not_silently_green(self) -> None:
        _telemetry_state(self.path, None, last_tick_at=_ts(1))

        problems, _ = health._pipeline_status()

        self.assertEqual(len(problems), 1)
        self.assertIn("tick-telemetry-missing", problems[0])

    def test_unreadable_state_file_is_red(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")

        problems, _ = health._pipeline_status()

        self.assertIn("production-state-unreadable", problems[0])


class StewardPipelineSignalTests(unittest.TestCase):
    """`steward scan`'s pipeline signal, against the production dispatcher's own records."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "dispatcher" / "production-state.json"
        env = mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        self.state = AgentState("steward", state_dir=Path(self.tmpdir.name) / "steward")
        state = mock.patch.object(steward_signals, "STATE", self.state)
        state.start()
        self.addCleanup(state.stop)

    def write(self, *, total: int, unhealthy: list[dict], healthy_last: bool = False) -> None:
        last = _tick(total + 1, healthy=True) if healthy_last else (unhealthy[-1] if unhealthy else {})
        _telemetry_state(self.path, {
            "tick_seq": total + 1,
            "last": last,
            "last_healthy_at": _ts(1),
            "unhealthy": unhealthy,
            "unhealthy_total": total,
        })

    @contextlib.contextmanager
    def steward_cli_env(self):
        """The steward's own commands with every non-pipeline signal source quiet, so what the
        gate does with the pipeline record is the only thing under test."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(steward_cli, "STATE", self.state))
            stack.enter_context(mock.patch.object(steward_signals.pipeline_ops, "list_cards",
                                                  return_value=[]))
            stack.enter_context(mock.patch.object(steward_signals, "WORKSPACES_ROOT",
                                                  Path(self.tmpdir.name) / "no-workspaces"))
            stack.enter_context(mock.patch.object(steward_signals, "PIPELINE_RESOURCE_HEALTH",
                                                  Path(self.tmpdir.name) / "no-resource-health.json"))
            stack.enter_context(mock.patch("sys.stdout", new=io.StringIO()))
            stack.enter_context(mock.patch("sys.stderr", new=io.StringIO()))
            yield

    def steward_runs(self) -> list[dict]:
        path = self.state.dir / "runs.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_first_scan_takes_a_baseline_instead_of_replaying_the_ring(self) -> None:
        self.write(total=3, unhealthy=[_tick(i, healthy=False) for i in (1, 2, 3)])

        hits, pending = steward_signals._pipeline_tick_signals(steward_signals._empty_watermark())

        self.assertEqual(hits, [])
        self.assertEqual(pending, 3)

    def test_a_new_unhealthy_tick_fires_once_and_is_not_lost_to_healthy_ticks(self) -> None:
        mark = dict(steward_signals._empty_watermark(), pipeline_unhealthy_total=3)
        self.write(total=4, unhealthy=[_tick(i, healthy=False) for i in (2, 3, 4)])

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
        self.assertEqual(hits[0]["seq"], 4)
        self.assertEqual(pending, 4)

        # An ordinary healthy production tick lands between the scan and the next one. It must not
        # consume the failure the steward has not advanced past yet.
        self.write(total=4, unhealthy=[_tick(i, healthy=False) for i in (2, 3, 4)], healthy_last=True)
        again, _ = steward_signals._pipeline_tick_signals(mark)
        self.assertEqual([hit["seq"] for hit in again], [4])

        # ...and once the watermark has moved, the same failure is silent.
        advanced = dict(mark, pipeline_unhealthy_total=pending)
        self.assertEqual(steward_signals._pipeline_tick_signals(advanced)[0], [])

    def test_rotation_out_of_the_ring_is_reported_not_swallowed(self) -> None:
        mark = dict(steward_signals._empty_watermark(), pipeline_unhealthy_total=0)
        self.write(total=5, unhealthy=[_tick(i, healthy=False) for i in (4, 5)])

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual(hits[0]["event"], "pipeline-telemetry-rotated")
        self.assertEqual(hits[0]["dropped"], 3)
        self.assertEqual([hit["seq"] for hit in hits[1:]], [4, 5])
        self.assertEqual(pending, 5)
        self.assertIn("pipeline-telemetry-rotated", [run["event"] for run in self.steward_runs()])

    def test_a_replaced_state_file_rescans_the_ring_and_says_so(self) -> None:
        mark = dict(steward_signals._empty_watermark(), pipeline_unhealthy_total=9)
        self.write(total=2, unhealthy=[_tick(i, healthy=False) for i in (1, 2)])

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual(hits[0]["event"], "pipeline-telemetry-reset")
        self.assertEqual([hit["seq"] for hit in hits[1:]], [1, 2])
        self.assertEqual(pending, 2)

    def test_missing_telemetry_wakes_the_head_and_is_logged_durably(self) -> None:
        mark = dict(steward_signals._empty_watermark(), pipeline_unhealthy_total=2)

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual([hit["event"] for hit in hits], ["production-state-missing"])
        self.assertEqual(hits[0]["level"], "warn")
        # The watermark does not move over a source that could not be read.
        self.assertEqual(pending, 2)
        self.assertEqual([run["event"] for run in self.steward_runs()], ["production-state-missing"])

    def test_a_quiet_precheck_keeps_the_baseline_so_a_later_failure_still_fires(self) -> None:
        """The real lifecycle: precheck skips quiet hours, and `advance` only ever runs after a
        dispatched head. If the baseline the first quiet scan took never reached disk, the next
        failure would be read as another first-ever scan and suppressed, forever (round-2 finding).
        """
        with self.steward_cli_env():
            self.write(total=0, unhealthy=[], healthy_last=True)
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)
            self.assertEqual(self.state.load_watermark()["pipeline_unhealthy_total"], 0)

            # A production tick fails. The steward is woken by it, and stays woken until it has
            # actually looked: a repeated precheck before `advance` must not consume the signal.
            self.write(total=1, unhealthy=[_tick(7, healthy=False, reason="board unreachable")])
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            hits = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["seq"] for hit in hits], [7])

            # ...and once the head has looked, the same failure is silent while a second one is not.
            self.assertEqual(steward_cli.cmd_scan(True), 0)
            self.assertEqual(steward_cli.cmd_advance(), 0)
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)
            self.write(total=2, unhealthy=[_tick(7, healthy=False), _tick(9, healthy=False)])
            self.assertEqual(steward_cli.cmd_precheck(), 0)

    def test_the_baseline_write_never_overwrites_a_watermark_or_invents_one(self) -> None:
        self.write(total=4, unhealthy=[])
        self.state.save_watermark(dict(steward_signals._empty_watermark(),
                                       pipeline_unhealthy_total=2, notified_blocked=["ref-1"]))

        steward_signals.ensure_pipeline_baseline({"pending": {"pipeline_unhealthy_total": 4}})

        mark = self.state.load_watermark()
        self.assertEqual(mark["pipeline_unhealthy_total"], 2)
        self.assertEqual(mark["notified_blocked"], ["ref-1"])

        # Telemetry that could not be read measured nothing, so it leaves no baseline behind.
        self.state.watermark_file.unlink()
        steward_signals.ensure_pipeline_baseline({"pending": {"pipeline_unhealthy_total": None}})
        self.assertIsNone(self.state.load_watermark().get("pipeline_unhealthy_total"))

    def test_a_hit_is_a_signal_and_renders(self) -> None:
        batch = {"signals": {"pipeline_ticks": [{"event": "pipeline-tick-unhealthy", "ts": _ts(1)}],
                             "new_blocked": [], "stale": [], "resource_flip": {},
                             "new_orphan_workspaces": []}}

        self.assertTrue(steward_signals.has_signal(batch))
        self.assertIn("production dispatcher", steward_signals.render_markdown(batch))


if __name__ == "__main__":
    unittest.main()
