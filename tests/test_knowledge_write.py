import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from secretary.checkpoint import CheckpointWriter
from secretary.data import DataExport
from secretary.knowledge_write import (
    KnowledgeValidationError,
    list_knowledge_documents,
    write_knowledge_document,
)
from secretary.state_repo import StateRepoError


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
    return result.stdout


CARD = {
    "id": 1,
    "reference": "secretary-719",
    "title": "Recoverable knowledge plane",
    "column": "Ready",
    "comments": [],
}

DOCUMENT = "decisions/2026-07-25-sprint-1.md"
BODY = "# Sprint 1\n\nWhat we decided and why.\n"


class KnowledgeRepoCase(unittest.TestCase):
    """An instance repo with both writers pointed at it."""

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

    def seed_board(self, cards: list[dict]) -> None:
        board = self.data_dir / "board"
        body = "".join(json.dumps(card, sort_keys=True) + "\n" for card in cards)
        (board / "cards.ndjson").write_text(body, encoding="utf-8")
        (board / "sprints.ndjson").write_text("", encoding="utf-8")
        (board / "events.ndjson").write_text("", encoding="utf-8")
        (board / "export.json").write_text(
            json.dumps({"version": 1, "card_count": len(cards), "sprint_count": 0}), encoding="utf-8"
        )

    def seed_runs(self, records: list[dict]) -> None:
        runs = self.data_dir / "runs"
        body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        (runs / "runs.ndjson").write_text(body, encoding="utf-8")
        (runs / "watermarks.json").write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
        (runs / "claims.json").write_text(json.dumps({"version": 1, "claims": {}}), encoding="utf-8")
        (runs / "export.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_record_count": len(records),
                    "watermark_count": 0,
                    "claim_count": 0,
                }
            ),
            encoding="utf-8",
        )

    def checkpoint(self):
        """Run the checkpoint writer with the export step stubbed by the seed."""

        def board_export(data_dir, **_kwargs):
            lines = (Path(data_dir) / "board" / "cards.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        def runs_export(data_dir, **_kwargs):
            lines = (Path(data_dir) / "runs" / "runs.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        with mock.patch("secretary.checkpoint.export_board", side_effect=board_export):
            with mock.patch("secretary.checkpoint.export_runs", side_effect=runs_export):
                return CheckpointWriter(self.data_dir, self.instance_dir).write()

    def write(self, *, document: str = DOCUMENT, text: str = BODY, actor: str = "po"):
        return write_knowledge_document(self.instance_dir, document=document, actor=actor, text=text)

    def head_files(self) -> list[str]:
        return git(self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD").split()


class KnowledgeWriteTests(KnowledgeRepoCase):
    def test_document_lands_in_state_knowledge_as_its_own_commit(self):
        result = self.write()

        self.assertTrue(result.changed)
        self.assertEqual(result.document, DOCUMENT)
        self.assertIn(f"state/knowledge/{DOCUMENT}", self.head_files())
        self.assertEqual(
            (self.instance_dir / "state" / "knowledge" / DOCUMENT).read_text(encoding="utf-8"),
            BODY,
        )
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), result.commit)
        message = git(self.instance_dir, "log", "-1", "--format=%B")
        self.assertIn(f"knowledge: {DOCUMENT}", message)
        self.assertIn("Principal: po", message)

    def test_commit_touches_only_the_knowledge_pathspec(self):
        self.seed_board([CARD])
        stray = self.instance_dir / "state" / "board"
        stray.mkdir(parents=True, exist_ok=True)
        (stray / "cards.ndjson").write_text("uncommitted tick output\n", encoding="utf-8")

        self.write()

        files = self.head_files()
        self.assertIn(f"state/knowledge/{DOCUMENT}", files)
        self.assertNotIn("state/board/cards.ndjson", files)

    def test_rewriting_the_same_content_adds_no_commit(self):
        first = self.write()
        again = self.write()

        self.assertFalse(again.changed)
        self.assertEqual(again.commit, first.commit)

    def test_editing_a_document_commits_the_new_revision(self):
        self.write()
        updated = self.write(text=BODY + "\nAnd a later revision.\n")

        self.assertTrue(updated.changed)
        self.assertIn(
            "And a later revision.",
            (self.instance_dir / "state" / "knowledge" / DOCUMENT).read_text(encoding="utf-8"),
        )

    def test_secret_in_the_document_is_rejected_before_any_commit(self):
        head_before = git(self.instance_dir, "rev-parse", "HEAD").strip()
        leaked = "# Notes\n\ntoken sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX\n"

        with self.assertRaises(KnowledgeValidationError) as caught:
            self.write(text=leaked)

        self.assertIn("secret detected", str(caught.exception))
        self.assertIn(f"state/knowledge/{DOCUMENT}", str(caught.exception))
        self.assertFalse((self.instance_dir / "state" / "knowledge" / DOCUMENT).exists())
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), head_before)

    def test_path_outside_state_knowledge_is_rejected(self):
        for bad in ("../instance.yaml", "/etc/passwd.md", "decisions/../../escape.md", "note.txt"):
            with self.subTest(path=bad):
                with self.assertRaises(KnowledgeValidationError):
                    self.write(document=bad)

    def test_empty_document_is_rejected(self):
        with self.assertRaises(KnowledgeValidationError):
            self.write(text="   \n")

    def test_missing_source_file_names_itself(self):
        with self.assertRaises(KnowledgeValidationError) as caught:
            write_knowledge_document(
                self.instance_dir,
                document=DOCUMENT,
                actor="po",
                source_file=Path(self.tmpdir.name) / "absent.md",
            )
        self.assertIn("absent.md", str(caught.exception))

    def test_write_outside_a_git_repo_fails_loudly(self):
        plain = Path(self.tmpdir.name) / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(StateRepoError):
            write_knowledge_document(plain, document=DOCUMENT, actor="po", text=BODY)

    def test_existing_documents_are_listed_without_migration(self):
        legacy = self.instance_dir / "state" / "knowledge" / "brainstorms" / "old.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("plain markdown, no frontmatter\n", encoding="utf-8")
        git(self.instance_dir, "add", "--", "state/knowledge")
        git(self.instance_dir, "commit", "--quiet", "-m", "legacy knowledge")

        self.write()

        self.assertEqual(
            list_knowledge_documents(self.instance_dir),
            ("brainstorms/old.md", DOCUMENT),
        )
        self.assertEqual(legacy.read_text(encoding="utf-8"), "plain markdown, no frontmatter\n")
        self.assertIn("state/knowledge/brainstorms/old.md", self.head_files())


class KnowledgeCheckpointRaceTests(KnowledgeRepoCase):
    def test_concurrent_knowledge_write_and_checkpoint_keep_both_sides(self):
        errors: list[BaseException] = []
        results: dict[str, object] = {}
        start = threading.Barrier(2)

        def run_knowledge() -> None:
            try:
                start.wait(timeout=10)
                results["knowledge"] = self.write()
            except BaseException as exc:  # noqa: BLE001 - reported to the test body
                errors.append(exc)

        def run_checkpoint() -> None:
            try:
                start.wait(timeout=10)
                results["checkpoint"] = self.checkpoint()
            except BaseException as exc:  # noqa: BLE001 - reported to the test body
                errors.append(exc)

        threads = [threading.Thread(target=run_knowledge), threading.Thread(target=run_checkpoint)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))

        self.assertEqual(results["checkpoint"].status, "committed")
        self.assertTrue(results["knowledge"].changed)

        # Both writers landed, each in its own commit, and neither left the index
        # holding the other's paths.
        files = self.head_files()
        self.assertIn(f"state/knowledge/{DOCUMENT}", files)
        self.assertIn("state/board/cards.ndjson", files)
        self.assertIn("state/runs/runs.ndjson", files)
        self.assertEqual(git(self.instance_dir, "status", "--porcelain").strip(), "")
        self.assertEqual(len(git(self.instance_dir, "log", "--format=%H").split()), 3)

    def test_repeated_interleavings_never_drop_a_side(self):
        for round_index in range(5):
            document = f"brainstorms/round-{round_index}.md"
            self.seed_board([{**CARD, "id": round_index + 1, "title": f"round {round_index}"}])
            errors: list[BaseException] = []
            start = threading.Barrier(2)

            def run_knowledge() -> None:
                try:
                    start.wait(timeout=10)
                    self.write(document=document, text=f"# round {round_index}\n")
                except BaseException as exc:  # noqa: BLE001 - reported to the test body
                    errors.append(exc)

            def run_checkpoint() -> None:
                try:
                    start.wait(timeout=10)
                    self.checkpoint()
                except BaseException as exc:  # noqa: BLE001 - reported to the test body
                    errors.append(exc)

            threads = [
                threading.Thread(target=run_knowledge),
                threading.Thread(target=run_checkpoint),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

            with self.subTest(round=round_index):
                self.assertEqual(errors, [])
                self.assertIn(f"state/knowledge/{document}", self.head_files())
                self.assertEqual(git(self.instance_dir, "status", "--porcelain").strip(), "")
                board = (self.instance_dir / "state" / "board" / "cards.ndjson").read_text(encoding="utf-8")
                self.assertIn(f"round {round_index}", board)


if __name__ == "__main__":
    unittest.main()
