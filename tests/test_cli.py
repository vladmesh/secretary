from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from secretary.cli import main


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"


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


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_doctor_dry_run_validates_example_instance(self):
        code, output = self.run_cli(
            ["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE)]
        )

        self.assertEqual(code, 0, output)
        self.assertIn("Secretary doctor report", output)
        self.assertIn("mode: dry-run", output)
        self.assertIn("name: example-secretary", output)
        self.assertIn("projects: 1", output)
        self.assertIn("adapters: 1", output)
        self.assertIn("data manifest: present", output)
        self.assertIn("host changes: none", output)
        self.assertIn("status: ok", output)

    def test_doctor_accepts_instance_file_path(self):
        code, output = self.run_cli(
            ["doctor", "--dry-run", "--instance", str(EXAMPLE_INSTANCE / "instance.yaml")]
        )

        self.assertEqual(code, 0, output)
        self.assertIn("status: ok", output)

    def test_doctor_checks_live_host_by_default(self):
        code, output = self.run_cli(
            ["doctor", "--instance", str(EXAMPLE_INSTANCE)]
        )

        self.assertIn("host inventory: read-only", output)
        self.assertIn("mode: read-only", output)

    def test_doctor_reports_missing_field_with_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_text("version: 1\nname: broken\n", encoding="utf-8")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance)]
            )

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

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )
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
            code, output = self.run_cli(
                ["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)]
            )

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
            code, output = self.run_cli(
                ["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)]
            )

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
                    "checkpoint": {"status": "blocked", "reason": "secret detected in state/board/cards.ndjson"},
                },
            )
            code, output = self.run_cli(
                ["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 1, output)
        self.assertIn("blocked: secret detected in state/board/cards.ndjson", output)
        self.assertIn("status: findings", output)

    def test_doctor_stays_quiet_about_checkpoints_an_instance_never_wrote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = self.seed_checkpoint_instance(Path(tmpdir), {"version": 1})
            code, output = self.run_cli(
                ["doctor", "--dry-run", "--offline", "--instance", str(instance_dir)]
            )

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

            init_code, init_output = self.run_cli(
                ["data", "init", "--instance", str(instance_dir)]
            )
            doctor_code, doctor_output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )
            manifest_exists = (data_dir / "data-manifest.json").is_file()

        self.assertEqual(init_code, 0, init_output)
        self.assertTrue(manifest_exists)
        self.assertEqual(doctor_code, 0, doctor_output)
        self.assertIn("data manifest: present", doctor_output)

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

            with mock.patch(
                "secretary.data.tempfile.mkstemp", side_effect=PermissionError("denied")
            ):
                code, output = self.run_cli(
                    ["data", "init", "--instance", str(instance_dir)]
                )

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
                code, output = self.run_cli(
                    ["data", "raw-kanboard-dump", "--instance", str(instance_dir)]
                )
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

            with mock.patch(
                "secretary.data.tempfile.mkdtemp", side_effect=PermissionError("denied")
            ):
                code, output = self.run_cli(
                    ["data", "raw-kanboard-dump", "--instance", str(instance_dir)]
                )

        self.assertEqual(code, 1)
        self.assertIn("secretary data raw-kanboard-dump: could not create raw dump", output)
        self.assertNotIn("Traceback", output)

    def test_export_commands_report_bad_data_dir_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = root / "instance"
            data_dir = root / "secretary-data"
            source = root / "panelmem-kb"
            state = root / "state"
            transcript_root = root / "transcripts"
            instance_dir.mkdir()
            data_dir.write_text("not a directory", encoding="utf-8")
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "fact.md").write_text("fact\n", encoding="utf-8")
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
                    [
                        "data",
                        "export-memory",
                        "--instance",
                        str(instance_dir),
                        "--source-dir",
                        str(source),
                    ],
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
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "fact.md").write_text("fact\n", encoding="utf-8")
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

            code, output = self.run_cli(
                [
                    "data",
                    "export-memory",
                    "--instance",
                    str(instance_dir),
                    "--source-dir",
                    str(source),
                ]
            )
            export_exists = (data_dir / "memory" / "export.ndjson").is_file()

        self.assertEqual(code, 0, output)
        self.assertIn("memory facts: 1", output)
        self.assertTrue(export_exists)

    def test_export_memory_command_reports_decode_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            fact = source / "memory" / "secretary" / "bad.md"
            fact.parent.mkdir(parents=True)
            fact.write_bytes(b"\xff\xfe")
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

            code, output = self.run_cli(
                [
                    "data",
                    "export-memory",
                    "--instance",
                    str(instance_dir),
                    "--source-dir",
                    str(source),
                ]
            )

        self.assertEqual(code, 1)
        self.assertIn("secretary data export-memory: could not decode memory fact", output)
        self.assertNotIn("Traceback", output)

    def test_memory_import_command_uses_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "panelmem-kb"
            (source / "memory" / "secretary").mkdir(parents=True)
            (source / "memory" / "secretary" / "fact.md").write_text("fact\n", encoding="utf-8")
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

            first_code, first_output = self.run_cli(
                [
                    "memory",
                    "import",
                    "--instance",
                    str(instance_dir),
                    "--from",
                    str(source),
                ]
            )
            second_code, second_output = self.run_cli(
                [
                    "memory",
                    "import",
                    "--instance",
                    str(instance_dir),
                    "--from",
                    str(source),
                ]
            )
            export_exists = (data_dir / "memory" / "export.ndjson").is_file()
            fact_exists = (
                instance_dir / "state" / "memory" / "facts" / "secretary" / "fact.md"
            ).is_file()

        self.assertEqual(first_code, 0, first_output)
        self.assertIn("memory facts: 1", first_output)
        self.assertIn("changed: yes", first_output)
        self.assertEqual(second_code, 0, second_output)
        self.assertIn("changed: no", second_output)
        self.assertTrue(export_exists)
        self.assertTrue(fact_exists)

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
            log_count = git(
                instance_dir, "rev-list", "--count", "HEAD", "--", "state/memory"
            )

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
                "id: Bad_Id\nrepo: /srv/x\nenabled: true\n"
                "adapter: example-project\ndefault_branch: main\n",
                encoding="utf-8",
            )

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 1)
        self.assertIn("example-project.yaml: id:", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_unreadable_instance_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.yaml"
            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(missing)]
            )

        self.assertEqual(code, 1)
        self.assertIn("config not found", output)
        self.assertNotIn("Traceback", output)

    def test_doctor_yaml_parse_error_does_not_leak_source(self):
        secret = "sk-live-DO-NOT-LEAK-9f3a2b"
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            # Unterminated quoted scalar: PyYAML's raw message would echo the line.
            instance.write_text(f'name: "{secret}\n', encoding="utf-8")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance)]
            )

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

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance)]
            )

        self.assertEqual(code, 1)
        self.assertIn("cannot parse config", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("Traceback", output)

    def test_doctor_reports_non_utf8_instance_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = Path(tmpdir) / "instance.yaml"
            instance.write_bytes(b"\xff")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance)]
            )

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
            code, output = self.run_cli(
                ["bootstrap", "--empty", "--dry-run", "--instance", str(instance)]
            )

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
            code, output = self.run_cli(
                ["restore", str(archive), "--instance", str(instance), "--dry-run"]
            )

        self.assertEqual(code, 2, output)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "restore")
        self.assertIn("archive not found", payload["error"])


if __name__ == "__main__":
    unittest.main()
