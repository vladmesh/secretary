"""Disposable Git repositories and simulated scope owners, including public maintenance."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.cli import run_residue_maintenance
from secretary.dispatch.cleanup import CleanupJournal, CleanupOwner, ownership_lock
from secretary.dispatch.host import CommandHostRuntime
from secretary.dispatch.observer import ObserverRecord
from secretary.dispatch.production import _reconcile_production
from secretary.dispatch.state import DispatcherRecord
from secretary.dispatch.types import HostError
from secretary.infra import git_worktree
from secretary.observer_root import observer_root_repo
from secretary.runtime.head import HeadRun, HeadSpec, StopInitiator, TaskRef
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from tests.fakes.dispatcher import FakeCatalog, FakeHost
from tests.production_runtime_fixtures import registered_production_runtime


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class OwnedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Cleanup Test")
        git(self.repo, "config", "user.email", "cleanup@example.invalid")
        (self.repo / "file").write_text("base\n")
        (self.repo / ".gitignore").write_text("ignored\nTASK.md\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--quiet", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.task = {"id": "task-1", "ref": "sample-1", "project": "sample", "sprint": "sprint:1",
                     "state": "done", "closed": False, "claim": {"worker": None, "claimed_at": None},
                     "workspace": {"base_branch": None}}
        self.tasks = {self.task["ref"]: self.task}
        self.workspace = self.data / "workspaces" / "sample" / "sample-1-worker"
        self.workspace.parent.mkdir(parents=True)
        git(self.repo, "worktree", "add", "-b", "pipeline/sample-1", str(self.workspace), "main")
        self.record = DispatcherRecord(worker="sample-1-worker", workspace=str(self.workspace), handle="",
                                       head="test", review_head="test", attempt_id="attempt-1",
                                       comment_baseline=0, review_baseline=0, state="assessment", claimed_at=1)
        self.stops = []
        self.stop_failure = False
        self.backend = SimpleNamespace(stop=self.stop)
        self.catalog = SimpleNamespace(bindings={"sample": {"repo": str(self.repo), "default_branch": "main"}})
        self.catalog.binding = lambda project: self.catalog.bindings[project]
        self.host = SimpleNamespace(head_runtime_for=lambda run: self.backend,
                                    _decide_workspace_environment_ownership=lambda path: "absent")
        self.runtime = SimpleNamespace(data_dir=self.data, catalog=self.catalog, host=self.host,
                                       reader=SimpleNamespace(show=lambda ref: copy.deepcopy(self.tasks[ref])),
                                       writer=SimpleNamespace(settle_cleanup_claim=self.settle_claim),
                                       audit=SimpleNamespace(events=lambda ref: [{"kind": "claimed"}]))
        self.owner = CleanupOwner(self.runtime)

    def stop(self, run, initiator):
        self.stops.append((run.run_id, run.scope_generation))
        return SimpleNamespace(ok=not self.stop_failure, reason="simulated stop failure",
                               run=run.finishing(initiator).exited())

    def settle_claim(self, task, worker):
        self.assertEqual(task["claim"]["worker"], worker)
        self.tasks[task["ref"]]["claim"] = {"worker": None, "claimed_at": None}

    def head(self, role="worker", generation="generation-1"):
        run = HeadRun(run_id="run-" + role, spec=HeadSpec(profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
                      workspace=str(self.workspace), task_ref=TaskRef.card(self.task["ref"]),
                      role=role, scope_generation=generation)
        setattr(self.record, "worker_head_run" if role == "worker" else "review_head_run", run.to_json())
        return run

    def request(self, disposition="done"):
        self.owner.remember(self.task, self.record)
        return self.owner.journal.request(self.task, disposition, self.record.to_json())

    def state(self, records):
        path = self.data / "dispatcher" / "production-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"records": records}))

    def observer(self, *, committed=False):
        repo = observer_root_repo(self.data)
        repo.mkdir(parents=True)
        git(repo, "init", "--quiet", "--initial-branch=observers")
        git(repo, "config", "user.name", "Cleanup Test")
        git(repo, "config", "user.email", "cleanup@example.invalid")
        git(repo, "commit", "--quiet", "--allow-empty", "-m", "root")
        path = self.data / "workspaces" / "observers" / "sprint-1"
        path.parent.mkdir(parents=True)
        git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        if committed:
            (path / "NOTES.md").write_text("retained user notes\n")
            git(path, "add", "NOTES.md")
            git(path, "commit", "--quiet", "-m", "user notes")
        self.host.observer_workspace = lambda ref: str(path)
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {
            "id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        run = HeadRun(run_id="observer-run", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        return repo, path, ObserverRecord(sprint="sprint:1", generation="observer-gen",
                                         workspace=str(path), head_possible=True, head_run=run.to_json())

    def maintenance(self, *, expected_exit=0):
        self.runtime.cleanup = self.owner
        args = argparse.Namespace(instance="unused", residue_replay=True, residue_inventory=False, limit=20)
        with mock.patch("secretary.dispatch.bootstrap.runtime_from_args", return_value=self.runtime), mock.patch("builtins.print") as output:
            self.assertEqual(run_residue_maintenance(args), expected_exit)
        return json.loads(output.call_args.args[0])

    def interrupt_git_directory_removal(self):
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        def interrupted(args, **kwargs):
            if args[3:5] == ["worktree", "remove"]:
                self.assertTrue(self.owner.journal.read()["intents"][key]["progress"]["removal_started"])
                shutil.rmtree(self.workspace)  # Simulate Git's first effect in this disposable repo.
                raise KeyboardInterrupt("Git interrupted before admin removal")
            return native(args, **kwargs)
        native = subprocess.run
        with mock.patch("secretary.infra.git_worktree.subprocess.run", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        self.assertFalse(self.workspace.exists())
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        return key

    def test_detached_unpublished_observer_commit_keeps_clean_checkout_and_notes(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        self.assertEqual(git(repo, "for-each-ref", "--contains=" + tip), "")
        self.assertEqual(git(path, "status", "--porcelain"), "")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertEqual((path / "NOTES.md").read_text(), "retained user notes\n")
        self.assertEqual(git(path, "rev-parse", "HEAD"), tip)
        self.assertIn(str(path), git(repo, "worktree", "list", "--porcelain"))

    def test_detached_published_observer_commit_has_retaining_ref_after_removal(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/remotes/origin/notes", tip)
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(repo, "rev-parse", "refs/remotes/origin/notes"), tip)
        self.assertEqual(result["commit_proof"]["refs"], ["refs/remotes/origin/notes"])

    def test_detached_observer_local_user_ref_is_retention_without_publication(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/heads/user-notes", tip)
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(path.exists())
        self.assertEqual(git(repo, "rev-parse", "refs/heads/user-notes"), tip)

    def test_observer_existing_empty_root_branch_retains_disposable_head(self):
        repo, path, observer = self.observer()
        tip = git(repo, "rev-parse", "refs/heads/observers")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())
        self.assertEqual(git(repo, "rev-parse", "refs/heads/observers"), tip)
        self.assertEqual(result["commit_proof"]["publication"], "owned observer root")

    def test_observer_changed_root_branch_does_not_adopt_unpublished_user_commit(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/heads/observers", tip)
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(path.exists())

    def test_detached_observer_without_existing_root_ref_is_preserved(self):
        repo, path, observer = self.observer()
        git(repo, "update-ref", "-d", "refs/heads/observers")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(path.exists())

    def test_public_maintenance_cannot_readopt_changed_recorded_ref(self):
        key = self.request("archive")
        self.task["closed"] = True
        remove = self.owner._remove_workspace
        def interrupted(intent, repo):
            remove(intent, repo)
            raise KeyboardInterrupt("crash before ref settlement")
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        (self.repo / "file").write_text("replacement published work\n")
        git(self.repo, "commit", "--quiet", "-am", "replacement")
        newer = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", newer)
        git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", newer)
        for _ in range(2):
            result = self.maintenance()
            self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), newer)
            self.assertEqual([r["status"] for r in result["replay"]], ["preserved"])
            self.assertIn("recorded ownership conflicts", result["residue"][0]["reason"])
            provenance = result["residue"][0]["recorded_owners"]
            self.assertEqual(provenance[0]["attempt_id"], "attempt-1")
            self.assertEqual(provenance[0]["identity"]["tip"], self.base)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_public_maintenance_keeps_replacement_after_unadmitted_disappearance(self):
        key = self.request("archive")
        self.task["closed"] = True
        git(self.repo, "worktree", "remove", str(self.workspace))
        (self.repo / "file").write_text("replacement\n")
        git(self.repo, "commit", "--quiet", "-am", "replacement")
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", tip)
        git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", tip)
        result = self.maintenance(expected_exit=1)
        self.assertIn("recorded ownership conflicts", result["residue"][0]["reason"])
        self.assertEqual([r["status"] for r in result["replay"]], ["pending"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), tip)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_public_maintenance_does_not_replace_unproven_recorded_attempt(self):
        key = self.owner.journal.remember(self.task, self.record.to_json(), disposition="archive")
        self.task["closed"] = True
        git(self.repo, "worktree", "remove", str(self.workspace))
        result = self.maintenance()
        self.assertIn("recorded ownership conflicts", result["residue"][0]["reason"])
        self.assertEqual([r["status"] for r in result["replay"]], ["preserved"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_public_maintenance_reuses_exact_pending_owner_and_retries(self):
        self.head(generation="")
        key = self.request("archive")
        self.task["closed"] = True
        with mock.patch("secretary.dispatch.cleanup.git_worktree.remove", return_value=False):
            result = self.maintenance(expected_exit=1)
        self.assertEqual([r["status"] for r in result["replay"]], ["pending"])
        self.assertEqual(result["residue"][0]["cleanup_ids"], [key])
        result = self.maintenance()
        self.assertEqual([r["status"] for r in result["replay"]], ["completed"])
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.maintenance()["replay"], [])

    def test_public_maintenance_reuses_owned_attempt_before_archival(self):
        self.head()
        key = self.owner.remember(self.task, self.record)
        self.task["closed"] = True
        self.assertEqual([r["status"] for r in self.maintenance()["replay"]], ["completed"])
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def assert_observer_waits_for_failure(self, failure):
        self.head(generation="")
        self.task["claim"]["worker"] = self.record.worker
        key = self.request("close")
        _, path, observer = self.observer()
        with failure:
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertTrue(result["progress"]["heads_stopped"])
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertTrue(path.exists())
        # The exact observer head stops now; only its workspace and completion wait for the card.
        self.assertIn(("observer-run", ""), self.stops)
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(result["progress"]["awaits_cards"], [key])
        self.owner.replay()
        card = self.owner.journal.read()["intents"][key]
        self.assertEqual(card["status"], "completed", card["reason"])
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())
        self.assertIn(("observer-run", ""), self.stops)

    def test_observer_waits_for_refused_card_removal_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch(
            "secretary.dispatch.cleanup.git_worktree.remove", return_value=False))

    def test_observer_waits_for_unreadable_card_git_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch.object(
            self.owner, "_dirty", side_effect=HostError("Git evidence unreadable")))

    def test_observer_waits_for_failed_card_ref_settlement_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch.object(
            self.owner, "_delete_branch", side_effect=HostError("ref transaction refused")))

    def test_observer_waits_for_failed_card_claim_settlement_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch.object(
            self.runtime.writer, "settle_cleanup_claim", side_effect=HostError("claim write refused")))

    def test_observer_waits_for_unverified_preservation_despite_settled_heads(self):
        key = self.owner.journal.remember(self.task, self.record.to_json(), disposition="close")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertTrue(result["progress"]["claim_settled"])
        self.assertFalse(result["progress"]["preservation_verified"])
        _, path, observer = self.observer()
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(path.exists())
        self.assertEqual(self.stops, [("observer-run", "")])
        self.assertEqual(result["progress"]["awaits_cards"], [key])

    def test_observer_can_follow_verified_dirty_card_preservation(self):
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        (self.workspace / "notes").write_text("user notes")
        key = self.request("close")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(result["progress"]["preservation_verified"])
        _, path, observer = self.observer()
        self.assertEqual(self.owner.cleanup_observer(observer)["status"], "completed")
        self.assertFalse(path.exists())
        self.assertEqual((self.workspace / "notes").read_text(), "user notes")
        self.assertIsNone(self.task["claim"]["worker"])

    def test_partial_git_directory_before_admin_removal_recovers_and_repeats(self):
        key = self.interrupt_git_directory_removal()
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertTrue(result["progress"]["workspace_removed"])
        self.assertNotIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "for-each-ref", "refs/heads/pipeline/"), "")
        self.assertIsNone(self.task["claim"]["worker"])
        self.assertEqual(CleanupOwner(self.runtime).replay_one(key), result)

    def test_missing_registered_directory_without_admission_is_not_adopted(self):
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        shutil.rmtree(self.workspace)
        for _ in range(2):
            result = self.owner.replay_one(key)
            self.assertEqual(result["status"], "pending", result["reason"])
            self.assertIn("no admitted removal proof", result["reason"])
            self.assertFalse(result["progress"].get("removal_started"))
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_rejects_substituted_admin_identity(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        old = admin.with_name(admin.name + "-original")
        admin.rename(old)
        shutil.copytree(old, admin)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("admin identity changed", result["reason"])
        self.assertTrue(admin.exists())
        self.assertFalse(result["progress"].get("workspace_removed"))
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_rejects_changed_admin_path_mapping(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        (admin / "gitdir").write_text(str(self.root / "foreign" / ".git") + "\n")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertTrue(admin.exists())
        self.assertFalse(result["progress"].get("workspace_removed"))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)

    def test_partial_removal_rejects_changed_admin_head_and_retains_claim(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        (admin / "HEAD").write_text(self.base + "\n")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("registration changed", result["reason"])
        self.assertTrue(admin.exists())
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_changed_ref_retains_replacement_and_claim(self):
        key = self.interrupt_git_directory_removal()
        (self.repo / "file").write_text("replacement work\n")
        git(self.repo, "commit", "--quiet", "-am", "replacement")
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", tip)
        git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", tip)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("HEAD changed", result["reason"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), tip)
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_revalidates_at_shared_native_effect(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        calls = []
        def capture(args, label):
            calls.append(args)
            if args[3:5] == ["worktree", "list"]:
                (admin / "HEAD").write_text(self.base + "\n")
            return subprocess.run(args, capture_output=True, text=True)
        self.host.run_capture = capture
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("registration changed", result["reason"])
        self.assertFalse(any(args[3:5] == ["worktree", "remove"] for args in calls))
        self.assertTrue(admin.exists())
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_unreadable_registration_retries_without_settlement(self):
        key = self.interrupt_git_directory_removal()
        with mock.patch("secretary.dispatch.cleanup._registered", side_effect=HostError("registration unreadable")):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)
        self.assertEqual(self.owner.replay()[0]["status"], "completed")

    def test_shared_primitive_missing_directory_needs_owner_proof(self):
        self.interrupt_git_directory_removal()
        def run(args, cwd):
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        self.assertFalse(git_worktree.remove(run, self.repo, self.workspace))
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))

    def test_partial_removal_rejects_replacement_workspace_before_native_effect(self):
        key = self.interrupt_git_directory_removal()
        self.workspace.mkdir()
        (self.workspace / "NOTES.md").write_text("replacement user notes")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertEqual((self.workspace / "NOTES.md").read_text(), "replacement user notes")
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_rejects_symlink_admin_substitution(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        original = self.root / "retained-admin"
        admin.rename(original)
        admin.symlink_to(original, target_is_directory=True)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("substituted", result["reason"])
        self.assertTrue(admin.is_symlink())
        self.assertTrue(original.exists())
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_lost_publication_stays_pending_with_registration(self):
        key = self.interrupt_git_directory_removal()
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertFalse(result["progress"].get("preservation_verified"))
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.assertEqual(self.owner.replay_one(key)["status"], "completed")

    def test_done_removes_merged_workspace_and_exact_local_branch(self):
        self.head()
        self.head("reviewer", "review-generation")
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(len(self.stops), 2)
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")
        self.assertEqual(git(self.repo, "rev-parse", "refs/remotes/origin/main"), self.base)
        self.assertTrue(result["progress"]["claim_settled"])

    def test_archive_request_survives_absent_active_record(self):
        self.head()
        self.owner.remember(self.task, self.record)
        self.task.update(closed=True, state="blocked")
        key = self.owner.journal.request(self.task, "archive")
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(self.workspace.exists())

    def test_close_routes_to_same_owner_and_exposes_progress(self):
        self.owner.remember(self.task, self.record)
        self.task["closed"] = True
        self.owner.journal.request(self.task, "close")
        self.assertEqual(self.owner.replay()[0]["status"], "completed")
        self.assertEqual(self.owner.journal.summary(sprint="sprint:1")[0]["disposition"], "close")

    def test_crash_after_git_removal_before_ref_or_claim_settlement(self):
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        remove = self.owner._remove_workspace
        def interrupted(intent, repo):
            remove(intent, repo)
            raise KeyboardInterrupt("crash after successful Git removal")
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        self.assertFalse(self.workspace.exists())
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertIsNone(self.task["claim"]["worker"])

    def test_crash_after_exact_ref_deletion_replays_its_admitted_proof(self):
        key = self.request()
        delete = self.owner._delete_branch
        def interrupted(intent, repo, base):
            delete(intent, repo, base)
            raise KeyboardInterrupt("crash after successful ref transaction")
        with mock.patch.object(self.owner, "_delete_branch", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        self.assertEqual(CleanupOwner(self.runtime).replay_one(key)["status"], "completed")

    def test_prior_attempts_reusing_the_same_workspace_reconcile_after_later_cleanup(self):
        self.owner.remember(self.task, self.record)
        self.record.attempt_id = "attempt-2"
        self.assertEqual(self.owner.cleanup(self.task, self.record, "done")["status"], "completed")
        self.task["closed"] = True
        self.owner.journal.request(self.task, "close")
        results = self.owner.replay()
        self.assertEqual([item["status"] for item in results], ["completed"])

    def test_stop_failure_is_durable_and_automatically_retryable(self):
        self.head()
        self.stop_failure = True
        key = self.request()
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(self.workspace.exists())
        self.stop_failure = False
        self.assertEqual(CleanupOwner(self.runtime).replay()[0]["status"], "completed")

    def test_removal_failure_is_not_a_clean_receipt(self):
        key = self.request()
        with mock.patch("secretary.dispatch.cleanup.git_worktree.remove", return_value=False):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(self.workspace.exists())
        self.assertEqual(self.owner.replay()[0]["status"], "completed")

    def test_tracked_untracked_and_ignored_work_are_preserved(self):
        for name in ("file", "untracked", "ignored"):
            with self.subTest(name=name):
                path = self.workspace / name
                path.write_text("author work")
                result = self.owner.cleanup(self.task, self.record, "done")
                self.assertEqual(result["status"], "preserved", result["reason"])
                self.assertTrue(path.exists())
                if name == "file":
                    git(self.workspace, "restore", "file")
                else:
                    path.unlink()

    def test_generated_prompt_requires_exact_bytes(self):
        path = self.workspace / "TASK.md"
        path.write_text("generated")
        self.owner.journal.generated(path, "generated")
        path.write_text("user notes")
        key = self.request()
        self.assertEqual(self.owner.replay_one(key)["status"], "preserved")
        path.write_text("generated")
        self.assertEqual(self.owner.replay_one(key)["status"], "completed")

    def test_interrupted_owned_environment_deletion_retains_its_exact_proof(self):
        namespace = self.workspace / ".secretary-task-env"
        namespace.mkdir()
        (namespace / "owner.json").write_text("dispatcher-owned")
        (namespace / "venv-file").write_text("generated")
        (self.repo / ".git" / "info" / "exclude").write_text(".secretary-task-env/\n")
        def ownership(path):
            root = Path(path) / ".secretary-task-env"
            if not root.exists():
                return "absent"
            if not (root / "owner.json").exists():
                raise HostError("environment owner unavailable")
            return "dispatcher"
        self.host._decide_workspace_environment_ownership = ownership
        key = self.request()
        def interrupted(path):
            (Path(path) / "owner.json").unlink()
            raise KeyboardInterrupt("interrupted environment removal")
        with mock.patch("secretary.dispatch.cleanup.shutil.rmtree", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(self.workspace.exists())

    def test_unpublished_commits_preserve_workspace_and_ref(self):
        (self.workspace / "file").write_text("unpublished")
        git(self.workspace, "commit", "-am", "unpublished")
        tip = git(self.workspace, "rev-parse", "HEAD")
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(self.workspace.exists())
        self.assertEqual(git(self.repo, "rev-parse", "pipeline/sample-1"), tip)

    def test_published_unmerged_ref_is_retained_at_exact_tip(self):
        (self.workspace / "file").write_text("candidate")
        git(self.workspace, "commit", "-am", "candidate")
        tip = git(self.workspace, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/pipeline/sample-1", tip)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(git(self.repo, "rev-parse", "pipeline/sample-1"), tip)

    def test_changed_head_and_branch_ref_are_preserved(self):
        key = self.request()
        (self.workspace / "file").write_text("changed")
        git(self.workspace, "commit", "-am", "changed")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(self.workspace.exists())

    def test_foreign_claim_and_replacement_precede_every_effect(self):
        self.head()
        key = self.request()
        self.task["claim"]["worker"] = "new-worker"
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertEqual(self.stops, [])
        self.task["claim"]["worker"] = None
        replacement = self.record.to_json()
        replacement["attempt_id"] = "new-attempt"
        self.state({self.task["ref"]: replacement})
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_replaced_directory_and_symlink_are_refused(self):
        key = self.request()
        saved = self.workspace.with_name("retained")
        self.workspace.rename(saved)
        self.workspace.symlink_to(saved, target_is_directory=True)
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(saved.exists())

    def test_registration_substitution_is_refused(self):
        key = self.request()
        (self.workspace / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(self.workspace.exists())

    def test_shared_git_removal_refuses_foreign_registration_and_ignored_files(self):
        from secretary.infra.git_worktree import remove
        def capture(args, cwd):
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        (self.workspace / "ignored").write_text("author work")
        self.assertFalse(remove(capture, self.repo, self.workspace))
        (self.workspace / "ignored").unlink()
        (self.workspace / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
        self.assertFalse(remove(capture, self.repo, self.workspace))
        self.assertTrue(self.workspace.exists())

    def test_deployed_unscoped_head_uses_its_runtime_stop_and_identity_fence(self):
        self.head(generation="")
        seen = []
        self.host._guard_head_run = lambda run, role, **kwargs: seen.append(run.run_id)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(seen, ["run-worker"])
        self.assertEqual(self.stops, [("run-worker", "")])

    def test_foreign_workspace_and_missing_proof_are_reported(self):
        self.record.workspace = str(self.repo)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(self.repo.exists())

    def test_missing_git_proof_still_settles_the_exact_recorded_head(self):
        self.head()
        self.record.workspace = str(self.repo)
        self.record.worker_head_run["workspace"] = str(self.repo)
        result = self.owner.cleanup(self.task, self.record, "inactive")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(self.stops, [("run-worker", "generation-1")])
        self.assertTrue(self.repo.exists())

    def test_unscoped_stop_receipt_survives_crash_and_replay_without_a_second_stop(self):
        run = self.head(generation="")
        def stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            return SimpleNamespace(ok=True, reason="", run=run.finishing(initiator).exited())
        self.backend.stop = stop
        key = self.request()
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        saved = self.owner.journal.read()["intents"][key]
        self.assertTrue(HeadRun.from_json(saved["heads"][-1]).settled)
        self.assertEqual(CleanupOwner(self.runtime).replay_one(key)["status"], "completed")
        self.assertEqual(self.stops, [(run.run_id, "")])

    def test_a_stop_receipt_for_another_generation_never_authorizes_git_effects(self):
        from dataclasses import replace
        run = self.head()
        settled = replace(run.finishing(StopInitiator(actor="test")).exited(), scope_generation="replacement")
        self.backend.stop = lambda *args: SimpleNamespace(ok=True, reason="", run=settled)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertIn("does not settle", result["reason"])
        self.assertTrue(self.workspace.exists())

    def test_a_bare_ok_stop_without_a_settled_run_is_pending(self):
        self.head()
        self.backend.stop = lambda *args: SimpleNamespace(ok=True)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["progress"].get("heads_stopped", False))
        self.assertTrue(self.workspace.exists())

    def test_a_settled_scoped_run_still_reaches_the_runtime_empty_proof_on_replay(self):
        self.head()
        key = self.request()
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        self.stop_failure = True
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertIn("has not verified", self.owner.journal.admission_refusal(self.task["ref"]))
        self.assertEqual(self.stops, [("run-worker", "generation-1"), ("run-worker", "generation-1")])
        self.assertTrue(self.workspace.exists())

    def test_unreadable_journal_never_adopts_residue(self):
        self.request()
        self.owner.journal.path.write_text("unreadable")
        with self.assertRaises(HostError):
            self.owner.replay()
        self.assertTrue(self.workspace.exists())

    def test_inventory_and_maintenance_replay_archived_branch_only_for_both_projects(self):
        git(self.repo, "worktree", "remove", str(self.workspace))
        second = self.root / "instance"
        git(self.root, "clone", "--quiet", str(self.repo), str(second))
        git(second, "branch", "pipeline/instance-2")
        self.catalog.bindings["instance"] = {"repo": str(second), "default_branch": "main"}
        self.tasks["instance-2"] = {**self.task, "id": "task-2", "ref": "instance-2", "project": "instance", "closed": True}
        self.task["closed"] = True
        inventory = self.owner.inventory()
        self.assertEqual(len(inventory["residue"]), 2)
        self.assertFalse(self.owner.journal.path.exists())
        args = argparse.Namespace(instance="unused", residue_replay=True, residue_inventory=False, limit=20)
        self.runtime.cleanup = self.owner
        with mock.patch("secretary.dispatch.bootstrap.runtime_from_args", return_value=self.runtime), mock.patch("builtins.print") as output:
            args.residue_replay = False
            args.residue_inventory = True
            self.assertEqual(run_residue_maintenance(args), 0)
        self.assertEqual(len(json.loads(output.call_args.args[0])["residue"]), 2)
        self.assertFalse(self.owner.journal.path.exists(), "public inventory must remain read-only")
        args.residue_replay = True
        args.residue_inventory = False
        with mock.patch("secretary.dispatch.bootstrap.runtime_from_args", return_value=self.runtime), mock.patch("builtins.print") as output:
            self.assertEqual(run_residue_maintenance(args), 0)
        rendered = json.loads(output.call_args.args[0])
        self.assertEqual([x["status"] for x in rendered["replay"]], ["completed", "completed"])
        self.assertEqual(git(second, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")

    def test_old_archived_worktree_without_runtime_proof_is_preserved(self):
        self.task["closed"] = True
        result = self.owner.inventory(catch_up=True)
        self.assertIn("historical worktree", result["residue"][0]["reason"])
        self.assertTrue(self.workspace.exists())
        self.assertEqual(self.owner.replay(), [])

    def test_newer_scope_owner_is_fenced_before_any_stop(self):
        from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        self.head()
        key = self.request()
        root = self.root / "heads"
        run_dir = root / "other-run"
        run_dir.mkdir(parents=True)
        ScopedHeadLifecycle("other-run", 128, generation="new-generation").persist(
            run_dir, role="worker", task="card:sample-1", workspace=str(self.workspace))
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("newer scope", result["reason"])
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_foreign_reviewer_heartbeat_refuses_before_worker_stop(self):
        self.head()
        self.head("reviewer")
        def guard(run, role, **kwargs):
            if role == "reviewer":
                raise HostError("reviewer heartbeat has mismatching identity")
        self.host._guard_head_run = guard
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_unreadable_scope_evidence_is_pending(self):
        key = self.request()
        root = self.root / "heads"
        run_dir = root / "unknown"
        run_dir.mkdir(parents=True)
        (run_dir / "scope-owner.json").write_text("unreadable")
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(self.workspace.exists())

    def test_scope_binding_and_path_substitution_refuse_before_stop(self):
        from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        run = self.head()
        root = self.root / "heads"
        directory = root / run.run_id
        directory.mkdir(parents=True)
        owner = ScopedHeadLifecycle(run.run_id, 128, generation=run.scope_generation)
        owner.persist(directory, role="worker", task="card:foreign", workspace=str(self.workspace))
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        key = self.request()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("binding differs", result["reason"])
        evidence = owner.read_owner(directory)
        evidence["task"] = "card:sample-1"
        owner.update_owner(directory, evidence)
        owner_path = directory / "scope-owner.json"
        owner_path.rename(directory / "borrowed.json")
        owner_path.symlink_to(directory / "borrowed.json")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_unknown_terminal_scope_needs_supported_native_disappearance_proof(self):
        from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        root = self.root / "heads"
        directory = root / "old-run"
        directory.mkdir(parents=True)
        ScopedHeadLifecycle("old-run", 128, generation="old").persist(
            directory, role="worker", task="card:sample-1", workspace=str(self.workspace))
        record = ScopedHeadLifecycle.read_owner(directory)
        record.update(launch_allowed=False, cleanup_complete=True)
        ScopedHeadLifecycle.update_owner(directory, record)
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        key = self.request()
        with mock.patch("secretary.runtime.local_pty_head.runtime_scope_inventory", return_value=SimpleNamespace(
                errors={}, disappeared=set())):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(self.workspace.exists())
        with mock.patch("secretary.runtime.local_pty_head.runtime_scope_inventory", return_value=SimpleNamespace(
                errors={}, disappeared={record["unit"]})):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])

    def test_replacement_admission_refuses_unsettled_old_heads(self):
        self.head()
        self.stop_failure = True
        self.owner.cleanup(self.task, self.record, "done")
        self.assertIn("has not verified head settlement", self.owner.journal.admission_refusal(self.task["ref"]))
        self.stop_failure = False
        self.owner.replay()
        self.assertEqual(self.owner.journal.admission_refusal(self.task["ref"]), "")

    def test_production_probe_cannot_replay_or_capture_durable_cleanup(self):
        from secretary.dispatch.production import _probe_runtime, ProbeAbort
        self.request()
        before = self.owner.journal.path.read_bytes()
        self.runtime.cleanup = self.owner
        self.runtime.production_state = SimpleNamespace()
        self.runtime.po = SimpleNamespace()
        probe = _probe_runtime(self.runtime)
        with self.assertRaises(ProbeAbort):
            probe.cleanup.replay()
        with self.assertRaises(ProbeAbort):
            probe.cleanup.cleanup(self.task, self.record, "inactive")
        self.assertEqual(self.owner.journal.path.read_bytes(), before)
        self.assertTrue(self.workspace.exists())

    def test_bounded_replay_rotates_past_persistent_failures(self):
        first = self.request()
        value = self.owner.journal.read()
        other = copy.deepcopy(value["intents"][first])
        second = "0" * 64 if first != "0" * 64 else "f" * 64
        value["intents"][second] = other
        self.owner.journal.save(value)
        with mock.patch.object(self.owner, "replay_one", return_value=other) as replay:
            self.owner.replay(limit=1)
            self.owner.replay(limit=1)
        self.assertEqual(set(call.args[0] for call in replay.call_args_list), {first, second})

    def test_tip_change_between_merge_proof_and_actual_deletion_is_fenced(self):
        key = self.request()
        original = self.owner._published
        calls = 0
        def change(repo, tip):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = git(self.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "replacement")
                git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", replacement)
            return original(repo, tip)
        with mock.patch.object(self.owner, "_published", side_effect=change):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertNotEqual(git(self.repo, "rev-parse", "pipeline/sample-1"), self.base)

    def test_claim_admission_and_cleanup_share_one_critical_section(self):
        entered = threading.Event()
        release = threading.Event()
        def competing():
            with ownership_lock(self.data):
                entered.set()
            release.set()
        with ownership_lock(self.data):
            thread = threading.Thread(target=competing)
            thread.start()
            self.assertFalse(entered.wait(.05))
            self.assertEqual(self.owner.cleanup(self.task, self.record, "done")["status"], "completed")
        thread.join(2)
        self.assertTrue(release.is_set())

    def test_public_observer_stop_retains_unreadable_registration_and_retries(self):
        host = CommandHostRuntime(
            FakeCatalog(), self.data, mode="real",
            production_runtime=registered_production_runtime(self.root),
        )
        self.runtime.host = host
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {
            "id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        host.cleanup_owner = self.owner
        path = Path(host.observer_workspace("sprint:1"))
        host._create_git_observer_workspace(path)
        run = HeadRun(run_id="observer-run", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        observer = ObserverRecord(sprint="sprint:1", workspace=str(path), head_possible=True,
                                  head_run=run.to_json())
        self.backend.forget_head = mock.Mock()
        with mock.patch.object(host, "head_runtime_for", return_value=self.backend):
            self.stop_failure = True
            with self.assertRaisesRegex(HostError, "stop pending"):
                host.stop_observer(observer)
            self.stop_failure = False
            with mock.patch("secretary.dispatch.cleanup._registered",
                            side_effect=HostError("worktree registrations are unreadable")):
                with self.assertRaisesRegex(HostError, "unreadable"):
                    host.stop_observer(observer)
            intent = next(iter(self.owner.journal.read()["intents"].values()))
            self.assertEqual(intent["status"], "pending")
            self.assertTrue(intent["progress"]["heads_stopped"])
            self.assertTrue(path.is_dir())
            self.assertTrue(host._git_observer_worktree_listed(str(path)))
            self.backend.forget_head.assert_not_called()
            host.stop_observer(observer)
        self.assertFalse(path.exists())
        self.assertFalse(host._git_observer_worktree_listed(str(path)))
        self.assertEqual(self.owner.journal.summary()[0]["status"], "completed")
        self.assertEqual(len(self.stops), 2, "retry reuses the second attempt's settled receipt")
        self.backend.forget_head.assert_called_once_with(run.run_id)

    def test_real_host_inactive_reconciliation_retains_failed_cleanup_and_retries(self):
        host = CommandHostRuntime(
            self.catalog, self.data, mode="real",
            production_runtime=registered_production_runtime(self.root),
        )
        self.runtime.host = host
        self.runtime.cleanup = self.owner
        host.cleanup_owner = self.owner
        self.head(generation="")
        records = {self.task["ref"]: self.record}
        with mock.patch.object(host, "head_runtime_for", return_value=self.backend):
            self.stop_failure = True
            refused = _reconcile_production(self.runtime, records, {}, set())
            self.assertEqual(refused[0]["status"], "pending")
            self.assertIn(self.task["ref"], records)
            self.assertTrue(self.workspace.is_dir())
            self.stop_failure = False
            retried = _reconcile_production(self.runtime, records, {}, set())
        self.assertEqual(retried[0]["status"], "completed")
        self.assertEqual(retried[1]["action"], "record-removed")
        self.assertEqual(records, {})
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.owner.journal.summary()[0]["status"], "completed")

    def test_recording_host_uses_inactive_head_lifecycle_without_claiming_git_ownership(self):
        host = FakeHost(self.data / "recording-workspaces")
        self.runtime.host = host
        self.runtime.cleanup = self.owner
        records = {self.task["ref"]: self.record}
        with mock.patch.object(self.owner, "cleanup") as cleanup:
            outcome = _reconcile_production(self.runtime, records, {}, set())
        cleanup.assert_not_called()
        self.assertEqual(host.calls, ["stop_workspace", "stop"])
        self.assertEqual(outcome[0]["action"], "record-removed")
        self.assertEqual(records, {})
        self.assertTrue(self.workspace.is_dir())
        self.assertFalse(self.owner.journal.path.exists())

    def test_observer_closed_handoff_waits_for_cards_and_preserves_user_work(self):
        self.request("close")
        repo = observer_root_repo(self.data)
        repo.mkdir(parents=True)
        git(repo, "init", "--quiet", "--initial-branch=observers")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "commit", "--quiet", "--allow-empty", "-m", "root")
        path = self.data / "workspaces" / "observers" / "sprint-1"
        path.parent.mkdir(parents=True)
        git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        (path / "NOTES.md").write_text("user notes")
        self.host.observer_workspace = lambda ref: str(path)
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {"id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        observer = ObserverRecord(sprint="sprint:1", generation="observer-gen", workspace=str(path))
        state = self.data / "dispatcher" / "production-state.json"
        state.write_text(json.dumps({"records": {}, "observers": {"sprint:1": observer.to_json()}}))
        handoff = self.owner.journal.observer_handoff({"id": "sprint-1", "ref": "sprint:1"})
        self.assertIsNotNone(handoff)
        self.assertTrue(path.exists(), "close handoff must not execute the observer cleanup")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending")
        self.assertIn("waits for card cleanup", result["reason"])
        self.owner.replay()
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue((path / "NOTES.md").exists())

    # Close ownership and observer exit (secretary-1917).

    def preserved_done_intent(self):
        """The 1904 shape: an exact done attempt verified-preserved for dirty work."""
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        (self.workspace / "notes").write_text("user notes")
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.task["closed"] = True
        return next(iter(self.owner.journal.read()["intents"]))

    def legacy_close_intent(self):
        """The shape the old request staged with no record: no attempt, identity or head."""
        return self.owner.journal.remember(self.task, {}, disposition="close")

    def test_close_reuses_verified_preserved_done_intent(self):
        key = self.preserved_done_intent()
        before = copy.deepcopy(self.owner.journal.read()["intents"][key])
        self.assertEqual(self.owner.journal.request(self.task, "close"), key)
        intents = self.owner.journal.read()["intents"]
        self.assertEqual(list(intents), [key])
        self.assertEqual(intents[key], before)
        _, path, observer = self.observer()
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())

    def test_close_and_archive_do_not_reopen_a_completed_intent(self):
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "completed", result["reason"])
        key = next(iter(self.owner.journal.read()["intents"]))
        self.task["closed"] = True
        for disposition in ("close", "archive"):
            self.assertEqual(self.owner.journal.request(self.task, disposition), key)
        intents = self.owner.journal.read()["intents"]
        self.assertEqual(list(intents), [key])
        self.assertEqual((intents[key]["status"], intents[key]["disposition"]), ("completed", "done"))
        self.assertEqual(self.owner.replay(), [])

    def test_request_stages_new_intent_only_without_any_intent(self):
        self.task["closed"] = True
        key = self.owner.journal.request(self.task, "close")
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])
        self.assertEqual(self.owner.journal.request(self.task, "archive"), key)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_existing_empty_duplicate_converges_to_its_attempt_owner(self):
        owner = self.preserved_done_intent()
        duplicate = self.legacy_close_intent()
        stops = list(self.stops)
        for _ in range(2):
            result = self.owner.replay_one(duplicate)
            expected = self.owner.journal.read()["intents"][owner]
            self.assertEqual((result["status"], result["reason"]), (expected["status"], expected["reason"]))
            self.assertEqual(result["progress"]["settled_by"], [owner])
            self.assertTrue(result["progress"]["preservation_verified"])
            self.assertTrue(result["progress"]["heads_stopped"])
            self.assertTrue(result["progress"]["claim_settled"])
        self.assertEqual(self.stops, stops, "a follower performs no effect of its own")
        self.assertEqual((self.workspace / "notes").read_text(), "user notes")
        _, path, observer = self.observer()
        self.assertEqual(self.owner.cleanup_observer(observer)["status"], "completed")
        self.assertFalse(path.exists())

    def test_empty_duplicate_follows_a_pending_owner_until_it_settles(self):
        self.task["claim"]["worker"] = self.record.worker
        owner = self.request("close")
        self.task["closed"] = True
        duplicate = self.legacy_close_intent()
        with mock.patch("secretary.dispatch.cleanup.git_worktree.remove", return_value=False):
            self.owner.replay_one(owner)
        result = self.owner.replay_one(duplicate)
        self.assertEqual(result["status"], "pending")
        self.assertIn("follows attempt owner " + owner, result["reason"])
        self.owner.replay_one(owner)
        result = self.owner.replay_one(duplicate)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(self.owner.replay(), [])

    def operation_record(self):
        git(self.repo, "worktree", "remove", str(self.workspace))
        git(self.repo, "branch", "-D", "pipeline/sample-1")
        return DispatcherRecord(worker="sample-1-operation", workspace="", handle="", head="", review_head="",
                                attempt_id="attempt-op", comment_baseline=0, review_baseline=0,
                                state="assessment", claimed_at=1)

    def test_operation_card_attempt_without_workspace_completes(self):
        record = self.operation_record()
        self.task.update(closed=True, claim={"worker": record.worker, "claimed_at": None})
        self.state({self.task["ref"]: record.to_json()})
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "pending")
        self.assertIn("awaits release of current record", result["reason"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.state({})
        result = self.owner.replay()[0]
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertIsNone(self.task["claim"]["worker"])
        self.assertEqual(self.stops, [])

    def test_operation_card_attempt_with_pipeline_ref_is_preserved_naming_it(self):
        record = self.operation_record()
        git(self.repo, "branch", "pipeline/sample-1")
        self.task["closed"] = True
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("refs/heads/pipeline/sample-1 exists at " + self.base, result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)

    def test_attempt_without_workspace_but_with_a_head_field_is_not_completed(self):
        record = self.operation_record()
        record.worker_pid_file = "/nonexistent/pid"
        self.task["closed"] = True
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "pending")
        self.assertIn("head ownership is missing", result["reason"])
        record.worker_pid_file = ""
        record.review_head = "reviewer-profile"
        record.attempt_id = "attempt-op-2"
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "preserved")
        self.assertIn("missing exact workspace/attempt ownership proof", result["reason"])
        self.assertFalse(result["progress"]["preservation_verified"])

    def test_legacy_empty_intent_is_verified_preserved_without_effects(self):
        self.task["closed"] = True
        key = self.legacy_close_intent()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("no attempt ownership was recorded", result["reason"])
        self.assertIn("left to the inventory", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(result["heads"], [])
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.is_dir())
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        before = self.owner.journal.path.read_bytes()
        self.owner.replay_one(key)
        self.assertEqual(self.owner.journal.path.read_bytes(), before)
        _, path, observer = self.observer()
        self.assertEqual(self.owner.cleanup_observer(observer)["status"], "completed")

    def test_legacy_empty_intent_waits_for_a_terminal_card(self):
        key = self.legacy_close_intent()
        self.task["state"] = "blocked"
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("closed or Done card", result["reason"])

    def test_legacy_empty_intent_live_pid_file_keeps_it_pending(self):
        self.task["closed"] = True
        key = self.legacy_close_intent()
        pid = self.root / "worker.pid"
        pid.write_text("1")
        with mock.patch("secretary.dispatch.watchdog.pid_file_path", return_value=str(pid)), \
                mock.patch("secretary.runtime.head.identity.head_process_status", return_value={"state": "alive"}):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("pid file " + str(pid) + " names a live or unknown process", result["reason"])
        self.assertFalse(result["progress"]["heads_stopped"])

    def test_legacy_empty_intent_current_record_keeps_it_pending(self):
        self.task["closed"] = True
        key = self.legacy_close_intent()
        self.state({self.task["ref"]: self.record.to_json()})
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("another active owner", result["reason"])
        other = {**self.record.to_json(), "worker": "other-worker", "attempt_id": "other"}
        self.state({"other-1": other})
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("awaits release of current record other-1", result["reason"])
        self.assertTrue(self.workspace.is_dir())

    def test_legacy_empty_intent_foreign_claim_keeps_it_pending(self):
        self.task.update(closed=True, claim={"worker": "foreign-worker", "claimed_at": None})
        key = self.legacy_close_intent()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("claim is foreign", result["reason"])
        self.assertEqual(self.task["claim"]["worker"], "foreign-worker")

    def test_public_observer_stop_exits_while_card_cleanup_is_pending(self):
        host = CommandHostRuntime(
            FakeCatalog(), self.data, mode="real",
            production_runtime=registered_production_runtime(self.root),
        )
        self.runtime.host = host
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {
            "id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        host.cleanup_owner = self.owner
        self.task["claim"]["worker"] = self.record.worker
        card = self.request("close")
        self.task["closed"] = True
        with mock.patch("secretary.dispatch.cleanup.git_worktree.remove", return_value=False):
            self.assertEqual(self.owner.replay_one(card)["status"], "pending")
        path = Path(host.observer_workspace("sprint:1"))
        host._create_git_observer_workspace(path)
        run = HeadRun(run_id="observer-run", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        observer = ObserverRecord(sprint="sprint:1", workspace=str(path), head_possible=True,
                                  head_run=run.to_json())
        self.backend.forget_head = mock.Mock()
        with mock.patch.object(host, "head_runtime_for", return_value=self.backend):
            host.stop_observer(observer)
            self.backend.forget_head.assert_called_once_with(run.run_id)
            self.assertIn(("observer-run", ""), self.stops)
            key = next(k for k, i in self.owner.journal.read()["intents"].items() if i["task"].get("kind") == "observer")
            intent = self.owner.journal.read()["intents"][key]
            self.assertEqual(intent["status"], "pending", "completion is not published early")
            self.assertEqual(intent["progress"]["awaits_cards"], [card])
            self.assertIn(card, intent["reason"])
            self.assertTrue(path.is_dir())
            self.assertEqual(self.owner.replay_one(key)["status"], "pending")
            self.assertTrue(path.is_dir())
            self.assertEqual(self.owner.replay_one(card)["status"], "completed")
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertNotIn("awaits_cards", result["progress"])
        self.assertFalse(path.exists())

    def test_observer_stop_failure_still_raises_while_cards_wait(self):
        self.task["claim"]["worker"] = self.record.worker
        self.request("close")
        _, path, observer = self.observer()
        self.stop_failure = True
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertNotIn("awaits_cards", result["progress"])

    def observer_key(self, record):
        import hashlib
        return hashlib.sha256(("sprint:1:" + record.generation + ":" + str(record.launches)).encode()).hexdigest()

    def replaced_observers(self):
        repo, path, first = self.observer()
        first.launches, first.launched_at = 1, 100.0
        second_run = HeadRun(run_id="observer-run-2", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        second = ObserverRecord(sprint="sprint:1", generation="observer-gen", launches=2, launched_at=200.0,
                                workspace=str(path), head_possible=True, head_run=second_run.to_json())
        state = self.data / "dispatcher" / "production-state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"records": {}, "observers": {"sprint:1": second.to_json()}}))
        fenced = []
        guarded = []
        self.host.fence_cleanup_scopes = lambda workspace, task, runs, recorded_only=False: fenced.append(
            ([r.run_id for r in runs], recorded_only))
        self.host._guard_head_run = lambda run, role, **kwargs: guarded.append(run.run_id)
        return path, first, second, fenced, guarded

    def test_predecessor_observer_intent_cannot_stop_the_replacement(self):
        path, first, _, fenced, guarded = self.replaced_observers()
        result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("replaced by observer-gen:2", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(self.stops, [("observer-run", "")])
        self.assertEqual((fenced, guarded), ([(["observer-run"], True)], []))
        self.assertIsNone(self.owner.journal.read()["intents"][self.observer_key(first)]["identity"])
        self.assertTrue(path.is_dir())

    def test_replacement_observer_intent_cannot_stop_the_predecessor(self):
        path, first, second, fenced, _ = self.replaced_observers()
        result = self.owner.cleanup_observer(second)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(self.stops, [("observer-run-2", "")])
        self.assertEqual(fenced, [(["observer-run-2"], False)])
        self.assertFalse(path.exists())

    def test_replaced_launch_with_settled_run_settles_without_touching_successor(self):
        """The de9a574d shape: launch 1 already stopped, launch 2 current and live."""
        path, first, second, fenced, guarded = self.replaced_observers()
        run = HeadRun.from_json(first.head_run)
        first.head_run = run.finishing(StopInitiator(actor="test")).exited().to_json()
        task = {"id": "sprint-1", "ref": "sprint:1", "sprint": "sprint:1", "project": "observers",
                "kind": "observer", "claim": {}}
        raw = first.to_json()
        raw.update(attempt_id="observer-gen:1", worker="observer-gen")
        key = self.owner.journal.remember(task, raw, disposition="observer-stop")
        handoff = self.owner.journal.remember(task, {**second.to_json(), "attempt_id": "observer-gen:2",
                                                     "worker": "observer-gen"}, disposition="observer-close")
        for _ in range(2):
            result = self.owner.replay_one(key)
            self.assertEqual(result["status"], "preserved", result["reason"])
            self.assertIn("handed to the successor", result["reason"])
        self.assertEqual(self.stops, [])
        self.assertEqual((fenced, guarded), ([(["observer-run"], True)] * 2, []))
        self.assertTrue(path.is_dir())
        # With the current record gone, the later recorded launch is still the successor.
        (self.data / "dispatcher" / "production-state.json").write_text(json.dumps({"records": {}}))
        result = self.owner.replay_one(key)
        self.assertIn("replaced by observer-gen:2", result["reason"])
        self.assertTrue(path.is_dir())
        self.assertEqual(self.owner.journal.read()["intents"][handoff]["status"], "pending")

    def test_heads_list_is_bounded_over_replays_and_remembers(self):
        from dataclasses import replace
        self.head()
        (self.workspace / "notes").write_text("user notes")
        counter = iter(range(1000))
        def stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            # The runtime re-proves a settled scoped run; each receipt differs in a non-key field.
            settled = run if run.settled else run.finishing(initiator).exited()
            return SimpleNamespace(ok=True, reason="", run=replace(settled, handle="h" + str(next(counter))))
        self.backend.stop = stop
        for index in range(5):
            self.record.worker_head_run["handle"] = "view-" + str(index)
            self.owner.remember(self.task, self.record)
        key = self.request("done")
        for _ in range(10):
            self.assertEqual(self.owner.replay_one(key)["status"], "preserved")
        heads = self.owner.journal.read()["intents"][key]["heads"]
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]["lifecycle"], "exited")
        self.assertEqual(len(self.stops), 10)

    def test_oversized_heads_list_compacts_on_next_checkpoint(self):
        worker = self.head().to_json()
        reviewer = self.head("reviewer", "review-generation").to_json()
        key = self.request()
        value = self.owner.journal.read()
        exited = HeadRun.from_json(worker).finishing(StopInitiator(actor="test")).exited().to_json()
        value["intents"][key]["heads"] = ([{**worker, "handle": str(n)} for n in range(200)] + [exited]
                                          + [{**worker, "handle": "stale"}] + [reviewer] * 100)
        self.owner.journal.save(value)
        heads = self.owner.journal.read()["intents"][key]["heads"]
        self.assertEqual([(h["run_id"], h["lifecycle"]) for h in heads],
                         [("run-worker", "exited"), ("run-reviewer", reviewer["lifecycle"])])

    def test_crash_after_stop_before_journal_save_retries_to_same_content(self):
        self.head()
        def stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            return SimpleNamespace(ok=True, reason="", run=run if run.settled else run.finishing(initiator).exited())
        self.backend.stop = stop
        key = self.request()
        stop = self.owner._stop
        def interrupted(intent, **kwargs):
            stop(intent, **kwargs)
            raise KeyboardInterrupt("crash after stop before saving heads_stopped")
        with mock.patch.object(self.owner, "_stop", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.owner.replay_one(key)
        saved = self.owner.journal.read()["intents"][key]
        self.assertFalse(saved["progress"]["heads_stopped"])
        self.assertTrue(self.workspace.exists())
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(len(result["heads"]), 1)
        self.assertFalse(self.workspace.exists())

    def test_replaying_a_preserved_intent_twice_keeps_the_journal_identical(self):
        key = self.preserved_done_intent()
        self.owner.replay_one(key)
        before = self.owner.journal.path.read_bytes()
        self.owner.replay_one(key)
        self.assertEqual(self.owner.journal.path.read_bytes(), before)

    def scoped_predecessor(self, *, role="observer", task="sprint:1", workspace=None, completed=True):
        """Launch 1 as a scoped run under a real local-PTY runtime root, replaced by launch 2."""
        from dataclasses import replace
        from secretary.runtime.head.identity import head_process_status
        from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        from secretary.runtime.local_pty_head import LocalPtyHeadRuntime
        path, first, second, _, _ = self.replaced_observers()
        heartbeat = self.root / "observer.pid"
        old = replace(HeadRun.from_json(first.head_run), scope_generation="old-scope", pid_file=str(heartbeat))
        first.head_run = old.finishing(StopInitiator(actor="test")).exited().to_json()
        first.pid_file = str(heartbeat)
        root = self.root / "heads"
        directory = root / old.run_id
        directory.mkdir(parents=True)
        scope = ScopedHeadLifecycle(old.run_id, 128, generation=old.scope_generation)
        scope.persist(directory, role=role, task=task, workspace=workspace or str(path))
        if completed:
            evidence = scope.read_owner(directory)
            evidence.update(launch_allowed=False, cleanup_complete=True)
            scope.update_owner(directory, evidence)
        read = []
        def identity(pid_file, **kwargs):
            read.append(pid_file)
            return head_process_status(pid_file, **kwargs)
        backend = LocalPtyHeadRuntime(root, head_process_status=identity, stop_timeout=0)
        self.host.head_runtime_for = lambda run: backend
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args, **kwargs: CommandHostRuntime.fence_cleanup_scopes(
            self.host, *args, **kwargs)
        return path, first, heartbeat, backend, read

    def test_replaced_observer_conflicting_scope_owner_refuses_before_any_stop_effect(self):
        from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        path, first, _, backend, _ = self.scoped_predecessor(
            role="worker", task="card:foreign", workspace="/foreign/workspace", completed=False)
        with mock.patch.object(backend, "_ask_to_stop") as ask, \
                mock.patch.object(ScopedHeadLifecycle, "stop_owned") as native:
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("binding differs from its recorded head", result["reason"])
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertEqual((ask.call_count, native.call_count), (0, 0))
        self.assertTrue(path.is_dir())

    def test_scoped_predecessor_settles_by_its_own_scope_not_the_shared_heartbeat(self):
        from secretary.runtime.head.identity import head_process_status, publish_heartbeat
        from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        from secretary.runtime.local_pty_head import MemoryScopeError
        path, first, heartbeat, _, read = self.scoped_predecessor()
        publish_heartbeat(str(heartbeat), {"run_id": "observer-run-2", "role": "observer", "task": "sprint:1"})
        with mock.patch.object(ScopedHeadLifecycle, "stop_owned",
                               side_effect=MemoryScopeError("scope still has descendants")) as native:
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("scope still has descendants", result["reason"])
        self.assertEqual(native.call_count, 1, "a retained exit receipt is not fresh scoped proof")
        with mock.patch.object(ScopedHeadLifecycle, "stop_owned") as native:
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("replaced by observer-gen:2", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertEqual(native.call_count, 1)
        self.assertNotIn(str(heartbeat), read)
        self.assertEqual(head_process_status(str(heartbeat))["record"]["run_id"], "observer-run-2")
        heads = result["heads"]
        self.assertEqual([(h["run_id"], h["pid_file"]) for h in heads], [("observer-run", str(heartbeat))])
        self.assertTrue(path.is_dir())

    def test_predecessor_cleanup_observer_never_reads_the_successor_workspace(self):
        from secretary.dispatch.cleanup import _identity
        path, first, _, _, _ = self.replaced_observers()
        exists = Path.exists
        touched = []
        def watched(self_path, *args, **kwargs):
            touched.append(str(self_path))
            return exists(self_path, *args, **kwargs)
        with mock.patch("secretary.dispatch.cleanup._identity", wraps=_identity) as identity, \
                mock.patch.object(Path, "exists", autospec=True, side_effect=watched):
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertEqual(identity.call_count, 0)
        self.assertNotIn(str(path), touched)
        self.assertIsNone(result["identity"])


if __name__ == "__main__":
    unittest.main()
