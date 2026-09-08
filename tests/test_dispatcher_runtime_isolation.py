"""Dispatcher boundary tests for workspace Python and production provenance."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.dispatch.host import CommandHostRuntime
from secretary.dispatch.runtime_provenance import RuntimeProvenance
from secretary.dispatcher_gate import GateResult
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_types import HostError


def _observation(classification: str = "valid") -> RuntimeProvenance:
    return RuntimeProvenance(
        classification,
        sys.executable,
        "/registered/secretary",
        "/registered/secretary/src/secretary/__init__.py",
        (),
    )


def _record(workspace: str = "") -> DispatcherRecord:
    return DispatcherRecord(
        worker="worker",
        workspace=workspace,
        handle="pane",
        head="codex",
        review_head="codex-reviewer",
        attempt_id="attempt",
        comment_baseline=0,
        review_baseline=0,
        state="reviewing",
        claimed_at=0,
    )


def _git_workspace(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "fixture@example.invalid"], check=True)
    (path / "tracked").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)


class _Runtime:
    interpreter = sys.executable
    product_root = "/registered/secretary"

    def __init__(self, answers: list[RuntimeProvenance]) -> None:
        self.answers = answers
        self.calls = 0

    def probe(self) -> RuntimeProvenance:
        answer = self.answers[self.calls]
        self.calls += 1
        return answer


class _Host(CommandHostRuntime):
    def __init__(self, root: Path, runtime: _Runtime) -> None:
        super().__init__(SimpleNamespace(), root, mode="real", production_runtime=runtime)  # type: ignore[arg-type]
        self.effects: list[str] = []
        self.environment_checks: list[str] = []

    def _decide_workspace_environment_ownership(self, workspace: str) -> str:
        self.environment_checks.append(workspace)
        return super()._decide_workspace_environment_ownership(workspace)

    def stop_workspace(self, record: DispatcherRecord) -> None:
        self.effects.append("stop")

    def _run_json(self, args: list[str]) -> dict:
        self.effects.append("remove")
        return {}


class DispatcherRuntimeIsolationTests(unittest.TestCase):
    def test_gate_is_fenced_before_and_after_backend_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation(), _observation()])
            host = _Host(Path(tmp), runtime)
            with mock.patch(
                "secretary.dispatch.host._gate_check", return_value=GateResult("green", "fixture")
            ) as gate:
                result = host.gate_check({}, _record(str(Path(tmp) / "task")))
        self.assertEqual(result.status, "green")
        self.assertEqual(runtime.calls, 2)
        self.assertEqual(host.environment_checks, [str(Path(tmp) / "task")])
        gate.assert_called_once()

    def test_cleanup_refuses_after_stop_but_before_worktree_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation(), _observation("workspace_targeted_editable")])
            host = _Host(Path(tmp), runtime)
            record = _record(str(Path(tmp) / "task"))
            with self.assertRaisesRegex(HostError, "workspace_targeted_editable"):
                host.teardown(record)
        self.assertEqual(host.effects, ["stop"])
        self.assertEqual(host.environment_checks, [str(Path(tmp) / "task")])

    def test_release_is_fenced_before_and_after_its_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation(), _observation()])
            host = _Host(Path(tmp), runtime)
            host.catalog = SimpleNamespace(integration_base=lambda project, override: "main")
            with (
                mock.patch("secretary.dispatch.host._validation_ci", return_value="github"),
                mock.patch.object(host, "_no_diff_research_delivery_is_complete", return_value=False),
                mock.patch.object(
                    host, "_merge_github_pr", side_effect=lambda *args: host.effects.append("merge")
                ),
            ):
                host.complete_green(
                    {"ref": "secretary-1", "project": "secretary", "workspace": {}},
                    _record(str(Path(tmp) / "task")),
                )
        self.assertEqual(runtime.calls, 2)
        self.assertEqual(host.effects, ["merge"])
        self.assertEqual(host.environment_checks, [str(Path(tmp) / "task")])

    def test_release_refusal_cannot_reach_the_merge_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation("wrong_root")])
            host = _Host(Path(tmp), runtime)
            with (
                mock.patch.object(host, "_merge_github_pr") as merge,
                self.assertRaisesRegex(HostError, "wrong_root"),
            ):
                host.complete_green(
                    {"ref": "secretary-1", "project": "secretary", "workspace": {}},
                    _record(str(Path(tmp) / "task")),
                )
        merge.assert_not_called()

    def test_workspace_prepare_creates_an_owned_interpreter_and_reuses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            runtime = _Runtime([_observation()])
            host = _Host(Path(tmp), runtime)
            host._prepare_workspace_environment(str(workspace))
            python = workspace / ".secretary-task-env" / "venv" / "bin" / "python3"
            first = python.stat().st_ino
            host._prepare_workspace_environment(str(workspace))
            self.assertEqual(python.stat().st_ino, first)
            self.assertTrue(os.access(python, os.X_OK))

    def test_dispatcher_environment_is_disjoint_from_adapter_owned_dot_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            adapter_environment = workspace / ".venv"
            adapter_environment.mkdir(parents=True)
            sentinel = adapter_environment / "adapter-owned"
            sentinel.write_text("untouched\n", encoding="utf-8")
            runtime = _Runtime([_observation()])
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=runtime
            )

            host._prepare_workspace_environment(str(workspace))

            dispatcher_python = workspace / ".secretary-task-env" / "venv" / "bin" / "python3"
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")
            self.assertFalse(any(adapter_environment.rglob("_secretary_production_dependencies.pth")))
            imported = subprocess.run(
                [str(dispatcher_python), "-I", "-c", "import secretary"],
                cwd=tmp,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(imported.returncode, 0, "production packages leaked into task venv")

    def test_unclaimed_reserved_environment_is_never_adopted_or_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            environment = workspace / ".secretary-task-env" / "venv"
            environment.mkdir(parents=True)
            sentinel = environment / "foreign"
            sentinel.write_text("untouched\n", encoding="utf-8")
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            with self.assertRaisesRegex(HostError, "ownership is unavailable"):
                host._prepare_workspace_environment(str(workspace))

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")

    def test_adapter_setup_receives_neither_dispatcher_nor_production_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            workspace.mkdir()
            catalog = SimpleNamespace(
                adapter=lambda project: {
                    "setup": {"commands": ["uv sync --locked"]},
                    "smoke": {"command": ".venv/bin/python -m tests.smoke"},
                }
            )
            runtime = _Runtime([_observation()])
            runtime.interpreter = "/opt/secretary/.venv/bin/python3"
            host = CommandHostRuntime(  # type: ignore[arg-type]
                catalog, Path(tmp), mode="real", production_runtime=runtime
            )
            commands: list[str] = []

            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": "/opt/secretary/.venv/bin:/usr/local/bin:/usr/bin"},
                    clear=True,
                ),
                mock.patch.object(
                    host,
                    "_run_shell",
                    side_effect=lambda command, cwd, label: commands.append(command),
                ),
            ):
                host._run_setup("adapter-project", str(workspace))

            self.assertEqual(len(commands), 2)
            for command in commands:
                self.assertIn("PATH=/usr/local/bin:/usr/bin", command)
                self.assertIn("unset VIRTUAL_ENV", command)
                self.assertNotIn(".secretary-task-env", command)
                self.assertNotIn("/opt/secretary/.venv/bin", command)

    def test_adapter_default_runtime_is_populated_from_candidate_dev_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            _git_workspace(workspace)
            (workspace / "pyproject.toml").write_text("[project]\nname = 'other-project'\n", encoding="utf-8")
            catalog = SimpleNamespace(
                adapter=lambda project: {
                    "broad_check": {"module": "tests.broad", "import_package": "secretary"}
                }
            )
            host = CommandHostRuntime(  # type: ignore[arg-type]
                catalog, Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )
            original_run = host._run
            commands: list[list[str]] = []

            def run(args: list[str], label: str, *, cwd: Path | None = None):
                commands.append(args)
                if label == "workspace candidate dependencies":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                return original_run(args, label, cwd=cwd)

            with (
                mock.patch.object(host, "_run", run),
                mock.patch.object(host, "_require_workspace_environment"),
            ):
                host._prepare_workspace_environment(str(workspace), project="other-project")

            candidate_python = str(workspace / ".secretary-task-env" / "venv" / "bin" / "python3")
            self.assertIn([candidate_python, "-m", "pip", "install", "-e", ".[dev]"], commands)

    def test_reserved_environment_is_locally_excluded_and_cannot_be_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "other-project"
            _git_workspace(repository)
            workspace = Path(tmp) / "task-worktree"
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "add", "-qb", "task", str(workspace)],
                check=True,
            )
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )

            host._prepare_workspace_environment(str(workspace))

            exclude = Path(
                subprocess.run(
                    ["git", "-C", str(workspace), "rev-parse", "--git-path", "info/exclude"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            self.assertIn(".secretary-task-env/", exclude.read_text(encoding="utf-8").splitlines())
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            host.verify_worker_result({}, _record(str(workspace)))
            subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
            staged = subprocess.run(
                ["git", "-C", str(workspace), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(staged.stdout, "")

    def test_rework_prepares_a_missing_pre_upgrade_environment_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            workspace.mkdir()
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(integration_base=lambda project, override: "main"),
                Path(tmp),
                mode="real",
                production_runtime=_Runtime([_observation()]),
            )
            order: list[str] = []
            task = {"ref": "secretary-1", "project": "secretary", "workspace": {}, "routing": {}}
            record = _record(str(workspace))

            with (
                mock.patch.object(host, "_require_project_available"),
                mock.patch.object(
                    host,
                    "_prepare_workspace_environment",
                    side_effect=lambda *args, **kwargs: order.append("prepare"),
                ),
                mock.patch.object(
                    host,
                    "_require_workspace_environment",
                    side_effect=lambda *args: order.append("require"),
                ),
                mock.patch.object(host, "_clear_report_bodies"),
                mock.patch.object(host, "_worker_task_doc", return_value="task\n"),
                mock.patch.object(
                    host, "_launch", side_effect=lambda *args, **kwargs: order.append("launch")
                ),
            ):
                host.restart_worker(task, record)

            self.assertEqual(order, ["prepare", "require", "launch"])

    def test_retained_review_prepares_a_missing_pre_upgrade_environment_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            workspace.mkdir()
            host = CommandHostRuntime(  # type: ignore[arg-type]
                SimpleNamespace(), Path(tmp), mode="real", production_runtime=_Runtime([_observation()])
            )
            order: list[str] = []
            task = {"ref": "secretary-1", "project": "secretary", "routing": {}}
            record = _record(str(workspace))
            document = Path(tmp) / "review.md"

            def launch(*args, **kwargs):
                order.append("launch")
                raise HostError("launch reached")

            with (
                mock.patch.object(host, "_require_project_available"),
                mock.patch.object(
                    host,
                    "_prepare_workspace_environment",
                    side_effect=lambda *args, **kwargs: order.append("prepare"),
                ),
                mock.patch.object(
                    host,
                    "_require_workspace_environment",
                    side_effect=lambda *args: order.append("require"),
                ),
                mock.patch.object(host, "_clear_body_file"),
                mock.patch.object(host, "_review_document", return_value=(document, "review")),
                mock.patch.object(host, "_split_anchor", return_value=""),
                mock.patch.object(host, "_launch", side_effect=launch),
                self.assertRaisesRegex(HostError, "launch reached"),
            ):
                host.start_review(task, record)

            self.assertEqual(order, ["prepare", "require", "launch"])


if __name__ == "__main__":
    unittest.main()
