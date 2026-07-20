import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.checkpoint import (
    PUSH_INTERVAL_SECONDS,
    CheckpointPusher,
    CheckpointWriter,
    checkpoint_snapshot,
    render_checkpoint_lines,
)
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

    def test_optional_entry_that_vanished_from_the_source_leaves_the_checkpoint(self):
        self.write()
        self.assertIn("state/board/events.ndjson", self.head_files())

        (self.data_dir / "board" / "events.ndjson").unlink()
        result = self.write()

        self.assertEqual(result.status, "committed")
        self.assertNotIn("state/board/events.ndjson", self.head_files())
        self.assertIn("state/board/cards.ndjson", self.head_files())

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


class FakeClock:
    """A clock the push window can be walked forward by hand."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CheckpointPusherTests(unittest.TestCase):
    """Contract: docs/RECOVERY.md, "Каденция и RPO", "Failure и divergence"."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.remote))
        self.instance_dir = self.root / "secretary-instance"
        self.instance_dir.mkdir()
        git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(self.instance_dir, "config", "user.name", "operator")
        git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        git(self.instance_dir, "remote", "add", "origin", str(self.remote))
        self.commit("config")
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def commit(self, message: str, *, repo: Path | None = None) -> str:
        repo = repo or self.instance_dir
        (repo / "instance.yaml").write_text(f"version: {message}\n", encoding="utf-8")
        git(repo, "add", "instance.yaml")
        git(repo, "commit", "--quiet", "-m", message)
        return git(repo, "rev-parse", "HEAD").strip()

    def pusher(self, **kwargs) -> CheckpointPusher:
        kwargs.setdefault("clock", self.clock)
        return CheckpointPusher(self.instance_dir, **kwargs)

    def remote_head(self) -> str:
        return git(self.remote, "rev-parse", "refs/heads/main").strip()

    def push_from_elsewhere(self) -> str:
        """Land a commit on the remote that the instance repo has never seen."""
        other = self.root / "other"
        git(self.root, "clone", "--quiet", str(self.remote), str(other))
        git(other, "config", "user.name", "operator")
        git(other, "config", "user.email", "operator@example.invalid")
        (other / "operator.txt").write_text("resolved by hand\n", encoding="utf-8")
        git(other, "add", "operator.txt")
        git(other, "commit", "--quiet", "-m", "from-another-host")
        git(other, "push", "--quiet", "origin", "main")
        return git(other, "rev-parse", "HEAD").strip()

    def test_first_push_lands_the_checkpoint_on_the_remote(self):
        state = self.pusher().push()

        self.assertEqual(state["status"], "pushed")
        self.assertEqual(self.remote_head(), git(self.instance_dir, "rev-parse", "HEAD").strip())
        self.assertEqual(state["last_push_commit"], self.remote_head())
        self.assertFalse(state["remote_diverged"])

    def test_push_waits_for_its_window_and_runs_once_the_window_is_due(self):
        state = self.pusher().push()
        head = self.commit("second")

        self.clock.advance(29 * 60)
        state = self.pusher().push(state)
        self.assertNotEqual(self.remote_head(), head)

        self.clock.advance(2 * 60)
        state = self.pusher().push(state)
        self.assertEqual(state["status"], "pushed")
        self.assertEqual(self.remote_head(), head)

    def test_a_checkpoint_already_on_the_remote_needs_no_push(self):
        state = self.pusher().push()
        self.clock.advance(PUSH_INTERVAL_SECONDS)

        state = self.pusher().push(state)

        self.assertEqual(state["status"], "unchanged")
        self.assertEqual(state["failures"], 0)

    def test_push_failure_is_recorded_and_the_next_window_retries(self):
        git(self.instance_dir, "remote", "set-url", "origin", str(self.root / "missing.git"))

        state = self.pusher().push()

        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["reason"])
        self.assertEqual(state["failures"], 1)
        self.assertFalse(state["remote_diverged"])

        git(self.instance_dir, "remote", "set-url", "origin", str(self.remote))
        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)

        self.assertEqual(state["status"], "pushed")
        self.assertEqual(state["failures"], 0)

    def test_failed_push_leaves_a_growing_lag_that_status_can_see(self):
        git(self.instance_dir, "remote", "set-url", "origin", str(self.root / "missing.git"))
        state = self.pusher().push()
        self.commit("second")

        snapshot = checkpoint_snapshot(self.instance_dir, push_state=state)

        self.assertEqual(snapshot["push_status"], "failed")
        self.assertEqual(snapshot["lag_commits"], 2)
        self.assertEqual(snapshot["last_push_at"], "")

    def test_remote_with_commits_we_lack_stops_the_push_without_forcing(self):
        state = self.pusher().push()
        theirs = self.push_from_elsewhere()
        self.commit("ours")
        self.clock.advance(PUSH_INTERVAL_SECONDS)

        state = self.pusher().push(state)

        self.assertEqual(state["status"], "diverged")
        self.assertTrue(state["remote_diverged"])
        # The remote still carries their commit: nothing overwrote it.
        self.assertEqual(self.remote_head(), theirs)

    def test_divergence_keeps_the_push_stopped_until_the_operator_resolves_it(self):
        state = self.pusher().push()
        theirs = self.push_from_elsewhere()
        self.commit("ours")
        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)

        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)
        self.assertEqual(state["status"], "diverged")
        self.assertEqual(self.remote_head(), theirs)

        # The operator merges the remote work by hand; the next window resumes.
        git(self.instance_dir, "fetch", "--quiet", "origin", "main")
        git(self.instance_dir, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
        self.clock.advance(PUSH_INTERVAL_SECONDS)
        state = self.pusher().push(state)

        self.assertEqual(state["status"], "pushed")
        self.assertFalse(state["remote_diverged"])
        self.assertEqual(self.remote_head(), git(self.instance_dir, "rev-parse", "HEAD").strip())

    def test_a_remote_that_moves_under_the_push_reads_as_divergence(self):
        state = self.pusher().push()
        theirs = self.push_from_elsewhere()
        self.commit("ours")
        self.clock.advance(PUSH_INTERVAL_SECONDS)

        pusher = self.pusher()
        # The probe saw a remote that has already moved on by the time git pushes;
        # git's own rejection has to land as divergence, not as a generic failure.
        with mock.patch.object(pusher, "_remote_head", return_value=""):
            state = pusher.push(state)

        self.assertEqual(state["status"], "diverged")
        self.assertTrue(state["remote_diverged"])
        self.assertEqual(self.remote_head(), theirs)

    def test_an_instance_without_a_remote_is_skipped_rather_than_failed(self):
        git(self.instance_dir, "remote", "remove", "origin")

        state = self.pusher().push()

        self.assertEqual(state["status"], "skipped")
        self.assertIn("no remote", state["reason"])
        self.assertEqual(state["failures"], 0)


class CheckpointSnapshotTests(unittest.TestCase):
    """Contract: docs/RECOVERY.md, "Observability"."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.instance_dir = Path(self.tmpdir.name) / "secretary-instance"
        self.instance_dir.mkdir(parents=True)
        git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(self.instance_dir, "config", "user.name", "operator")
        git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance_dir, "add", "instance.yaml")
        git(self.instance_dir, "commit", "--quiet", "-m", "config")
        self.head = git(self.instance_dir, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_a_pushed_checkpoint_reports_no_lag(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            push_state={"status": "pushed", "last_push_at": "2026-07-20T10:00:00Z", "last_push_commit": self.head},
        )

        self.assertEqual(snapshot["last_commit"], self.head)
        self.assertEqual(snapshot["lag_commits"], 0)
        self.assertEqual(snapshot["lag_minutes"], 0)
        self.assertEqual(snapshot["last_push_at"], "2026-07-20T10:00:00Z")

    def test_lag_counts_the_commits_the_remote_does_not_have(self):
        pushed = self.head
        for index in range(2):
            (self.instance_dir / "state.txt").write_text(f"{index}\n", encoding="utf-8")
            git(self.instance_dir, "add", "state.txt")
            git(self.instance_dir, "commit", "--quiet", "-m", f"checkpoint {index}")

        snapshot = checkpoint_snapshot(self.instance_dir, push_state={"last_push_commit": pushed})

        self.assertEqual(snapshot["lag_commits"], 2)
        self.assertIsInstance(snapshot["lag_minutes"], int)

    def test_the_blocked_gate_reason_and_divergence_alarm_are_visible(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            write_state={"status": "blocked", "reason": "secret detected in state/board/cards.ndjson"},
            push_state={"status": "diverged", "reason": "remote origin/main is at deadbeef", "remote_diverged": True},
        )

        self.assertEqual(snapshot["blocked_reason"], "secret detected in state/board/cards.ndjson")
        self.assertTrue(snapshot["remote_diverged"])
        lines = "\n".join(render_checkpoint_lines(snapshot))
        self.assertIn("alarm: remote diverged", lines)
        self.assertIn("blocked: secret detected", lines)

    def test_a_committed_gate_reports_no_blocking_reason(self):
        snapshot = checkpoint_snapshot(
            self.instance_dir,
            write_state={"status": "committed", "reason": ""},
        )

        self.assertEqual(snapshot["blocked_reason"], "")
        self.assertEqual(snapshot["push_status"], "pending")


if __name__ == "__main__":
    unittest.main()
