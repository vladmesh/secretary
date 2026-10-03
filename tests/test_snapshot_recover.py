"""`ummanu recover` from a remote whose tip is an exporter snapshot (docs/RECOVERY.md, "Fresh install
and recovery").

The remote is a local bare repository holding a real `SnapshotExporter` commit. Recovery runs through
`install()` with only the board store, the memory model, project checkouts and the host steps other
than the head registry stood in for; the clone, the manifest check, the snapshot repository, the
takeover marker, the live root, the checkpoint and the recovery identity are all real. The board
import against PostgreSQL is `tests/test_snapshot_recovery_postgres.py`.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import CARD, PRODUCT_ROOT, SPRINT, _checkpoint, _git
from tests.fakes.snapshot_remote import (
    HEAD,
    REVISION,
    exporter_producers,
    exporter_remote,
    git,
)
from ummanu import installation, upgrade
from ummanu.board.migrate import head_revision
from ummanu.checkpoint import (
    SNAPSHOT_BASE_REF,
    SNAPSHOT_MANIFEST,
    SNAPSHOT_REF,
    SnapshotExporter,
    snapshot_foreign_commits,
    tick_checkpoint_pusher,
    tick_checkpoint_writer,
)
from ummanu.config import validate_instance
from ummanu.head_registry import installed_heads, installed_pair
from ummanu.infra.export_allowlist import is_exported
from ummanu.installation import InstallError, _recovery_identity


def _args(fixture, **overrides) -> SimpleNamespace:
    values = {
        "instance_dir": str(fixture.target),
        "instance_remote": str(fixture.remote),
        "installation_user": getpass.getuser(),
        "recover": True,
        "adopt": False,
        "dry_run": False,
        "runtime_env": None,
        "product_root": str(PRODUCT_ROOT),
        "bootstrap_credential_file": None,
        "bootstrap_credential_stdin": False,
        "recovery_phrase_file": None,
        "recovery_phrase_stdin": False,
        "host_fixture": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _head_registry_only(context, steps=installation.STEPS):
    """The recover materializer with only its head-registry step running (the host is not this test's)."""
    return upgrade.run_steps(
        context, steps=tuple(step for step in steps if step is upgrade.step_head_registry)
    )


def _rebuilt_index(data_dir: Path, instance_dir: Path, **_kwargs) -> int:
    """The reindex without the embedding model: one index file for the facts the live root holds."""
    facts = sorted((instance_dir / "state" / "memory" / "facts").rglob("*.md"))
    (data_dir / "memory").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory" / "index.sqlite").write_text("\n".join(map(str, facts)), encoding="utf-8")
    return len(facts)


class SnapshotRecoverCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-recover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = exporter_remote(self.root)
        self.board = mock.Mock(return_value=len(self.fixture.cards))

    def recover(self, *, failures: dict[str, BaseException] | None = None, **overrides):
        """One `ummanu recover`; `failures` makes the named installation callable raise instead."""
        patches = {
            "check_prerequisites": mock.Mock(),
            "import_normalized_board": self.board,
            "rebuild_memory_index": mock.Mock(side_effect=_rebuilt_index),
            "provision_project_checkouts": mock.Mock(return_value=[]),
            "provision_codex_home": mock.Mock(return_value=0),
            "run_steps": mock.Mock(side_effect=_head_registry_only),
            "mark_reconcile_applied": mock.Mock(),
            "restore_findings": mock.Mock(return_value=[]),
        }
        for name, failure in (failures or {}).items():
            patches[name] = mock.Mock(side_effect=failure)
        with ExitStack() as stack:
            for name, replacement in patches.items():
                stack.enter_context(mock.patch.object(installation, name, replacement))
            return installation.install(_args(self.fixture, **overrides))

    def steps(self, result) -> dict[str, tuple[str, str]]:
        return {step.name: (step.status, step.detail) for step in result.steps}

    @property
    def repository(self) -> Path:
        return self.fixture.data_dir / "backup" / "instance.git"

    def live_files(self) -> dict[str, bytes]:
        target = self.fixture.target
        return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}

    def leftovers(self) -> list[str]:
        parent = self.fixture.target.parent
        return (
            sorted(p.name for p in parent.iterdir() if p.name.startswith(".instance."))
            if parent.exists()
            else []
        )

    def relocate_data_dir(self, value: str) -> str:
        """Cut the source again with `data_dir: <value>`, rooted at the live root it is recovered into."""
        path = self.fixture.source / "instance.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(f"data_dir: {self.fixture.data_dir}\n", f"data_dir: {value}\n"), "utf-8")
        return self.fixture.cut()

    def push_tree(self, edit, *, message: str = "edited by hand") -> str:
        """Commit `edit(work tree)` on top of the remote tip with ordinary Git and push it."""
        work = self.root / "edit"
        shutil.rmtree(work, ignore_errors=True)
        subprocess.run(["git", "clone", "--quiet", str(self.fixture.remote), str(work)], check=True)
        edit(work)
        git(work, "add", "-A")
        git(
            work,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        )
        git(work, "push", "--quiet", "origin", "HEAD:main")
        return self.fixture.tip


class SnapshotRecoveryTests(SnapshotRecoverCase):
    def test_a_snapshot_remote_recovers_into_a_plain_live_root_a_bare_repository_and_the_data_plane(self):
        tip = self.fixture.tip

        result = self.recover()

        steps = self.steps(result)
        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(steps["instance-checkout"][0], "changed")
        self.assertIn(f"recovered exporter snapshot {tip[:12]}", steps["instance-checkout"][1])
        # The snapshot repository: a depth-1 bare clone of the remote tip, origin the configured remote.
        repo = self.repository
        self.assertEqual(git(repo, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(git(repo, "rev-parse", "--is-shallow-repository"), "true")
        self.assertEqual(git(repo, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(repo, "rev-list", "--count", SNAPSHOT_REF), "1")
        self.assertEqual(git(repo, "config", "--get", "remote.origin.url"), str(self.fixture.remote))
        self.assertEqual(git(repo, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        # The live root: exactly the tree's allowlisted files, byte for byte, with their modes.
        tree = git(repo, "ls-tree", "-r", "--name-only", "--full-tree", tip).splitlines()
        exported = sorted(path for path in tree if is_exported(path))
        live = self.live_files()
        self.assertEqual(sorted(live), exported)
        for path in exported:
            blob = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "blob", f"{tip}:{path}"], capture_output=True, check=True
            ).stdout
            self.assertEqual(live[path], blob, path)
        self.assertTrue((self.fixture.target / "persona/hooks/check.sh").stat().st_mode & 0o100)
        self.assertFalse((self.fixture.target / "persona/rules.md").stat().st_mode & 0o111)
        for absent in (".git", "state/board", "state/runs", SNAPSHOT_MANIFEST, "runtime.env", "README.md"):
            self.assertFalse((self.fixture.target / absent).exists(), absent)
        self.assertIn("state/board/layout.json", tree)
        self.assertEqual(self.leftovers(), [])
        # The data plane came from the tree's board and runs.
        board = self.fixture.data_dir / "board"
        self.assertEqual(json.loads((board / "cards.json").read_text(encoding="utf-8"))["cards"], [CARD])
        self.assertEqual(
            json.loads((board / "sprints.json").read_text(encoding="utf-8"))["sprints"], [SPRINT]
        )
        self.assertEqual(steps["checkpoint"], ("changed", "1 board card(s), 0 run record(s)"))
        self.board.assert_called_once_with(self.fixture.data_dir, instance=self.fixture.target)
        # The heads were regenerated into the data directory from the recovered canon.
        pair = installed_pair(self.fixture.target)
        self.assertEqual(pair.snapshot.parent, self.fixture.data_dir / "heads")
        self.assertEqual(installed_heads(self.fixture.target)["role_defaults"]["new_card"], HEAD)
        self.assertFalse((self.fixture.target / "heads" / "heads.yaml").exists())
        # The first tick runs in exporter mode against the recovered repository.
        writer = tick_checkpoint_writer(self.fixture.data_dir, self.fixture.target)
        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, repo.resolve())

    def test_the_recovery_identity_is_the_live_roots_facts_and_the_trees_board_and_runs(self):
        self.recover()
        progress = json.loads((self.fixture.data_dir / "recovery-progress.json").read_text(encoding="utf-8"))
        bindings = validate_instance(self.fixture.target).bindings
        legacy_shaped = self.root / "legacy-shaped"
        shutil.copytree(self.fixture.target, legacy_shaped)
        work = self.root / "tree"
        subprocess.run(["git", "clone", "--quiet", str(self.fixture.remote), str(work)], check=True)
        shutil.copytree(work / "state" / "board", legacy_shaped / "state" / "board")
        shutil.copytree(work / "state" / "runs", legacy_shaped / "state" / "runs")

        # The same inputs a legacy checkout of this tree would give: one identity for one state.
        self.assertEqual(progress["identity"], _recovery_identity(legacy_shaped, bindings))
        self.assertEqual(progress["checkpoint"], "complete")
        self.assertEqual(progress["board"], "complete")

    def test_after_recovery_one_exporter_window_commits_and_pushes_on_the_recovered_tip(self):
        recovered = self.fixture.tip
        self.recover()
        (self.fixture.target / "state" / "knowledge" / "decisions" / "two.md").write_text("# Two\n", "utf-8")

        with exporter_producers():
            writer = tick_checkpoint_writer(self.fixture.data_dir, self.fixture.target)
            writer._product_revision, writer._board_schema_head = REVISION, head_revision()
            cut = writer.write()
        pushed = tick_checkpoint_pusher(writer).push({}, now=1_800_000_000.0)

        self.assertEqual(cut.status, "committed", cut.reason)
        self.assertEqual(git(self.repository, "rev-parse", f"{cut.commit}^"), recovered)
        self.assertEqual(pushed["status"], "pushed", pushed.get("reason"))
        self.assertEqual(self.fixture.tip, cut.commit)
        self.assertEqual(snapshot_foreign_commits(self.fixture.target, self.fixture.data_dir), "")
        # A plumbing commit after the recovery is still named.
        tree = git(self.repository, "rev-parse", f"{cut.commit}^{{tree}}")
        foreign = git(
            self.repository,
            *("-c", "user.name=a head", "-c", "user.email=head@example.invalid"),
            *("commit-tree", tree, "-p", cut.commit, "-m", "fix by hand"),
        )
        git(self.repository, "update-ref", SNAPSHOT_REF, foreign)
        finding = snapshot_foreign_commits(self.fixture.target, self.fixture.data_dir)
        self.assertIn(foreign[:12], finding)
        self.assertNotIn(cut.commit[:12], finding)
        self.assertNotIn(recovered[:12], finding)

    def test_host_local_files_never_come_from_the_tree(self):
        planted = {
            "secrets/installation.key": b"not from the tree\n",
            "runtime.env": b"UMMANU_FROM_TREE=1\n",
            "board-store.env": b"PGPASSWORD=from-tree\n",
        }

        def plant(work: Path) -> None:
            manifest = json.loads((work / SNAPSHOT_MANIFEST).read_text(encoding="utf-8"))
            for relative, payload in planted.items():
                (work / relative).parent.mkdir(parents=True, exist_ok=True)
                (work / relative).write_bytes(payload)
                manifest["files"][relative] = hashlib.sha256(payload).hexdigest()
            (work / SNAPSHOT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

        self.push_tree(plant)

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        for relative in planted:
            self.assertFalse((self.fixture.target / relative).exists(), relative)

    def test_a_relative_snapshot_repo_is_resolved_as_the_exporter_resolves_it(self):
        self.fixture.source.joinpath("instance.yaml").write_text(
            self.fixture.source.joinpath("instance.yaml")
            .read_text(encoding="utf-8")
            .replace("host:", "  snapshot_repo: offsite/snap.git\nhost:"),
            encoding="utf-8",
        )
        tip = self.fixture.cut()

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        relocated = self.fixture.data_dir / "offsite" / "snap.git"
        self.assertEqual(git(relocated, "rev-parse", SNAPSHOT_REF), tip)
        self.assertFalse(self.repository.exists())
        self.assertEqual(
            tick_checkpoint_writer(self.fixture.data_dir, self.fixture.target).snapshot_repo, relocated
        )

    def test_a_live_root_inside_the_data_directory_recovers(self):
        """The layout this sprint deploys: live root `<data>/instance`, `data_dir: ..` and the snapshot
        repository at its default, `<data>/backup/instance.git`, beside the live root."""
        data_dir = self.root / "ummanu-data"
        self.fixture.target = data_dir / "instance"
        tip = self.relocate_data_dir("..")
        self.fixture.data_dir = data_dir

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        repository = data_dir / "backup" / "instance.git"
        self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(repository, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        self.assertFalse(repository.is_relative_to(self.fixture.target))
        tree = git(repository, "ls-tree", "-r", "--name-only", "--full-tree", tip).splitlines()
        self.assertEqual(sorted(self.live_files()), sorted(path for path in tree if is_exported(path)))
        self.assertTrue((data_dir / "data-manifest.json").is_file())
        self.assertEqual(self.leftovers(), [])
        writer = tick_checkpoint_writer(data_dir, self.fixture.target)
        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, repository.resolve())
        # A rerun finds the same live root and the same repository beside it.
        again = self.recover()
        self.assertEqual(again.status, "ok", again.render())
        self.assertEqual(self.steps(again)["instance-checkout"][0], "unchanged")


class SnapshotRefusalTests(SnapshotRecoverCase):
    def assert_refused_before_writing(self, result, reason: str) -> None:
        steps = self.steps(result)
        self.assertEqual(result.status, "failed", result.render())
        self.assertIn(reason, steps["install"][1])
        self.assertFalse(self.fixture.target.exists())
        self.assertFalse(self.fixture.data_dir.exists())
        self.assertEqual(self.leftovers(), [])

    def edit_manifest(self, change) -> None:
        def edit(work: Path) -> None:
            path = work / SNAPSHOT_MANIFEST
            manifest = json.loads(path.read_text(encoding="utf-8"))
            change(manifest)
            path.write_text(json.dumps(manifest), encoding="utf-8")

        self.push_tree(edit)

    def test_a_malformed_manifest_is_refused_by_name_before_anything_is_written(self):
        self.push_tree(lambda work: (work / SNAPSHOT_MANIFEST).write_text("{not json", encoding="utf-8"))

        self.assert_refused_before_writing(self.recover(), "snapshot manifest is malformed: not JSON")

    def test_an_unknown_manifest_version_is_refused_by_name(self):
        self.edit_manifest(lambda manifest: manifest.update(version=2))

        self.assert_refused_before_writing(
            self.recover(), "snapshot manifest version 2 is unknown to this product, which reads version 1"
        )

    def test_a_file_the_manifest_does_not_list_is_refused(self):
        self.push_tree(
            lambda work: (work / "persona" / "extra.md").write_text("unlisted\n", encoding="utf-8")
        )

        self.assert_refused_before_writing(
            self.recover(), "in the tree but not the manifest: persona/extra.md"
        )

    def test_a_listed_file_the_tree_lacks_is_refused(self):
        self.push_tree(lambda work: (work / "persona" / "rules.md").unlink())

        self.assert_refused_before_writing(
            self.recover(), "in the manifest but not the tree: persona/rules.md"
        )

    def test_a_digest_mismatch_is_refused(self):
        self.push_tree(
            lambda work: (work / "persona" / "rules.md").write_text("Be loud.\n", encoding="utf-8")
        )

        self.assert_refused_before_writing(
            self.recover(), "with a digest the manifest does not hold: persona/rules.md"
        )

    def test_a_board_schema_newer_than_the_product_is_refused_naming_both_heads(self):
        self.edit_manifest(lambda manifest: manifest.update(board_schema_head="9999_from_the_future"))

        result = self.recover()

        self.assert_refused_before_writing(result, "9999_from_the_future")
        self.assertIn(f"this product's schema head {head_revision()}", self.steps(result)["install"][1])

    def test_a_data_directory_inside_the_live_root_is_refused_before_anything_is_written(self):
        self.relocate_data_dir("data")
        target = self.fixture.target

        for prepared in ("absent", "empty"):
            with self.subTest(target=prepared):
                if prepared == "empty":
                    target.mkdir()
                result = self.recover()

                self.assertEqual(result.status, "failed", result.render())
                refusal = self.steps(result)["install"][1]
                self.assertIn(f"data directory {target / 'data'} inside the live root {target}", refusal)
                self.assertIn("not supported, nothing was written", refusal)
                if prepared == "absent":
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(list(target.iterdir()), [])
                self.assertFalse(self.fixture.data_dir.exists())
                self.assertEqual(self.leftovers(), [])

    def test_a_divergent_non_empty_live_root_is_refused_and_left_as_it_was(self):
        target = self.fixture.target
        (target / "persona").mkdir(parents=True)
        (target / "persona" / "rules.md").write_text("mine\n", encoding="utf-8")
        (target / "notes.txt").write_text("keep\n", encoding="utf-8")
        planted = {"persona/rules.md": b"mine\n", "notes.txt": b"keep\n"}

        # Without an `instance.yaml` it is no live root of any snapshot: today's refusal, unread remote.
        with mock.patch.object(installation, "_snapshot_checkout") as probe:
            result = self.recover()
        probe.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertIn("is not a valid instance checkout", self.steps(result)["install"][1])
        self.assertEqual(self.live_files(), planted)

        # With one it is compared with the tip, and a divergence is refused.
        instance_yaml = (self.fixture.source / "instance.yaml").read_bytes()
        (target / "instance.yaml").write_bytes(instance_yaml)
        planted["instance.yaml"] = instance_yaml
        result = self.recover()

        self.assertEqual(result.status, "failed")
        refusal = self.steps(result)["install"][1]
        self.assertIn("is not empty and is not snapshot", refusal)
        self.assertIn("adapters/ummanu.yaml missing", refusal)
        self.assertEqual(self.live_files(), planted)
        self.assertFalse(self.repository.exists())
        self.assertEqual(self.leftovers(), [])

    def test_an_existing_snapshot_repository_the_remote_does_not_extend_is_refused(self):
        self.recover(failures={"_materialize_live_root": InstallError("interrupted after the bare clone")})
        kept = git(self.repository, "rev-parse", SNAPSHOT_REF)

        # Another history: an orphan commit of a valid snapshot tree, force-pushed over the tip.
        work = self.root / "rewrite"
        subprocess.run(["git", "clone", "--quiet", str(self.fixture.remote), str(work)], check=True)
        git(work, "checkout", "--quiet", "--orphan", "rewritten")
        (work / "persona" / "rules.md").write_text("Be loud.\n", encoding="utf-8")
        manifest = json.loads((work / SNAPSHOT_MANIFEST).read_text(encoding="utf-8"))
        manifest["files"]["persona/rules.md"] = hashlib.sha256(b"Be loud.\n").hexdigest()
        (work / SNAPSHOT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        git(work, "add", "-A")
        git(
            work,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "rewrite",
        )
        git(work, "push", "--quiet", "--force", "origin", "rewritten:main")

        result = self.recover()

        self.assertEqual(result.status, "failed")
        self.assertIn("which the remote tip", self.steps(result)["install"][1])
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), kept)
        self.assertFalse(self.fixture.target.exists())

    def test_a_snapshot_on_another_branch_than_main_is_refused(self):
        git(self.fixture.remote, "branch", "-m", "main", "trunk")

        self.assert_refused_before_writing(self.recover(), "the exporter publishes main")


class SnapshotRetryTests(SnapshotRecoverCase):
    def progress(self) -> dict:
        return json.loads((self.fixture.data_dir / "recovery-progress.json").read_text(encoding="utf-8"))

    def assert_recovered(self) -> None:
        tip = self.fixture.tip
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(self.repository, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        tree = git(self.repository, "ls-tree", "-r", "--name-only", "--full-tree", tip).splitlines()
        live = {path for path in self.live_files() if is_exported(path)}
        self.assertEqual(live, {path for path in tree if is_exported(path)})
        self.assertEqual(self.leftovers(), [])
        again = self.recover()
        self.assertEqual(again.status, "ok", again.render())
        steps = self.steps(again)
        for name in ("instance-checkout", "checkpoint", "board", "memory"):
            self.assertEqual(steps[name][0], "unchanged", (name, steps[name]))
        self.assertEqual(self.board.call_count, 1)

    def test_an_interruption_after_the_bare_clone_resumes_to_the_same_end_state(self):
        interrupted = self.recover(
            failures={"_materialize_live_root": InstallError("simulated interruption")}
        )
        self.assertEqual(interrupted.status, "failed")
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), self.fixture.tip)
        self.assertFalse(self.fixture.target.exists())

        resumed = self.recover()

        self.assertEqual(resumed.status, "ok", resumed.render())
        self.assertEqual(self.steps(resumed)["instance-checkout"][0], "changed")
        self.assert_recovered()

    def test_an_interruption_after_materialisation_resumes_with_the_same_identity_and_no_second_import(self):
        interrupted = self.recover(failures={"check_prerequisites": InstallError("simulated interruption")})
        self.assertEqual(interrupted.status, "failed")
        live_before = self.live_files()
        # What bootstrap and the secret store leave in a live root is the host's own, never divergence.
        (self.fixture.target / "board-store.env").write_text("FIXTURE=1\n", encoding="utf-8")
        (self.fixture.target / "board-store.env").chmod(0o600)
        failed_import = self.recover(
            failures={"import_normalized_board": InstallError("simulated interruption")}
        )
        self.assertEqual(failed_import.status, "failed")
        identity = self.progress()["identity"]

        resumed = self.recover()

        self.assertEqual(resumed.status, "ok", resumed.render())
        self.assertEqual(self.steps(resumed)["instance-checkout"][0], "unchanged")
        self.assertIn("reused exporter snapshot", self.steps(resumed)["instance-checkout"][1])
        self.assertEqual(self.steps(resumed)["checkpoint"][0], "unchanged")
        self.assertEqual(self.progress()["identity"], identity)
        self.assertEqual({k: v for k, v in self.live_files().items() if k != "board-store.env"}, live_before)
        self.assert_recovered()
        self.assertEqual(self.progress()["identity"], identity)

    def test_a_fast_forward_of_an_interrupted_snapshot_repository_resumes_on_the_new_tip(self):
        self.recover(failures={"_materialize_live_root": InstallError("simulated interruption")})
        first = git(self.repository, "rev-parse", SNAPSHOT_REF)
        (self.fixture.source / "state" / "knowledge" / "decisions" / "two.md").write_text("# Two\n", "utf-8")
        tip = self.fixture.cut()
        self.assertNotEqual(tip, first)

        result = self.recover()

        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(git(self.repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(self.repository, "cat-file", "blob", SNAPSHOT_BASE_REF), tip)
        self.assertEqual(
            (self.fixture.target / "state" / "knowledge" / "decisions" / "two.md").read_text(
                encoding="utf-8"
            ),
            "# Two\n",
        )

    def test_a_dry_run_on_a_materialised_live_root_writes_nothing(self):
        self.recover(failures={"check_prerequisites": InstallError("simulated interruption")})
        before = (self.live_files(), git(self.repository, "rev-parse", SNAPSHOT_REF))

        result = self.recover(dry_run=True)

        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(self.steps(result)["instance-checkout"][0], "would-change")
        self.assertEqual(self.steps(result)["checkpoint"][0], "would-change")
        self.assertEqual((self.live_files(), git(self.repository, "rev-parse", SNAPSHOT_REF)), before)
        self.assertEqual(self.leftovers(), [])


class LegacyShapeTests(unittest.TestCase):
    def test_a_remote_tip_without_a_manifest_takes_the_legacy_clone_step(self):
        # A bare remote, and a work tree named as the remote (as tests/test_secret_recover.py does).
        for bare in (True, False):
            with self.subTest(bare=bare), tempfile.TemporaryDirectory(prefix="legacy-shape-") as temporary:
                root = Path(temporary)
                source, target, data = root / "source", root / "instance", root / "data"
                source.mkdir()
                _checkpoint(source, data)
                _git(source, "init", "-b", "main")
                _git(source, "add", ".")
                _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "x")
                remote = source
                if bare:
                    remote = root / "instance.git"
                    subprocess.run(
                        ["git", "clone", "--quiet", "--bare", str(source), str(remote)], check=True
                    )
                legacy_clone = mock.Mock(wraps=installation._clone_or_reuse)

                with (
                    mock.patch.object(installation, "_clone_or_reuse", legacy_clone),
                    mock.patch.object(installation, "check_prerequisites"),
                    mock.patch.object(installation, "import_normalized_board", return_value=1),
                    mock.patch.object(installation, "rebuild_memory_index", return_value=1),
                    mock.patch.object(
                        installation, "materialize_host", return_value=SimpleNamespace(steps=[])
                    ),
                    mock.patch.object(
                        installation,
                        "materialize_pipeline_state",
                        return_value=SimpleNamespace(records=0, changed=False),
                    ),
                    mock.patch.object(installation, "provision_project_checkouts", return_value=[]),
                    mock.patch.object(installation, "provision_codex_home", return_value=0),
                    mock.patch.object(installation, "mark_reconcile_applied"),
                    mock.patch.object(installation, "restore_findings", return_value=[]),
                ):
                    result = installation.install(_args(SimpleNamespace(target=target, remote=remote)))

                self.assertEqual(result.status, "ok", result.render())
                legacy_clone.assert_called_once()
                self.assertEqual(
                    {step.name: step.detail for step in result.steps}["instance-checkout"],
                    "cloned private instance remote",
                )
                self.assertTrue((target / ".git").is_dir())
                self.assertTrue((target / "state" / "board" / "cards.ndjson").is_file())
                self.assertFalse((data / "backup").exists())
                self.assertEqual(
                    sorted(p.name for p in root.iterdir() if p.name.startswith(".instance.")), []
                )

    def test_a_work_tree_target_never_reads_the_remote_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git").mkdir(parents=True)
            with (
                mock.patch.object(installation, "_snapshot_checkout") as probe,
                mock.patch.object(installation, "_clone_or_reuse", side_effect=InstallError("legacy step")),
            ):
                result = installation.install(_args(SimpleNamespace(target=target, remote="remote")))

            probe.assert_not_called()
            self.assertIn("legacy step", {step.name: step.detail for step in result.steps}["install"])

    def test_a_fresh_install_and_an_offline_dry_run_never_read_the_remote_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            for overrides in ({"recover": False}, {"dry_run": True}):
                with (
                    self.subTest(**overrides),
                    mock.patch.object(installation, "_snapshot_checkout") as probe,
                    mock.patch.object(installation, "_ensure_installation_user"),
                    mock.patch.object(
                        installation, "_clone_or_reuse", side_effect=InstallError("legacy step")
                    ),
                ):
                    installation.install(_args(SimpleNamespace(target=target, remote="remote"), **overrides))
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
