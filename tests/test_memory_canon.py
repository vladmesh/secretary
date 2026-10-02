"""The memory canon without Git: one enforcement point, all-or-nothing writes, revision, verify.

Contract: docs/RECOVERY.md, "Writers". Every live root here is a plain directory, not a Git work
tree, and every assertion reads files and their bytes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from ummanu.memory import canon
from ummanu.memory.canon import (
    CanonTransaction,
    canon_revision,
    fact_content_hash,
    parse_fact_text,
    pending_undo,
)
from ummanu.memory.pack import load_product_pack, materialize_product_pack
from ummanu.memory_journal import export_memory_snapshot, verify_memory_journal
from ummanu.memory_write import commit_memory_proposal, propose_memory_fact, supersede_memory_fact

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ummanu"
ACTOR = "curator:claude/session"

# The memory path: every module that writes, packs, exports, verifies or reads the canon.
MEMORY_MODULES = (
    "memory_write.py",
    "memory_journal.py",
    "memory/pack.py",
    "memory/canon.py",
    "memory_service.py",
    "memory_reindex.py",
    "data.py",
)
# What a module on the memory path may not use: the instance repository's Git surface, a child
# process runner, or a `git` argv.
BANNED_STATE_REPO = frozenset(
    {
        "git",
        "git_command",
        "run_git",
        "run_git_bytes",
        "run_as_git_child",
        "commit",
        "head",
        "status",
        "require_repo",
        "is_tracked",
        "is_ignored",
        "ensure_ignored",
        "commit_identity",
    }
)
BANNED_MODULES = frozenset({"subprocess", "ummanu._proc"})


def git_findings(relative: str, source: str) -> list[str]:
    """Every way `source` could start a Git child, as `file:line what`."""
    found = []
    for node in ast.walk(ast.parse(source, filename=relative)):
        where = f"{relative}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "state_repo" and node.attr in BANNED_STATE_REPO:
                found.append(f"{where} state_repo.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = "." * node.level + node.module
            if module in BANNED_MODULES:
                found.append(f"{where} import {module}")
            for alias in node.names:
                if module.endswith("state_repo") and alias.name in BANNED_STATE_REPO:
                    found.append(f"{where} import state_repo.{alias.name}")
                if module == "ummanu" and alias.name == "_proc":
                    found.append(f"{where} import ummanu._proc")
        elif isinstance(node, ast.Import):
            found.extend(f"{where} import {alias.name}" for alias in node.names if alias.name in BANNED_MODULES)
        elif isinstance(node, ast.Constant) and node.value == "git":
            found.append(f"{where} 'git' argv")
    return found


class MemoryPathHasNoGitTests(unittest.TestCase):
    """Acceptance 1: the one place that enforces a Git-free memory path."""

    def test_no_memory_module_can_start_a_git_child(self):
        for relative in MEMORY_MODULES:
            with self.subTest(module=relative):
                source = (SOURCE / relative).read_text(encoding="utf-8")
                self.assertEqual(git_findings(relative, source), [])

    def test_each_planted_route_to_git_is_caught(self):
        planted = {
            "state_repo.commit(instance, MEMORY_PATHSPEC, message)": "state_repo.commit",
            "state_repo.head(instance)": "state_repo.head",
            "state_repo.git(instance, ['ls-files'], label='x')": "state_repo.git",
            "state_repo.status(instance, MEMORY_PATHSPEC)": "state_repo.status",
            "state_repo.require_repo(instance)": "state_repo.require_repo",
            "state_repo.is_tracked(instance, 'x')": "state_repo.is_tracked",
            "from ummanu.state_repo import run_git": "import state_repo.run_git",
            "import subprocess": "import subprocess",
            "from ummanu import _proc": "import ummanu._proc",
            "run(['git', 'show', 'HEAD:x'])": "'git' argv",
        }
        for line, expected in planted.items():
            with self.subTest(line=line):
                findings = git_findings("planted.py", line + "\n")
                self.assertTrue(any(expected in finding for finding in findings), findings)

    def test_the_lock_and_the_layout_stay_allowed(self):
        allowed = "state_repo.state_repo_lock(instance)\nstate_repo.memory_facts_dir(instance)\nx = '.git'\n"
        self.assertEqual(git_findings("allowed.py", allowed), [])


class CanonCase(unittest.TestCase):
    """A live root that is a plain directory, with three facts and a pack ledger."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "ummanu-data"
        self.memory_dir = self.data_dir / "memory"
        self.instance = self.root / "instance"
        self.facts = self.instance / "state" / "memory" / "facts"
        self.seed(
            {
                "global/keep.md": "---\nsource: curator:claude/session\n---\nkeep me\n",
                "global/old.md": "---\nsource: curator:claude/session\n---\nold fact\n",
                "ummanu/other.md": "---\nsource: curator:claude/session\n---\nother fact\r\nwith CRLF\r\n",
            }
        )
        ledger = self.instance / "state" / "memory" / "packs" / "other-pack.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"untouched": true}\n', encoding="utf-8")
        self.assertFalse((self.instance / ".git").exists())

    def seed(self, facts: dict[str, str]) -> None:
        for relative, text in facts.items():
            path = self.facts / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))

    def canon_files(self) -> dict[str, bytes]:
        """Every file under `state/memory` with its bytes, and every directory as `None`."""
        memory = self.instance / "state" / "memory"
        tree: dict[str, bytes] = {}
        for path in sorted(memory.rglob("*")):
            relative = path.relative_to(memory).as_posix()
            tree[relative] = None if path.is_dir() else path.read_bytes()  # type: ignore[assignment]
        return tree

    def fact_file(self, text: str, name: str = "fact.md") -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def supersede(self, slug: str = "new", supersedes: tuple[str, ...] = ("old",), scope: str = "global"):
        return supersede_memory_fact(
            self.data_dir,
            self.instance,
            actor=ACTOR,
            scope=scope,
            slug=slug,
            fact_file=self.fact_file(f"fact {slug}\n", f"{slug}.md"),
            supersedes=list(supersedes),
            source=ACTOR,
        )

    def commit(self, slug: str, scope: str = "global"):
        proposal = propose_memory_fact(
            self.data_dir,
            actor=ACTOR,
            scope=scope,
            slug=slug,
            fact_file=self.fact_file(f"fact {slug}\n", f"{slug}.md"),
            source=ACTOR,
        )
        return commit_memory_proposal(self.data_dir, self.instance, actor=ACTOR, propose_id=proposal.propose_id)


class AllOrNothingWriteTests(CanonCase):
    """Acceptance 2: a failed or crashed write leaves the canon byte-identical."""

    def test_a_failure_after_the_new_fact_and_before_removal_restores_the_canon(self):
        before = self.canon_files()
        real_remove = CanonTransaction.remove

        def fail_before_removing(transaction, path):
            self.assertTrue((self.facts / "project-new" / "new.md").is_file())
            raise RuntimeError("injected before removal")

        with mock.patch.object(CanonTransaction, "remove", fail_before_removing):
            with self.assertRaisesRegex(RuntimeError, "injected before removal"):
                self.supersede(scope="project:project-new", supersedes=("global/old",))

        self.assertEqual(self.canon_files(), before)
        self.assertIsNone(pending_undo(self.memory_dir))
        self.assertIs(CanonTransaction.remove, real_remove)

    def test_a_failure_after_removal_restores_the_canon(self):
        before = self.canon_files()

        def fail_after_removal(facts_dir):
            self.assertTrue((self.facts / "global" / "new.md").is_file())
            self.assertFalse((self.facts / "global" / "old.md").exists())
            raise RuntimeError("injected after removal")

        with mock.patch("ummanu.memory_write.canon_revision", side_effect=fail_after_removal):
            with self.assertRaisesRegex(RuntimeError, "injected after removal"):
                self.supersede()

        self.assertEqual(self.canon_files(), before)
        self.assertIsNone(pending_undo(self.memory_dir))

    def test_a_crashed_write_is_restored_by_the_next_write_before_it_proceeds(self):
        before = self.canon_files()
        # A crash: the failure happens after removal and nothing after it runs, not even rollback.
        with (
            mock.patch.object(CanonTransaction, "rollback", lambda transaction: None),
            mock.patch("ummanu.memory_write.canon_revision", side_effect=RuntimeError("process died")),
        ):
            with self.assertRaisesRegex(RuntimeError, "process died"):
                self.supersede(scope="project:project-new", supersedes=("global/old",))

        self.assertNotEqual(self.canon_files(), before)
        self.assertEqual(pending_undo(self.memory_dir), ("facts/project-new/new.md", "facts/global/old.md"))
        self.assertEqual(verify_memory_journal(self.data_dir, self.instance).dirty, True)

        result = self.commit("next")

        expected = dict(before)
        expected["facts/global/next.md"] = (self.facts / "global" / "next.md").read_bytes()
        self.assertEqual(self.canon_files(), expected)
        self.assertIsNone(pending_undo(self.memory_dir))
        self.assertEqual(result.commit, canon_revision(self.facts))

    def test_a_crash_before_the_journal_names_a_path_changes_nothing(self):
        before = self.canon_files()
        (self.memory_dir / canon.UNDO_DIR).mkdir(parents=True)

        self.commit("next")

        expected = dict(before)
        expected["facts/global/next.md"] = (self.facts / "global" / "next.md").read_bytes()
        self.assertEqual(self.canon_files(), expected)

    def test_the_undo_area_restores_mode_and_bytes_of_a_replaced_file(self):
        target = self.facts / "global" / "keep.md"
        target.chmod(0o644)
        before = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with canon.canon_transaction(self.memory_dir, self.facts.parent) as transaction:
                transaction.write(target, "replaced\n")
                raise RuntimeError("boom")

        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_the_undo_area_lives_outside_the_canon(self):
        self.memory_dir.mkdir(parents=True)
        with mock.patch.object(CanonTransaction, "commit", lambda transaction: None):
            with canon.canon_transaction(self.memory_dir, self.facts.parent) as transaction:
                transaction.write(self.facts / "global" / "x.md", "x\n")
        self.assertTrue((self.memory_dir / canon.UNDO_DIR / canon.UNDO_JOURNAL).is_file())
        self.assertFalse(any(".undo" in path for path in self.canon_files()))

    def test_a_write_outside_the_canon_root_is_refused(self):
        self.memory_dir.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "outside its root"):
            with canon.canon_transaction(self.memory_dir, self.facts.parent) as transaction:
                transaction.write(self.instance / "instance.yaml", "x\n")
        self.assertFalse((self.instance / "instance.yaml").exists())


class ContentRevisionTests(CanonCase):
    """Acceptance 3: the content revision stands where a commit id was."""

    def test_the_same_canon_gives_the_same_revision_anywhere(self):
        copy = self.root / "copy"
        subprocess_free_copy(self.facts, copy)
        self.assertEqual(canon_revision(self.facts), canon_revision(copy))
        self.assertTrue(canon_revision(self.facts).startswith("sha256:"))

    def test_any_changed_byte_changes_the_revision(self):
        before = canon_revision(self.facts)
        path = self.facts / "global" / "keep.md"
        payload = bytearray(path.read_bytes())
        payload[-2] ^= 1
        path.write_bytes(bytes(payload))
        self.assertNotEqual(canon_revision(self.facts), before)

    def test_a_renamed_fact_with_the_same_bytes_changes_the_revision(self):
        before = canon_revision(self.facts)
        (self.facts / "global" / "keep.md").rename(self.facts / "global" / "kept.md")
        self.assertNotEqual(canon_revision(self.facts), before)

    def test_a_non_fact_file_does_not_change_the_revision(self):
        before = canon_revision(self.facts)
        (self.facts / "global" / ".keep.md.123.tmp").write_text("partial\n", encoding="utf-8")
        self.assertEqual(canon_revision(self.facts), before)

    def test_writer_result_completed_proposal_and_export_manifest_carry_the_revision(self):
        result = self.commit("fresh")
        manifest = json.loads((self.memory_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result.commit, canon_revision(self.facts))
        self.assertEqual(manifest["journal"]["commit"], result.commit)
        self.assertEqual(manifest["source"]["head"], result.commit)

        superseded = self.supersede(slug="newer", supersedes=("fresh",))
        manifest = json.loads((self.memory_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(superseded.commit, result.commit)
        self.assertEqual(superseded.commit, canon_revision(self.facts))
        self.assertEqual(manifest["journal"]["commit"], superseded.commit)

    def test_the_export_text_is_the_fact_bytes(self):
        export_memory_snapshot(self.data_dir, self.instance)
        rows = {
            row["id"]: row["text"]
            for row in map(json.loads, (self.memory_dir / "export.ndjson").read_text(encoding="utf-8").splitlines())
        }
        self.assertEqual(rows["ummanu/other"].encode("utf-8"), (self.facts / "ummanu" / "other.md").read_bytes())

    def test_pack_reconciliation_carries_the_revision(self):
        product = self.root / "product"
        write_product_pack(product, {"one": "one"})

        result = materialize_product_pack(load_product_pack(product), instance_dir=self.instance, data_dir=self.data_dir)
        manifest = json.loads((self.memory_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertTrue(result.changed)
        self.assertEqual(result.commit, canon_revision(self.facts))
        self.assertEqual(manifest["journal"]["commit"], result.commit)
        self.assertIsNone(pending_undo(self.memory_dir))

    def test_a_failed_pack_reconciliation_leaves_the_canon_byte_identical(self):
        product = self.root / "product"
        write_product_pack(product, {"one": "one", "two": "two"})
        before = self.canon_files()

        def handoff(path: Path) -> None:
            raise OSError("chown refused")

        from ummanu.memory.pack import MemoryPackError

        with self.assertRaisesRegex(MemoryPackError, "chown refused"):
            materialize_product_pack(
                load_product_pack(product),
                instance_dir=self.instance,
                data_dir=self.data_dir,
                runtime_handoff=handoff,
            )

        self.assertEqual(self.canon_files(), before)
        self.assertIsNone(pending_undo(self.memory_dir))


def subprocess_free_copy(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def write_product_pack(product: Path, facts: dict[str, str]) -> None:
    pack = product / "packaging" / "memory" / "product-ummanu"
    pack.mkdir(parents=True, exist_ok=True)
    entries = []
    for fact_id, text in facts.items():
        raw = f"---\nsource: product:ummanu\n---\n{text}\n".encode()
        (pack / f"{fact_id}.md").write_bytes(raw)
        entries.append({"id": fact_id, "path": f"{fact_id}.md", "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {
        "schema": 1,
        "product": "ummanu",
        "namespace": "product:ummanu",
        "status": "active",
        "ownership": "shipped",
        "fact_format": "markdown-frontmatter-v1",
        "reconciliation": {
            "identity": "id",
            "digest": "sha256",
            "manifest_is_complete": True,
            "absent_id": "delete",
            "unchanged_digest": "retain_embedding",
        },
        "overlay_policy": {"local_overlay_allowed": True, "shipped_id_collision": "reject"},
        "facts": entries,
    }
    (pack / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def write_index(index: Path, facts: dict[str, str]) -> None:
    """An index in the service's schema, built from `id -> file text` the way the service builds one."""
    index.parent.mkdir(parents=True, exist_ok=True)
    index.unlink(missing_ok=True)
    with sqlite3.connect(index) as conn:
        conn.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT UNIQUE, content_hash TEXT, "
            "text TEXT, scope TEXT, tags TEXT, source TEXT, created_at TEXT)"
        )
        for fact_id, raw in facts.items():
            fact = parse_fact_text(raw, f"{fact_id}.md", fact_id=fact_id)
            conn.execute(
                "INSERT INTO memories(fact_id, content_hash, text, scope, tags, source, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    fact["id"],
                    fact_content_hash(fact),
                    fact["text"],
                    fact["scope"],
                    fact["tags"],
                    fact["source"],
                    fact["created_at"],
                ),
            )
        conn.commit()


class VerifyByIdAndContentTests(unittest.TestCase):
    """Acceptance 4: verify compares id sets and content hashes, never counts (issue:482b8add)."""

    SLUGS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.data_dir = root / "ummanu-data"
        self.instance = root / "instance"
        self.facts = self.instance / "state" / "memory" / "facts"
        self.index = self.data_dir / "memory" / "index.sqlite"
        self.write_canon({f"global/{slug}": f"---\nsource: curator\n---\n{slug} text\n" for slug in self.SLUGS})
        export_memory_snapshot(self.data_dir, self.instance)
        write_index(self.index, self.canon())

    def write_canon(self, facts: dict[str, str]) -> None:
        for fact_id, text in facts.items():
            path = self.facts / f"{fact_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def canon(self) -> dict[str, str]:
        return {
            path.relative_to(self.facts).as_posix().removesuffix(".md"): path.read_text(encoding="utf-8")
            for path in sorted(self.facts.rglob("*.md"))
        }

    def test_a_consistent_canon_export_and_index_verify_ok(self):
        report = verify_memory_journal(self.data_dir, self.instance)

        self.assertTrue(report.ok, report.findings)
        self.assertEqual((report.fact_count, report.export_count, report.index_count), (7, 7, 7))
        self.assertEqual(report.journal_commit, canon_revision(self.facts))
        self.assertFalse(report.dirty)

    def test_same_count_with_five_renamed_slugs_and_changed_texts_is_named(self):
        """The issue:482b8add scenario: verify said ok on equal counts while the index was stale."""
        renamed = dict(zip(self.SLUGS[:5], ("alpha-2", "bravo-2", "charlie-2", "delta-2", "echo-2")))
        for old, new in renamed.items():
            (self.facts / "global" / f"{old}.md").rename(self.facts / "global" / f"{new}.md")
            (self.facts / "global" / f"{new}.md").write_text(f"---\nsource: curator\n---\n{new} rewritten\n", "utf-8")
        (self.facts / "global" / "golf.md").write_text("---\nsource: curator\n---\ngolf rewritten\n", "utf-8")

        report = verify_memory_journal(self.data_dir, self.instance)

        self.assertFalse(report.ok)
        self.assertEqual((report.fact_count, report.export_count, report.index_count), (7, 7, 7))
        findings = "\n".join(report.findings)
        for label in ("export", "index"):
            missing = next(item for item in report.findings if item.startswith(f"memory {label} is missing"))
            extra = next(item for item in report.findings if item.startswith(f"memory {label} has facts"))
            changed = next(item for item in report.findings if item.startswith(f"memory {label} content differs"))
            for old, new in renamed.items():
                self.assertIn(f"global/{new}", missing)
                self.assertIn(f"global/{old}", extra)
            self.assertIn("global/golf", changed)
            self.assertNotIn("global/foxtrot", changed)
        self.assertNotIn("count mismatch", findings)

    def test_an_index_row_whose_text_was_changed_under_its_hash_is_named(self):
        with sqlite3.connect(self.index) as conn:
            conn.execute("UPDATE memories SET text = 'stale text' WHERE fact_id = 'global/delta'")
            conn.commit()

        report = verify_memory_journal(self.data_dir, self.instance)

        self.assertIn("memory index content differs from the canon: global/delta", report.findings)

    def test_an_index_that_cannot_be_checked_by_id_is_a_finding(self):
        self.index.unlink()
        with sqlite3.connect(self.index) as conn:
            conn.execute("create table memories(id integer primary key)")
            conn.executemany("insert into memories default values", [()] * 7)
            conn.commit()

        report = verify_memory_journal(self.data_dir, self.instance)

        self.assertFalse(report.ok)
        self.assertEqual(report.index_count, 7)
        self.assertTrue(any("no fact ids" in finding for finding in report.findings), report.findings)

    def test_leftover_undo_state_is_a_finding(self):
        memory_dir = self.data_dir / "memory"
        with mock.patch.object(CanonTransaction, "commit", lambda transaction: None):
            with canon.canon_transaction(memory_dir, self.facts.parent) as transaction:
                transaction.remove(self.facts / "global" / "alpha.md")

        report = verify_memory_journal(self.data_dir, self.instance)

        self.assertFalse(report.ok)
        self.assertTrue(report.dirty)
        self.assertTrue(
            any("memory undo state is left behind" in item and "facts/global/alpha.md" in item for item in report.findings),
            report.findings,
        )


class NoGitChildTests(CanonCase):
    """The whole memory path runs on a plain directory with process creation forbidden."""

    def test_write_supersede_export_verify_and_pack_start_no_child(self):
        product = self.root / "product"
        write_product_pack(product, {"one": "one"})
        pack = load_product_pack(product)
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a child process was started")):
            self.commit("fresh")
            self.supersede(slug="newer", supersedes=("fresh",))
            export_memory_snapshot(self.data_dir, self.instance)
            materialize_product_pack(pack, instance_dir=self.instance, data_dir=self.data_dir)
            report = verify_memory_journal(self.data_dir, self.instance)

        self.assertEqual(report.export_count, report.fact_count)
        self.assertTrue((self.facts / "global" / "newer.md").is_file())
        self.assertTrue((self.facts / "product-ummanu" / "one.md").is_file())


if __name__ == "__main__":
    unittest.main()
