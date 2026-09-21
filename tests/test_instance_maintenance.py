"""Instance repository maintenance runs on its own timer, never inside a tick (secretary-1657)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from secretary import state_repo
from secretary.checkpoint import CheckpointWriter
from secretary.cli import main
from secretary.config import validate_instance
from secretary.host import (
    Expectations,
    LiveHostSource,
    SystemdLayout,
    _CmdResult,
    build_doctor_expectations,
    build_plan,
    load_packaged_units,
    packaging_root,
)
from secretary.host_apply import resolve_packaged
from secretary.infra import instance_maintenance

REPO_ROOT = Path(__file__).resolve().parents[1]
UNITS = REPO_ROOT / "packaging" / "systemd"
SERVICE = "secretary-instance-maintenance.service"
TIMER = "secretary-instance-maintenance.timer"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _instance_repo(root: Path) -> Path:
    """A repository shaped like the checkpoint's target, with its canon tracked."""
    instance = root / "instance"
    (instance / "state" / "board").mkdir(parents=True)
    (instance / "state" / "runs").mkdir(parents=True)
    (instance / "state" / "board" / "cards.ndjson").write_text("", encoding="utf-8")
    (instance / "state" / "runs" / "runs.ndjson").write_text("", encoding="utf-8")
    _git(instance, "init", "--quiet", "--initial-branch", "main")
    _git(instance, "config", "user.name", "operator")
    _git(instance, "config", "user.email", "operator@example.invalid")
    _git(instance, "config", "commit.gpgsign", "false")
    # A detached gc would race the assertions below; the control must finish before they read.
    _git(instance, "config", "gc.autoDetach", "false")
    _git(instance, "add", "state")
    _git(instance, "commit", "--quiet", "-m", "canon")
    return instance


def _cross_the_auto_gc_threshold(instance: Path) -> None:
    """Write loose objects until Git's `gc --auto` estimate passes the stock 6,700.

    Git estimates the loose count from the `objects/17` fan-out directory alone, so only blobs
    landing there are written: 28 of them read as more than 6,700 objects.
    """
    target = instance / ".git" / "objects" / "17"
    target.mkdir(parents=True, exist_ok=True)
    written = 0
    index = 0
    while written < 40:
        body = f"loose object {index}\n".encode()
        index += 1
        raw = b"blob %d\0" % len(body) + body
        digest = hashlib.sha1(raw).hexdigest()
        if not digest.startswith("17"):
            continue
        (target / digest[2:]).write_bytes(zlib.compress(raw))
        written += 1


def _started_commands(trace: Path) -> list[list[str]]:
    """Every Git process argv the trace2 event stream saw start, the parent included."""
    commands: list[list[str]] = []
    if not trace.exists():
        return commands
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") in {"start", "child_start"} and isinstance(event.get("argv"), list):
            commands.append([str(argument) for argument in event["argv"]])
    return commands


def _packing(commands: list[list[str]]) -> list[list[str]]:
    words = {"gc", "pack-objects", "repack", "maintenance"}
    return [argv for argv in commands if words & set(argv)]


class _WithoutSuiteGitConfig(unittest.TestCase):
    """The suite turns implicit gc off for every Git child (`tests/__init__.py`); these tests are
    about what the instance repository's own configuration does, so they run without that."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        count = int(os.environ.pop("GIT_CONFIG_COUNT", "0") or 0)
        for index in range(count):
            os.environ.pop(f"GIT_CONFIG_KEY_{index}", None)
            os.environ.pop(f"GIT_CONFIG_VALUE_{index}", None)


class NoInTickGcTests(_WithoutSuiteGitConfig):
    """Acceptance 1: a checkpoint commit above the threshold starts no gc or pack-objects."""

    def _checkpoint_commit(self, root: Path, instance: Path) -> list[list[str]]:
        (instance / "state" / "board" / "cards.ndjson").write_text('{"ref": "x"}\n', encoding="utf-8")
        trace = root / "trace.json"
        trace.unlink(missing_ok=True)
        writer = CheckpointWriter(root / "data", instance)
        with mock.patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}):
            result = writer._commit(board_cards=1, run_records=0)
        self.assertEqual(result.status, "committed")
        return _started_commands(trace)

    def test_without_the_product_controls_the_same_commit_would_gc(self):
        # The control: the fixture really is above Git's threshold, so the assertion below is
        # about the configuration and not about a repository too small to pack.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = _instance_repo(root)
            _cross_the_auto_gc_threshold(instance)

            commands = self._checkpoint_commit(root, instance)

        self.assertTrue(_packing(commands), commands)

    def test_a_checkpoint_commit_over_the_threshold_starts_no_gc_or_pack_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = _instance_repo(root)
            _cross_the_auto_gc_threshold(instance)
            state_repo.configure_packing_controls(instance)
            loose_before = instance_maintenance.count_objects(instance)["count"]

            commands = self._checkpoint_commit(root, instance)
            loose_after = instance_maintenance.count_objects(instance)["count"]

        self.assertTrue(commands, "trace2 recorded no Git process at all")
        self.assertEqual(_packing(commands), [])
        self.assertGreater(loose_after, loose_before)

    def test_the_lifecycle_sets_implicit_gc_off_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            state_repo.configure_packing_controls(instance)

            self.assertEqual(_git(instance, "config", "--local", "--get", "gc.auto").strip(), "0")
            self.assertEqual(
                _git(instance, "config", "--local", "--get", "maintenance.auto").strip(), "false"
            )


class MaintenanceRunTests(_WithoutSuiteGitConfig):
    def test_a_run_above_the_threshold_packs_the_loose_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            _cross_the_auto_gc_threshold(instance)
            state_repo.configure_packing_controls(instance)

            result = instance_maintenance.run(instance)
            gc_auto = _git(instance, "config", "--local", "--get", "gc.auto").strip()

        self.assertGreaterEqual(result["loose_objects"]["before"], 40)
        self.assertLess(result["loose_objects"]["after"], result["loose_objects"]["before"])
        self.assertGreaterEqual(result["packs"]["after"], 1)
        # The run restates the thresholds on its command line; it never re-enables in-tick gc.
        self.assertEqual(gc_auto, "0")

    def test_a_quiet_repository_is_left_as_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            state_repo.configure_packing_controls(instance)

            result = instance_maintenance.run(instance)

        self.assertEqual(result["packs"], {"before": 0, "after": 0})
        self.assertEqual(result["loose_objects"]["before"], result["loose_objects"]["after"])

    def test_packing_runs_outside_the_state_repo_lock_and_touches_no_ref(self):
        held = {"lock": False}
        seen: list[tuple[list[str], bool]] = []

        @contextlib.contextmanager
        def lock(_instance):
            held["lock"] = True
            try:
                yield
            finally:
                held["lock"] = False

        def git(_instance, args, *, label, timeout=120):
            seen.append((list(args), held["lock"]))
            return "count: 0\npacks: 0\n" if "count-objects" in args else ""

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(state_repo, "state_repo_lock", lock),
            mock.patch.object(state_repo, "git", git),
        ):
            instance = _instance_repo(Path(tmp))
            instance_maintenance.run(instance)

        gc = next((args, locked) for args, locked in seen if "gc" in args)
        self.assertFalse(gc[1], "the packing step must not hold the state-repo lock")
        for setting in ("gc.packRefs=false", "gc.reflogExpire=never", "gc.reflogExpireUnreachable=never"):
            self.assertIn(setting, gc[0])
        self.assertIn("gc.autoDetach=false", gc[0])
        self.assertEqual(gc[0][-3:], ["gc", "--auto", "--quiet"])
        reflog = next((args, locked) for args, locked in seen if "reflog" in args)
        self.assertTrue(reflog[1], "reflog expiry writes refs and belongs under the lock")

    def test_the_cli_reports_a_run_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance_repo(Path(tmp))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["instance-maintenance", "--instance", str(instance)])

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertIn("loose_objects", payload)

    def test_the_cli_fails_on_a_directory_that_is_no_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["instance-maintenance", "--instance", tmp])

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")


class MaintenanceUnitTests(unittest.TestCase):
    """Acceptance 2: a product-owned timer, planned like every other shipped component."""

    def _rendered(self) -> dict[str, bytes]:
        units = load_packaged_units(
            UNITS,
            "secretary-",
            SystemdLayout(
                REPO_ROOT, Path("/srv/instance"), Path("/srv/data"), "operator", Path("/home/operator")
            ),
        )
        return {unit.name: unit.content for unit in units}

    def test_the_service_runs_the_product_command_once_per_trigger(self):
        service = self._rendered()[SERVICE].decode()

        self.assertIn("Type=oneshot\n", service)
        self.assertIn("User=operator\n", service)
        self.assertIn(
            f"ExecStart={REPO_ROOT}/.venv/bin/secretary instance-maintenance --instance /srv/instance\n",
            service,
        )
        self.assertIn("IOSchedulingClass=idle\n", service)
        # systemd's default 90 s start timeout must not cut the pack short: the unit's own bound
        # sits above the product's, with a margin, so the product's refusal fires first.
        timeouts = [
            line.partition("=")[2] for line in service.splitlines() if line.startswith("TimeoutStartSec=")
        ]
        self.assertEqual(len(timeouts), 1)
        product_bound = instance_maintenance.GC_TIMEOUT_SECONDS + instance_maintenance.REFLOG_TIMEOUT_SECONDS
        self.assertGreaterEqual(int(timeouts[0]), product_bound + 15 * 60)
        # Fired by its timer only: an [Install] section would let it start at boot.
        self.assertNotIn("[Install]", service)

    def test_the_timer_is_daily_catches_up_and_is_installable(self):
        timer = self._rendered()[TIMER].decode()

        self.assertIn("OnCalendar=04:17:00\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertIn(f"Unit={SERVICE}\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)

    def test_the_plan_owns_both_units_unless_the_component_is_opted_out(self):
        units = load_packaged_units(UNITS, "secretary-")
        planned = {
            resource.name
            for resource in build_plan({"host": {"unit_prefix": "secretary-"}}, [], packaged=units)
        }
        opted_out = {
            resource.name
            for resource in build_plan(
                {
                    "host": {
                        "unit_prefix": "secretary-",
                        "components": {"instance-maintenance": {"enabled": False}},
                    }
                },
                [],
                packaged=units,
            )
        }

        self.assertLessEqual({SERVICE, TIMER}, planned)
        self.assertFalse({SERVICE, TIMER} & opted_out)


class MaintenanceStatusTests(unittest.TestCase):
    """The timer is listed under `host.schedules` with the evidence that it ran."""

    def test_status_lists_the_timer_with_its_last_trigger(self):
        instance = REPO_ROOT / "examples" / "instance"
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(REPO_ROOT)}),
        ):
            fixture = Path(tmp)
            report = validate_instance(instance)
            expected = build_doctor_expectations(
                report.instance,
                report.bindings,
                packaged=resolve_packaged(
                    report.instance,
                    packaging_root(REPO_ROOT),
                    product_root=REPO_ROOT,
                    instance_path=instance,
                    data_dir=report.data_dir,
                ),
                data_dir=report.data_dir,
            )
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(
                    [
                        *(f"{name} enabled active" for name in sorted(expected.units) if name != SERVICE),
                        f"{SERVICE} static inactive",
                    ]
                ),
                encoding="utf-8",
            )
            (fixture / "timer-triggers.txt").write_text(
                f"{TIMER} Mon 2026-09-21 04:21:09 UTC\n", encoding="utf-8"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["status", "--json", "--host-fixture", str(fixture), "--instance", str(instance)])

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertIn(SERVICE, expected.units)
        schedule = next(row for row in payload["host"]["schedules"] if row["name"] == TIMER)
        self.assertEqual(schedule["enabled"], "enabled")
        self.assertEqual(schedule["active"], "active")
        self.assertEqual(schedule["last_trigger"], "Mon 2026-09-21 04:21:09 UTC")
        service = next(row for row in payload["host"]["units"] if row["name"] == SERVICE)
        self.assertEqual(service["active"], "inactive")
        others = [row for row in payload["host"]["schedules"] if row["name"] != TIMER]
        self.assertTrue(others)
        self.assertTrue(all(row["last_trigger"] is None for row in others))

    def test_the_live_probe_reads_last_trigger_for_timers_only(self):
        shown: list[str] = []

        class RuntimeHost(LiveHostSource):
            def _run(self, cmd):
                if cmd[1] == "list-unit-files":
                    return _CmdResult(True, 0, f"{TIMER} enabled enabled\n{SERVICE} static -\n", "")
                if cmd[1] == "is-enabled":
                    return _CmdResult(True, 0, "enabled\n", "")
                if cmd[1] == "is-active":
                    return _CmdResult(True, 0, "active\n", "")
                if cmd[1] == "show":
                    shown.append(cmd[-1])
                    return _CmdResult(True, 0, "Mon 2026-09-21 04:21:09 UTC\n", "")
                return _CmdResult(True, 0, "", "")

        expected = Expectations(
            units={TIMER, SERVICE},
            unit_prefix="secretary-",
            unit_runtime={TIMER: (True, True), SERVICE: (False, False)},
        )
        result = RuntimeHost().collect(expected)

        self.assertEqual(result.errors, {})
        self.assertEqual(shown, [TIMER])
        self.assertEqual(result.inventory.timer_triggers, {TIMER: "Mon 2026-09-21 04:21:09 UTC"})


if __name__ == "__main__":
    unittest.main()
