"""Dispatcher boundary tests for workspace Python and production provenance."""

from __future__ import annotations

import os
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
                result = host.gate_check({}, _record())
        self.assertEqual(result.status, "green")
        self.assertEqual(runtime.calls, 2)
        gate.assert_called_once()

    def test_cleanup_refuses_after_stop_but_before_worktree_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _Runtime([_observation(), _observation("workspace_targeted_editable")])
            host = _Host(Path(tmp), runtime)
            record = _record(str(Path(tmp) / "task"))
            with self.assertRaisesRegex(HostError, "workspace_targeted_editable"):
                host.teardown(record)
        self.assertEqual(host.effects, ["stop"])

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
            workspace.mkdir()
            runtime = _Runtime([_observation()])
            host = _Host(Path(tmp), runtime)
            host._prepare_workspace_environment(str(workspace))
            python = workspace / ".venv" / "bin" / "python3"
            first = python.stat().st_ino
            host._prepare_workspace_environment(str(workspace))
            self.assertEqual(python.stat().st_ino, first)
            self.assertTrue(os.access(python, os.X_OK))


if __name__ == "__main__":
    unittest.main()
