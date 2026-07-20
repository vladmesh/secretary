import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.checkpoint import CheckpointWriter
from secretary.data import DataExport
from secretary.tasks import TaskAudit


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    )
    return result.stdout


CARD = {
    "id": 1,
    "reference": "secretary-637",
    "title": "Checkpoint writer",
    "column": "Ready",
    "comments": [],
}


class CheckpointWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.data_dir = root / "secretary-data"
        self.instance_dir = root / "secretary-instance"
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "runs").mkdir(parents=True)
        self.instance_dir.mkdir()
        git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(self.instance_dir, "config", "user.name", "operator")
        git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance_dir, "add", "instance.yaml")
        git(self.instance_dir, "commit", "--quiet", "-m", "config")
        self.seed_board([CARD])
        self.seed_runs([])

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def seed_board(self, cards: list[dict], *, card_count: int | None = None) -> None:
        board = self.data_dir / "board"
        board.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(card, sort_keys=True) + "\n" for card in cards)
        (board / "cards.ndjson").write_text(body, encoding="utf-8")
        (board / "events.ndjson").write_text("", encoding="utf-8")
        (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
        (board / "export.json").write_text(
            json.dumps({"version": 1, "card_count": card_count if card_count is not None else len(cards)}),
            encoding="utf-8",
        )

    def seed_runs(self, records: list[dict], *, run_record_count: int | None = None) -> None:
        runs = self.data_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        (runs / "runs.ndjson").write_text(body, encoding="utf-8")
        (runs / "watermarks.json").write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
        (runs / "claims.json").write_text(json.dumps({"version": 1, "claims": {}}), encoding="utf-8")
        (runs / "export.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_record_count": (
                        run_record_count if run_record_count is not None else len(records)
                    ),
                    "watermark_count": 0,
                    "claim_count": 0,
                }
            ),
            encoding="utf-8",
        )

    def writer(self) -> CheckpointWriter:
        return CheckpointWriter(self.data_dir, self.instance_dir)

    def write(self):
        """Run the writer with the export step stubbed by the seeded snapshot."""

        def board_export(data_dir, **_kwargs):
            lines = (Path(data_dir) / "board" / "cards.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        def runs_export(data_dir, **_kwargs):
            lines = (Path(data_dir) / "runs" / "runs.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        with mock.patch("secretary.checkpoint.export_board", side_effect=board_export):
            with mock.patch("secretary.checkpoint.export_runs", side_effect=runs_export):
                return self.writer().write()

    def head_files(self) -> list[str]:
        return git(self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD").split()

    def test_board_and_runs_land_in_state_as_one_commit(self):
        result = self.write()

        self.assertEqual(result.status, "committed")
        self.assertEqual(result.board_cards, 1)
        files = self.head_files()
        self.assertIn("state/board/cards.ndjson", files)
        self.assertIn("state/board/events.ndjson", files)
        self.assertIn("state/board/export.json", files)
        self.assertIn("state/runs/runs.ndjson", files)
        self.assertIn("state/runs/claims.json", files)
        self.assertIn("state/runs/watermarks.json", files)
        self.assertIn("state/runs/export.json", files)
        self.assertEqual(
            git(self.instance_dir, "rev-parse", "HEAD").strip(), result.commit
        )

    def test_derived_board_dump_is_not_part_of_the_checkpoint(self):
        self.write()

        files = self.head_files()
        self.assertNotIn("state/board/cards.json", files)
        self.assertIn("state/board/.gitignore", files)
        ignore = (self.instance_dir / "state" / "board" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("cards.json", ignore.splitlines())

    def test_unchanged_state_skips_the_commit(self):
        first = self.write()
        second = self.write()

        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.commit, "")
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), first.commit)

    def test_changed_board_commits_again(self):
        first = self.write()
        self.seed_board([CARD, {**CARD, "id": 2, "reference": "secretary-638"}])
        second = self.write()

        self.assertEqual(second.status, "committed")
        self.assertNotEqual(second.commit, first.commit)

    def test_pending_audit_blocks_the_commit(self):
        TaskAudit(self.data_dir).stage("request-1", {"event_id": "e1"})

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("pending", result.reason)
        self.assertNotIn("state/board/cards.ndjson", self.head_files())

    def test_count_mismatch_blocks_the_commit(self):
        self.seed_board([CARD], card_count=4)

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("board export count mismatch", result.reason)
        self.assertNotIn("state/board/cards.ndjson", self.head_files())

    def test_run_record_mismatch_blocks_the_commit(self):
        self.seed_runs([{"line": 1}], run_record_count=9)

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("runs export count mismatch", result.reason)

    def test_secret_in_a_card_blocks_the_commit(self):
        self.seed_board([{**CARD, "description": "token ghp_" + "a" * 40}])

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("secret detected in state/board/cards.ndjson", result.reason)
        self.assertNotIn("state/board/cards.ndjson", self.head_files())

    def test_blocked_snapshot_leaves_the_previous_checkpoint_intact(self):
        first = self.write()
        self.seed_board([{**CARD, "description": "token ghp_" + "a" * 40}])
        blocked = self.write()

        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), first.commit)
        published = (self.instance_dir / "state" / "board" / "cards.ndjson").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("ghp_", published)

    def test_export_failure_blocks_without_touching_state(self):
        with mock.patch(
            "secretary.checkpoint.export_board", side_effect=RuntimeError("pipeline list failed")
        ):
            result = self.writer().write()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "pipeline list failed")
        self.assertFalse((self.instance_dir / "state").exists())

    def test_ignored_state_blocks_instead_of_reporting_unchanged(self):
        (self.instance_dir / ".gitignore").write_text("state/\n", encoding="utf-8")

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("not tracked by the instance repo", result.reason)

    def test_partially_ignored_state_blocks_instead_of_committing_half(self):
        (self.instance_dir / ".gitignore").write_text("state/runs/\n", encoding="utf-8")

        result = self.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("state/runs/runs.ndjson", result.reason)
        self.assertNotIn("state/board/cards.ndjson", self.head_files())

    def test_operator_config_changes_stay_out_of_the_checkpoint_commit(self):
        (self.instance_dir / "instance.yaml").write_text("version: 2\n", encoding="utf-8")
        git(self.instance_dir, "add", "instance.yaml")

        self.write()

        committed = git(
            self.instance_dir, "show", "--name-only", "--format=", "HEAD"
        ).split()
        self.assertNotIn("instance.yaml", committed)
        self.assertIn("state/board/cards.ndjson", committed)


if __name__ == "__main__":
    unittest.main()
