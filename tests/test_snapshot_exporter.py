"""The snapshot exporter: one cut per changed window into a bare snapshot repository.

Contract: docs/RECOVERY.md, "Layout" and "Writers". Every assertion about what a cut holds reads the
committed tree of the snapshot repository, never the exporter's staging.
"""

import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu import secret_store, state_repo
from ummanu.checkpoint import (
    SNAPSHOT_ALLOWLIST,
    SNAPSHOT_AUTHOR_EMAIL,
    SNAPSHOT_AUTHOR_NAME,
    SNAPSHOT_MANIFEST,
    SNAPSHOT_MANIFEST_FORMAT,
    SNAPSHOT_MANIFEST_VERSION,
    SNAPSHOT_REF,
    SNAPSHOT_SUBJECT_PREFIX,
    CheckpointWriter,
    SnapshotExporter,
    _allowlisted_files,
    live_root_is_work_tree,
    tick_checkpoint_writer,
)
from ummanu.cli import main as cli_main
from ummanu.data import DataExport
from ummanu.secret_store import initialize_store, set_secret
from ummanu.secret_words import RECOVERY_WORDS

CARD = {"id": 1, "reference": "ummanu-637", "title": "Snapshot exporter", "column": "Ready", "comments": []}
REVISION = "0123456789abcdef0123456789abcdef01234567"
SCHEMA_HEAD = "0026_test_head"
# A token the redaction patterns recognise by shape, so a cut holding it must be refused.
TOKEN = "sk-ant-api03-" + "A" * 40


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=check).stdout


def blob_bytes(repo: Path, oid: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "blob", oid], capture_output=True, check=True).stdout


def cheap_key_params():
    """Fast key derivation for tests; the format is the store's own."""
    return mock.patch.object(
        secret_store,
        "_new_key_params",
        return_value={
            "format": secret_store.KEY_PARAMS_FORMAT,
            "version": secret_store.KEY_PARAMS_VERSION,
            "kdf": {
                "id": secret_store.PHRASE_KDF_ID,
                "salt": secret_store._b64(b"0123456789abcdef"),
                "length": secret_store.KEY_LENGTH,
                "n": 2**10,
                "r": 8,
                "p": 1,
            },
        },
    )


class _SettledAudit:
    def status(self) -> dict:
        return {"ok": True, "pending": 0}


class SnapshotCase(unittest.TestCase):
    """A live root in today's layout, a seeded local export and a settled audit."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "ummanu-data"
        self.live = self.root / "instance"
        self.live.mkdir()
        self.write_config()
        self.seed_board([CARD])
        self.seed_runs([])
        for patcher in (
            mock.patch.object(CheckpointWriter, "_audit_owner", return_value=(None, _SettledAudit())),
            mock.patch("ummanu.checkpoint.export_board", side_effect=self._export(len_of="board/cards.ndjson")),
            mock.patch("ummanu.checkpoint.export_runs", side_effect=self._export(len_of="runs/runs.ndjson")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _export(*, len_of: str):
        def export(data_dir, **_kwargs):
            lines = (Path(data_dir) / len_of).read_text(encoding="utf-8")
            return DataExport(path=Path(data_dir), count=len(lines.splitlines()), source="test")

        return export

    def write_config(self, extra_offsite: str = "") -> None:
        (self.live / "instance.yaml").write_text(
            "version: 1\n"
            "name: test-home\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:owner/instance.git\n" + extra_offsite,
            encoding="utf-8",
        )
        files = {
            "projects/ummanu.yaml": "id: ummanu\n",
            "adapters/ummanu.yaml": "id: ummanu\n",
            "heads/heads.toml": "[heads]\n",
            "heads/heads.yaml": "generated: true\n",
            "heads/source.yaml": "revision: abc\n",
            "persona/rules.md": "Be brief.\n",
            "persona/nested/voice.md": "Plain.\n",
            "skills/manifest.toml": "[skills]\n",
            "policies/README.md": "dead\n",
            "tests/test_instance.py": "pass\n",
            "README.md": "instance\n",
            "CONTEXT.md": "context\n",
            "adapter-drafts/ummanu.yaml": "draft: true\n",
            "gate-runs/run.json": "{}\n",
            "state/checks/broad.json": "{}\n",
            "state/board/stale.ndjson": "live-root board files never enter a cut\n",
            "state/knowledge/decisions/one.md": "# One\n",
            "state/memory/facts/ummanu/fact.md": "a fact\n",
        }
        for relative, text in files.items():
            path = self.live / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def seed_board(self, cards: list[dict], *, audit: str = "") -> None:
        board = self.data_dir / "board"
        board.mkdir(parents=True, exist_ok=True)
        (board / "cards.ndjson").write_text("".join(json.dumps(c, sort_keys=True) + "\n" for c in cards), "utf-8")
        (board / "sprints.ndjson").write_text("", encoding="utf-8")
        (board / "events.ndjson").write_text("", encoding="utf-8")
        (board / "audit.ndjson").write_text(audit, encoding="utf-8")
        (board / "export.json").write_text(
            json.dumps({"version": 1, "card_count": len(cards), "sprint_count": 0}), encoding="utf-8"
        )

    def seed_runs(self, records: list[dict]) -> None:
        runs = self.data_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "runs.ndjson").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), "utf-8")
        (runs / "watermarks.json").write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
        (runs / "claims.json").write_text(json.dumps({"version": 1, "claims": {}}), encoding="utf-8")
        (runs / "export.json").write_text(
            json.dumps({"version": 1, "run_record_count": len(records), "watermark_count": 0, "claim_count": 0}),
            encoding="utf-8",
        )

    @property
    def repo(self) -> Path:
        return self.root / "snapshot.git"

    def exporter(self, **options) -> SnapshotExporter:
        options.setdefault("snapshot_repo", self.repo)
        options.setdefault("product_revision", REVISION)
        options.setdefault("board_schema_head", SCHEMA_HEAD)
        return SnapshotExporter(self.data_dir, self.live, **options)

    def tip(self, repo: Path | None = None) -> str:
        return git(repo or self.repo, "rev-parse", "--verify", "--quiet", SNAPSHOT_REF, check=False).strip()

    def tree(self, rev: str = SNAPSHOT_REF, repo: Path | None = None) -> dict[str, tuple[str, str]]:
        """path -> (mode, blob id) of the committed tree."""
        listing = git(repo or self.repo, "ls-tree", "-r", "--full-tree", rev)
        entries: dict[str, tuple[str, str]] = {}
        for line in listing.splitlines():
            meta, _, path = line.partition("\t")
            mode, _kind, oid = meta.split()
            entries[path] = (mode, oid)
        return entries

    def committed(self, path: str, rev: str = SNAPSHOT_REF) -> bytes:
        return blob_bytes(self.repo, self.tree(rev)[path][1])


class SnapshotRepositoryTests(SnapshotCase):
    def test_first_window_initialises_the_default_bare_repository_with_a_root_commit(self):
        result = self.exporter(snapshot_repo=None).write()

        repo = self.data_dir / "backup" / "instance.git"
        self.assertEqual(result.status, "committed", result.reason)
        self.assertEqual(git(repo, "rev-parse", "--is-bare-repository").strip(), "true")
        self.assertEqual(self.tip(repo), result.commit)
        self.assertEqual(git(repo, "rev-list", "--parents", "-n", "1", result.commit).split(), [result.commit])
        self.assertFalse((self.live / ".git").exists())

    def test_a_relative_snapshot_repo_is_rooted_at_the_data_dir(self):
        self.write_config("  snapshot_repo: offsite/snap.git\n")

        exporter = self.exporter(snapshot_repo=None)

        self.assertEqual(exporter.snapshot_repo, (self.data_dir / "offsite" / "snap.git").resolve())
        self.assertEqual(exporter.write().status, "committed")
        self.assertTrue(self.tip(self.data_dir / "offsite" / "snap.git"))

    def test_every_commit_carries_the_exporter_identity_and_the_previous_tip_as_its_only_parent(self):
        first = self.exporter().write()
        (self.live / "state" / "knowledge" / "decisions" / "two.md").write_text("# Two\n", encoding="utf-8")
        second = self.exporter().write()

        self.assertEqual(second.status, "committed")
        self.assertEqual(git(self.repo, "rev-list", "--parents", "-n", "1", second.commit).split(), [second.commit, first.commit])
        for commit in (first.commit, second.commit):
            fields = git(self.repo, "log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%s", commit).splitlines()
            self.assertEqual(fields[:4], [SNAPSHOT_AUTHOR_NAME, SNAPSHOT_AUTHOR_EMAIL] * 2)
            self.assertTrue(fields[4].startswith(SNAPSHOT_SUBJECT_PREFIX), fields[4])

    def test_an_unchanged_cut_makes_no_commit(self):
        first = self.exporter().write()
        again = self.exporter().write()

        self.assertEqual(again.status, "unchanged")
        self.assertEqual(self.tip(), first.commit)
        self.assertEqual(git(self.repo, "rev-list", "--count", SNAPSHOT_REF).strip(), "1")

    def test_the_ref_update_is_compare_and_swap_against_the_tip_read_at_window_start(self):
        first = self.exporter().write()
        (self.live / "persona" / "rules.md").write_text("Be briefer.\n", encoding="utf-8")
        exporter = self.exporter()
        real_build = exporter._build_tree

        def build_then_race(*args, **kwargs):
            tree = real_build(*args, **kwargs)
            foreign = git(self.repo, "commit-tree", f"{first.commit}^{{tree}}", "-p", first.commit, "-m", "foreign").strip()
            git(self.repo, "update-ref", SNAPSHOT_REF, foreign)
            self.foreign = foreign
            return tree

        with mock.patch.object(exporter, "_build_tree", side_effect=build_then_race):
            result = exporter.write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("snapshot branch moved during the window", result.reason)
        self.assertEqual(self.tip(), self.foreign)

    def test_a_non_bare_directory_at_the_snapshot_path_is_refused(self):
        self.repo.mkdir()
        (self.repo / "notes.txt").write_text("not a repository\n", encoding="utf-8")

        result = self.exporter().write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("is not a bare Git repository", result.reason)

    def test_staging_never_outlives_a_window(self):
        self.assertEqual(self.exporter().write().status, "committed")
        (self.live / "state" / "knowledge" / "leak.md").write_text(TOKEN, encoding="utf-8")
        self.assertEqual(self.exporter().write().status, "blocked")

        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.endswith(".tmp")), [])


class SnapshotCutTests(SnapshotCase):
    def test_the_cut_is_the_export_the_allowlist_and_the_manifest(self):
        result = self.exporter().write()

        paths = set(self.tree())
        self.assertEqual(result.status, "committed", result.reason)
        expected_config = {
            "instance.yaml",
            "projects/ummanu.yaml",
            "adapters/ummanu.yaml",
            "heads/heads.toml",
            "persona/rules.md",
            "persona/nested/voice.md",
            "skills/manifest.toml",
            "state/knowledge/decisions/one.md",
            "state/memory/facts/ummanu/fact.md",
        }
        self.assertLessEqual(expected_config, paths)
        self.assertIn(SNAPSHOT_MANIFEST, paths)
        self.assertIn("state/board/layout.json", paths)
        self.assertIn("state/board/cards/0000/00000000.json", paths)
        self.assertIn("state/board/analytics-manifest.json", paths)
        self.assertIn("state/runs/runs.ndjson", paths)
        everything_else = {
            p for p in paths if p != SNAPSHOT_MANIFEST and not p.startswith(("state/board/", "state/runs/"))
        }
        self.assertEqual(everything_else, expected_config)
        self.assertNotIn("state/board/stale.ndjson", paths)
        self.assertEqual(self.committed("state/board/cards/0000/00000000.json"), (json.dumps(CARD, sort_keys=True) + "\n").encode())

    def test_the_manifest_digests_every_other_file_and_holds_no_clock(self):
        self.exporter().write()

        manifest = json.loads(self.committed(SNAPSHOT_MANIFEST))
        self.assertEqual(set(manifest), {"format", "version", "files", "product_revision", "board_schema_head"})
        self.assertEqual(manifest["format"], SNAPSHOT_MANIFEST_FORMAT)
        self.assertEqual(manifest["version"], SNAPSHOT_MANIFEST_VERSION)
        self.assertEqual(manifest["product_revision"], REVISION)
        self.assertEqual(manifest["board_schema_head"], SCHEMA_HEAD)
        others = {p for p in self.tree() if p != SNAPSHOT_MANIFEST}
        self.assertEqual(set(manifest["files"]), others)
        for path in others:
            self.assertEqual(manifest["files"][path], hashlib.sha256(self.committed(path)).hexdigest(), path)

    def test_the_same_state_yields_the_same_tree_in_another_repository(self):
        first = self.exporter().write()
        other = self.root / "other.git"
        second = self.exporter(snapshot_repo=other).write()

        self.assertEqual(second.status, "committed")
        self.assertEqual(
            git(self.repo, "rev-parse", f"{first.commit}^{{tree}}").strip(),
            git(other, "rev-parse", f"{second.commit}^{{tree}}").strip(),
        )

    def test_the_default_manifest_names_the_running_revision_and_the_shipped_schema_head(self):
        from ummanu.board.migrate import head_revision
        from ummanu.head_registry import product_revision

        self.exporter(product_revision=None, board_schema_head=None).write()

        manifest = json.loads(self.committed(SNAPSHOT_MANIFEST))
        self.assertEqual(manifest["board_schema_head"], head_revision())
        self.assertEqual(manifest["product_revision"], product_revision(Path(__file__).resolve().parents[1]))

    def test_a_file_deleted_from_the_live_root_leaves_the_next_cut(self):
        self.exporter().write()
        (self.live / "persona" / "nested" / "voice.md").unlink()
        (self.live / "projects" / "ummanu.yaml").unlink()

        result = self.exporter().write()

        self.assertEqual(result.status, "committed")
        paths = set(self.tree())
        self.assertNotIn("persona/nested/voice.md", paths)
        self.assertNotIn("projects/ummanu.yaml", paths)
        manifest = json.loads(self.committed(SNAPSHOT_MANIFEST))
        self.assertNotIn("persona/nested/voice.md", manifest["files"])

    def test_a_grown_audit_log_gains_one_segment_and_keeps_the_committed_one(self):
        self.seed_board([CARD], audit='{"line": 1}\n')
        first = self.exporter().write()
        self.seed_board([CARD], audit='{"line": 1}\n{"line": 2}\n')

        second = self.exporter().write()

        self.assertEqual(second.status, "committed")
        before, after = self.tree(first.commit), self.tree(second.commit)
        segment = "state/board/audit/0000/00000000.ndjson"
        self.assertEqual(before[segment], after[segment])
        self.assertEqual(self.committed("state/board/audit/0000/00000001.ndjson"), b'{"line": 2}\n')

    def test_rewritten_audit_history_is_stored_as_one_segment(self):
        self.seed_board([CARD], audit='{"line": 1}\n')
        self.exporter().write()
        self.seed_board([CARD], audit='{"line": 9}\n{"line": 2}\n')

        self.exporter().write()

        audit = sorted(p for p in self.tree() if p.startswith("state/board/audit/"))
        self.assertEqual(audit, ["state/board/audit/0000/00000000.ndjson"])
        self.assertEqual(self.committed(audit[0]), b'{"line": 9}\n{"line": 2}\n')

    def test_the_live_export_may_not_truncate_the_published_run_history(self):
        self.seed_runs([{"source": "a.ndjson", "line": 1}])
        self.assertEqual(self.exporter().write().status, "committed")
        self.seed_runs([])

        result = self.exporter().write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("refusing to truncate", result.reason)

    def test_the_board_gate_still_refuses_a_miscounted_export(self):
        self.seed_board([CARD])
        (self.data_dir / "board" / "export.json").write_text(json.dumps({"card_count": 4, "sprint_count": 0}), "utf-8")

        result = self.exporter().write()

        self.assertEqual(result.status, "blocked")
        self.assertIn("board export count mismatch", result.reason)
        self.assertEqual(self.tip(), "")

    def test_the_cut_is_taken_under_the_live_root_writer_lock(self):
        exporter = self.exporter()
        real_copy = exporter._copy_allowlist
        held: list[bool] = []

        def copy_while_probing(cut):
            with open(self.live / f".{state_repo.STATE_LOCK_NAME}", "a+") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    held.append(False)
            return real_copy(cut)

        with mock.patch.object(exporter, "_copy_allowlist", side_effect=copy_while_probing):
            self.assertEqual(exporter.write().status, "committed")

        self.assertEqual(held, [True])


class SnapshotSecretTests(SnapshotCase):
    def assert_blocked_naming(self, path: str) -> None:
        result = self.exporter().write()
        self.assertEqual(result.status, "blocked")
        self.assertIn("secret detected in snapshot", result.reason)
        self.assertIn(path, result.reason)
        self.assertEqual(self.tip(), "")

    def test_a_secret_in_an_allowlisted_file_blocks_the_window_by_path(self):
        (self.live / "state" / "knowledge" / "decisions" / "one.md").write_text(f"token {TOKEN}\n", encoding="utf-8")
        self.assert_blocked_naming("state/knowledge/decisions/one.md")

    def test_a_secret_in_the_board_blocks_the_window_by_its_committed_path(self):
        self.seed_board([dict(CARD, title=f"pasted {TOKEN}")])
        self.assert_blocked_naming("state/board/cards/0000/00000000.json")

    def test_a_runtime_env_value_anywhere_in_the_cut_blocks_the_window(self):
        (self.live / "runtime.env").write_text("SOME_INTEGRATION_TOKEN=opaque-runtime-value-1234\n", encoding="utf-8")
        (self.live / "persona" / "rules.md").write_text("opaque-runtime-value-1234\n", encoding="utf-8")
        self.assert_blocked_naming("persona/rules.md")

    def test_the_manifest_itself_is_scanned(self):
        result = self.exporter(product_revision=TOKEN).write()

        self.assertEqual(result.status, "blocked")
        self.assertIn(SNAPSHOT_MANIFEST, result.reason)
        self.assertEqual(self.tip(), "")


class SnapshotFailClosedTests(SnapshotCase):
    def assert_refused(self, relative: str) -> None:
        result = self.exporter().write()
        self.assertEqual(result.status, "blocked")
        self.assertIn(f"snapshot refuses {relative}", result.reason)
        self.assertEqual(self.tip(), "")

    def test_a_symlink_at_an_allowlisted_path_blocks_the_window(self):
        (self.live / "state" / "knowledge" / "decoy.md").symlink_to(self.live / "instance.yaml")
        self.assert_refused("state/knowledge/decoy.md")

    def test_a_dangling_symlink_at_an_allowlisted_path_blocks_the_window(self):
        (self.live / "projects" / "gone.yaml").symlink_to(self.root / "nowhere.yaml")
        self.assert_refused("projects/gone.yaml")

    def test_a_symlinked_allowlisted_directory_blocks_the_window(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.md").write_text("outside the live root\n", encoding="utf-8")
        shutil.rmtree(self.live / "persona")
        (self.live / "persona").symlink_to(outside)
        self.assert_refused("persona")

    def test_a_symlinked_parent_of_an_allowlisted_file_blocks_the_window(self):
        outside = self.root / "outside-state"
        shutil.move(self.live / "state", outside)
        (self.live / "state").symlink_to(outside)
        self.assert_refused("state")

    def test_a_fifo_at_an_allowlisted_path_blocks_the_window(self):
        os.unlink(self.live / "heads" / "heads.toml")
        os.mkfifo(self.live / "heads" / "heads.toml")
        self.assert_refused("heads/heads.toml")

    def test_a_directory_where_a_file_is_allowlisted_blocks_the_window(self):
        (self.live / "adapters" / "odd.yaml").mkdir()
        self.assert_refused("adapters/odd.yaml")

    def test_a_symlink_outside_the_allowlist_is_not_followed_or_refused(self):
        (self.live / "notes.md").symlink_to(self.live / "instance.yaml")
        (self.live / "projects" / "README.md").symlink_to(self.live / "instance.yaml")

        result = self.exporter().write()

        self.assertEqual(result.status, "committed", result.reason)
        self.assertNotIn("notes.md", self.tree())
        self.assertNotIn("projects/README.md", self.tree())


class SnapshotCredentialTests(SnapshotCase):
    """`installation.key`, `runtime.env` and `board-store.env` can never enter a cut."""

    def setUp(self) -> None:
        super().setUp()
        # The store needs a repository to initialise; the live root is a plain copy of it.
        seed = self.root / "store-seed"
        seed.mkdir()
        git(seed, "init", "--quiet", "--initial-branch", "main")
        git(seed, "config", "user.name", "operator")
        git(seed, "config", "user.email", "operator@example.invalid")
        with cheap_key_params():
            initialize_store(seed, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
        set_secret(
            seed,
            secret_id="integration.token",
            value=b"opaque-sealed-credential-5678",
            scope="installation",
            purpose="a sealed credential",
            actor="tester",
            environment="INTEGRATION_TOKEN",
        )
        shutil.copytree(seed / "secrets", self.live / "secrets")
        self.credentials = {
            "secrets/installation.key": (self.live / "secrets" / "installation.key").read_bytes(),
            "runtime.env": b"UMMANU_EXAMPLE_API_TOKEN=runtime-credential-value-0001\n",
            "board-store.env": b"UMMANU_DB_PASSWORD=board-store-password-value-0002\n",
        }
        (self.live / "runtime.env").write_bytes(self.credentials["runtime.env"])
        (self.live / "board-store.env").write_bytes(self.credentials["board-store.env"])

    def assert_no_credential_bytes_in(self, rev: str) -> None:
        tree = self.tree(rev)
        for path in self.credentials:
            self.assertNotIn(path, tree)
        for path, (_mode, oid) in tree.items():
            payload = blob_bytes(self.repo, oid)
            for name, secret in self.credentials.items():
                self.assertNotIn(secret.strip(), payload, f"{name} bytes in {path}")
                for line in secret.splitlines():
                    value = line.partition(b"=")[2] or line
                    self.assertNotIn(value, payload, f"{name} value in {path}")

    def test_a_decoy_symlink_to_the_key_blocks_and_nothing_secret_is_ever_committed(self):
        decoy = self.live / "secrets" / "values" / "decoy.enc.json"
        decoy.symlink_to(self.live / "secrets" / "installation.key")

        blocked = self.exporter().write()

        self.assertEqual(blocked.status, "blocked")
        self.assertIn("snapshot refuses secrets/values/decoy.enc.json", blocked.reason)
        self.assertEqual(self.tip(), "")

        decoy.unlink()
        committed = self.exporter().write()

        self.assertEqual(committed.status, "committed", committed.reason)
        self.assertIn("secrets/catalog.yaml", self.tree())
        self.assertIn("secrets/installation-key.json", self.tree())
        self.assertTrue(any(p.startswith("secrets/values/") and p.endswith(".enc.json") for p in self.tree()))
        self.assert_no_credential_bytes_in(committed.commit)

    def test_a_sealed_secret_value_in_the_cut_blocks_the_window(self):
        """The store is read from a live root with no repository, so its values still gate the cut."""
        (self.live / "state" / "memory" / "facts" / "ummanu" / "fact.md").write_text(
            "pasted opaque-sealed-credential-5678\n", encoding="utf-8"
        )

        result = self.exporter().write()

        self.assertFalse(live_root_is_work_tree(self.live))
        self.assertEqual(result.status, "blocked")
        self.assertIn("secret detected in snapshot: state/memory/facts/ummanu/fact.md", result.reason)

    def test_the_allowlist_admits_no_credential_file(self):
        admitted = _allowlisted_files(self.live)

        self.assertIn("secrets/catalog.yaml", admitted)
        for credential in self.credentials:
            self.assertTrue((self.live / credential).is_file(), credential)
            self.assertNotIn(credential, admitted)


class SnapshotLegacyCompatibilityTests(SnapshotCase):
    """For one live state in today's layout, both writers produce the same canon."""

    def setUp(self) -> None:
        super().setUp()
        git(self.live, "init", "--quiet", "--initial-branch", "main")
        git(self.live, "config", "user.name", "operator")
        git(self.live, "config", "user.email", "operator@example.invalid")
        with cheap_key_params():
            initialize_store(self.live, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
        (self.live / ".gitignore").write_text("runtime.env\nsecrets/installation.key\n/board-store.env\nstate/checks/\nadapter-drafts/\ngate-runs/\n", "utf-8")
        shutil.rmtree(self.live / "state" / "board")
        git(self.live, "add", "-A")
        git(self.live, "commit", "--quiet", "-m", "config")
        self.seed_board([CARD], audit='{"line": 1}\n')
        self.seed_runs([{"source": "a.ndjson", "line": 1}])

    def canon(self, tree: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
        def admitted(path: str) -> bool:
            if path.startswith(("state/board/", "state/runs/")):
                return True
            for pattern in SNAPSHOT_ALLOWLIST:
                head, _, last = pattern.rpartition("/")
                if last == "**" and path.startswith(f"{head}/"):
                    return True
                if path.rpartition("/")[0] == head and Path(path).match(last):
                    return True
            return False

        return {path: entry for path, entry in tree.items() if admitted(path)}

    def legacy_write(self):
        return CheckpointWriter(self.data_dir, self.live).write()

    def test_the_exporter_tree_equals_the_legacy_tracked_tree_on_the_canon_paths(self):
        for audit in ('{"line": 1}\n', '{"line": 1}\n{"line": 2}\n'):
            self.seed_board([CARD, dict(CARD, id=2, reference="ummanu-638")], audit=audit)
            legacy = self.legacy_write()
            self.assertEqual(legacy.status, "committed", legacy.reason)
            git_head = git(self.live, "rev-parse", "HEAD").strip()
            status = git(self.live, "status", "--porcelain", "--ignored")

            exported = self.exporter().write()

            self.assertEqual(exported.status, "committed", exported.reason)
            snapshot = self.tree()
            self.assertEqual(set(snapshot) - set(self.canon(self.tree("HEAD", repo=self.live))), {SNAPSHOT_MANIFEST})
            del snapshot[SNAPSHOT_MANIFEST]
            self.assertEqual(snapshot, self.canon(self.tree("HEAD", repo=self.live)))
            # The live root's repository is read by nobody: same HEAD, same status, no new ref.
            self.assertEqual(git(self.live, "rev-parse", "HEAD").strip(), git_head)
            self.assertEqual(git(self.live, "status", "--porcelain", "--ignored"), status)

    def test_a_live_root_that_is_a_work_tree_keeps_the_legacy_tick_commit(self):
        self.assertTrue(live_root_is_work_tree(self.live))
        writer = tick_checkpoint_writer(self.data_dir, self.live)
        self.assertIs(type(writer), CheckpointWriter)

        result = writer.write()

        self.assertEqual(result.status, "committed", result.reason)
        self.assertEqual(git(self.live, "rev-parse", "HEAD").strip(), result.commit)
        self.assertIn("state/runs/runs.ndjson", self.tree("HEAD", repo=self.live))

    def test_a_live_root_without_git_gets_the_exporter_at_the_default_location(self):
        shutil.rmtree(self.live / ".git")

        writer = tick_checkpoint_writer(self.data_dir, self.live)

        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, (self.data_dir / "backup" / "instance.git").resolve())

    def test_the_one_shot_verb_exports_a_work_tree_live_root_without_touching_its_repository(self):
        head = git(self.live, "rev-parse", "HEAD").strip()
        refs = git(self.live, "for-each-ref")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = cli_main(
                [
                    "data",
                    "snapshot",
                    "--instance",
                    str(self.live),
                    "--data-dir",
                    str(self.data_dir),
                    "--snapshot-repo",
                    str(self.repo),
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "committed")
        self.assertEqual(payload["commit"], self.tip())
        self.assertEqual(payload["snapshot_repo"], str(self.repo.resolve()))
        self.assertEqual(git(self.live, "rev-parse", "HEAD").strip(), head)
        self.assertEqual(git(self.live, "for-each-ref"), refs)
        self.assertEqual(git(self.repo, "remote").strip(), "")


if __name__ == "__main__":
    unittest.main()
