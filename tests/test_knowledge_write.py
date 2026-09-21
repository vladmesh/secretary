import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from secretary.board.checkpoint_layout import open_checkpoint_board
from secretary.checkpoint import CheckpointWriter
from secretary.cli import main as cli_main
from secretary.data import DataExport
from secretary.knowledge_write import (
    KnowledgeValidationError,
    list_knowledge_documents,
    write_knowledge_directory,
    write_knowledge_document,
)
from secretary.state_repo import StateRepoError
from tests.fakes.tasks import FakeKanboard


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
        (board / "audit.ndjson").write_text("", encoding="utf-8")
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
        """Run the checkpoint writer with the export step stubbed by the seed.

        The writer's gate is the audit of the card client it is given (`tasks.task_audit_for`), so
        this installation is handed the Kanboard fake whose canon is the file journal the seed
        writes. Without one the writer would ask the switch for a client this temporary instance has
        no transport for, and block before the race these cases are about could even start.
        """

        def board_export(data_dir, **_kwargs):
            lines = (Path(data_dir) / "board" / "cards.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        def runs_export(data_dir, **_kwargs):
            lines = (Path(data_dir) / "runs" / "runs.ndjson").read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        with mock.patch("secretary.checkpoint.export_board", side_effect=board_export):
            with mock.patch("secretary.checkpoint.export_runs", side_effect=runs_export):
                return CheckpointWriter(
                    self.data_dir, self.instance_dir, client=FakeKanboard()
                ).write()

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
            with self.subTest(path=bad), self.assertRaises(KnowledgeValidationError):
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
        self.assertIn("state/board/cards/0000/00000000.json", files)
        self.assertIn("state/runs/runs.ndjson", files)
        self.assertEqual(git(self.instance_dir, "status", "--porcelain").strip(), "")
        self.assertEqual(len(git(self.instance_dir, "log", "--format=%H").split()), 3)

    def test_repeated_interleavings_never_drop_a_side(self):
        for round_index in range(5):
            self._one_interleaving(round_index)

    def _one_interleaving(self, round_index: int) -> None:
        """One round of the interleaving, as its own call rather than a loop body.

        The two threads close over this call's locals instead of a loop variable, so a round
        cannot observe the next round's document, barrier or error list even if a thread outlives
        its join.
        """
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
            board = open_checkpoint_board(self.instance_dir / "state" / "board").read_text("cards.ndjson")
            self.assertIn(f"round {round_index}", board)


REPORT = "reports/secretary-1640"


class KnowledgeDirectoryWriteTests(KnowledgeRepoCase):
    """`write_knowledge_directory` and `knowledge write --dir` (secretary-1640)."""

    def source(self, files: dict[str, str | bytes]) -> Path:
        root = Path(self.tmpdir.name) / "source"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8")
        return root

    def write_dir(self, source: Path, *, directory: str = REPORT):
        return write_knowledge_directory(
            self.instance_dir, directory=directory, actor="dispatcher", source_dir=source
        )

    def report_files(self) -> list[str]:
        return git(
            self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD", "--", f"state/knowledge/{REPORT}"
        ).split()

    def assert_refused(self, source: Path, reason: str, *, directory: str = REPORT) -> None:
        head_before = git(self.instance_dir, "rev-parse", "HEAD").strip()
        with self.assertRaises(KnowledgeValidationError) as caught:
            self.write_dir(source, directory=directory)
        self.assertEqual(caught.exception.reason, reason, str(caught.exception))
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), head_before)
        self.assertFalse((self.instance_dir / "state" / "knowledge" / "reports").exists())

    def test_the_directory_lands_with_subdirectories_and_binary_files_unchanged(self):
        binary = bytes(range(256))
        result = self.write_dir(
            self.source(
                {"report.md": "# Findings\n", "data/results.csv": "a,b\n1,2\n", "data/blob.bin": binary}
            )
        )

        self.assertTrue(result.changed)
        self.assertEqual(
            sorted(self.report_files()),
            [
                f"state/knowledge/{REPORT}/{name}"
                for name in ("data/blob.bin", "data/results.csv", "report.md")
            ],
        )
        target = self.instance_dir / "state" / "knowledge" / REPORT
        self.assertEqual((target / "data" / "blob.bin").read_bytes(), binary)
        self.assertEqual(git(self.instance_dir, "rev-parse", "HEAD").strip(), result.commit)
        self.assertEqual(git(self.instance_dir, "status", "--porcelain", "--", "state/knowledge").strip(), "")

    def test_a_rewrite_replaces_the_whole_directory_and_identical_content_adds_no_commit(self):
        first = self.write_dir(self.source({"report.md": "one\n", "old.txt": "gone soon\n"}))
        second = self.write_dir(self.source({"report.md": "two\n"}))
        again = self.write_dir(self.source({"report.md": "two\n"}))

        self.assertTrue(second.changed)
        self.assertNotEqual(first.commit, second.commit)
        self.assertEqual(self.report_files(), [f"state/knowledge/{REPORT}/report.md"])
        self.assertFalse((self.instance_dir / "state" / "knowledge" / REPORT / "old.txt").exists())
        self.assertFalse(again.changed)
        self.assertEqual(again.commit, second.commit)
        history = git(self.instance_dir, "log", "--format=%H", "--", f"state/knowledge/{REPORT}").split()
        self.assertEqual(len(history), 2)

    def test_the_commit_touches_only_the_directory_pathspec(self):
        (self.instance_dir / "state" / "knowledge").mkdir(parents=True)
        stray = self.instance_dir / "state" / "knowledge" / "loose.md"
        stray.write_text("uncommitted\n", encoding="utf-8")

        self.write_dir(self.source({"report.md": "one\n"}))

        self.assertNotIn("state/knowledge/loose.md", self.head_files())

    def test_a_symlink_in_the_source_is_refused(self):
        source = self.source({"report.md": "one\n"})
        (source / "link.md").symlink_to(source / "report.md")
        self.assert_refused(source, "special_file")

    def test_every_git_named_entry_is_refused(self):
        """secretary-1640 remark: `.gitignore`, `.gitattributes`, `.gitmodules` change what git records."""
        for name in (".git", ".gitignore", ".gitattributes", ".gitmodules", "data/.gitignore", "data/.git-keep"):
            with self.subTest(entry=name):
                self.assert_refused(self.source({"report.md": "one\n", name: "x\n"}), "special_file")
        source = self.source({"report.md": "one\n"})
        (source / "nested" / ".git").mkdir(parents=True)
        self.assert_refused(source, "special_file")

    def swap_leftovers(self) -> list[str]:
        """Every swap-shaped name under `state/knowledge`, where a knowledge commit would find it."""
        knowledge = self.instance_dir / "state" / "knowledge"
        return sorted(
            str(path.relative_to(knowledge))
            for path in knowledge.rglob("*")
            if path.name.endswith((".old", ".new")) or path.name.startswith("swap.")
        )

    def test_a_crash_during_the_swap_leaves_nothing_a_knowledge_commit_picks_up(self):
        """Staging lives outside `state/knowledge`; the next write under the lock puts the swap right.

        The crash is simulated as a `BaseException` between moving the previous directory aside and
        moving the new one in, the window in which neither is at the target.
        """
        self.write_dir(self.source({"report.md": "one\n", "data/a.csv": "1\n"}))
        committed = sorted(self.report_files())
        real_replace = __import__("os").replace
        calls = []

        def crash_on_second(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:
                raise KeyboardInterrupt("simulated crash")
            return real_replace(src, dst)

        with (
            mock.patch("secretary.knowledge_write.os.replace", side_effect=crash_on_second),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.write_dir(self.source({"report.md": "two\n"}))

        target = self.instance_dir / "state" / "knowledge" / REPORT
        self.assertFalse(target.exists(), "the crash hit the window with nothing at the target")
        self.assertEqual(self.swap_leftovers(), [], "nothing swap-shaped was left under state/knowledge")
        swap_root = self.instance_dir / "state" / ".knowledge-swap"
        self.assertTrue(any(swap_root.iterdir()), "the interrupted swap is parked outside knowledge")
        # A staging directory an earlier crash left before anything moved is swept as well.
        (swap_root / "swap.stale" / "new").mkdir(parents=True)

        result = self.write(document="decisions/after-the-crash.md", text="# After\n")

        self.assertTrue(result.changed)
        self.assertEqual((target / "report.md").read_text(encoding="utf-8"), "one\n", "put back, not lost")
        self.assertEqual(sorted(self.report_files()), committed, "the commit neither dropped nor added")
        self.assertFalse(any(name.startswith("state/.knowledge-swap") for name in self.head_files()))
        self.assertEqual(self.swap_leftovers(), [])
        self.assertEqual(list(swap_root.iterdir()), [])
        self.assertEqual(git(self.instance_dir, "status", "--porcelain", "--", "state/knowledge").strip(), "")

    def test_a_target_path_that_escapes_is_refused(self):
        source = self.source({"report.md": "one\n"})
        for bad in ("../outside", "reports/../../escape", "/abs/dir", "reports/with space"):
            with self.subTest(path=bad):
                self.assert_refused(source, "path", directory=bad)

    def test_a_missing_or_empty_source_is_refused(self):
        self.assert_refused(Path(self.tmpdir.name) / "absent", "source_missing")
        empty = self.source({})
        (empty / "only-a-subdirectory").mkdir()
        self.assert_refused(empty, "source_empty")

    def test_a_secret_in_any_text_file_is_refused(self):
        leaked = "token sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX\n"
        self.assert_refused(self.source({"report.md": "clean\n", "scripts/run.sh": leaked}), "secret")

    def test_a_source_over_the_cap_is_refused(self):
        source = self.source({"report.md": "clean\n", "data.bin": b"\0" * 64})
        with mock.patch("secretary.knowledge_write.KNOWLEDGE_DIRECTORY_CAP_BYTES", 32):
            self.assert_refused(source, "size_cap")

    def test_cli_takes_exactly_one_of_file_and_dir(self):
        source = self.source({"report.md": "one\n"})
        base = ["knowledge", "write", "--instance", str(self.instance_dir), "--actor", "po", "--path", REPORT]
        with mock.patch("sys.stderr"):
            self.assertNotEqual(cli_main(base), 0)
            self.assertNotEqual(
                cli_main([*base, "--dir", str(source), "--file", str(source / "report.md")]), 0
            )
        with mock.patch("builtins.print"):
            self.assertEqual(cli_main([*base, "--dir", str(source)]), 0)
        self.assertEqual(self.report_files(), [f"state/knowledge/{REPORT}/report.md"])


if __name__ == "__main__":
    unittest.main()
