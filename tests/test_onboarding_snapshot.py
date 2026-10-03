"""Onboarding reaches the snapshot with no commit step (issue:3024304a).

`project add`, `provision` and `gate` write `projects/<id>.yaml` and `adapters/<id>.yaml` into a live
root that has no Git; the next exporter window carries them, and a snapshot recovery from that cut
restores them, so a Product naming the project validates. The gate's setup, smoke and validation
commands are stood in for at `gate._command`; the clean worktree is real. The exporter and the
recovery are the ones `tests/test_snapshot_recover.py` runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests.fakes.snapshot_remote import exporter_remote, git, recover_snapshot
from tests.support.git import make_repo
from ummanu.checkpoint import SNAPSHOT_MANIFEST, SNAPSHOT_REF
from ummanu.cli import main
from ummanu.config import validate_instance
from ummanu.onboarding import OnboardingStorage
from ummanu.product_issues import registered_projects

PROJECT = "sample-project"
REGISTRATION = (f"projects/{PROJECT}.yaml", f"adapters/{PROJECT}.yaml")


def _passed(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


class OnboardingSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="onboarding-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = exporter_remote(self.root, cut=False)
        self.live = self.fixture.source
        self.repo = make_repo(self.root)
        self.outputs: list[str] = []

    def cli(self, *argv: str) -> dict:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([*argv, "--instance", str(self.live)])
        self.outputs.append(output.getvalue())
        self.assertEqual(code, 0, output.getvalue())
        return yaml.safe_load(output.getvalue())

    def onboard(self) -> None:
        self.cli("project", "add", str(self.repo))
        started = self.cli("project", "provision-start", PROJECT)
        task = started["task"]
        # The provision agent's answer, written next to its task.
        result = Path(started["task_path"]).with_name("result.yaml")
        result.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "run_id": task["run_id"],
                    "identity": {"id": task["identity"]["id"], "adapter": task["identity"]["adapter"]},
                    "input_revision": dict(task["input_revision"]),
                    "status": "drafted",
                    "adapter": {
                        "setup": {"commands": ["python3 -m pip install -e ."]},
                        "smoke": {"command": "python3 -m unittest discover -s tests"},
                        "validation": {"ci": "github"},
                        "artifact_policy": {"write_project_files": False},
                    },
                    "project_local_adapter": {"proposed": False, "requires_opt_in": True},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.cli("project", "provision-apply", PROJECT)["status"], "drafted")
        with mock.patch("ummanu.gate._command", side_effect=_passed):
            self.assertEqual(self.cli("project", "gate", PROJECT)["status"], "passed")

    def tree_blob(self, path: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.fixture.remote), "cat-file", "blob", f"{SNAPSHOT_REF}:{path}"],
            capture_output=True,
            check=True,
        ).stdout

    def test_onboarding_on_a_live_root_without_git_reaches_the_cut_and_the_recovered_root(self) -> None:
        self.assertFalse((self.live / ".git").exists())

        self.onboard()

        self.assertFalse((self.live / ".git").exists())
        binding = yaml.safe_load((self.live / REGISTRATION[0]).read_text(encoding="utf-8"))
        self.assertIs(binding["enabled"], True)
        # Nothing tells the operator to commit: the registration is the exporter's to carry.
        for output in self.outputs:
            self.assertNotIn("commit", output.lower())

        # One exporter window: the cut holds both files, byte for byte, under their manifest digests.
        tip = self.fixture.cut()
        manifest = json.loads(self.tree_blob(SNAPSHOT_MANIFEST))
        for path in REGISTRATION:
            with self.subTest(path=path):
                committed = self.tree_blob(path)
                self.assertEqual(committed, (self.live / path).read_bytes())
                self.assertEqual(manifest["files"][path], hashlib.sha256(committed).hexdigest())

        # The host is lost, onboarding's drafts and runs with it; the cut alone brings the project back.
        shutil.rmtree(self.fixture.data_dir)
        result = recover_snapshot(self.fixture, board=mock.Mock(return_value=len(self.fixture.cards)))

        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(git(self.fixture.data_dir / "backup" / "instance.git", "rev-parse", SNAPSHOT_REF), tip)
        target = self.fixture.target
        for path in REGISTRATION:
            with self.subTest(path=path):
                self.assertEqual((target / path).read_bytes(), (self.live / path).read_bytes())
        report = validate_instance(target)
        self.assertTrue(report.ok, [str(error) for error in report.errors])
        self.assertIn(PROJECT, {binding.get("id") for binding in report.bindings})
        # The registry a Product's projects are checked against names it.
        self.assertIn(PROJECT, registered_projects(target))
        # Onboarding's generated state is not configuration and was not in the cut.
        self.assertFalse(OnboardingStorage(self.fixture.data_dir).draft(PROJECT).exists())


if __name__ == "__main__":
    unittest.main()
