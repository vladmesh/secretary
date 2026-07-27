from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.runtime import dispatch
from triggered_agents.runtime import state as runtime_state


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
            {"terminal": {"tail": ["Claude Code", "Thinking"]}},
        ])
        sent: list[list[str]] = []
        patches = self._common_run_patches() + [
            mock.patch.object(dispatch, "_orca_json", side_effect=lambda args: next(screens)),
            mock.patch.object(dispatch, "_orca", side_effect=sent.append),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
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
