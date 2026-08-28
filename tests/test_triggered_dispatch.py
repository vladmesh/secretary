from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from triggered_agents.runtime import codex_preflight, dispatch, tui_delivery
from triggered_agents.runtime import state as runtime_state
from triggered_agents.runtime.head import HeadSpec
from triggered_agents.runtime.agent_prompt_transport import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
)
from triggered_agents.runtime.claude_sessions import claude_project_dir_name
from triggered_agents.runtime.pane_host import Pane, PaneHostError


class FakeSessionHost:
    """A session manager for a test: it records what it was asked and answers what it was given.

    The scheduler reaches Orca through `SessionHost` and nothing else (secretary-1416), so a test
    of what a tick does to its terminals is a test against one of these — no subprocess, no `orca`
    on the box, and every pane verb observable as the call it is rather than as an argument vector
    somebody has to re-parse.
    """

    def __init__(
        self,
        *,
        panes: tuple[Pane, ...] = (),
        screens: tuple[str, ...] = (),
        idle: bool = True,
        wait_error: BaseException | None = None,
        list_error: BaseException | None = None,
    ) -> None:
        self._panes = list(panes)
        self._screens = list(screens)
        self._idle = idle
        self._wait_error = wait_error
        self._list_error = list_error
        self.sends: list[str] = []
        self.enters: list[bool] = []
        self.waits: list[tuple[str, int]] = []
        self.reads: list[tuple[str, int | None]] = []
        self.opened: list[tuple[str, str, str]] = []
        self.closed: list[str] = []
        self.stopped: list[str] = []

    # PaneHost
    def send(self, handle: str, text: str, *, enter: bool) -> dict:
        self.sends.append(text)
        self.enters.append(enter)
        return {"send": {"accepted": True, "bytesWritten": len(text.encode()) + (1 if enter else 0)}}

    def read(self, handle: str, *, limit: int | None = None) -> dict:
        self.reads.append((handle, limit))
        screen = (
            self._screens.pop(0) if len(self._screens) > 1 else (self._screens[0] if self._screens else "")
        )
        return {"terminal": {"tail": screen.splitlines()}}

    def wait_idle(self, handle: str, *, timeout_ms: int) -> dict:
        self.waits.append((handle, timeout_ms))
        if self._wait_error is not None:
            raise self._wait_error
        return {"wait": {"satisfied": self._idle}}

    # SessionHost
    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        self.opened.append((workspace, title, command))
        pane = Pane(handle=f"term-{len(self.opened)}", leaf=f"leaf-{len(self.opened)}", title=title)
        self._panes.append(pane)
        return pane

    def split_pane(self, handle: str, command: str) -> Pane:
        raise AssertionError("the scheduler never splits a pane")

    def rename_pane(self, handle: str, title: str) -> None:
        raise AssertionError("the scheduler never renames a pane")

    def close_pane(self, handle: str) -> None:
        self.closed.append(handle)
        self._panes = [pane for pane in self._panes if pane.handle != handle]

    def panes(self, workspace: str) -> list[Pane]:
        if self._list_error is not None:
            raise self._list_error
        return list(self._panes)

    def stop_workspace(self, workspace: str) -> None:
        self.stopped.append(workspace)
        self._panes = []


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
        self.term = Pane(
            handle="term-live", leaf="leaf-live", title="triggered-agent:retro", last_output_at=1.0
        )

    def test_scheduled_memory_launch_binds_the_bearer_to_its_heartbeat(self):
        data_dir = Path(self.tmp.name) / "data"
        spec = HeadSpec(profile_id="test", adapter="codex")
        with mock.patch.object(dispatch, "_installation_data_dir", return_value=data_dir):
            run = dispatch._standing_memory_run("curator", spec, self.workspace, "curator-run")
            command = dispatch._memory_heartbeat(run, dispatch._memory_bound_launch("curator", run, "codex"))

        self.assertIn("secretary.memory.grant_env", command)
        self.assertIn(str(Path(run.pid_file)), command)
        self.assertNotIn("SECRETARY_MEMORY_ACCESS_TOKEN=", command)

    def _common_run_patches(self):
        """Everything a warm-reuse tick decides that is not about terminals.

        The pane inventory is deliberately NOT among them any more: it comes from the fake session
        host, so what these tests exercise is the real recognition, survivor and reuse path over an
        inventory a session manager answered (secretary-1416).
        """
        return [
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
            mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)),
            mock.patch.object(dispatch, "_is_idle", return_value=True),
            mock.patch.object(dispatch, "_is_ephemeral", return_value=False),
            mock.patch.object(dispatch, "_reuse_head_is_red", return_value=False),
            mock.patch.object(dispatch, "_dispatch_command", return_value=self.command),
            mock.patch("triggered_agents.runtime.dispatch.time.sleep"),
        ]

    def _running(self, patches):
        stack = contextlib.ExitStack()
        for patch in patches:
            stack.enter_context(patch)
        return stack

    def _actions(self, agent: str = "retro") -> list[str]:
        runs = self.state_root / agent / "runs.jsonl"
        return [json.loads(line)["action"] for line in runs.read_text(encoding="utf-8").splitlines()]

    def test_unreadable_pause_state_blocks_dispatch_and_is_reported(self) -> None:
        output = io.StringIO()
        with (
            mock.patch(
                "triggered_agents.agents.pipeline.pause.is_paused",
                side_effect=OSError("pause.json: input/output error"),
            ),
            contextlib.redirect_stderr(output),
        ):
            self.assertTrue(dispatch._pipeline_paused())

        self.assertIn("pipeline pause state is unreadable; refusing dispatch", output.getvalue())

    def test_unreadable_automation_spec_disables_warm_reuse(self) -> None:
        with mock.patch.object(dispatch, "_load_spec", side_effect=ValueError("malformed automation.toml")):
            self.assertTrue(dispatch._is_ephemeral("curator"))

    def test_live_agent_repl_is_reused_after_delivery_is_confirmed(self) -> None:
        host = FakeSessionHost(
            panes=(self.term,),
            screens=(
                "Claude Code\n❯",
                "Claude Code\n✻ Forming... (4s · ↑ 13.2k tokens)",
            ),
        )
        patches = self._common_run_patches() + [
            mock.patch.object(dispatch, "_claude_user_turn_after", side_effect=[False, True]),
        ]
        with self._running(patches):
            self.assertEqual(dispatch.run("retro", host=host), 0)

        self.assertEqual(host.sends, ["/clear", "/retro"])
        self.assertEqual(host.enters, [True, True])
        self.assertEqual(self._actions(), ["reused"])

    def test_the_reused_terminal_is_the_one_the_survivor_rule_picked(self) -> None:
        """Warm reuse addresses the pane that printed last, and closes the duplicates beside it.

        The `/clear`, the skill and the saved handle all name that one pane; every other terminal
        the inventory recognised as this agent's is closed, which is the legacy-duplicate cleanup
        this branch has always done (secretary-1416 keeps it a `close_pane` per extra pane, not a
        workspace stop).
        """
        survivor = Pane(handle="term-new", title="triggered-agent:retro", last_output_at=200.0)
        stale = Pane(handle="term-old", title="triggered-agent:retro", last_output_at=100.0)
        host = FakeSessionHost(panes=(stale, survivor), screens=("Claude Code\n❯",))
        patches = [
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
            mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)),
            mock.patch.object(dispatch, "_is_ephemeral", return_value=False),
            mock.patch.object(dispatch, "_reuse_head_is_red", return_value=False),
            mock.patch.object(dispatch, "_dispatch_command", return_value=self.command),
            mock.patch("triggered_agents.runtime.dispatch.time.sleep"),
            mock.patch.object(dispatch, "_claude_user_turn_after", return_value=True),
        ]
        with self._running(patches):
            self.assertEqual(dispatch.run("retro", host=host), 0)

        self.assertEqual(host.closed, ["term-old"])
        self.assertEqual(host.stopped, [])
        self.assertEqual([handle for handle, _ in host.waits], ["term-new"])
        self.assertEqual(self._actions(), ["reused"])
        self.assertEqual(runtime_state.AgentState("retro").load_terminal_handle(), "term-new")

    def test_the_idle_probe_keeps_its_condition_its_budget_and_its_two_readings(self) -> None:
        """`tui-idle` within `IDLE_PROBE_MS`; a refused or timed-out probe is busy, never idle."""
        answered = FakeSessionHost()
        self.assertTrue(dispatch._is_idle("term-live", host=answered))
        self.assertEqual(answered.waits, [("term-live", dispatch.IDLE_PROBE_MS)])

        self.assertFalse(dispatch._is_idle("term-live", host=FakeSessionHost(idle=False)))
        refused = FakeSessionHost(wait_error=RuntimeError("orca terminal wait failed"))
        self.assertFalse(dispatch._is_idle("term-live", host=refused))
        timed_out = FakeSessionHost(
            wait_error=subprocess.TimeoutExpired(cmd=["orca"], timeout=dispatch.ORCA_TIMEOUT_S)
        )
        self.assertFalse(dispatch._is_idle("term-live", host=timed_out))

    def test_a_busy_terminal_is_left_alone_on_the_clock_the_inventory_carries(self) -> None:
        """The watchdog reads the pane's own last-output time, in seconds, off the inventory."""
        now = time.time()
        working = Pane(handle="term-live", title="triggered-agent:retro", last_output_at=now - 5)
        host = FakeSessionHost(panes=(working,), idle=False)
        patches = [
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
            mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)),
            mock.patch.object(dispatch, "_is_ephemeral", return_value=False),
            mock.patch.object(dispatch, "_dispatch_command", return_value=self.command),
        ]
        with self._running(patches):
            self.assertEqual(dispatch.run("retro", host=host), 0)

        self.assertEqual(self._actions(), ["busy-skip"])
        self.assertEqual(host.sends, [])
        self.assertEqual(host.stopped, [])

    def test_a_stop_is_a_worktree_stop_confirmed_by_a_fresh_inventory(self) -> None:
        """Not a close of the head's own pane: the whole workspace goes quiet, and the proof that
        it did is the list that follows, never the stop's own answer."""
        host = FakeSessionHost(panes=(self.term,))
        state = runtime_state.AgentState("retro")
        with mock.patch("triggered_agents.runtime.dispatch.time.sleep"):
            self.assertTrue(dispatch._stop_and_confirm(self.workspace, state, host=host))

        self.assertEqual(host.stopped, [self.workspace])
        self.assertEqual(host.closed, [])

    def test_a_stop_the_host_refuses_is_still_judged_by_the_inventory(self) -> None:
        """`terminal stop`'s refusal was dropped before the seam and is dropped behind it: what
        decides is whether the workspace is empty afterwards."""

        class RefusingStop(FakeSessionHost):
            def stop_workspace(self, workspace: str) -> None:
                super().stop_workspace(workspace)
                raise PaneHostError("orca terminal stop failed")

        host = RefusingStop(panes=(self.term,))
        state = runtime_state.AgentState("retro")
        with mock.patch("triggered_agents.runtime.dispatch.time.sleep"):
            self.assertTrue(dispatch._stop_and_confirm(self.workspace, state, host=host))
        self.assertEqual(host.stopped, [self.workspace])

    def test_a_terminal_created_moments_ago_is_not_read_as_never_spawned(self) -> None:
        """The visibility grace survives the move: an empty inventory right after a create is a
        pane Orca has not registered yet, not a workspace to put a second head in."""
        host = FakeSessionHost()
        state = runtime_state.AgentState("retro")
        state.save_terminal_handle("term-live", created_at=time.time())
        fresh = mock.Mock(return_value=self.command)
        patches = [
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
            mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)),
            mock.patch.object(dispatch, "_is_ephemeral", return_value=False),
            mock.patch.object(dispatch, "_spawn_fresh_terminal", fresh),
        ]
        with self._running(patches):
            self.assertEqual(dispatch.run("retro", host=host), 0)

        fresh.assert_not_called()
        self.assertEqual(host.opened, [])
        self.assertEqual(self._actions(), ["recent-create-guard"])

    def test_shell_panel_restarts_without_sending_slash_commands(self) -> None:
        host = FakeSessionHost(panes=(self.term,), screens=("dev@host:~/workspace$",))
        fresh = mock.Mock(return_value=self.command)
        patches = self._common_run_patches() + [
            mock.patch.object(dispatch, "_stop_and_confirm", return_value=True),
            mock.patch.object(dispatch, "_spawn_fresh_terminal", fresh),
        ]
        with self._running(patches):
            self.assertEqual(dispatch.run("retro", host=host), 0)

        fresh.assert_called_once()
        self.assertEqual(host.sends, [])
        self.assertEqual(self._actions(), ["warm-repl-restart"])

    def test_the_panel_is_read_as_a_bounded_screen_not_a_whole_scrollback(self) -> None:
        """The 200-line window the REPL check has always been decided on travels with the read."""
        host = FakeSessionHost(screens=("Claude Code\n❯",))
        self.assertTrue(dispatch._agent_repl_visible("term-live", host=host))
        self.assertEqual(host.reads, [("term-live", 200)])

    def test_shell_output_above_the_repl_prompt_does_not_restart_a_live_agent(self) -> None:
        screen = "\n".join(["Claude Code", "dev@host:~/workspace$ from Bash output", "❯"])
        self.assertTrue(dispatch._agent_repl_visible("term-live", host=FakeSessionHost(screens=(screen,))))

    def test_claude_user_turn_after_reads_the_workspace_session_log(self) -> None:
        projects = Path(self.tmp.name) / "claude-projects"
        # Claude Code's own naming, not a second reading of it in the test: a temporary directory
        # name carries underscores as readily as a product workspace does, and spelling the rule
        # here is how the catalogue and the reader drifted apart in the first place.
        slug = claude_project_dir_name(self.workspace)
        session = projects / slug / "session.jsonl"
        session.parent.mkdir(parents=True)
        since = time.time()
        session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": datetime.fromtimestamp(since + 1, UTC).isoformat(),
                }
            )
            + "\n",
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
        host = FakeSessionHost(panes=(self.term,), screens=("Claude Code\n❯",))
        moved: list[tuple] = []
        patches = self._common_run_patches() + [
            mock.patch.object(dispatch, "_fresh_steward_report_in_progress", return_value=None),
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "REUSE_DELIVERY_TIMEOUT_S", 0),
            mock.patch(
                "triggered_agents.agents.pipeline.ops.move_card",
                side_effect=lambda *args, **kwargs: moved.append((args, kwargs)),
            ),
        ]
        with self._running(patches), self.assertRaises(dispatch.ReuseDeliveryError):
            dispatch.run("steward", host=host)

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
        from triggered_agents.agents.pipeline import heads as pipeline_heads
        from triggered_agents.agents.pipeline import health as pipeline_health

        with (
            mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/retro"}),
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
            mock.patch.object(pipeline_heads, "load_registry", return_value=self.registry),
            mock.patch.object(pipeline_health, "refresh", return_value={}),
            mock.patch.object(pipeline_health, "resolve_head", return_value="codex"),
        ):
            skill, launch, profile, after_start, profile_data = dispatch._launch_cmd("retro")

        self.assertEqual((skill, profile), ("/retro", "codex"))
        self.assertTrue(after_start)
        self.assertNotIn("codex exec", launch)
        self.assertNotIn("/retro", launch)
        # The profile the command was rendered from travels with it, so the preflight that has to
        # run before the pane exists reads the same CODEX_HOME the launch names.
        self.assertEqual(profile_data, self.REGISTRY["profiles"]["codex"])

    def test_the_fresh_terminal_is_given_its_skill_once_the_pane_is_ready(self) -> None:
        """Through the product's one interactive delivery path, not a launch-only one."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)
        host = FakeSessionHost()
        state = mock.Mock()

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_ensure_claude_ready"),
            mock.patch.object(dispatch, "_create_terminal", return_value="term-codex"),
            mock.patch.object(dispatch, "_codex_turn_after", return_value=True),
            mock.patch("triggered_agents.runtime.tui_delivery.time.sleep"),
        ):
            dispatch._spawn_fresh_terminal("retro", None, self.workspace, state, "dispatch", host=host)

        self.assertEqual(host.sends, [f"{BRACKETED_PASTE_START}/retro{BRACKETED_PASTE_END}", ""])
        self.assertEqual(host.enters, [False, True])
        self.assertIn(("term-codex", tui_delivery.TUI_IDLE_TIMEOUT_MS), host.waits)
        # The skill itself is never typed the warm-reuse way into a head being brought up.
        self.assertNotIn("/retro", host.sends)

    def test_a_warm_codex_head_is_prompted_through_the_same_path(self) -> None:
        """Warm reuse is the same delivery contract: no second transport for an older pane."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)
        host = FakeSessionHost()

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_codex_turn_after", return_value=True),
            mock.patch.object(dispatch, "_claude_user_turn_after", return_value=False),
            mock.patch("triggered_agents.runtime.tui_delivery.time.sleep"),
        ):
            dispatch._send_reuse_dispatch(
                "retro", None, "term-codex", self.workspace, mock.Mock(), "dispatch", host=host
            )

        self.assertEqual(host.sends, [f"{BRACKETED_PASTE_START}/retro{BRACKETED_PASTE_END}", ""])
        self.assertEqual(host.enters, [False, True])
        self.assertNotIn("/retro", host.sends)

    def test_an_unconfirmed_codex_prompt_fails_on_the_shared_delivery_contract(self) -> None:
        """A pane that takes the prompt but never records a turn is the shared path's failure."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_ensure_claude_ready"),
            mock.patch.object(dispatch, "_create_terminal", return_value="term-codex"),
            mock.patch.object(dispatch, "_codex_turn_after", return_value=False),
            mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_TIMEOUT_S", 0.05),
            mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01),
            mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0),
        ):
            with self.assertRaises(tui_delivery.TuiDeliveryError):
                dispatch._spawn_fresh_terminal(
                    "retro", None, self.workspace, mock.Mock(), "dispatch", host=FakeSessionHost()
                )

    def test_a_claude_service_head_is_not_typed_at_after_its_launch(self) -> None:
        """Its command already seeds the skill; sending it again would run the agent twice."""
        command = dispatch.DispatchCommand("/retro", "claude '/retro'", "claude-opus", None)
        host = FakeSessionHost()

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_ensure_claude_ready"),
            mock.patch.object(dispatch, "_create_terminal", return_value="term-claude"),
        ):
            dispatch._spawn_fresh_terminal("retro", None, self.workspace, mock.Mock(), "dispatch", host=host)

        self.assertEqual(host.sends, [])

    def test_a_pane_that_never_becomes_ready_fails_the_dispatch(self) -> None:
        """The terminal is left up on purpose: the next tick finds it idle and re-sends there,
        instead of a second head being created beside a silent one."""
        command = dispatch.DispatchCommand("/retro", "codex", "codex", None, prompt_after_start=True)
        state = mock.Mock()

        class NeverReady(FakeSessionHost):
            def send(self, handle: str, text: str, *, enter: bool) -> dict:
                raise AssertionError("a pane that never came up must not be sent a prompt")

        host = NeverReady(wait_error=RuntimeError('orca wait failed: {"error": {"code": "timeout"}}'))

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_ensure_claude_ready"),
            mock.patch.object(dispatch, "_create_terminal", return_value="term-codex"),
            mock.patch.object(dispatch, "_stop_and_confirm") as stop,
            self.assertRaises(RuntimeError),
        ):
            dispatch._spawn_fresh_terminal("retro", None, self.workspace, state, "dispatch", host=host)

        self.assertEqual(host.sends, [])
        stop.assert_not_called()
        state.save_active_report.assert_not_called()

    def test_an_agent_pinned_to_an_old_codex_id_still_resolves(self) -> None:
        """The spec's last-resort head is a product-side id; the installation republished its own."""
        from triggered_agents.agents.pipeline import heads as pipeline_heads

        registry = pipeline_heads.Registry(self.REGISTRY["resources"], self.REGISTRY["profiles"], {})

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
        with_codex = pipeline_heads.Registry(
            resources,
            {
                "codex-terra": {"resource": "openai-sub", "adapter": "claude", "fallback": []},
                "codex": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
            },
            {},
        )
        claude_only = pipeline_heads.Registry(
            resources,
            {
                "codex-terra": {"resource": "openai-sub", "adapter": "claude", "fallback": []},
            },
            {},
        )

        with mock.patch.object(pipeline_heads, "load_registry", return_value=with_codex):
            self.assertEqual(dispatch._preferred_head("retro", {"head": "codex-terra"}), "codex")
        with (
            mock.patch.object(pipeline_heads, "load_registry", return_value=claude_only),
            mock.patch.object(
                dispatch, "_load_spec", return_value={"skill": "/retro", "head": "codex-terra"}
            ),
            mock.patch.object(dispatch, "_workspace", return_value=self.workspace),
        ):
            with self.assertRaises(pipeline_heads.HeadRegistryError):
                dispatch._preferred_head("retro", {"head": "codex-terra"})
            with self.assertRaises(pipeline_heads.HeadRegistryError):
                dispatch._launch_cmd("retro")


class TriggeredCodexPreflightTests(unittest.TestCase):
    """A fresh service pane is only created once its workspace can actually hold a Codex head.

    The interactive Codex heads a service tick brings up (curator, retro, steward) are asked about
    directory trust before they will take a prompt, and nobody is sitting in front of that pane to
    answer. So the tick answers it first, through the same preflight the Secretary dispatcher uses,
    and a workspace it cannot prepare fails before a pane exists rather than after (secretary-1173).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = str(self.root / "workspace")
        Path(self.workspace).mkdir()
        self.codex_home = self.root / "codex-home"
        self.profile = {
            "resource": "openai-sub",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "codex_home": str(self.codex_home),
            "fallback": [],
        }
        self.command = dispatch.DispatchCommand(
            "/retro", "codex", "codex", None, prompt_after_start=True, head_profile=self.profile
        )

    def _trusted(self) -> dict:
        import tomllib

        return tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))

    @staticmethod
    def _allowed_attestation(_profile, run, **_kwargs):
        """An independently accepted provider-schema result for shared-boundary tests."""
        return run.with_fanout_policy(
            {
                "version": 1,
                "state": "allowed",
                "terminal_state": "clean",
                "run_id": run.run_id,
                "role": run.role,
                "model": run.spec.model or "",
                "binary_path": "/test/codex",
                "binary_digest": "0" * 64,
                "cli_version": "test-codex",
                "tool_schema_digest": "0" * 64,
                "provider_schema_verdict": "no_callable_child_spawn_surface",
                "events": [],
            }
        )

    def test_a_fresh_codex_service_workspace_is_trusted_before_its_pane_exists(self) -> None:
        order: list[str] = []

        def create(agent, ws, launch, state, profile, *, host):
            order.append("create")
            # What the pane is created against has to be a workspace already recorded as trusted;
            # answering the dialog afterwards would be answering it too late.
            order.append("trusted" if (self.codex_home / "config.toml").is_file() else "untrusted")
            return "term-codex"

        shared_preflight = dispatch.preflight_codex_launch

        def recording_preflight(*args, **kwargs):
            order.append("preflight")
            return shared_preflight(*args, **kwargs)

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=self.command),
            mock.patch.object(dispatch, "_create_terminal", side_effect=create),
            mock.patch.object(codex_preflight, "attest_codex_fanout", side_effect=self._allowed_attestation),
            mock.patch.object(dispatch, "preflight_codex_launch", side_effect=recording_preflight),
            mock.patch.object(
                dispatch, "_deliver_interactive_skill", side_effect=lambda *a, **kw: order.append("deliver")
            ),
        ):
            dispatch._spawn_fresh_terminal(
                "retro", None, self.workspace, mock.Mock(), "dispatch", host=FakeSessionHost()
            )

        self.assertEqual(order, ["preflight", "create", "trusted", "deliver"])
        trusted = self._trusted()
        self.assertEqual(trusted["projects"][str(Path(self.workspace).resolve())]["trust_level"], "trusted")

    def test_a_claude_service_head_keeps_its_own_best_effort_preparation(self) -> None:
        """Claude's first-run prep is unchanged, and no codex config is written for it."""
        command = dispatch.DispatchCommand("/retro", "claude '/retro'", "claude-opus", None)

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_ensure_claude_ready") as claude_ready,
            mock.patch.object(dispatch, "_create_terminal", return_value="term-claude"),
        ):
            dispatch._spawn_fresh_terminal(
                "retro", None, self.workspace, mock.Mock(), "dispatch", host=FakeSessionHost()
            )

        claude_ready.assert_called_once_with(self.workspace)
        self.assertFalse(self.codex_home.exists())

    def test_a_schema_absent_service_preflight_opens_and_delivers(self) -> None:
        """Schema evidence is advisory; the shared trust preflight still precedes the pane."""
        state = mock.Mock()

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=self.command),
            mock.patch.object(dispatch, "_create_terminal", return_value="term-codex") as create,
            mock.patch.object(dispatch, "_deliver_interactive_skill") as deliver,
        ):
            dispatch._spawn_fresh_terminal(
                "retro", None, self.workspace, state, "dispatch", host=FakeSessionHost()
            )

        create.assert_called_once()
        deliver.assert_called_once_with("term-codex", self.workspace, "/retro", host=mock.ANY)
        state.save_active_report.assert_called_once_with(None, "term-codex")
        self.assertEqual(
            self._trusted()["projects"][str(Path(self.workspace).resolve())]["trust_level"], "trusted"
        )

    def test_an_untrusted_workspace_rejects_an_otherwise_allowed_service_preflight(self) -> None:
        """Trust is still the hard pre-pane check, regardless of provider telemetry."""
        self.codex_home.mkdir()
        config = self.codex_home / "config.toml"
        config.write_text(
            f'[projects.{json.dumps(str(Path(self.workspace).resolve()))}]\ntrust_level = "untrusted"\n',
            encoding="utf-8",
        )
        state = mock.Mock()

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=self.command),
            mock.patch.object(codex_preflight, "attest_codex_fanout", side_effect=self._allowed_attestation),
            mock.patch.object(dispatch, "_create_terminal") as create,
            mock.patch.object(dispatch, "_deliver_interactive_skill") as deliver,
        ):
            with self.assertRaises(dispatch.CodexPreflightError):
                dispatch._spawn_fresh_terminal(
                    "retro", None, self.workspace, state, "dispatch", host=FakeSessionHost()
                )

        create.assert_not_called()
        deliver.assert_not_called()
        self.assertIn('trust_level = "untrusted"', config.read_text(encoding="utf-8"))

    def test_a_failed_preflight_escalates_the_steward_card_instead_of_closing_it(self) -> None:
        """No head was started, so no sweep happened: the card must not be recorded as one.

        Done is what a dispatch that already had a pane closes out with. A workspace that could
        never hold a head is a condition a later tick cannot heal on its own, so the card goes to
        the board's own wait-for-a-human state with the reason attached.
        """
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text(
            f'[projects.{json.dumps(str(Path(self.workspace).resolve()))}]\ntrust_level = "untrusted"\n',
            encoding="utf-8",
        )
        command = dispatch.DispatchCommand(
            "/steward --card secretary-817",
            "codex",
            "codex",
            "secretary-817",
            prompt_after_start=True,
            head_profile=self.profile,
        )
        moved: list[tuple] = []
        state = mock.Mock()

        with (
            mock.patch.object(dispatch, "_dispatch_command", return_value=command),
            mock.patch.object(dispatch, "_create_terminal") as create,
            mock.patch(
                "triggered_agents.agents.pipeline.ops.move_card",
                side_effect=lambda *args, **kwargs: moved.append((args, kwargs)),
            ),
            self.assertRaises(dispatch.CodexPreflightError),
        ):
            dispatch._spawn_fresh_terminal(
                "steward", None, self.workspace, state, "dispatch", host=FakeSessionHost()
            )

        create.assert_not_called()
        self.assertEqual(moved[0][0], ("steward", "secretary-817", "Blocked"))
        self.assertIn("no head was started", moved[0][1]["reason"])
        self.assertIn("trust_level 'untrusted'", moved[0][1]["reason"])
        self.assertNotIn("Done", [call[0][2] for call in moved])
        state.clear_active_report.assert_called_once_with("secretary-817")

    def test_the_escalation_is_allowed_by_the_boards_own_transition_matrix(self) -> None:
        """The column this lands a report card in is one the steward may actually move it to."""
        from triggered_agents.agents.pipeline import model

        model.check_move("steward", "In progress", "Blocked")

    def test_the_service_launcher_and_the_dispatcher_run_one_preflight(self) -> None:
        """Not two implementations that agree today: the same function object."""
        from secretary import dispatcher_launcher

        self.assertIs(dispatch.preflight_codex_launch, codex_preflight.preflight_codex_launch)
        self.assertIs(
            dispatcher_launcher._preflight_codex_workspace, codex_preflight.ensure_codex_workspace_trusted
        )
