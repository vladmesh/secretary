from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher import CommandHostRuntime, HostError
from secretary.dispatcher_launcher import HeadLaunch
from secretary.dispatcher_state import DispatcherRecord


class DispatcherTuiLaunchTests(unittest.TestCase):
    def test_tui_launch_waits_then_sends_initial_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(workspace, [{"terminal": {"tail": ["\x1b[1mWorking\x1b[0m"]}}])

            handle = host._launch(
                str(workspace),
                "title",
                "codex",
                "TASK.md",
                role="worker",
                env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                codex_mode="tui",
            )

        self.assertEqual(handle, "term-tui")
        create_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "create"])
        wait_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "wait"])
        send_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "send"])
        self.assertLess(create_i, wait_i)
        self.assertLess(wait_i, send_i)
        self.assertEqual(host.calls[wait_i][host.calls[wait_i].index("--terminal") + 1], "term-tui")
        self.assertIn("Read TASK.md", host.calls[send_i][host.calls[send_i].index("--text") + 1])
        self.assertEqual(host.catalog.modes, ["tui"])

    def test_tui_launch_delivers_short_pointer_not_task_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("full spec body that must not be delivered\n", encoding="utf-8")
            host = RecordingTuiHost(workspace, [{"terminal": {"tail": ["\x1b[1mWorking\x1b[0m"]}}])

            host._launch(
                str(workspace),
                "title",
                "codex",
                "TASK.md",
                role="worker",
                env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                codex_mode="tui",
                launch_prompt="The full task is in TASK.md. Read it first.",
            )

        send_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "send"])
        delivered = host.calls[send_i][host.calls[send_i].index("--text") + 1]
        self.assertEqual(delivered, "The full task is in TASK.md. Read it first.")
        self.assertNotIn("full spec body", delivered)

    def test_tui_delivery_resends_enter_when_prompt_stays_in_composer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(
                workspace,
                [
                    {"terminal": {"tail": ["\u203a Read TASK.md"]}},
                    {"terminal": {"tail": ["thinking"]}},
                ],
            )

            with mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_RESEND_GRACE_S", 0), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_POLL_S", 0.01):
                host._launch(
                    str(workspace),
                    "title",
                    "codex",
                    "TASK.md",
                    role="worker",
                    env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                    codex_mode="tui",
                )

        sends = [call for call in host.calls if call[:3] == ["orca", "terminal", "send"]]
        self.assertEqual(len(sends), 2)
        self.assertIn("Read TASK.md", sends[0][sends[0].index("--text") + 1])
        self.assertEqual(sends[1][sends[1].index("--text") + 1], "")

    def test_tui_delivery_failure_closes_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(workspace, [{"terminal": {"tail": ["\u203a Read TASK.md"]}}])

            with mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_TIMEOUT_S", 0.03), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_POLL_S", 0.01), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_RESEND_GRACE_S", 0), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_RETRIES", 1), \
                 self.assertRaises(HostError):
                host._launch(
                    str(workspace),
                    "title",
                    "codex",
                    "TASK.md",
                    role="worker",
                    env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                    codex_mode="tui",
                )

        self.assertIn(["orca", "terminal", "close", "--terminal", "term-tui", "--json"], host.calls)

    def test_tui_wait_failure_closes_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(workspace, [], fail_ops={"wait"})

            with self.assertRaises(HostError):
                host._launch(
                    str(workspace),
                    "title",
                    "codex",
                    "TASK.md",
                    role="worker",
                    env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                    codex_mode="tui",
                )

        self.assertIn(["orca", "terminal", "close", "--terminal", "term-tui", "--json"], host.calls)

    def test_tui_delivery_accepts_codex_session_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            sessions = Path(tmp) / "sessions" / "2099" / "01" / "02"
            sessions.mkdir(parents=True)
            (sessions / "session.jsonl").write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"cwd": str(workspace.resolve())}}),
                    json.dumps({
                        "type": "event_msg",
                        "timestamp": "2099-01-02T03:04:05Z",
                        "payload": {"type": "user_message"},
                    }),
                ]),
                encoding="utf-8",
            )
            host = RecordingTuiHost(workspace, [{"terminal": {"tail": ["idle"]}}])

            with mock.patch.dict(os.environ, {"SECRETARY_CODEX_SESSIONS": str(Path(tmp) / "sessions")}), \
                 mock.patch("secretary.dispatcher_tui.TUI_DELIVERY_POLL_S", 0.01):
                handle = host._launch(
                    str(workspace),
                    "title",
                    "codex",
                    "TASK.md",
                    role="worker",
                    env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                    codex_mode="tui",
                )

        self.assertEqual(handle, "term-tui")
        self.assertNotIn(["orca", "terminal", "close", "--terminal", "term-tui", "--json"], host.calls)

    def test_tui_activity_uses_rollout_mtime_only_for_tui_profiles(self) -> None:
        """Alternate-screen TUI output gets a progress signal without masking exec stalls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = ActivityCatalog()
            host = CommandHostRuntime(catalog, root / "data", mode="noop")  # type: ignore[arg-type]
            task = {"routing": {}}
            tui_record = DispatcherRecord(
                worker="worker", workspace=str(root), handle="term", head="codex-tui",
                review_head="codex-tui", attempt_id="attempt", comment_baseline=0,
                review_baseline=0, state="claimed", claimed_at=0.0,
            )
            exec_record = DispatcherRecord(
                worker="worker", workspace=str(root), handle="term", head="codex-exec",
                review_head="codex-exec", attempt_id="attempt", comment_baseline=0,
                review_baseline=0, state="claimed", claimed_at=0.0,
            )

            with mock.patch(
                "triggered_agents.agents.pipeline.codex_sessions.latest_activity_for",
                return_value=123.0,
            ) as latest_activity:
                self.assertEqual(host.codex_tui_activity(task, tui_record, "worker"), 123.0)
                self.assertIsNone(host.codex_tui_activity(task, exec_record, "worker"))

        latest_activity.assert_called_once_with(str(root))


class TuiCatalog:
    def __init__(self) -> None:
        self.modes: list[str | None] = []

    def prepare_head_workspace(self, head: str, workspace: str) -> None:
        return None

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        codex_mode: str | None = None,
        launch_prompt: str | None = None,
    ) -> HeadLaunch:
        self.modes.append(codex_mode)
        return HeadLaunch(
            "CODEX_HOME=/tmp/codex-home codex --dangerously-bypass-approvals-and-sandbox",
            prompt_after_start=True,
        )


class ActivityCatalog:
    def __init__(self) -> None:
        self._heads = {
            "profiles": {
                "codex-tui": {"adapter": "codex", "codex_mode": "tui"},
                "codex-exec": {"adapter": "codex", "codex_mode": "exec"},
            },
        }

    def _head_profile(self, head: str) -> dict:
        return self._heads["profiles"][head]


class RecordingTuiHost(CommandHostRuntime):
    def __init__(self, root: Path, reads: list[dict], *, fail_ops: set[str] | None = None) -> None:
        self.catalog = TuiCatalog()
        super().__init__(self.catalog, root, mode="real")  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self.reads = list(reads)
        self.fail_ops = fail_ops or set()

    def _run_json(self, args: list[str]) -> dict:
        self.calls.append(args)
        if args[:3] == ["orca", "terminal", "create"]:
            return {"terminal": {"handle": "term-tui"}}
        if args[:3] == ["orca", "terminal", "wait"] and "wait" in self.fail_ops:
            raise HostError("terminal wait failed")
        if args[:3] == ["orca", "terminal", "send"] and "send" in self.fail_ops:
            raise HostError("terminal send failed")
        if args[:3] == ["orca", "terminal", "read"]:
            if len(self.reads) > 1:
                return self.reads.pop(0)
            return self.reads[0] if self.reads else {"terminal": {"tail": []}}
        return {}
