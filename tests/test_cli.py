from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from secretary.cli import main


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"


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

    def test_doctor_requires_dry_run(self):
        code, output = self.run_cli(
            ["doctor", "--instance", str(EXAMPLE_INSTANCE)]
        )

        self.assertEqual(code, 2)
        self.assertIn("requires --dry-run", output)

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

    def test_doctor_warns_when_offsite_marker_is_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n"
                "  backup_pull_max_age_days: 7\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 0, output)
        self.assertIn("offsite warnings:", output)
        self.assertIn("last_fetch missing", output)
        self.assertIn("status: ok", output)

    def test_doctor_reports_stale_offsite_marker_as_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n"
                "  backup_pull_max_age_days: 1\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            marker = data_dir / "backups" / "last_fetch"
            marker.write_text("2000-01-01T00:00:00Z\n", encoding="utf-8")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 1, output)
        self.assertIn("offsite findings:", output)
        self.assertIn("stale", output)
        self.assertIn("status: findings", output)

    def test_doctor_reports_future_offsite_marker_as_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n"
                "  backup_pull_max_age_days: 1\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])
            marker = data_dir / "backups" / "last_fetch"
            marker.write_text("2999-01-01T00:00:00Z\n", encoding="utf-8")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 1, output)
        self.assertIn("offsite findings:", output)
        self.assertIn("future", output)
        self.assertIn("status: findings", output)

    def test_doctor_reports_unavailable_offsite_marker_as_finding(self):
        class BrokenMarker:
            def is_file(self) -> bool:
                raise PermissionError("denied")

        with tempfile.TemporaryDirectory() as tmpdir:
            instance_dir = Path(tmpdir) / "instance"
            data_dir = Path(tmpdir) / "secretary-data"
            instance_dir.mkdir()
            (instance_dir / "instance.yaml").write_text(
                "version: 1\n"
                "name: example\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n"
                "  instance_remote: git@example.invalid:x/y.git\n"
                "  backup_pull_max_age_days: 1\n",
                encoding="utf-8",
            )
            self.run_cli(["data", "init", "--instance", str(instance_dir)])

            with mock.patch(
                "secretary.offsite.last_fetch_path", return_value=BrokenMarker()
            ):
                code, output = self.run_cli(
                    ["doctor", "--dry-run", "--instance", str(instance_dir)]
                )

        self.assertEqual(code, 1, output)
        self.assertIn("offsite findings:", output)
        self.assertIn("unavailable", output)
        self.assertNotIn("Traceback", output)
        self.assertIn("status: findings", output)

    def test_doctor_warns_when_backups_are_stale(self):
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
            backups = data_dir / "backups"
            core = backups / "secretary-backup-core-20260709T000000Z.tar.age"
            full = backups / "secretary-backup-full-20260708T000000Z.tar.age"
            core.write_bytes(b"core")
            full.write_bytes(b"full")
            stale = datetime(2026, 7, 8, tzinfo=UTC).timestamp()
            core.touch()
            full.touch()
            import os

            os.utime(core, (stale, stale))
            os.utime(full, (stale, stale))

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 0, output)
        self.assertIn("backup warnings:", output)
        self.assertIn("core archive is stale", output)
        self.assertIn("full archive is stale", output)

    def test_doctor_warns_when_backups_dir_is_missing(self):
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
            shutil.rmtree(data_dir / "backups")

            code, output = self.run_cli(
                ["doctor", "--dry-run", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 0, output)
        self.assertIn("backup warnings:", output)
        self.assertIn("backup directory is unavailable", output)

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
                    mock.Mock(archive=Path("/tmp/core.tar.age"), manifest={"version": 1, "backup_kind": "core"}),
                    mock.Mock(archive=Path("/tmp/full.tar.age"), manifest={"version": 1, "backup_kind": "full"}),
                ]
                code, output = self.run_cli(
                    [
                        "backup",
                        "create",
                        "--instance",
                        str(instance_dir),
                        "--age-recipient",
                        "age1example",
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
        self.assertIn("backup warnings:", doctor_output)
        self.assertIn("backup core archive is missing", doctor_output)
        self.assertIn("backup full archive is missing", doctor_output)

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
        self.assertIn("backup core archive is missing", doctor_output)

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

    @mock.patch("secretary.data.subprocess.run")
    def test_raw_kanboard_dump_command_uses_data_dir(self, run):
        def fake_run(command, **_kwargs):
            destination = Path(command[-1])
            destination.mkdir(parents=True)
            (destination / "db.sqlite").write_bytes(b"sqlite")

        run.side_effect = fake_run
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

            code, output = self.run_cli(
                ["data", "raw-kanboard-dump", "--instance", str(instance_dir)]
            )

        self.assertEqual(code, 0, output)
        self.assertIn("kanboard raw dump:", output)
        self.assertEqual(run.call_args.args[0][0:2], ["docker", "cp"])

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
                    "secretary data export: cannot prepare board data dir",
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

    def test_target_command_stubs_are_explicit(self):
        for command in (
            ["reconcile"],
            ["backup"],
            ["restore"],
            ["project", "add", "example"],
            ["task", "list"],
            ["memory", "commit"],
        ):
            with self.subTest(command=command):
                code, output = self.run_cli(command)
                self.assertEqual(code, 1)
                self.assertIn("not implemented", output)


if __name__ == "__main__":
    unittest.main()
