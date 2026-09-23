"""secretary-1700: a card whose heads all run supervised gets a plain `git worktree`, never Orca's.

The selection is made once, when the workspace is placed, from the card's claimed worker and
reviewer profiles; every later operation on that workspace reads its manager back from the path.
So these tests hold three things: a supervised card's checkout is cut, resumed, discarded, stopped
and torn down with no `orca` argv at all; a card with a legacy head keeps the Orca path; and a card
whose Orca checkout already exists stays on Orca even after its profiles move to `local-pty`.

The host runs in real mode over a real project checkout cloned from a real bare remote; only the
parts of a bring-up that are not about the workspace (the Python environment, the task document,
the head launch itself) are stood in for.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.dispatch.host import CommandHostRuntime, LaunchedHead
from secretary.dispatch.launch import CAUSE_WORKSPACE_CONTRACT
from secretary.dispatch.state import DispatcherRecord
from secretary.dispatch.types import HostError
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from tests.fakes.dispatcher import FakeCatalog
from tests.production_runtime_fixtures import registered_production_runtime

PROJECT = "sample"
ORCA_BINDING = "sample_orca"
REF = "sample-7"
WORKER = "sample-7-worker"
BRANCH = f"pipeline/{REF}"
WORKER_HEAD = "codex-local-pty"
REVIEW_HEAD = "claude-local-pty"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class _Catalog(FakeCatalog):
    """The fake registry plus one supervised profile per role, bound to a real project checkout."""

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        self.profiles[WORKER_HEAD] = {
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "effort": "default",
            "resource": "openai-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }
        self.profiles[REVIEW_HEAD] = {
            "adapter": "claude",
            "model": "opus",
            "resource": "claude-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }

    def binding(self, project: str) -> dict:
        return {"repo": str(self.repo), "orca_binding": ORCA_BINDING, "default_branch": "main"}


class _RecordingHost(CommandHostRuntime):
    """Every child the host runs is recorded; an `orca` argv fails the test unless it is allowed."""

    def __init__(self, catalog: _Catalog, data_dir: Path, root: Path) -> None:
        super().__init__(  # type: ignore[arg-type]
            catalog, data_dir, mode="real", production_runtime=registered_production_runtime(root)
        )
        self.argvs: list[list[str]] = []
        self.orca_allowed = False
        self.launches: list[dict[str, Any]] = []

    def _record(self, args: list[str]) -> None:
        self.argvs.append(list(args))
        if args and args[0] == "orca" and not self.orca_allowed:
            raise AssertionError(f"orca was called on a git-managed workspace path: {args}")

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super()._run(args, label, cwd=cwd)

    def _run_json(self, args):  # type: ignore[override]
        self._record(args)
        if args[:3] == ["orca", "worktree", "rm"]:
            return {}
        return super()._run_json(args)

    def run_capture(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super().run_capture(args, label, cwd=cwd)

    # What a bring-up does besides the workspace is not what these tests are about.
    def _prepare_workspace_environment(self, workspace: str, *, project: str = "") -> None:
        return None

    def _require_workspace_environment(self, workspace: str) -> None:
        return None

    def _run_setup(self, project: str, workspace: str) -> None:
        return None

    def _clear_report_bodies(self, reference: str) -> None:
        return None

    def _clear_body_file(self, kind: str, reference: str, review_round: int) -> None:
        return None

    def _worker_task_doc(self, *args: Any, **kwargs: Any) -> str:
        return "task\n"

    def _review_document(self, task: dict[str, Any], record: DispatcherRecord) -> tuple[Path, str]:
        return Path(record.workspace).parent / "review.md", "review it"

    def _launch(self, workspace: str, title: str, head: str, prompt_file: str, **kwargs: Any) -> LaunchedHead:
        self.launches.append({"workspace": workspace, "head": head, **kwargs})
        return LaunchedHead(handle=f"run:{head}", head=head)


class _Fixture:
    """A bare remote with `main` and a published predecessor branch, and a clone to cut from."""

    def __init__(self, root: Path) -> None:
        self.remote = root / "remote.git"
        self.repo = root / "project"
        author = root / "author"
        git(root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.remote))
        git(root, "init", "--quiet", "--initial-branch", "main", str(author))
        for checkout in (author,):
            git(checkout, "config", "user.name", "Test User")
            git(checkout, "config", "user.email", "test@example.invalid")
        git(author, "remote", "add", "origin", str(self.remote))
        (author / "README.md").write_text("seed\n", encoding="utf-8")
        git(author, "add", "-A")
        git(author, "commit", "--quiet", "-m", "seed")
        git(author, "push", "--quiet", "origin", "main")
        self.main_sha = git(author, "rev-parse", "HEAD")
        git(root, "clone", "--quiet", str(self.remote), str(self.repo))
        git(author, "checkout", "--quiet", "-b", "pipeline/sample-6")
        (author / "predecessor.txt").write_text("unreleased\n", encoding="utf-8")
        git(author, "add", "-A")
        git(author, "commit", "--quiet", "-m", "predecessor")
        git(author, "push", "--quiet", "origin", "pipeline/sample-6")
        self.candidate_sha = git(author, "rev-parse", "HEAD")


def _task(
    *, seed: str = "", worker_head: str = WORKER_HEAD, review_head: str = REVIEW_HEAD
) -> dict[str, Any]:
    workspace: dict[str, Any] = {"seed_ref": seed} if seed else {}
    return {
        "ref": REF,
        "project": PROJECT,
        "workspace": workspace,
        "routing": {"head_override": worker_head, "review_head_override": review_head},
    }


def _record(workspace: str, *, review_head: str = REVIEW_HEAD) -> DispatcherRecord:
    return DispatcherRecord(
        worker=WORKER,
        workspace=workspace,
        handle="",
        head=WORKER_HEAD,
        review_head=review_head,
        attempt_id="attempt-1",
        comment_baseline=0,
        review_baseline=0,
        state="review_starting",
        claimed_at=0.0,
    )


class GitWorkspaceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name).resolve()
        self.fixture = _Fixture(self.root)
        self.orca_root = self.root / "orca-workspaces"
        env = mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.orca_root)})
        env.start()
        self.addCleanup(env.stop)
        self.data_dir = self.root / "data"
        self.catalog = _Catalog(self.fixture.repo)
        self.host = _RecordingHost(self.catalog, self.data_dir, self.root)
        self.git_path = self.data_dir / "workspaces" / PROJECT / WORKER
        self.orca_path = self.orca_root / ORCA_BINDING / WORKER

    def _prepare(self, task: dict[str, Any], **kwargs: Any) -> Path:
        prepared = self.host.prepare_worker(task, WORKER, WORKER_HEAD, attempt_id="attempt-1", **kwargs)
        return Path(prepared["workspace"])

    def _registered(self) -> set[Path]:
        listing = git(self.fixture.repo, "worktree", "list", "--porcelain")
        return {
            Path(line.removeprefix("worktree ")).resolve()
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }

    def _no_orca(self) -> None:
        self.assertFalse([argv for argv in self.host.argvs if argv and argv[0] == "orca"])

    # -- selection -----------------------------------------------------------------------------

    def test_a_supervised_card_is_placed_under_the_data_dir_by_project_id(self) -> None:
        self.assertEqual(self.host.restore_workspace(_task(), WORKER), str(self.git_path))
        self._no_orca()

    def test_a_card_with_a_legacy_worker_or_reviewer_keeps_the_orca_path(self) -> None:
        for task in (_task(worker_head="codex"), _task(review_head="codex-reviewer")):
            with self.subTest(routing=task["routing"]):
                self.assertEqual(self.host.restore_workspace(task, WORKER), str(self.orca_path))

    # -- create --------------------------------------------------------------------------------

    def test_a_branch_seed_is_cut_on_the_card_branch_with_no_orca_call(self) -> None:
        workspace = self._prepare(_task(seed="pipeline/sample-6"))

        self.assertEqual(workspace, self.git_path)
        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.candidate_sha)
        self.assertIn(workspace.resolve(), self._registered())
        self._no_orca()

    def test_an_exact_sha_seed_is_cut_on_the_card_branch_with_no_orca_call(self) -> None:
        workspace = self._prepare(_task(seed=self.fixture.candidate_sha))

        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.candidate_sha)
        self._no_orca()

    def test_an_ordinary_card_is_cut_from_its_integration_base(self) -> None:
        workspace = self._prepare(_task())

        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.main_sha)
        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)

    def test_a_resumed_card_is_validated_in_place_with_no_orca_call(self) -> None:
        first = self._prepare(_task())
        (first / "work.txt").write_text("in progress\n", encoding="utf-8")
        self.host.argvs.clear()

        again = self._prepare(_task(), require_existing_workspace=True)

        self.assertEqual(again, first)
        self.assertEqual((again / "work.txt").read_text(encoding="utf-8"), "in progress\n")
        self.assertIn(["git", "-C", str(first), "branch", "--show-current"], self.host.argvs)
        self._no_orca()

    def test_a_worktree_the_check_rejects_is_removed_branch_included_before_the_error(self) -> None:
        refusal = HostError(
            "resume workspace is on the wrong branch", bring_up_cause=CAUSE_WORKSPACE_CONTRACT
        )
        with (
            mock.patch.object(_RecordingHost, "_validate_resumable_workspace", side_effect=refusal),
            self.assertRaises(HostError) as refused,
        ):
            self._prepare(_task())

        self.assertIn("wrong branch", str(refused.exception))
        self.assertEqual(refused.exception.bring_up_cause, CAUSE_WORKSPACE_CONTRACT)
        self.assertFalse(self.git_path.exists())
        self.assertNotIn(self.git_path.resolve(), self._registered())
        self.assertEqual(git(self.fixture.repo, "branch", "--list", BRANCH), "")
        self._no_orca()

    # -- stop and teardown ---------------------------------------------------------------------

    def test_stop_and_teardown_remove_the_worktree_with_no_orca_call(self) -> None:
        workspace = self._prepare(_task())
        self.host.argvs.clear()

        self.host.stop(_record(str(workspace)))
        self.host.teardown(_record(str(workspace)))

        self.assertFalse(workspace.exists())
        self.assertNotIn(workspace.resolve(), self._registered())
        self.assertIn(["git", "-C", str(self.fixture.repo), "worktree", "prune"], self.host.argvs)
        self._no_orca()

    def test_a_refused_stop_leaves_the_git_worktree_in_place(self) -> None:
        workspace = self._prepare(_task())
        self.host.argvs.clear()

        with mock.patch.object(
            _RecordingHost, "stop_workspace", side_effect=HostError("the worker head was not stopped")
        ):
            self.host.teardown(_record(str(workspace)))

        self.assertTrue(workspace.is_dir())
        self.assertIn(workspace.resolve(), self._registered())
        self.assertFalse([argv for argv in self.host.argvs if "worktree" in argv])

    # -- a card created on Orca stays on Orca --------------------------------------------------

    def test_an_orca_workspace_is_resumed_and_torn_down_through_orca_after_profiles_move(self) -> None:
        # Cut the way Orca cuts it: under the Orca root, named by the binding's registration, on the
        # card branch. The card's profiles are already both `local-pty`.
        self.orca_path.parent.mkdir(parents=True)
        git(self.fixture.repo, "worktree", "add", "--quiet", "-b", BRANCH, str(self.orca_path), "origin/main")

        self.assertEqual(self.host.restore_workspace(_task(), WORKER), str(self.orca_path))
        self.assertEqual(self._prepare(_task(), require_existing_workspace=True), self.orca_path)
        self.assertFalse(self.git_path.exists())

        self.host.orca_allowed = True
        self.host.teardown(_record(str(self.orca_path)))

        self.assertIn(
            ["orca", "worktree", "rm", "--worktree", f"path:{self.orca_path}", "--force", "--json"],
            self.host.argvs,
        )
        # Orca's removal is Orca's: git was not asked to take this worktree back.
        self.assertNotIn(
            ["git", "-C", str(self.fixture.repo), "worktree", "remove", "--force", str(self.orca_path)],
            self.host.argvs,
        )

    def test_a_git_workspace_stays_git_after_its_profiles_move_to_legacy(self) -> None:
        workspace = self._prepare(_task())

        self.assertEqual(self.host.restore_workspace(_task(worker_head="codex"), WORKER), str(workspace))

    # -- the reviewer --------------------------------------------------------------------------

    def test_a_supervised_reviewer_starts_in_the_same_workspace_without_the_pane_inventory(self) -> None:
        workspace = self._prepare(_task())
        self.host.launches.clear()

        with (
            mock.patch.object(
                _RecordingHost, "_split_anchor", side_effect=AssertionError("split anchor asked")
            ),
            mock.patch.object(
                _RecordingHost, "_worktree_terminals", side_effect=AssertionError("pane inventory asked")
            ),
        ):
            launched = self.host.start_review(_task(), _record(str(workspace)))

        [review] = self.host.launches
        self.assertEqual(review["workspace"], str(workspace))
        self.assertEqual(review["head"], REVIEW_HEAD)
        self.assertEqual(review["split_from"], "")
        self.assertEqual(launched.commit, self.fixture.main_sha)
        self._no_orca()

    def test_an_orca_reviewer_still_splits_off_the_workers_pane(self) -> None:
        workspace = self._prepare(_task())
        self.host.launches.clear()

        with mock.patch.object(_RecordingHost, "_split_anchor", return_value="pane-1") as anchor:
            self.host.start_review(_task(), _record(str(workspace), review_head="codex-reviewer"))

        anchor.assert_called_once()
        self.assertEqual(self.host.launches[0]["split_from"], "pane-1")


class GitWorkspaceRootTests(unittest.TestCase):
    """The git root never claims a path the Orca root holds."""

    def test_an_orca_root_at_or_above_the_git_root_leaves_every_path_to_orca(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            host = CommandHostRuntime(FakeCatalog(), data, mode="real")  # type: ignore[arg-type]
            path = str(data / "workspaces" / PROJECT / WORKER)
            for orca_root, owned in ((data / "orca", True), (data / "workspaces", False), (data, False)):
                with (
                    self.subTest(orca_root=orca_root),
                    mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(orca_root)}),
                ):
                    self.assertEqual(host._is_git_workspace(path), owned)

    def test_only_the_project_and_worker_shape_under_the_git_root_is_git_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            host = CommandHostRuntime(FakeCatalog(), data, mode="real")  # type: ignore[arg-type]
            root = data / "workspaces"
            with mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(data / "orca")}):
                self.assertTrue(host._is_git_workspace(str(root / PROJECT / WORKER)))
                for other in (root / WORKER, root / PROJECT / WORKER / "nested", root, data / "elsewhere"):
                    with self.subTest(path=other):
                        self.assertFalse(host._is_git_workspace(str(other)))

    def test_a_name_that_is_not_one_path_component_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = CommandHostRuntime(FakeCatalog(), Path(tmp), mode="real")  # type: ignore[arg-type]
            for project, worker in (("..", WORKER), (PROJECT, "a/b"), ("", WORKER)):
                with self.subTest(project=project, worker=worker), self.assertRaises(HostError):
                    host._git_workspaces.path(project, worker)


if __name__ == "__main__":
    unittest.main()
