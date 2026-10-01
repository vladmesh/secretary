"""Disposable Git repositories and simulated scope owners, including public maintenance."""

from __future__ import annotations

import argparse
import copy
import json
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
from secretary.dispatch.state import DispatcherRecord
from secretary.dispatch.types import HostError
from secretary.observer_root import observer_root_repo
from secretary.runtime.head import HeadRun, HeadSpec, StopInitiator, TaskRef
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME


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


if __name__ == "__main__":
    unittest.main()
