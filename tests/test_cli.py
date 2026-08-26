from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import MEMORY_EXIT_PERMISSION, build_parser, main
from tests.head_registry import write_installed_pair

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"
# One resource whose probe answers and one whose probe cannot be started, with a profile so the
# registry validates. No probe here reaches a provider.
PROBE_SNAPSHOT = """resources:
  broken-probe:
    account: broken-account
    probe: secretary-1464-no-such-probe --check
  green-probe:
    account: green-account
    probe: 'true'
profiles:
  only-head:
    resource: green-probe
    adapter: claude
    fallback: []
role_defaults:
  new_card: only-head
"""


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def init_instance_repo(instance_dir: Path) -> None:
    """Turn a written-out instance dir into the private repo the writers commit to."""
    git(instance_dir, "init", "--quiet", "--initial-branch", "main")
    git(instance_dir, "config", "user.name", "operator")
    git(instance_dir, "config", "user.email", "operator@example.invalid")
    git(instance_dir, "config", "commit.gpgsign", "false")
    git(instance_dir, "add", "instance.yaml")
    git(instance_dir, "commit", "--quiet", "-m", "config")


class BoardCommandInstanceTests(unittest.TestCase):
    """Every board command names the installation it talks to, reads included.

    Three of them did not, each failing differently: `task list`/`task show` fell through to
    `Path.home()/secretary-instance` resolved at import, so on the appliance host they answered
    from the production board no matter what the caller named; `sprint list` carried no `instance`
    attribute at all and died with an AttributeError inside its reader. One parser-level assertion
    covers the family, so a new subcommand cannot rejoin it quietly.
    """

    def _subparsers(self, name: str):
        top = {
            action.dest: action
            for action in build_parser()._subparsers._group_actions  # type: ignore[union-attr]
        }["command"]
        return top.choices[name].__dict__["_subparsers"]._group_actions[0].choices  # type: ignore[union-attr]

    def test_every_task_and_sprint_subcommand_takes_an_instance(self) -> None:
        for group in ("task", "sprint"):
            for name, parser in self._subparsers(group).items():
                options = {
                    option
                    for action in parser._actions  # type: ignore[union-attr]
                    for option in action.option_strings
                }
                self.assertIn("--instance", options, f"{group} {name} names no installation")


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_doctor_dry_run_validates_example_instance(self):
        code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE)])

        self.assertEqual(code, 0, output)
        self.assertIn("Secretary doctor report", output)
        self.assertIn("mode: dry-run", output)
        self.assertIn("name: example-secretary", output)
        self.assertIn("projects: 1", output)
        self.assertIn("adapters: 1", output)
        self.assertIn("data manifest: present", output)
        self.assertIn("memory model cache: /var/lib/secretary-data/memory/fastembed-cache", output)
        self.assertIn("host changes: none", output)
        self.assertIn("status: ok", output)

    def test_doctor_warns_when_model_cache_is_under_tmp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_text(
                "version: 1\nname: temporary-cache\ndata_dir: /tmp/secretary-data\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y\n",
                encoding="utf-8",
            )
            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance)])

        self.assertEqual(code, 0, output)
        self.assertIn("memory model cache: /tmp/secretary-data/memory/fastembed-cache", output)
        self.assertIn("warning: memory model cache is in a temporary directory", output)

    def test_doctor_accepts_instance_file_path(self):
        code, output = self.run_cli(
            ["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE / "instance.yaml")]
        )

        self.assertEqual(code, 0, output)
        self.assertIn("status: ok", output)

    def test_doctor_checks_live_host_by_default(self):
        code, output = self.run_cli(["doctor", "--instance", str(EXAMPLE_INSTANCE)])

        self.assertIn("host inventory: read-only", output)
        self.assertIn("mode: read-only", output)

    def test_doctor_reports_missing_field_with_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_text("version: 1\nname: broken\n", encoding="utf-8")

            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance)])

        self.assertEqual(code, 1)
        self.assertIn("config problem", output)
        # Missing required 'data_dir' / 'offsite' surfaced at the root.
        self.assertIn("data_dir", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_warns_when_data_manifest_is_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance_dir)])
            strict_code, strict_output = self.run_cli(
                ["doctor", "--dry-run", "--strict", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 0, output)
        self.assertIn("data manifest: absent", output)
        self.assertIn("warnings: 1", output)
        self.assertEqual(strict_code, 1, strict_output)
        self.assertIn("status: warnings", strict_output)

    def seed_checkpoint_instance(self, tmpdir: Path, production: dict) -> Path:
        """An instance repo with one commit and a production state to read."""
        instance_dir = tmpdir / "instance"
        data_dir = tmpdir / "secretary-data"
        instance_dir.mkdir()
        (instance_dir / "instance.yaml").write_text(
            "version: 1\n"
            "name: example\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        git(instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(instance_dir, "config", "user.name", "operator")
        git(instance_dir, "config", "user.email", "operator@example.invalid")
        git(instance_dir, "add", "instance.yaml")
        git(instance_dir, "commit", "--quiet", "-m", "config")
        self.run_cli(["data", "init", "--instance", str(instance_dir)])
        state = data_dir / "dispatcher"
        state.mkdir(parents=True, exist_ok=True)
        (state / "production-state.json").write_text(json.dumps(production), encoding="utf-8")
        return instance_dir

    def seed_probe_instance(self, tmpdir: Path, *, recorded: dict | None = None) -> Path:
        """An installation whose head registry describes one runnable probe and one broken one.

        Neither probe reaches a provider: `true` stands for a resource that answers, and a command
        no host has stands for the shape this card is about — a probe that never starts, which is
        what production had while `python3` resolved to an interpreter without the product on it.
        """
        instance_dir = tmpdir / "instance"
        data_dir = tmpdir / "secretary-data"
        instance_dir.mkdir()
        (instance_dir / "instance.yaml").write_text(
            "version: 1\n"
            "name: example\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        write_installed_pair(instance_dir, PROBE_SNAPSHOT)
        if recorded is not None:
            state = data_dir / "dispatcher"
            state.mkdir(parents=True, exist_ok=True)
            (state / "resource_health.json").write_text(json.dumps(recorded), encoding="utf-8")
        return instance_dir

    def test_doctor_names_a_probe_that_cannot_run_apart_from_a_red_resource(self):
        """secretary-1464: the P0 item of `issue:6cfbbb9b` about probes. A resource whose probe
        cannot be started is a defect of this installation — claims on it went ungated — so it is a
        doctor finding, while the resource that answered is only reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_probe_instance(Path(tmpdir))
            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance_dir)])

        self.assertEqual(code, 1, output)
        self.assertIn("resource probes: read-only", output)
        self.assertIn("green-probe: ready", output)
        self.assertIn("broken-probe: probe_broken", output)
        self.assertIn("resource probe findings:", output)
        self.assertIn("resource broken-probe probe cannot run", output)
        self.assertIn("claims on this resource are not gated by health", output)
        self.assertNotIn("resource green-probe probe cannot run", output)
        self.assertIn("status: findings", output)

    def test_doctor_reports_a_recorded_broken_probe_without_running_anything(self):
        """Offline, and inside the probe TTL, doctor answers from the verdict the dispatcher wrote,
        so the operator sees the ungated resource without doctor spending a provider call."""
        recorded = {
            "broken-probe": {
                "resource": "broken-probe",
                "status": "probe_broken",
                "reason": "probe could not be launched: No module named triggered_agents",
                "checked_at": time.time(),
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_probe_instance(Path(tmpdir), recorded=recorded)
            with mock.patch("secretary.cli.run_probe") as run:
                code, output = self.run_cli(
                    ["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)]
                )

        run.assert_not_called()
        self.assertEqual(code, 1, output)
        self.assertIn("broken-probe: probe_broken (recorded)", output)
        self.assertIn("No module named triggered_agents", output)
        self.assertIn("resource broken-probe probe cannot run", output)
        # Offline cannot answer for the other resource at all, and says nothing rather than green.
        self.assertNotIn("green-probe", output)

    def test_doctor_says_nothing_about_probes_without_an_installed_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_checkpoint_instance(Path(tmpdir), {"version": 1})
            code, output = self.run_cli(["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)])

        self.assertEqual(code, 0, output)
        self.assertNotIn("resource probes", output)

    def test_doctor_lists_background_automations_but_offline_leaves_them_uninspected(self):
        code, output = self.run_cli(["doctor", "--dry-run", "--offline", "--instance", str(EXAMPLE_INSTANCE)])

        self.assertEqual(code, 0, output)
        self.assertIn("background automations: read-only", output)
        self.assertIn("not inspected", output)
        # Offline must never shell out to orca, so no role can be reported as reconciled or missing.
        self.assertNotIn(": managed", output)
        self.assertNotIn(": missing", output)

    def test_background_automations_report_missing_and_managed_roles(self):
        from secretary.cli import print_background_automations

        # No live automation of any name -> every shipped background role reads as not provisioned.
        with mock.patch("secretary.automations.OrcaAutomationClient.list", return_value=[]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                print_background_automations(inspect=True)
        rendered = out.getvalue()
        self.assertIn("background automations: read-only", rendered)
        for role in ("curator", "retro", "steward"):
            self.assertIn(f"{role}: missing (not provisioned)", rendered)

    def test_background_automations_report_an_unreadable_inventory_as_unavailable(self):
        from secretary.automations import AutomationError
        from secretary.cli import print_background_automations

        with mock.patch(
            "secretary.automations.OrcaAutomationClient.list",
            side_effect=AutomationError("list automations: orca not found"),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                print_background_automations(inspect=True)
        rendered = out.getvalue()
        self.assertIn("unavailable: list automations: orca not found", rendered)
        # A hiccup reading Orca must not masquerade as "every role missing".
        self.assertNotIn("missing (not provisioned)", rendered)

    def test_doctor_prints_checkpoint_freshness(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_checkpoint_instance(
                Path(tmpdir),
                {
                    "version": 1,
                    "checkpoint": {"status": "committed", "commit": "abc123"},
                    "checkpoint_push": {
                        "status": "pushed",
                        "last_push_at": "2026-07-20T10:00:00Z",
                        "last_push_commit": "deadbeef",
                    },
                },
            )
            code, output = self.run_cli(["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)])

        self.assertEqual(code, 0, output)
        self.assertIn("checkpoint freshness: read-only", output)
        self.assertIn("last push: 2026-07-20T10:00:00Z", output)
        self.assertIn("push: pushed", output)
        self.assertIn("lag: ", output)
        self.assertIn("status: ok", output)

    def test_doctor_reports_remote_divergence_as_a_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_checkpoint_instance(
                Path(tmpdir),
                {
                    "version": 1,
                    "checkpoint_push": {
                        "status": "diverged",
                        "reason": "remote origin/main is at deadbeef0000",
                        "remote_diverged": True,
                    },
                },
            )
            code, output = self.run_cli(["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)])

        self.assertEqual(code, 1, output)
        self.assertIn("alarm: remote diverged", output)
        self.assertIn("checkpoint findings:", output)
        self.assertIn("remote diverged: remote origin/main is at deadbeef0000", output)
        self.assertIn("status: findings", output)

    def test_doctor_reports_a_blocked_checkpoint_gate_as_a_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_checkpoint_instance(
                Path(tmpdir),
                {
                    "version": 1,
                    "checkpoint": {
                        "status": "blocked",
                        "reason": "secret detected in state/board/cards.ndjson",
                    },
                },
            )
            code, output = self.run_cli(["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)])

        self.assertEqual(code, 1, output)
        self.assertIn("blocked: secret detected in state/board/cards.ndjson", output)
        self.assertIn("status: findings", output)

    def test_doctor_stays_quiet_about_checkpoints_an_instance_never_wrote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_checkpoint_instance(Path(tmpdir), {"version": 1})
            code, output = self.run_cli(["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)])

        self.assertEqual(code, 0, output)
        self.assertNotIn("checkpoint freshness", output)

    def test_backup_create_accepts_kind_both(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            with mock.patch("secretary.cli.create_backups") as create:
                create.return_value = [
                    mock.Mock(archive=Path("/tmp/core.tar"), manifest={"version": 1, "backup_kind": "core"}),
                    mock.Mock(archive=Path("/tmp/full.tar"), manifest={"version": 1, "backup_kind": "full"}),
                ]
                code, output = self.run_cli(
                    [
                        "backup",
                        "create",
                        "--instance",
                        str(instance_dir),
                        "--kind",
                        "both",
                    ]
                )

        self.assertEqual(code, 0, output)
        self.assertIn("kind: core", output)
        self.assertIn("kind: full", output)
        self.assertEqual(create.call_args.kwargs["backup_kinds"], ("core", "full"))

    def test_data_init_generates_manifest_that_doctor_finds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            init_code, init_output = self.run_cli(["data", "init", "--instance", str(instance_dir)])
            doctor_code, doctor_output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )
            manifest_exists = (data_dir / "data-manifest.json").is_file()

        self.assertEqual(init_code, 0, init_output)
        self.assertTrue(manifest_exists)
        self.assertEqual(doctor_code, 0, doctor_output)
        self.assertIn("data manifest: present", doctor_output)

    def test_data_init_anchors_relative_data_dir_from_a_foreign_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            foreign = root / "foreign-workspace"
            foreign.mkdir()
            directories = (root / "instance-dir", root / "instance-file")
            for instance_dir in directories:
                instance_dir.mkdir()
                (instance_dir / "instance.yaml").write_text(
                    "version: 1\n"
                    "name: example\n"
                    "data_dir: relative-data\n"
                    "offsite:\n"
                    "  instance_remote: git@example.invalid:x/y.git\n",
                    encoding="utf-8",
                )

            with contextlib.chdir(foreign):
                directory_code, directory_output = self.run_cli(
                    ["data", "init", "--instance", str(directories[0])]
                )
                file_code, file_output = self.run_cli(
                    ["data", "init", "--instance", str(directories[1] / "instance.yaml")]
                )

            for instance_dir, code, output in (
                (directories[0], directory_code, directory_output),
                (directories[1], file_code, file_output),
            ):
                self.assertEqual(code, 0, output)
                self.assertTrue((instance_dir / "relative-data" / "data-manifest.json").is_file())
            self.assertFalse((foreign / "relative-data").exists())

    def test_data_export_artifacts_refuses_an_invalid_instance_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                "data_dir: relative-data\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            projects = instance_dir / "projects"
            projects.mkdir()
            (projects / "bad.yaml").write_text("unexpected: value\n", encoding="utf-8")

            code, output = self.run_cli(["data", "export-artifacts", "--instance", str(instance_dir)])
            untouched = not (instance_dir / "relative-data").exists()

        self.assertEqual(code, 1, output)
        self.assertIn("secretary data: 6 config problem(s):", output)
        self.assertIn("bad.yaml", output)
        self.assertTrue(untouched)

    def test_data_init_overwrites_broken_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            data_dir.mkdir()
            (data_dir / "data-manifest.json").write_text("{not-json", encoding="utf-8")
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            code, output = self.run_cli(["data", "init", "--instance", str(instance_dir)])
            doctor_code, doctor_output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 0, output)
        self.assertEqual(doctor_code, 0, doctor_output)
        self.assertIn("data manifest: present", doctor_output)

    def test_data_init_reports_manifest_publish_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            with mock.patch(
                "secretary.cli.init_layout",
                side_effect=RuntimeError("could not write data manifest: full"),
            ):
                code, output = self.run_cli(["data", "init", "--instance", str(instance_dir)])

        self.assertEqual(code, 1)
        self.assertIn("secretary data init: could not write data manifest", output)
        self.assertNotIn("Traceback", output)

    def test_data_init_reports_layout_prepare_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            data_dir.write_text("not a directory", encoding="utf-8")
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            code, output = self.run_cli(["data", "init", "--instance", str(instance_dir)])

        self.assertEqual(code, 1)
        self.assertIn("secretary data init: cannot prepare secretary-data layout", output)
        self.assertNotIn("Traceback", output)

    def test_data_init_reports_manifest_tempfile_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            with mock.patch("secretary.data.tempfile.mkstemp", side_effect=PermissionError("denied")):
                code, output = self.run_cli(["data", "init", "--instance", str(instance_dir)])

        self.assertEqual(code, 1)
        self.assertIn("secretary data init: could not write data manifest", output)
        self.assertNotIn("Traceback", output)

    def test_raw_kanboard_dump_command_uses_data_dir(self):
        def fake_run(command, **_kwargs):
            destination = Path(command[-1])
            destination.mkdir(parents=True)
            (destination / "db.sqlite").write_bytes(b"sqlite")

        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])

            with mock.patch("secretary.data.subprocess.run", side_effect=fake_run) as run:
                code, output = self.run_cli(["data", "raw-kanboard-dump", "--instance", str(instance_dir)])
                docker_command = run.call_args.args[0]

        self.assertEqual(code, 0, output)
        self.assertIn("kanboard raw dump:", output)
        self.assertEqual(docker_command[0:2], ["docker", "cp"])

    def test_raw_kanboard_dump_reports_staging_prepare_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])

            with mock.patch("secretary.data.tempfile.mkdtemp", side_effect=PermissionError("denied")):
                code, output = self.run_cli(["data", "raw-kanboard-dump", "--instance", str(instance_dir)])

        self.assertEqual(code, 1)
        self.assertIn("secretary data raw-kanboard-dump: could not create raw dump", output)
        self.assertNotIn("Traceback", output)

    def test_export_commands_report_bad_data_dir_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            state = root / "state"
            transcript_root = root / "transcripts"
            instance_dir.mkdir()
            data_dir.write_text("not a directory", encoding="utf-8")
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text("{}\n", encoding="utf-8")
            (state / "pipeline" / "cards.json").write_text("{}", encoding="utf-8")
            transcript_root.mkdir()
            (transcript_root / "session.jsonl").write_text("{}\n", encoding="utf-8")
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )

            commands = [
                (
                    ["data", "export", "--instance", str(instance_dir)],
                    "secretary data export: cannot prepare memory data dir",
                ),
                (
                    ["data", "export-board", "--instance", str(instance_dir)],
                    "secretary data export-board: cannot prepare board data dir",
                ),
                (
                    ["data", "export-memory", "--instance", str(instance_dir)],
                    "secretary data export-memory: cannot prepare memory data dir",
                ),
                (
                    [
                        "data",
                        "export-runs",
                        "--instance",
                        str(instance_dir),
                        "--state-dir",
                        str(state),
                    ],
                    "secretary data export-runs: cannot prepare runs data dir",
                ),
                (
                    [
                        "data",
                        "export-transcripts",
                        "--instance",
                        str(instance_dir),
                        "--root",
                        str(transcript_root),
                    ],
                    "secretary data export-transcripts: cannot prepare transcripts data dir",
                ),
            ]

            for argv, expected in commands:
                with self.subTest(argv=argv):
                    code, output = self.run_cli(argv)

                self.assertEqual(code, 1)
                self.assertIn(expected, output)
                self.assertNotIn("Traceback", output)

    def test_export_memory_command_uses_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            init_instance_repo(instance_dir)
            facts = instance_dir / "state" / "memory" / "facts" / "secretary"
            facts.mkdir(parents=True)
            (facts / "fact.md").write_text("fact\n", encoding="utf-8")

            code, output = self.run_cli(["data", "export-memory", "--instance", str(instance_dir)])
            export_exists = (data_dir / "memory" / "export.ndjson").is_file()

        self.assertEqual(code, 0, output)
        self.assertIn("memory facts: 1", output)
        self.assertTrue(export_exists)

    def test_export_memory_command_reports_decode_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            init_instance_repo(instance_dir)
            fact = instance_dir / "state" / "memory" / "facts" / "secretary" / "bad.md"
            fact.parent.mkdir(parents=True)
            fact.write_bytes(b"\xff\xfe")

            code, output = self.run_cli(["data", "export-memory", "--instance", str(instance_dir)])

        self.assertEqual(code, 1)
        self.assertIn("secretary data export-memory: could not decode memory fact", output)
        self.assertNotIn("Traceback", output)

    def test_memory_protocol_commands_return_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            init_instance_repo(instance_dir)
            fact = root / "fact.md"
            fact.write_text("fact from cli\n", encoding="utf-8")

            propose_code, propose_output = self.run_cli(
                [
                    "memory",
                    "propose",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "curator:claude/session",
                    "--scope",
                    "project:secretary",
                    "--slug",
                    "cli-fact",
                    "--file",
                    str(fact),
                    "--source",
                    "curator:claude/session",
                    "--tags",
                    "secretary,memory",
                ]
            )
            proposal = json.loads(propose_output)
            commit_code, commit_output = self.run_cli(
                [
                    "memory",
                    "commit",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "curator:claude/session",
                    "--propose-id",
                    proposal["propose_id"],
                ]
            )
            committed = json.loads(commit_output)

        self.assertEqual(propose_code, 0, propose_output)
        self.assertEqual(proposal["op"], "propose")
        self.assertEqual(proposal["fact"], "secretary/cli-fact")
        self.assertEqual(commit_code, 0, commit_output)
        self.assertEqual(committed["op"], "commit")
        self.assertEqual(committed["changed_facts"], ["secretary/cli-fact"])
        self.assertTrue(committed["commit"])

    def test_butler_proposes_through_the_cli_but_cannot_commit(self):
        """The butler reaches the curator inbox and stops there.

        Contract: docs/PROTOCOLS.md, "Memory" — proposing is not publishing.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            init_instance_repo(instance_dir)
            fact = root / "fact.md"
            fact.write_text("the owner prefers evening digests\n", encoding="utf-8")

            propose_code, propose_output = self.run_cli(
                [
                    "memory",
                    "propose",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "butler:telegram/session",
                    "--scope",
                    "global",
                    "--slug",
                    "butler-cli-fact",
                    "--file",
                    str(fact),
                    "--source",
                    "butler:telegram/session",
                ]
            )
            proposal = json.loads(propose_output)
            commit_code, commit_output = self.run_cli(
                [
                    "memory",
                    "commit",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "butler:telegram/session",
                    "--propose-id",
                    proposal["propose_id"],
                ]
            )
            refusal = json.loads(commit_output)
            canon_written = (
                instance_dir / "state" / "memory" / "facts" / "global" / "butler-cli-fact.md"
            ).exists()
            staged = (data_dir / "memory" / ".staging" / proposal["propose_id"] / "proposal.json").is_file()

        self.assertEqual(propose_code, 0, propose_output)
        self.assertTrue(proposal["ok"])
        self.assertEqual(proposal["fact"], "global/butler-cli-fact")
        self.assertEqual(proposal["actor"], "butler:telegram/session")
        self.assertEqual(proposal["source"], "butler:telegram/session")
        self.assertEqual(commit_code, MEMORY_EXIT_PERMISSION)
        self.assertFalse(refusal["ok"])
        self.assertEqual(refusal["error"], "permission")
        self.assertIn("cannot commit canonical memory", refusal["message"])
        self.assertIn("butler proposals await curator review", refusal["message"])
        self.assertFalse(canon_written)
        self.assertTrue(staged)

    def test_memory_verify_command_reports_parity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            init_instance_repo(instance_dir)
            fact = root / "fact.md"
            fact.write_text("verified cli fact\n", encoding="utf-8")
            _propose_code, propose_output = self.run_cli(
                [
                    "memory",
                    "propose",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "curator:claude/session",
                    "--scope",
                    "project:secretary",
                    "--slug",
                    "verified-cli",
                    "--file",
                    str(fact),
                    "--source",
                    "curator:claude/session",
                ]
            )
            proposal = json.loads(propose_output)
            self.run_cli(
                [
                    "memory",
                    "commit",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "curator:claude/session",
                    "--propose-id",
                    proposal["propose_id"],
                ]
            )
            index = data_dir / "memory" / "index.sqlite"

            with sqlite3.connect(index) as conn:
                conn.execute("create table memories(id integer primary key)")
                conn.execute("insert into memories default values")
                conn.commit()

            code, output = self.run_cli(["memory", "verify", "--instance", str(instance_dir)])

        self.assertEqual(code, 0, output)
        self.assertIn("memory facts: 1", output)
        self.assertIn("export facts: 1", output)
        self.assertIn("index facts: 1", output)
        self.assertIn("status: ok", output)

    def test_memory_commit_reports_export_failure_with_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            init_instance_repo(instance_dir)
            fact = root / "fact.md"
            fact.write_text("cli retryable fact\n", encoding="utf-8")
            propose_code, propose_output = self.run_cli(
                [
                    "memory",
                    "propose",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "curator:claude/session",
                    "--scope",
                    "project:secretary",
                    "--slug",
                    "cli-retryable",
                    "--file",
                    str(fact),
                    "--source",
                    "curator:claude/session",
                ]
            )
            proposal = json.loads(propose_output)

            with mock.patch(
                "secretary.memory_write._publish_memory_export",
                side_effect=RuntimeError("disk full"),
            ):
                failed_code, failed_output = self.run_cli(
                    [
                        "memory",
                        "commit",
                        "--instance",
                        str(instance_dir),
                        "--actor",
                        "curator:claude/session",
                        "--propose-id",
                        proposal["propose_id"],
                    ]
                )
            failed = json.loads(failed_output)
            retry_code, retry_output = self.run_cli(
                [
                    "memory",
                    "commit",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "curator:claude/session",
                    "--propose-id",
                    proposal["propose_id"],
                ]
            )
            retried = json.loads(retry_output)
            log_count = git(instance_dir, "rev-list", "--count", "HEAD", "--", "state/memory")

        self.assertEqual(propose_code, 0, propose_output)
        self.assertEqual(failed_code, 1, failed_output)
        self.assertEqual(failed["error"], "export")
        self.assertEqual(failed["fact"], "secretary/cli-retryable")
        self.assertTrue(failed["commit"])
        self.assertEqual(retry_code, 0, retry_output)
        self.assertEqual(retried["commit"], failed["commit"])
        self.assertEqual(log_count, "1")

    def test_memory_protocol_commands_use_stable_error_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            init_instance_repo(instance_dir)
            fact = root / "fact.md"
            fact.write_text("fact from cli\n", encoding="utf-8")

            validation_code, validation_output = self.run_cli(
                [
                    "memory",
                    "propose",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "curator:claude/session",
                    "--scope",
                    "bad",
                    "--slug",
                    "cli-fact",
                    "--file",
                    str(fact),
                    "--source",
                    "curator:claude/session",
                ]
            )
            permission_code, permission_output = self.run_cli(
                [
                    "memory",
                    "propose",
                    "--instance",
                    str(instance_dir),
                    "--actor",
                    "worker:codex/session",
                    "--scope",
                    "project:secretary",
                    "--slug",
                    "cli-fact",
                    "--file",
                    str(fact),
                    "--source",
                    "worker:codex/session",
                ]
            )

        self.assertEqual(validation_code, 2, validation_output)
        self.assertEqual(json.loads(validation_output)["error"], "validation")
        self.assertEqual(permission_code, 3, permission_output)
        self.assertEqual(json.loads(permission_output)["error"], "permission")

    def test_export_transcripts_command_accepts_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            transcript_root = root / "transcripts"
            transcript_root.mkdir()
            (transcript_root / "session.jsonl").write_text("{}\n", encoding="utf-8")
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])

            code, output = self.run_cli(
                [
                    "data",
                    "export-transcripts",
                    "--instance",
                    str(instance_dir),
                    "--root",
                    str(transcript_root),
                ]
            )
            inventory_exists = (data_dir / "transcripts" / "inventory.json").is_file()

        self.assertEqual(code, 0, output)
        self.assertIn("transcripts: 1", output)
        self.assertTrue(inventory_exists)

    def test_doctor_reports_bad_nested_field_with_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            shutil.copytree(EXAMPLE_INSTANCE, instance_dir)
            binding = instance_dir / "projects" / "example-project.yaml"
            binding.write_text(
                "id: Bad_Id\nrepo: /srv/x\nenabled: true\nadapter: example-project\ndefault_branch: main\n",
                encoding="utf-8",
            )

            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance_dir)])

        self.assertEqual(code, 1)
        self.assertIn("example-project.yaml: id:", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_unreadable_instance_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.yaml"
            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(missing)])

        self.assertEqual(code, 1)
        self.assertIn("config not found", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_yaml_parse_error_does_not_leak_source(self):
        secret = "sk-live-DO-NOT-LEAK-9f3a2b"
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            # Unterminated quoted scalar: PyYAML's raw message would echo the line.
            instance.write_text(f'name: "{secret}\n', encoding="utf-8")

            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance)])

        self.assertEqual(code, 1)
        self.assertIn("cannot parse config", output)
        self.assertIn("line", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("Traceback", output)

    def test_doctor_yaml_alias_error_does_not_leak_alias_name(self):
        secret = "sk_live_alias_do_not_leak_9f3a"
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            # Undefined alias: PyYAML's raw problem text names the alias.
            instance.write_text(f"name: *{secret}\n", encoding="utf-8")

            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance)])

        self.assertEqual(code, 1)
        self.assertIn("cannot parse config", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_non_utf8_instance_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_bytes(b"\xff")

            code, output = self.run_cli(["doctor", "--dry-run", "--instance", str(instance)])

        self.assertEqual(code, 1)
        self.assertIn("cannot decode config as UTF-8", output)
        self.assertNotIn("Traceback", output)

    def test_remaining_target_command_stubs_are_explicit(self):
        for command in (
            ["reconcile"],
            ["backup"],
        ):
            with self.subTest(command=command):
                code, output = self.run_cli(command)
                self.assertEqual(code, 1)
                self.assertIn("not implemented", output)

    def test_bootstrap_empty_returns_machine_readable_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance.mkdir()
            (instance / "instance.yaml").write_text(
                "version: 1\nname: test\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n",
                encoding="utf-8",
            )
            code, output = self.run_cli(["bootstrap", "--empty", "--dry-run", "--instance", str(instance)])

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "bootstrap")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(data_dir.exists())

    def test_restore_reports_preflight_error_as_json_exit_two(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            instance.mkdir()
            (instance / "instance.yaml").write_text(
                "version: 1\nname: test\n"
                f"data_dir: {root / 'secretary-data'}\n"
                "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n",
                encoding="utf-8",
            )
            archive = root / "missing.tar"
            code, output = self.run_cli(["restore", str(archive), "--instance", str(instance), "--dry-run"])

        self.assertEqual(code, 2, output)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "restore")
        self.assertIn("archive not found", payload["error"])


if __name__ == "__main__":
    unittest.main()
