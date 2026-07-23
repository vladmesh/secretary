from __future__ import annotations

import contextlib
import getpass
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.cli import main
from secretary.installation import InstallError, _clone_or_reuse, _ensure_installation_user, materialize_checkpoint


CARD = {
    "reference": "secretary-1",
    "title": "Recovered",
    "description": "from checkpoint",
    "column": "Ready",
    "swimlane": "secretary",
    "position": 1,
    "fields": {"project": "secretary", "task_type": "code"},
    "metadata": {},
    "comments": [],
}


def _checkpoint(instance: Path, data_dir: Path) -> None:
    board = instance / "state" / "board"
    runs = instance / "state" / "runs"
    facts = instance / "state" / "memory" / "facts"
    board.mkdir(parents=True)
    runs.mkdir(parents=True)
    facts.mkdir(parents=True)
    (instance / ".gitignore").write_text("runtime.env\n", encoding="utf-8")
    (instance / "instance.yaml").write_text(
        "version: 1\n"
        "name: recovered\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: placeholder\n"
        "host:\n  unit_prefix: secretary-\n",
        encoding="utf-8",
    )
    (board / "cards.ndjson").write_text(json.dumps(CARD) + "\n", encoding="utf-8")
    (board / "events.ndjson").write_text("", encoding="utf-8")
    (board / "export.json").write_text(json.dumps({"card_count": 1}), encoding="utf-8")
    (runs / "runs.ndjson").write_text("", encoding="utf-8")
    (runs / "claims.json").write_text('{"claims": {}}', encoding="utf-8")
    (runs / "watermarks.json").write_text('{"files": []}', encoding="utf-8")
    (runs / "export.json").write_text(
        json.dumps({"run_record_count": 0, "claim_count": 0, "watermark_count": 0}),
        encoding="utf-8",
    )
    (facts / "fact.md").write_text("# recovered fact\n", encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


class InstallationTests(unittest.TestCase):
    def test_existing_runtime_env_is_not_a_bootstrap_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git").mkdir(parents=True)
            (target / "runtime.env").write_text("KANBOARD_API_TOKEN=existing\n", encoding="utf-8")

            with mock.patch("secretary.installation._run", return_value="remote"):
                with self.assertRaisesRegex(InstallError, "choose --recover"):
                    _clone_or_reuse("remote", target, recovery=False, dry_run=True)

            (target / ".secretary-bootstrap").write_text("bootstrap\n", encoding="utf-8")
            with mock.patch("secretary.installation._run", side_effect=("remote", "")):
                self.assertEqual(
                    _clone_or_reuse("remote", target, recovery=False, dry_run=True),
                    "reused checkpoint checkout",
                )

    def test_existing_installation_user_requires_recover_or_adopt_choice(self):
        with self.assertRaisesRegex(InstallError, "choose --recover.*adopt"):
            _ensure_installation_user(getpass.getuser(), recovery=False, dry_run=False)

    def test_checkpoint_materialization_builds_local_json_and_never_copies_derived_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data)
            stale = instance / "state" / "memory" / "index.sqlite"
            stale.write_bytes(b"must not move")

            self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

            cards = json.loads((data / "board" / "cards.json").read_text(encoding="utf-8"))
            self.assertEqual(cards["cards"], [CARD])
            self.assertFalse((data / "memory" / "index.sqlite").exists())
            self.assertFalse((data / "worktrees").exists())

    def test_non_secretary_data_target_is_refused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            data.mkdir()
            marker = data / "owned-by-operator"
            marker.write_text("keep", encoding="utf-8")
            _checkpoint(instance, data)

            with self.assertRaisesRegex(InstallError, "choose adopt"):
                materialize_checkpoint(instance, data)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_clean_target_clones_then_resumes_recovery_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            remote = root / "instance.git"
            target = root / "target"
            data = root / "data"
            source.mkdir()
            _checkpoint(source, data)
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True,
                           capture_output=True, text=True)
            # The config identity is the clone URL the recovery command verifies.
            text = (source / "instance.yaml").read_text(encoding="utf-8")
            (source / "instance.yaml").write_text(text.replace("placeholder", str(remote)), encoding="utf-8")
            _git(source, "add", "instance.yaml")
            _git(source, "commit", "-m", "remote identity")
            _git(source, "push", str(remote), "HEAD:master")

            base = [
                "--instance-remote", str(remote), "--instance-dir", str(target),
                "--installation-user", getpass.getuser(),
            ]
            with mock.patch("secretary.installation._ensure_installation_user"):
                first_code, first_output = self._cli(["install", *base])
            self.assertEqual(first_code, 1)
            self.assertTrue((target / ".git").exists())
            self.assertIn("runtime credentials are required", first_output)

            runtime = target / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://127.0.0.1/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=test\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            host = SimpleNamespace(steps=[SimpleNamespace(status="changed")])
            patches = (
                mock.patch("secretary.installation.check_prerequisites"),
                mock.patch("secretary.installation.import_normalized_board", return_value=1),
                mock.patch("secretary.installation.rebuild_memory_index", return_value=1),
                mock.patch("secretary.installation.materialize_host", return_value=host),
                mock.patch("secretary.installation.restore_findings", return_value=[]),
                mock.patch("secretary.bootstrap.ensure_pipeline_board"),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                second_code, second_output = self._cli(["recover", *base])
                third_code, third_output = self._cli(["recover", *base])

            self.assertEqual(second_code, 0, second_output)
            self.assertEqual(third_code, 0, third_output)
            self.assertIn("status: ok", second_output)
            self.assertEqual(
                json.loads((data / "board" / "cards.json").read_text(encoding="utf-8"))["cards"],
                [CARD],
            )
            self.assertFalse((target / "state" / "memory" / "index.sqlite").exists())

    def test_recover_dry_run_validates_checkpoint_without_materializing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            target = root / "target"
            data = root / "data"
            source.mkdir()
            _checkpoint(source, data)
            materialize_checkpoint(source, data)
            before = {
                path.relative_to(data): path.read_bytes()
                for path in data.rglob("*")
                if path.is_file()
            }
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            runtime = target / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://127.0.0.1/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=test\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)

            with (
                mock.patch("secretary.installation.check_prerequisites"),
                mock.patch("secretary.installation.import_normalized_board") as board,
                mock.patch("secretary.installation.rebuild_memory_index") as memory,
                mock.patch("secretary.installation.materialize_host") as host,
                mock.patch("secretary.installation.mark_reconcile_applied") as reconcile,
            ):
                code, output = self._cli([
                    "recover",
                    "--instance-remote", str(source),
                    "--instance-dir", str(target),
                    "--installation-user", getpass.getuser(),
                    "--dry-run",
                ])

            self.assertEqual(code, 0, output)
            self.assertIn("would-change checkpoint", output)
            self.assertIn("preview made no recovery changes", output)
            after = {
                path.relative_to(data): path.read_bytes()
                for path in data.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            board.assert_not_called()
            memory.assert_not_called()
            host.assert_not_called()
            reconcile.assert_not_called()

    def test_existing_checkout_requires_explicit_recovery_choice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            _checkpoint(source, root / "data")
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(["git", "clone", str(source), str(target)], check=True,
                           capture_output=True, text=True)

            with mock.patch("secretary.installation._ensure_installation_user"):
                code, output = self._cli([
                    "install", "--instance-remote", str(source), "--instance-dir", str(target),
                    "--installation-user", getpass.getuser(),
                ])
            self.assertEqual(code, 1)
            self.assertIn("choose --recover", output)
            self.assertIn("adopt", output)

    @staticmethod
    def _cli(argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()


if __name__ == "__main__":
    unittest.main()
