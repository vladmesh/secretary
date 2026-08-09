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
from secretary.dispatcher_tui import (
    READINESS_BLOCKED,
    READINESS_BUSY,
    READINESS_READY,
    READINESS_UNKNOWN,
    deliver_interactive_prompt,
    latest_claude_user_turn_for,
    terminal_readiness,
    terminal_turn_started,
)
from tests.test_dispatcher_observer import (
    BLOCKED_PANE_WAIT_BODY,
    STALE_HANDLE_WAIT_FAILURE,
    TIMEOUT_WAIT_FAILURE,
)


class DispatcherTuiLaunchTests(unittest.TestCase):
    def test_out_of_band_delivery_rejects_confirm_before_touching_terminal(self) -> None:
        terminal_calls: list[list[str]] = []
        callback_calls = [0]

        def run_json(command: list[str]) -> dict:
            terminal_calls.append(command)
            return {}

        def confirm(_sent_at: float) -> bool:
            callback_calls[0] += 1
            return True

        with self.assertRaisesRegex(ValueError, "out-of-band delivery cannot use"):
            deliver_interactive_prompt(
                "term-observer", "wake", run_json=run_json,
                confirm=confirm, ack_out_of_band=True,
            )

        self.assertEqual(terminal_calls, [])
        self.assertEqual(callback_calls[0], 0)

    def test_claude_turn_detection_accepts_real_status_lines(self) -> None:
        def run_json(command: list[str]) -> dict:
            return {"terminal": {"tail": [
                "The completed response says it was thinking while working.",
                "✻ Forming... (4s · ↑ 13.2k tokens)",
            ]}}

        self.assertTrue(terminal_turn_started("term-claude", adapter="claude", run_json=run_json))

        def completed_run_json(command: list[str]) -> dict:
            return {"terminal": {"tail": [
                "The completed response says it was thinking while working.",
            ]}}

        self.assertFalse(terminal_turn_started("term-claude", adapter="claude", run_json=completed_run_json))

    def test_readiness_tells_a_busy_pane_from_one_that_cannot_be_probed(self) -> None:
        """A refused probe is its own answer: it is neither a ready pane nor a working one."""
        def answer(payload: dict | Exception):
            def run_json(_command: list[str]) -> dict:
                if isinstance(payload, Exception):
                    raise payload
                return payload

            return run_json

        self.assertEqual(
            terminal_readiness("term", run_json=answer({"wait": {"satisfied": True}})),
            READINESS_READY,
        )
        # A pane Orca reports as held in a dialog answers, and answers "not ready, not working":
        # a prompt sent to it went nowhere, so the delivery path re-enters it.
        self.assertEqual(
            terminal_readiness(
                "term", run_json=answer({"wait": {"satisfied": False, "blockedReason": "modal"}})
            ),
            READINESS_BLOCKED,
        )
        # A pane that is simply working answers the same way, without a reason.
        self.assertEqual(
            terminal_readiness("term", run_json=answer({"wait": {"satisfied": False}})),
            READINESS_BUSY,
        )
        # The condition not being met in time comes back as a failed command carrying its code.
        self.assertEqual(
            terminal_readiness("term", run_json=answer(HostError(TIMEOUT_WAIT_FAILURE))),
            READINESS_BUSY,
        )
        # So does a pane Orca looked at and could not call ready: the CLI exits non-zero for it
        # too, and the answer survives only in the body it printed.
        self.assertEqual(
            terminal_readiness(
                "term",
                run_json=answer(HostError(f"orca terminal wait failed: {BLOCKED_PANE_WAIT_BODY}")),
            ),
            READINESS_BLOCKED,
        )
        for failure in (
            HostError(STALE_HANDLE_WAIT_FAILURE),
            HostError("orca terminal wait failed: [Errno 2] No such file or directory: 'orca'"),
        ):
            self.assertEqual(
                terminal_readiness("term", run_json=answer(failure)), READINESS_UNKNOWN
            )

    def test_claude_delivery_accepts_its_durable_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            projects = Path(tmp) / "claude-projects"
            session = projects / str(workspace.resolve()).replace("/", "-") / "session.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                json.dumps({"type": "user", "timestamp": "2099-01-02T03:04:05Z"}) + "\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def run_json(command: list[str]) -> dict:
                calls.append(command)
                if command[2] == "read":
                    return {"terminal": {"tail": ["Claude Code", "❯"]}}
                return {}

            with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(projects)}):
                deliver_interactive_prompt(
                    "term-claude",
                    "Read TASK.md",
                    run_json=run_json,
                    confirm=lambda sent_at: bool(latest_claude_user_turn_for(str(workspace), sent_at)),
                )

        self.assertTrue(any(command[2] == "send" for command in calls))

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
            )

        self.assertEqual(handle.handle, "term-tui")
        create_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "create"])
        wait_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "wait"])
        send_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "send"])
        self.assertLess(create_i, wait_i)
        self.assertLess(wait_i, send_i)
        self.assertEqual(host.calls[wait_i][host.calls[wait_i].index("--terminal") + 1], "term-tui")
        self.assertIn("Read TASK.md", host.calls[send_i][host.calls[send_i].index("--text") + 1])
        # Nothing selected the interactive shape: the launcher was asked for a Codex head and
        # there is no other kind to ask for.
        self.assertEqual(host.catalog.heads, ["codex"])

    def test_a_card_still_carrying_exec_is_launched_and_prompted_the_same_way(self) -> None:
        """The route a restored or long-lived card takes: legacy `codex_launch_mode` on the card.

        It reaches the bring-up exactly as it is stored and changes nothing. The pane is waited
        for and the prompt is delivered into it, which is the one Codex bring-up there is.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(workspace, [{"terminal": {"tail": ["\x1b[1mWorking\x1b[0m"]}}])

            host._launch(
                str(workspace),
                "title",
                "codex",
                "TASK.md",
                role="worker",
                env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                task={"ref": "secretary-1173", "routing": {"codex_launch_mode": "exec"}},
            )

        create_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "create"])
        wait_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "wait"])
        send_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "send"])
        self.assertLess(create_i, wait_i)
        self.assertLess(wait_i, send_i)
        launched = host.calls[create_i][host.calls[create_i].index("--command") + 1]
        self.assertNotIn("codex exec", launched)

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
                launch_prompt="The full task is in TASK.md. Read it first.",
            )

        send_i = next(i for i, call in enumerate(host.calls) if call[:3] == ["orca", "terminal", "send"])
        delivered = host.calls[send_i][host.calls[send_i].index("--text") + 1]
        self.assertEqual(delivered, "The full task is in TASK.md. Read it first.")
        self.assertNotIn("full spec body", delivered)

    def test_tui_delivery_resends_enter_when_the_pane_did_not_take_the_prompt(self) -> None:
        """A Codex launch is delivered by the shared path, and re-entered the same way.

        Replaces the composer-text check this used to make: the prompt is entered again because
        Orca says the pane took nothing, here the update dialog that swallows the first Enter, and
        it is confirmed by this role's own criterion, its turn having started.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(
                workspace,
                [
                    {"terminal": {"tail": ["\u203a Read TASK.md"]}},
                    {"terminal": {"tail": ["thinking"]}},
                ],
                waits=[
                    {"wait": {"satisfied": True}},
                    {"wait": {"satisfied": False, "blockedReason": "codex-update-prompt"}},
                ],
            )

            with mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0), \
                 mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01):
                host._launch(
                    str(workspace),
                    "title",
                    "codex",
                    "TASK.md",
                    role="worker",
                    env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
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

            with mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_TIMEOUT_S", 0.03), \
                 mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01), \
                 mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0), \
                 mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RETRIES", 1), \
                 self.assertRaises(HostError):
                host._launch(
                    str(workspace),
                    "title",
                    "codex",
                    "TASK.md",
                    role="worker",
                    env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
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
                 mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01):
                handle = host._launch(
                    str(workspace),
                    "title",
                    "codex",
                    "TASK.md",
                    role="worker",
                    env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
                )

        self.assertEqual(handle.handle, "term-tui")
        self.assertNotIn(["orca", "terminal", "close", "--terminal", "term-tui", "--json"], host.calls)

    def test_tui_activity_uses_rollout_mtime_for_every_codex_head(self) -> None:
        """Alternate-screen TUI output gets a progress signal; another adapter gets none.

        The card's retired `codex_launch_mode` no longer gates it either: a legacy `exec` on the
        card cannot take the supplement away from the interactive head that actually ran.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = ActivityCatalog()
            host = CommandHostRuntime(catalog, root / "data", mode="noop")  # type: ignore[arg-type]
            codex_record = DispatcherRecord(
                worker="worker", workspace=str(root), handle="term", head="codex-tui",
                review_head="codex-tui", attempt_id="attempt", comment_baseline=0,
                review_baseline=0, state="claimed", claimed_at=0.0,
            )
            claude_record = DispatcherRecord(
                worker="worker", workspace=str(root), handle="term", head="claude-opus",
                review_head="claude-opus", attempt_id="attempt", comment_baseline=0,
                review_baseline=0, state="claimed", claimed_at=0.0,
            )

            with mock.patch(
                "triggered_agents.agents.pipeline.codex_sessions.latest_activity_for",
                return_value=123.0,
            ) as latest_activity:
                self.assertEqual(host.codex_tui_activity({"routing": {}}, codex_record, "worker"), 123.0)
                self.assertEqual(
                    host.codex_tui_activity(
                        {"routing": {"codex_launch_mode": "exec"}}, codex_record, "worker"
                    ),
                    123.0,
                )
                self.assertIsNone(host.codex_tui_activity({"routing": {}}, claude_record, "worker"))

        self.assertEqual(latest_activity.call_args_list, [mock.call(str(root))] * 2)

    def test_tui_activity_ignores_a_head_removed_from_the_snapshot(self) -> None:
        """A stale record cannot turn an optional supplemental signal into an inventory failure."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = ActivityCatalog()
            host = CommandHostRuntime(catalog, root / "data", mode="noop")  # type: ignore[arg-type]
            record = DispatcherRecord(
                worker="worker", workspace=str(root), handle="term", head="removed-head",
                review_head="removed-head", attempt_id="attempt", comment_baseline=0,
                review_baseline=0, state="claimed", claimed_at=0.0,
            )

            self.assertIsNone(host.codex_tui_activity({"routing": {}}, record, "worker"))


class TuiCatalog:
    def __init__(self) -> None:
        self.heads: list[str] = []

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str = "") -> None:
        return None

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> HeadLaunch:
        self.heads.append(head)
        return HeadLaunch(
            "CODEX_HOME=/tmp/codex-home codex --dangerously-bypass-approvals-and-sandbox",
            prompt_after_start=True,
        )


class ActivityCatalog:
    def __init__(self) -> None:
        self._heads = {
            "profiles": {
                "codex-tui": {"adapter": "codex", "codex_mode": "tui"},
                "claude-opus": {"adapter": "claude", "model": "opus"},
            },
        }

    def _head_profile(self, head: str) -> dict:
        try:
            return self._heads["profiles"][head]
        except KeyError:
            raise HostError(f"unknown head {head}") from None


class RecordingTuiHost(CommandHostRuntime):
    def __init__(
        self,
        root: Path,
        reads: list[dict],
        *,
        fail_ops: set[str] | None = None,
        waits: list[dict] | None = None,
    ) -> None:
        self.catalog = TuiCatalog()
        super().__init__(self.catalog, root, mode="real")  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self.reads = list(reads)
        # What Orca answers each `terminal wait`, the last one repeating. The default is a pane it
        # calls ready, which is how a prompt that was not taken looks.
        self.waits = list(waits or [])
        self.fail_ops = fail_ops or set()

    def _next(self, answers: list[dict], default: dict) -> dict:
        if not answers:
            return default
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def _run_json(self, args: list[str]) -> dict:
        self.calls.append(args)
        if args[:3] == ["orca", "terminal", "create"]:
            return {"terminal": {"handle": "term-tui"}}
        if args[:3] == ["orca", "terminal", "wait"] and "wait" in self.fail_ops:
            raise HostError("terminal wait failed")
        if args[:3] == ["orca", "terminal", "send"] and "send" in self.fail_ops:
            raise HostError("terminal send failed")
        if args[:3] == ["orca", "terminal", "wait"]:
            return self._next(self.waits, {})
        if args[:3] == ["orca", "terminal", "read"]:
            return self._next(self.reads, {"terminal": {"tail": []}})
        return {}
