"""secretary-1705: an observer on a supervised runtime gets a plain `git worktree`, never Orca's.

The same rule as a card's workspace (secretary-1700): the manager is chosen at launch from the
observer's declared profile, and every later operation reads it back from the recorded path. So
these tests hold four things: a `local-pty` observer's workspace is cut, stopped and removed with no
`orca` argv at all; the launch intent and the bring-up name the same path for either runtime; an
`orca-legacy` observer keeps the Orca worktree; and a live record whose workspace Orca made stays on
Orca even after its profile moves to `local-pty`.

The host runs in real mode over the real observer repo it creates; only the head itself (its pane,
its process, its stop) is stood in for.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from secretary.dispatch.host import OBSERVER_REPO_BRANCH, CommandHostRuntime
from secretary.dispatch.observer import ObserverRecord, _write_launch_intent
from secretary.dispatch.types import HostError
from secretary.observer_root import observer_root_repo
from secretary.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from tests.fakes.dispatcher import FakeCatalog
from tests.production_runtime_fixtures import registered_production_runtime

REF = "sprint:1705"
TOKEN = "sprint-1705"
SUPERVISED_HEAD = "claude-local-pty"
#: The fake registry's observer profile names no runtime, which is `orca-legacy`.
LEGACY_HEAD = "codex-observer"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class _Catalog(FakeCatalog):
    """The fake registry plus one supervised observer profile whose head command does nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.profiles[SUPERVISED_HEAD] = {
            "adapter": "claude",
            "model": "opus",
            "resource": "claude-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }

    def head_launch(self, head: str, prompt_file: str, **_kwargs: Any) -> HeadCommand:
        return HeadCommand("true", adapter="claude")

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str) -> None:
        return None


class _RecordingHost(CommandHostRuntime):
    """Every child and every head step is recorded in order; `orca` fails unless allowed."""

    def __init__(self, data_dir: Path, root: Path) -> None:
        super().__init__(  # type: ignore[arg-type]
            _Catalog(), data_dir, mode="real", production_runtime=registered_production_runtime(root)
        )
        self.events: list[Any] = []
        self.orca_allowed = False
        self.orca_registered: set[str] = set()

    def _record(self, args: list[str]) -> None:
        self.events.append(list(args))
        if args and args[0] == "orca" and not self.orca_allowed:
            raise AssertionError(f"orca was called on a git-managed observer path: {args}")

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super()._run(args, label, cwd=cwd)

    def _run_json(self, args):  # type: ignore[override]
        self._record(args)
        if args and args[0] == "orca":
            # Answered here, never by a real CLI: a developer's live Orca must not be asked.
            return self._orca(args)
        return super()._run_json(args)

    def run_capture(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super().run_capture(args, label, cwd=cwd)

    def _orca(self, args: list[str]) -> dict[str, Any]:
        step = args[1:3]
        if step == ["worktree", "show"]:
            path = args[args.index("--worktree") + 1].split(":", 1)[1]
            if path not in self.orca_registered:
                raise HostError("orca worktree show failed: selector_not_found")
            return {}
        if step == ["worktree", "create"]:
            path = str(Path(self.observer_workspace(REF)).parent / args[args.index("--name") + 1])
            Path(path).mkdir(parents=True, exist_ok=True)
            self.orca_registered.add(path)
            return {"worktree": {"path": path}}
        return {}

    def _open_head_pane(self, run, title, command):  # type: ignore[override]
        self.events.append("head-start")
        return dataclasses.replace(run, handle="run:observer", leaf="leaf:observer")

    def _confirm_head_process_gone(self, pid_file, **_kwargs):  # type: ignore[override]
        self.events.append("head-confirmed-gone")

    def _guard_head_run(self, *_args, **_kwargs):  # type: ignore[override]
        return {"known": False}


class _HeadRuntime:
    """The head's backend, standing in for both: a head stop, and Orca's by-worktree teardown."""

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def stop(self, run, initiator):
        self.events.append(f"head-stop:{run.spec.runtime or ORCA_LEGACY_RUNTIME}")
        return SimpleNamespace(ok=True, reason="")

    def stop_workspace(self, workspace: str) -> None:
        self.events.append(["orca", "stop_workspace", f"path:{workspace}"])

    def forget_head(self, run_id: str) -> None:
        return None


class ObserverGitWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name).resolve()
        self.orca_root = self.root / "orca-workspaces"
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.orca_root),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
                "SECRETARY_CLAUDE_PROJECTS": str(self.root / "claude-projects"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.data_dir = self.root / "data"
        self.host = _RecordingHost(self.data_dir, self.root)
        self.git_path = self.data_dir / "workspaces" / "observers" / TOKEN
        self.orca_path = self.orca_root / "observers" / TOKEN

    def prepare(self, head: str, recorded: str = "") -> dict[str, Any]:
        return self.host.prepare_observer({"ref": REF}, head, prompt="# Sprint\n", recorded_workspace=recorded)

    def record(self, launched: dict[str, Any], head: str) -> ObserverRecord:
        return ObserverRecord(
            sprint=REF,
            head=head,
            workspace=str(launched["workspace"]),
            handle=str(launched["handle"]),
            leaf=str(launched["leaf"]),
            pid_file=str(launched["pid_file"]),
            head_run=dict(launched["head_run"]),
        )

    def stop(self, record: ObserverRecord) -> None:
        with mock.patch.object(self.host, "head_runtime_for", return_value=_HeadRuntime(self.host.events)):
            self.host.stop_observer(record)

    def orca_argvs(self) -> list[list[str]]:
        return [event for event in self.host.events if isinstance(event, list) and event[:1] == ["orca"]]

    def worktree_argvs(self) -> list[list[str]]:
        return [event[3:] for event in self.host.events if isinstance(event, list) and event[3:4] == ["worktree"]]

    # -- the supervised observer -----------------------------------------------------------------

    def test_a_local_pty_observer_is_a_detached_git_worktree_with_no_orca_argv_from_intent_to_removal(self) -> None:
        intent = self.host.observer_workspace(REF, SUPERVISED_HEAD)
        self.assertEqual(intent, str(self.git_path))

        launched = self.prepare(SUPERVISED_HEAD, intent)

        self.assertEqual(launched["workspace"], intent)
        repo = observer_root_repo(self.data_dir)
        self.assertEqual(git(self.git_path, "rev-parse", "--show-toplevel"), str(self.git_path))
        self.assertEqual(git(self.git_path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD", "not detached")
        self.assertEqual(git(self.git_path, "rev-parse", "HEAD"), git(repo, "rev-parse", OBSERVER_REPO_BRANCH))
        self.assertEqual(git(repo, "branch", "--format=%(refname:short)"), OBSERVER_REPO_BRANCH, "a branch was made")
        self.assertEqual(git(repo, "log", "--format=%s", OBSERVER_REPO_BRANCH), "observer root")
        self.assertTrue((self.git_path / "SPRINT.md").is_file())

        self.stop(self.record(launched, SUPERVISED_HEAD))

        self.assertFalse(self.git_path.exists())
        self.assertNotIn(str(self.git_path), git(repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.orca_argvs(), [])

    def test_the_stop_confirms_the_head_gone_before_git_removes_and_prunes_its_worktree(self) -> None:
        launched = self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF, SUPERVISED_HEAD))
        del self.host.events[:]

        self.stop(self.record(launched, SUPERVISED_HEAD))

        steps = [
            event if isinstance(event, str) else event[3:5]
            for event in self.host.events
            if isinstance(event, str) or event[3:5] in (["worktree", "remove"], ["worktree", "prune"])
        ]
        self.assertEqual(
            steps,
            [f"head-stop:{LOCAL_PTY_RUNTIME}", "head-confirmed-gone", ["worktree", "remove"], ["worktree", "prune"]],
        )
        remove = next(event for event in self.host.events if isinstance(event, list) and event[3:5] == ["worktree", "remove"])
        self.assertIn("--force", remove)

    def test_a_respawn_reuses_a_live_worktree_and_recuts_a_removed_one(self) -> None:
        workspace = self.host.observer_workspace(REF, SUPERVISED_HEAD)
        launched = self.prepare(SUPERVISED_HEAD, workspace)
        (self.git_path / "notes.txt").write_text("kept\n", encoding="utf-8")

        self.prepare(SUPERVISED_HEAD, workspace)
        self.assertTrue((self.git_path / "notes.txt").exists(), "a registered worktree was re-created")
        self.assertEqual([argv[:2] for argv in self.worktree_argvs()].count(["worktree", "add"]), 1)

        self.stop(self.record(launched, SUPERVISED_HEAD))
        again = self.prepare(SUPERVISED_HEAD, workspace)

        self.assertEqual(again["workspace"], workspace)
        self.assertEqual(git(self.git_path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(self.orca_argvs(), [])

    def test_a_stop_over_a_worktree_already_gone_only_confirms_the_head(self) -> None:
        launched = self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF, SUPERVISED_HEAD))
        record = self.record(launched, SUPERVISED_HEAD)
        self.stop(record)
        del self.host.events[:]

        self.stop(record)

        self.assertIn("head-confirmed-gone", self.host.events)
        self.assertEqual(self.orca_argvs(), [])
        self.assertNotIn(["worktree", "remove"], [argv[:2] for argv in self.worktree_argvs()])

    # -- the same path from intent and bring-up ---------------------------------------------------

    def test_the_intent_and_the_bring_up_name_the_same_path_for_either_runtime(self) -> None:
        for head, expected in ((SUPERVISED_HEAD, self.git_path), (LEGACY_HEAD, self.orca_path)):
            with self.subTest(head=head):
                self.host.orca_allowed = head == LEGACY_HEAD
                intent = self.host.observer_workspace(REF, head)
                self.assertEqual(intent, str(expected))
                self.assertEqual(self.prepare(head, intent)["workspace"], intent)
                self.assertEqual(self.prepare(head)["workspace"], intent, "without a recorded path")

    def test_the_launch_intent_fixes_the_path_the_bring_up_then_cuts(self) -> None:
        runtime = SimpleNamespace(host=self.host, production_state=SimpleNamespace(save=lambda _payload: None))
        for head, expected in ((SUPERVISED_HEAD, self.git_path), (LEGACY_HEAD, self.orca_path)):
            with self.subTest(head=head):
                self.host.orca_allowed = head == LEGACY_HEAD
                record = ObserverRecord(sprint=REF)
                self.assertIsNone(_write_launch_intent(runtime, {}, {}, REF, record, head, 1))
                self.assertEqual(record.workspace, str(expected))
                self.assertEqual(self.prepare(head, record.workspace)["workspace"], record.workspace)

    def test_a_record_that_names_an_orca_workspace_keeps_it_in_the_next_intent(self) -> None:
        runtime = SimpleNamespace(host=self.host, production_state=SimpleNamespace(save=lambda _payload: None))
        record = ObserverRecord(sprint=REF, workspace=str(self.orca_path))

        self.assertIsNone(_write_launch_intent(runtime, {}, {}, REF, record, SUPERVISED_HEAD, 2))

        self.assertEqual(record.workspace, str(self.orca_path))

    # -- the Orca route ---------------------------------------------------------------------------

    def test_an_orca_legacy_observer_keeps_todays_orca_worktree(self) -> None:
        self.host.orca_allowed = True

        launched = self.prepare(LEGACY_HEAD, self.host.observer_workspace(REF, LEGACY_HEAD))

        self.assertEqual(launched["workspace"], str(self.orca_path))
        repo = observer_root_repo(self.data_dir)
        self.assertEqual(
            [argv[1:3] for argv in self.orca_argvs()],
            [["worktree", "show"], ["repo", "add"], ["worktree", "create"]],
        )
        self.assertIn(["orca", "repo", "add", "--path", str(repo), "--json"], self.orca_argvs())
        self.assertEqual(self.worktree_argvs(), [])
        self.assertFalse(self.git_path.exists())

    def test_a_live_orca_observer_record_on_a_now_local_pty_profile_stays_on_orca(self) -> None:
        """The invariant sprint:1459's own observer depends on: stopped, respawned and torn down
        through Orca, and its workspace never moved, re-created or registered again."""
        self.host.orca_allowed = True
        self.orca_path.mkdir(parents=True)
        (self.orca_path / "SPRINT.md").write_text("live\n", encoding="utf-8")
        self.host.orca_registered.add(str(self.orca_path))
        pid_file = self.host.observer_pid_file(REF)
        record = ObserverRecord(
            sprint=REF,
            head=SUPERVISED_HEAD,
            workspace=str(self.orca_path),
            handle="term-obs",
            leaf="leaf-obs",
            pid_file=pid_file,
            head_run=HeadRun(
                run_id="live-observer",
                spec=HeadSpec(profile_id=SUPERVISED_HEAD, adapter="claude", runtime=ORCA_LEGACY_RUNTIME),
                workspace=str(self.orca_path),
                task_ref=TaskRef.sprint(REF),
                role="observer",
                pid_file=pid_file,
                handle="term-obs",
                leaf="leaf-obs",
            ).to_json(),
        )

        # Respawn over the recorded path: Orca already has it, so nothing is created or registered.
        launched = self.prepare(SUPERVISED_HEAD, record.workspace)
        self.assertEqual(launched["workspace"], str(self.orca_path))
        self.assertEqual([argv[1:3] for argv in self.orca_argvs()], [["worktree", "show"]])
        self.assertEqual((self.orca_path / "SPRINT.md").read_text(encoding="utf-8"), "# Sprint\n")
        del self.host.events[:]

        self.stop(record)

        self.assertEqual(
            [argv[1:3] for argv in self.orca_argvs()],
            [["worktree", "show"], ["stop_workspace", f"path:{self.orca_path}"], ["worktree", "rm"]],
        )
        self.assertIn(f"path:{self.orca_path}", self.orca_argvs()[-1])
        self.assertEqual(self.worktree_argvs(), [])
        self.assertFalse(self.git_path.exists())
        self.assertFalse(observer_root_repo(self.data_dir).exists(), "the git route was taken")


if __name__ == "__main__":
    unittest.main()
