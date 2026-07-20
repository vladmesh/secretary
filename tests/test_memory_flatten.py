"""Memory lives flat in the private repo: migration, writer target, coexistence.

Contract: docs/RECOVERY.md, "Layout" and "Writer".
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from secretary import state_repo
from secretary.checkpoint import CheckpointWriter
from secretary.data import DataExport
from secretary.memory_journal import migrate_memory_journal, verify_memory_journal
from secretary.memory_write import commit_memory_proposal, propose_memory_fact
from secretary.tasks import TaskAudit
from unittest import mock


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    )
    return result.stdout


def init_instance_repo(instance_dir: Path) -> Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    git(instance_dir, "init", "--quiet", "--initial-branch", "main")
    git(instance_dir, "config", "user.name", "operator")
    git(instance_dir, "config", "user.email", "operator@example.invalid")
    (instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    git(instance_dir, "add", "instance.yaml")
    git(instance_dir, "commit", "--quiet", "-m", "config")
    return instance_dir


def seed_nested_journal(data_dir: Path, facts: dict[str, str]) -> Path:
    """Build a pre-flatten `memory/facts` journal, nested `.git` and all."""
    legacy = data_dir / "memory" / "facts"
    legacy.mkdir(parents=True)
    git(legacy, "init", "--quiet", "--initial-branch", "main")
    git(legacy, "config", "user.name", "memory")
    git(legacy, "config", "user.email", "memory@example.invalid")
    for relative, text in facts.items():
        target = legacy / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(legacy, "add", "-A", ".")
    git(legacy, "commit", "--quiet", "-m", "memory import: seed\n\nOp: import\n")
    return legacy


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.data_dir = root / "secretary-data"
        self.instance_dir = init_instance_repo(root / "secretary-instance")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def facts_dir(self) -> Path:
        return state_repo.memory_facts_dir(self.instance_dir)

    def test_flatten_carries_existing_facts_and_drops_the_nested_git(self):
        legacy = seed_nested_journal(
            self.data_dir,
            {"global/one.md": "first fact\n", "secretary/two.md": "second fact\n"},
        )

        result = migrate_memory_journal(self.data_dir, self.instance_dir)

        self.assertEqual(result.migrated, 2)
        self.assertFalse(legacy.exists())
        facts = self.facts_dir()
        self.assertEqual((facts / "global" / "one.md").read_text(encoding="utf-8"), "first fact\n")
        self.assertEqual(
            (facts / "secretary" / "two.md").read_text(encoding="utf-8"), "second fact\n"
        )
        tracked = git(self.instance_dir, "ls-files", "--", "state/memory").split()
        self.assertIn("state/memory/facts/global/one.md", tracked)
        self.assertIn("state/memory/facts/secretary/two.md", tracked)
        self.assertEqual(git(self.instance_dir, "status", "--porcelain", "--", "state/memory"), "")

    def test_first_write_after_flatten_keeps_the_migrated_facts(self):
        """The 637 regression: the flatten must not be what drops the facts."""
        seed_nested_journal(self.data_dir, {"global/one.md": "first fact\n"})
        fact_file = Path(self.tmpdir.name) / "new.md"
        fact_file.write_text("a brand new fact\n", encoding="utf-8")

        proposal = propose_memory_fact(
            self.data_dir,
            actor="curator:claude/session",
            scope="global",
            slug="two",
            fact_file=fact_file,
            source="curator:claude/session",
        )
        commit_memory_proposal(
            self.data_dir,
            self.instance_dir,
            actor="curator:claude/session",
            propose_id=proposal.propose_id,
        )

        facts = self.facts_dir()
        self.assertEqual((facts / "global" / "one.md").read_text(encoding="utf-8"), "first fact\n")
        self.assertIn("a brand new fact", (facts / "global" / "two.md").read_text(encoding="utf-8"))
        tracked = git(self.instance_dir, "ls-files", "--", "state/memory").split()
        self.assertIn("state/memory/facts/global/one.md", tracked)
        self.assertIn("state/memory/facts/global/two.md", tracked)

    def test_flatten_is_idempotent_after_a_crash_between_commit_and_cleanup(self):
        """A carried-but-not-deleted legacy tree is finished, not refused."""
        legacy = seed_nested_journal(self.data_dir, {"global/one.md": "first fact\n"})
        with mock.patch(
            "secretary.memory_journal._remove_legacy_journal", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                migrate_memory_journal(self.data_dir, self.instance_dir)
        self.assertTrue(legacy.is_dir())

        result = migrate_memory_journal(self.data_dir, self.instance_dir)

        self.assertEqual(result.migrated, 0)
        self.assertFalse(legacy.exists())
        self.assertEqual(
            (self.facts_dir() / "global" / "one.md").read_text(encoding="utf-8"), "first fact\n"
        )

    def test_flatten_refuses_when_the_two_trees_disagree(self):
        """Divergent copies are an operator call; neither side is silently lost."""
        seed_nested_journal(self.data_dir, {"global/one.md": "legacy body\n"})
        target = self.facts_dir() / "global" / "one.md"
        target.parent.mkdir(parents=True)
        target.write_text("repo body\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "memory flatten refused"):
            migrate_memory_journal(self.data_dir, self.instance_dir)

        self.assertTrue((self.data_dir / "memory" / "facts" / "global" / "one.md").is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "repo body\n")

    def test_verify_flags_a_nested_journal_that_is_still_present(self):
        seed_nested_journal(self.data_dir, {"global/one.md": "first fact\n"})

        report = verify_memory_journal(self.data_dir, self.instance_dir)

        self.assertFalse(report.ok)
        self.assertTrue(
            any("nested memory journal is still present" in finding for finding in report.findings),
            report.findings,
        )


class MemorySecretGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.data_dir = root / "secretary-data"
        self.instance_dir = init_instance_repo(root / "secretary-instance")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_a_fact_carrying_a_secret_never_reaches_the_repo(self):
        fact_file = Path(self.tmpdir.name) / "leaky.md"
        fact_file.write_text(
            "token ghp_0123456789abcdefghijklmnopqrstuvwxyzAB\n", encoding="utf-8"
        )
        proposal = propose_memory_fact(
            self.data_dir,
            actor="curator:claude/session",
            scope="global",
            slug="leaky",
            fact_file=fact_file,
            source="curator:claude/session",
        )

        with self.assertRaisesRegex(Exception, "secret detected in memory fact"):
            commit_memory_proposal(
                self.data_dir,
                self.instance_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )

        facts = state_repo.memory_facts_dir(self.instance_dir)
        self.assertFalse((facts / "global" / "leaky.md").exists())
        self.assertEqual(git(self.instance_dir, "status", "--porcelain", "--", "state/memory"), "")


class TwoWriterTests(unittest.TestCase):
    """Tick writer and memory writer share the repo but never the pathspec."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.data_dir = root / "secretary-data"
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "runs").mkdir(parents=True)
        self.instance_dir = init_instance_repo(root / "secretary-instance")
        self.seed_board_and_runs()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def seed_board_and_runs(self) -> None:
        board = self.data_dir / "board"
        (board / "cards.ndjson").write_text('{"id": 1}\n', encoding="utf-8")
        (board / "export.json").write_text('{"card_count": 1}\n', encoding="utf-8")
        runs = self.data_dir / "runs"
        (runs / "runs.ndjson").write_text("", encoding="utf-8")
        (runs / "export.json").write_text(
            '{"run_record_count": 0, "watermark_count": 0, "claim_count": 0}\n', encoding="utf-8"
        )
        (runs / "watermarks.json").write_text('{"files": []}\n', encoding="utf-8")
        (runs / "claims.json").write_text('{"claims": {}}\n', encoding="utf-8")

    def writer(self) -> CheckpointWriter:
        writer = CheckpointWriter(self.data_dir, self.instance_dir)
        writer._regenerate = lambda: (1, 0)
        return writer

    def write_fact(self, slug: str) -> None:
        fact_file = Path(self.tmpdir.name) / f"{slug}.md"
        fact_file.write_text(f"fact {slug}\n", encoding="utf-8")
        proposal = propose_memory_fact(
            self.data_dir,
            actor="curator:claude/session",
            scope="global",
            slug=slug,
            fact_file=fact_file,
            source="curator:claude/session",
        )
        commit_memory_proposal(
            self.data_dir,
            self.instance_dir,
            actor="curator:claude/session",
            propose_id=proposal.propose_id,
        )

    def test_tick_checkpoint_leaves_memory_facts_alone(self):
        self.write_fact("one")
        memory_head = git(
            self.instance_dir, "log", "-1", "--format=%H", "--", "state/memory"
        ).strip()

        with mock.patch.object(TaskAudit, "status", return_value={"ok": True, "pending": 0}):
            result = self.writer().write()

        self.assertEqual(result.status, "committed")
        # The tick commit touched board and runs only; memory's tip did not move.
        touched = git(
            self.instance_dir, "show", "--name-only", "--format=", "HEAD"
        ).split()
        self.assertTrue(touched)
        self.assertFalse([path for path in touched if path.startswith("state/memory")], touched)
        self.assertEqual(
            git(self.instance_dir, "log", "-1", "--format=%H", "--", "state/memory").strip(),
            memory_head,
        )
        facts = state_repo.memory_facts_dir(self.instance_dir)
        self.assertEqual((facts / "global" / "one.md").read_text(encoding="utf-8").count("fact"), 1)

    def test_a_memory_write_lands_in_the_checkpoint_history(self):
        with mock.patch.object(TaskAudit, "status", return_value={"ok": True, "pending": 0}):
            self.writer().write()

        self.write_fact("one")

        # HEAD carries the fact, so the 30-minute push ships it with everything else.
        listed = git(self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD").split()
        self.assertIn("state/memory/facts/global/one.md", listed)
        self.assertEqual(git(self.instance_dir, "status", "--porcelain", "--", "state/memory"), "")

    def test_concurrent_tick_and_memory_writes_both_land(self):
        """The state lock serializes two writers racing on one index."""
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def tick() -> None:
            try:
                start.wait(timeout=10)
                with mock.patch.object(
                    TaskAudit, "status", return_value={"ok": True, "pending": 0}
                ):
                    self.writer().write()
            except BaseException as exc:  # noqa: BLE001 - reported to the main thread
                errors.append(exc)

        def memory() -> None:
            try:
                start.wait(timeout=10)
                self.write_fact("one")
            except BaseException as exc:  # noqa: BLE001 - reported to the main thread
                errors.append(exc)

        threads = [threading.Thread(target=tick), threading.Thread(target=memory)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        listed = git(self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD").split()
        self.assertIn("state/memory/facts/global/one.md", listed)
        self.assertIn("state/board/cards.ndjson", listed)
        self.assertEqual(git(self.instance_dir, "status", "--porcelain", "--", "state"), "")


if __name__ == "__main__":
    unittest.main()
