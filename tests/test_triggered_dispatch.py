from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from triggered_agents.runtime import dispatch
from triggered_agents.runtime import state as runtime_state
from triggered_agents.runtime import tui_delivery


class TriggeredDispatchReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name) / "state"
        self.workspace = str(Path(self.tmp.name) / "workspace")
        Path(self.workspace).mkdir()
        self.env = mock.patch.dict(os.environ, {"TA_STATE": str(self.state_root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state_root_patch = mock.patch.object(runtime_state, "STATE_ROOT", self.state_root)
        self.state_root_patch.start()
        self.addCleanup(self.state_root_patch.stop)
        self.command = dispatch.DispatchCommand("/retro", "claude '/retro'", None)
        self.term = {"handle": "term-live", "lastOutputAt": 1}

    def _common_run_patches(self):
        return [
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
            mock.patch.object(dispatch, "_agent_terminals", return_value=[self.term]),
            mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)),
            mock.patch.object(dispatch, "_is_idle", return_value=True),
            mock.patch.object(dispatch, "_is_ephemeral", return_value=False),
            mock.patch.object(dispatch, "_reuse_head_is_red", return_value=False),
            mock.patch.object(dispatch, "_dispatch_command", return_value=self.command),
            mock.patch("triggered_agents.runtime.dispatch.time.sleep"),
        ]

    def _actions(self, agent: str = "retro") -> list[str]:
        runs = self.state_root / agent / "runs.jsonl"
        return [json.loads(line)["action"] for line in runs.read_text(encoding="utf-8").splitlines()]

    def test_live_agent_repl_is_reused_after_delivery_is_confirmed(self) -> None:
        screens = iter([
            {"terminal": {"tail": ["Claude Code", "❯"]}},
            {"terminal": {"tail": ["Claude Code", "✻ Forming... (4s · ↑ 13.2k tokens)"]}},
        ])
        sent: list[list[str]] = []
        patches = self._common_run_patches() + [
            mock.patch.object(dispatch, "_orca_json", side_effect=lambda args: next(screens)),
            mock.patch.object(dispatch, "_orca", side_effect=sent.append),
            mock.patch.object(dispatch, "_claude_user_turn_after", side_effect=[False, True]),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            self.assertEqual(dispatch.run("retro"), 0)

        self.assertEqual([call[call.index("--text") + 1] for call in sent], ["/clear", "/retro"])
        self.assertEqual(self._actions(), ["reused"])

    def test_shell_panel_restarts_without_sending_slash_commands(self) -> None:
        sent: list[list[str]] = []
        fresh = mock.Mock(return_value=self.command)
        patches = self._common_run_patches() + [
            mock.patch.object(
                dispatch,
                "_orca_json",
                return_value={"terminal": {"tail": ["dev@host:~/workspace$"]}},
            ),
            mock.patch.object(dispatch, "_orca", side_effect=sent.append),
            mock.patch.object(dispatch, "_stop_and_confirm", return_value=True),
            mock.patch.object(dispatch, "_spawn_fresh_terminal", fresh),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
            self.assertEqual(dispatch.run("retro"), 0)

        fresh.assert_called_once()
        self.assertEqual(sent, [])
        self.assertEqual(self._actions(), ["warm-repl-restart"])

    def test_shell_output_above_the_repl_prompt_does_not_restart_a_live_agent(self) -> None:
        screen = "\n".join(["Claude Code", "dev@host:~/workspace$ from Bash output", "❯"])
        with mock.patch.object(dispatch, "_terminal_screen", return_value=screen):
            self.assertTrue(dispatch._agent_repl_visible("term-live"))

    def test_claude_user_turn_after_reads_the_workspace_session_log(self) -> None:
        projects = Path(self.tmp.name) / "claude-projects"
        slug = self.workspace.replace("/", "-")
        session = projects / slug / "session.jsonl"
        session.parent.mkdir(parents=True)
        since = time.time()
        session.write_text(
            json.dumps({
                "type": "user",
                "timestamp": datetime.fromtimestamp(since + 1, timezone.utc).isoformat(),
            }) + "\n",
            encoding="utf-8",
        )
        # The reader skips a log whose mtime is not past `since`, and a filesystem whose timestamp
        # granularity is coarser than `time.time()` stamps this write a hair before it. Pin the
        # mtime so the test asks about the record it wrote rather than about clock resolution.
        os.utime(session, (since + 1, since + 1))
        with mock.patch.dict(os.environ, {"TA_CLAUDE_PROJECTS": str(projects)}):
            self.assertTrue(dispatch._claude_user_turn_after(self.workspace, since))

    def test_unconfirmed_reuse_recovers_steward_card_and_fails_dispatch(self) -> None:
        command = dispatch.DispatchCommand("/steward --card secretary-817", "claude", None, "secretary-817")
        screens = iter([
            {"terminal": {"tail": ["Claude Code", "❯"]}},
            {"terminal": {"tail": ["Claude Code", "❯"]}},
        ])
        moved: list[tuple] = []
        patches = self._common_run_patches() + [
            mock.patch.object(dispatch, "_fresh_steward_report_in_progress", return_value=None),
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_orca_json", side_effect=lambda args: next(screens)),
            mock.patch.object(dispatch, "_orca"),
            mock.patch.object(dispatch, "REUSE_DELIVERY_TIMEOUT_S", 0),
            mock.patch("triggered_agents.agents.pipeline.ops.move_card", side_effect=lambda *args, **kwargs: moved.append((args, kwargs))),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12], patches[13]:
            with self.assertRaises(dispatch.ReuseDeliveryError):
                dispatch.run("steward")

        self.assertEqual(moved[0][0], ("steward", "secretary-817", "Done"))
        self.assertIn("dispatch failed", moved[0][1]["reason"])
        self.assertEqual(self._actions("steward"), ["dispatch-recovery", "reuse-delivery-unconfirmed"])


class TriggeredCodexHeadTests(unittest.TestCase):
    """A service head on Codex is an interactive session, brought up and then prompted.

    Curator, retro and steward launch through the same registry as a worker, so they inherited the
    TUI-only rule with it (secretary-1173): their command carries no skill, and the tick has to put
    it in front of the head itself rather than assuming `terminal create` finished the dispatch.
    """

    REGISTRY = {
        "resources": {"openai-sub": {"account": "openai-subscription", "probe": "true"}},
        "profiles": {"codex": {"resource": "openai-sub", "adapter": "codex", "fallback": []}},
        "role_defaults": {"retro": "codex"},
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = str(Path(self.tmp.name) / "workspace")
        Path(self.workspace).mkdir()
        from triggered_agents.agents.pipeline import heads as pipeline_heads
        self.registry = pipeline_heads.Registry(
            self.REGISTRY["resources"], self.REGISTRY["profiles"], self.REGISTRY["role_defaults"]
        )

    def test_a_codex_service_head_is_launched_without_its_skill(self) -> None:
        from triggered_agents.agents.pipeline import health as pipeline_health
        from triggered_agents.agents.pipeline import heads as pipeline_heads

        with mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/retro"}), \
             mock.patch.object(dispatch, "_workspace", return_value=self.workspace), \
             mock.patch.object(pipeline_heads, "load_registry", return_value=self.registry), \
             mock.patch.object(pipeline_health, "refresh", return_value={}), \
             mock.patch.object(pipeline_health, "resolve_head", return_value="codex"):
            skill, launch, profile, after_start = dispatch._launch_cmd("retro")

        self.assertEqual((skill, profile), ("/retro", "codex"))
        self.assertTrue(after_start)
        self.assertNotIn("codex exec", launch)
        self.assertNotIn("/retro", launch)

    def _orca_calls(self, calls: list[list[str]]):
        """Answer the shared interactive delivery path's Orca calls: idle pane, accepted send."""
        def run(args: list[str]) -> dict:
            calls.append(list(args))
            if "wait" in args:
                return {"wait": {"satisfied": True}}
            return {}

        return run

    def _sent_text(self, calls: list[list[str]]) -> list[str]:
        return [call[call.index("--text") + 1] for call in calls if "send" in call]

    def test_the_fresh_terminal_is_given_its_skill_once_the_pane_is_ready(self) -> None:
        """Through the product's one interactive delivery path, not a launch-only one."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)
        calls: list[list[str]] = []
        state = mock.Mock()

        with mock.patch.object(dispatch, "_dispatch_command", return_value=command), \
             mock.patch.object(dispatch, "_ensure_claude_ready"), \
             mock.patch.object(dispatch, "_create_terminal", return_value="term-codex"), \
             mock.patch.object(dispatch, "_orca_json", side_effect=self._orca_calls(calls)), \
             mock.patch.object(dispatch, "_orca") as legacy_send, \
             mock.patch.object(dispatch, "_codex_turn_after", return_value=True), \
             mock.patch("triggered_agents.runtime.tui_delivery.time.sleep"):
            dispatch._spawn_fresh_terminal("retro", None, self.workspace, state, "dispatch")

        self.assertEqual(self._sent_text(calls), ["/retro"])
        self.assertIn(["terminal", "wait", "--terminal", "term-codex", "--for", "tui-idle",
                       "--timeout-ms", str(tui_delivery.TUI_IDLE_TIMEOUT_MS)], calls)
        legacy_send.assert_not_called()

    def test_a_warm_codex_head_is_prompted_through_the_same_path(self) -> None:
        """Warm reuse is the same delivery contract: no second transport for an older pane."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)
        calls: list[list[str]] = []

        with mock.patch.object(dispatch, "_dispatch_command", return_value=command), \
             mock.patch.object(dispatch, "_orca_json", side_effect=self._orca_calls(calls)), \
             mock.patch.object(dispatch, "_orca") as legacy_send, \
             mock.patch.object(dispatch, "_codex_turn_after", return_value=True), \
             mock.patch.object(dispatch, "_claude_user_turn_after", return_value=False), \
             mock.patch("triggered_agents.runtime.tui_delivery.time.sleep"):
            dispatch._send_reuse_dispatch(
                "retro", None, "term-codex", self.workspace, mock.Mock(), "dispatch"
            )

        self.assertEqual(self._sent_text(calls), ["/retro"])
        legacy_send.assert_not_called()

    def test_an_unconfirmed_codex_prompt_fails_on_the_shared_delivery_contract(self) -> None:
        """A pane that takes the prompt but never records a turn is the shared path's failure."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)

        with mock.patch.object(dispatch, "_dispatch_command", return_value=command), \
             mock.patch.object(dispatch, "_ensure_claude_ready"), \
             mock.patch.object(dispatch, "_create_terminal", return_value="term-codex"), \
             mock.patch.object(dispatch, "_orca_json", side_effect=self._orca_calls([])), \
             mock.patch.object(dispatch, "_codex_turn_after", return_value=False), \
             mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_TIMEOUT_S", 0.05), \
             mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01), \
             mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0):
            with self.assertRaises(tui_delivery.TuiDeliveryError):
                dispatch._spawn_fresh_terminal("retro", None, self.workspace, mock.Mock(), "dispatch")

    def test_a_claude_service_head_is_not_typed_at_after_its_launch(self) -> None:
        """Its command already seeds the skill; sending it again would run the agent twice."""
        command = dispatch.DispatchCommand("/retro", "claude '/retro'", "claude-opus", None)
        sent: list[list[str]] = []

        with mock.patch.object(dispatch, "_dispatch_command", return_value=command), \
             mock.patch.object(dispatch, "_ensure_claude_ready"), \
             mock.patch.object(dispatch, "_create_terminal", return_value="term-claude"), \
             mock.patch.object(dispatch, "_orca", side_effect=sent.append):
            dispatch._spawn_fresh_terminal("retro", None, self.workspace, mock.Mock(), "dispatch")

        self.assertEqual(sent, [])

    def test_a_pane_that_never_becomes_ready_fails_the_dispatch(self) -> None:
        """The terminal is left up on purpose: the next tick finds it idle and re-sends there,
        instead of a second head being created beside a silent one."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)
        state = mock.Mock()

        def never_ready(args: list[str]) -> dict:
            if "wait" in args:
                raise RuntimeError('orca wait failed: {"error": {"code": "timeout"}}')
            raise AssertionError("a pane that never came up must not be sent a prompt")

        with mock.patch.object(dispatch, "_dispatch_command", return_value=command), \
             mock.patch.object(dispatch, "_ensure_claude_ready"), \
             mock.patch.object(dispatch, "_create_terminal", return_value="term-codex"), \
             mock.patch.object(dispatch, "_orca_json", side_effect=never_ready), \
             mock.patch.object(dispatch, "_orca") as orca, \
             mock.patch.object(dispatch, "_stop_and_confirm") as stop:
            with self.assertRaises(RuntimeError):
                dispatch._spawn_fresh_terminal("retro", None, self.workspace, state, "dispatch")

        orca.assert_not_called()
        stop.assert_not_called()
        state.save_active_report.assert_not_called()

    def test_an_agent_pinned_to_an_old_codex_id_still_resolves(self) -> None:
        """The spec's last-resort head is a product-side id; the installation republished its own."""
        from triggered_agents.agents.pipeline import heads as pipeline_heads
        registry = pipeline_heads.Registry(
            self.REGISTRY["resources"], self.REGISTRY["profiles"], {}
        )

        with mock.patch.object(pipeline_heads, "load_registry", return_value=registry):
            self.assertEqual(dispatch._preferred_head("retro", {"head": "codex-terra"}), "codex")
            self.assertIsNone(dispatch._preferred_head("retro", {}))

    def test_a_service_head_pinned_to_an_old_codex_id_stays_in_family(self) -> None:
        """The same registry a worker can meet: an old Codex id republished as a Claude profile.

        A service agent pinned to that id asked for Codex, so it reaches the interactive Codex
        head this registry does publish — and when it publishes none, the dispatch is refused
        rather than quietly rendered as some other family's launch command.
        """
        from triggered_agents.agents.pipeline import heads as pipeline_heads
        resources = self.REGISTRY["resources"]
        with_codex = pipeline_heads.Registry(resources, {
            "codex-terra": {"resource": "openai-sub", "adapter": "claude", "fallback": []},
            "codex": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
        }, {})
        claude_only = pipeline_heads.Registry(resources, {
            "codex-terra": {"resource": "openai-sub", "adapter": "claude", "fallback": []},
        }, {})

        with mock.patch.object(pipeline_heads, "load_registry", return_value=with_codex):
            self.assertEqual(dispatch._preferred_head("retro", {"head": "codex-terra"}), "codex")
        with mock.patch.object(pipeline_heads, "load_registry", return_value=claude_only), \
             mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/retro", "head": "codex-terra"}), \
             mock.patch.object(dispatch, "_workspace", return_value=self.workspace):
            with self.assertRaises(pipeline_heads.HeadRegistryError):
                dispatch._preferred_head("retro", {"head": "codex-terra"})
            with self.assertRaises(pipeline_heads.HeadRegistryError):
                dispatch._launch_cmd("retro")
