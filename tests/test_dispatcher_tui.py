from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher import CommandHostRuntime, HostError
from triggered_agents.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_tui import (
    DELIVERY_ACCEPTED,
    DELIVERY_CONFIRMED,
    READINESS_BLOCKED,
    READINESS_BUSY,
    READINESS_READY,
    READINESS_STALE_HANDLE,
    READINESS_UNKNOWN,
    READINESS_UNAVAILABLE,
    TuiDeliveryError,
    claude_project_dir_name,
    deliver_interactive_prompt,
    delivery_readiness_state,
    latest_claude_user_turn_for,
    bind_claude_provider_progress_source,
    prepare_claude_provider_progress_source,
    provider_progress_for_run,
    terminal_readiness,
    terminal_turn_started,
    turn_started_confirm,
)
from secretary.dispatcher_worker_lifecycle import ContinuationProviderCondition
from triggered_agents.runtime.codex_preflight import codex_provider_source_descriptor
from tests.test_dispatcher_observer import (
    BLOCKED_PANE_WAIT_BODY,
    STALE_HANDLE_WAIT_FAILURE,
    TIMEOUT_WAIT_FAILURE,
)
from tests.fanout_fixtures import accepted_transport_run


class DispatcherTuiLaunchTests(unittest.TestCase):
    def test_provider_progress_uses_only_text_free_run_bound_cursors(self) -> None:
        """Both provider shapes reject competing workspace files and expose only opaque cursors."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            claude_root = Path(tmp) / "claude-projects"
            with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(claude_root)}):
                claude_run = prepare_claude_provider_progress_source(HeadRun(
                    run_id="claude-bound", spec=HeadSpec(profile_id="claude", adapter="claude"),
                    workspace=str(workspace), task_ref=TaskRef.card("secretary-1429"), role="worker",
                ))
                transcript = claude_root / claude_project_dir_name(str(workspace)) / "session.jsonl"
                foreign = claude_root / claude_project_dir_name(str(workspace)) / "foreign.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text('{"type":"assistant","message":"secret"}\n', encoding="utf-8")
                claude_run = bind_claude_provider_progress_source(claude_run)
                foreign.write_text('{"type":"assistant","message":"foreign"}\n', encoding="utf-8")
                claude = provider_progress_for_run(claude_run)
            self.assertEqual(claude["state"], "observed")
            self.assertEqual(claude["source"], "claude-session")
            self.assertIn(":", claude["cursor"])
            self.assertNotIn("secret", str(claude))
            self.assertNotIn("foreign", str(claude))

            codex_root = Path(tmp) / "codex-sessions"
            codex_path = codex_root / "session.jsonl"
            codex_root.mkdir()
            codex_path.write_text(
                '{"type":"session_meta","payload":{"session_id":"own","cwd":"' + str(workspace) + '"}}\n'
                '{"type":"event_msg","payload":{"type":"thread.started","thread_id":"parent"}}\n',
                encoding="utf-8",
            )
            from secretary.codex_provider_events import _range_digest, _read_source
            from secretary.dispatcher_worker_lifecycle import head_run_binding
            parsed = _read_source(codex_path)
            self.assertIsNotNone(parsed)
            _meta, lines = parsed
            _, run_fingerprint = head_run_binding(HeadRun(
                run_id="codex-bound", spec=HeadSpec(profile_id="codex", adapter="codex"),
                workspace=str(workspace), task_ref=TaskRef.card("secretary-1429"), role="worker",
            ).to_json())
            codex_run = HeadRun(
                run_id="codex-bound", spec=HeadSpec(profile_id="codex", adapter="codex"),
                workspace=str(workspace), task_ref=TaskRef.card("secretary-1429"), role="worker",
            )
            source = {
                "version": 1, "kind": "codex_session_event_jsonl", "state": "bound",
                "run_id": codex_run.run_id, "head_run_fingerprint": run_fingerprint,
                "workspace": str(workspace.resolve()), "role": "worker",
                "task_ref": codex_run.task_ref.to_json(), "root": str(codex_root.resolve()),
                "path": str(codex_path.resolve()), "session_id": "own", "parent_thread_id": "parent",
                "initial_range": {
                    "first": {"line": 1, "digest": lines[0].digest},
                    "root": {"line": 2, "digest": lines[1].digest},
                    "last": {"line": 2, "digest": lines[1].digest},
                    "digest": _range_digest(lines),
                },
                "cursor": {"line": 2, "digest": lines[1].digest}, "bound_at": "2026-08-13T00:00:00Z",
            }
            codex_run = codex_run.with_fanout_policy({
                **codex_run.fanout_policy, "provider_source": source,
            })
            foreign_codex = codex_root / "foreign.jsonl"
            foreign_codex.write_text('{"type":"session_meta","payload":{"session_id":"foreign"}}\n', encoding="utf-8")
            codex = provider_progress_for_run(codex_run)
            self.assertEqual(codex["state"], "observed")
            self.assertEqual(codex["source"], "codex-session")
            self.assertNotIn("foreign", str(codex))

    def test_provider_progress_keeps_an_unavailable_source_distinct_from_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = provider_progress_for_run(HeadRun(
                run_id="claude-unbound", spec=HeadSpec(profile_id="claude", adapter="claude"),
                workspace=str(Path(tmp) / "workspace"), task_ref=TaskRef.card("secretary-1429"),
                role="worker",
            ))
        self.assertEqual(unavailable["state"], "unavailable")
        self.assertNotEqual(unavailable["state"], READINESS_BUSY)

    def test_provider_progress_types_only_a_complete_legacy_unbound_codex_v1_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            run = HeadRun(
                run_id="codex-unbound",
                spec=HeadSpec(profile_id="codex", adapter="codex", model="gpt-5.6-terra"),
                workspace=str(workspace),
                task_ref=TaskRef.card("secretary-1435"),
                role="worker",
            )
            source = {
                "version": 1,
                "kind": "codex_session_event_jsonl",
                "state": "unbound",
                **codex_provider_source_descriptor(run),
                "root": str((Path(tmp) / "sessions").resolve()),
                "baseline": [],
            }
            policy = {
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
                "provider_source_required": True,
                "provider_source": source,
            }

            legacy = provider_progress_for_run(run.with_fanout_policy(policy))
            foreign = provider_progress_for_run(run.with_fanout_policy({
                **policy,
                "provider_source": {**source, "workspace": "/foreign"},
            }))
            malformed = provider_progress_for_run(run.with_fanout_policy({
                **policy,
                "provider_source": {**source, "baseline": [1]},
            }))
            relative_root = provider_progress_for_run(run.with_fanout_policy({
                **policy,
                "provider_source": {**source, "root": "relative-session-root"},
            }))
            relative_baseline = provider_progress_for_run(run.with_fanout_policy({
                **policy,
                "provider_source": {**source, "baseline": ["relative-old-session.jsonl"]},
            }))
            noncanonical_root = provider_progress_for_run(run.with_fanout_policy({
                **policy,
                "provider_source": {**source, "root": str(Path(tmp) / "sessions" / ".." / "sessions")},
            }))
            outside_baseline = provider_progress_for_run(run.with_fanout_policy({
                **policy,
                "provider_source": {
                    **source, "baseline": [str((Path(tmp) / "outside.jsonl").resolve())],
                },
            }))

        self.assertEqual(legacy["state"], "unavailable")
        self.assertEqual(
            legacy["continuation_condition"],
            ContinuationProviderCondition.LEGACY_UNBOUND_V1.value,
        )
        self.assertNotIn("continuation_condition", foreign)
        self.assertNotIn("continuation_condition", malformed)
        self.assertNotIn("continuation_condition", relative_root)
        self.assertNotIn("continuation_condition", relative_baseline)
        self.assertNotIn("continuation_condition", noncanonical_root)
        self.assertNotIn("continuation_condition", outside_baseline)

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
            session = projects / claude_project_dir_name(str(workspace)) / "session.jsonl"
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
        self.assertEqual(
            delivered,
            "\x1b[200~The full task is in TASK.md. Read it first.\x1b[201~",
        )
        self.assertNotIn("full spec body", delivered)

    def test_tui_delivery_resends_enter_when_the_pane_did_not_take_the_prompt(self) -> None:
        """A Codex launch is delivered by the shared path, and re-entered the same way.

        Orca says the pane took nothing \u2014 here the update dialog that swallows the first Enter \u2014
        and the composer is holding the payload the send wrote, so the Enter alone is re-entered
        and the delivery is confirmed by this role's own criterion, its turn having started.

        The first screen is the pane before the send: the delivery fingerprints the composer there
        so that the one it reads afterwards can be compared against it rather than against
        emptiness, which a TUI's own hint text is not.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Read TASK.md\n", encoding="utf-8")
            host = RecordingTuiHost(
                workspace,
                [
                    {"terminal": {"tail": ["\u203a"]}},
                    {"terminal": {"tail": ["\u203a [Pasted Content 13 chars]"]}},
                    {"terminal": {"tail": ["\u203a [Pasted Content 13 chars]"]}},
                    {"terminal": {"tail": ["thinking"]}},
                ],
                waits=[
                    {"wait": {"satisfied": True}},
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
        self.assertEqual(len(sends), 3)
        self.assertEqual(
            sends[0][sends[0].index("--text") + 1],
            "\x1b[200~Read TASK.md\n\x1b[201~",
        )
        self.assertEqual(sends[1][sends[1].index("--text") + 1], "")
        self.assertIn("--enter", sends[1])
        self.assertEqual(sends[2][sends[2].index("--text") + 1], "")
        self.assertIn("--enter", sends[2])

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


class ClaudeTranscriptPathTests(unittest.TestCase):
    """Where Claude Code keeps a workspace's transcripts, checked against where it keeps them.

    Both halves of the 2026-08-11 blind bring-up were the same mistake: a claim about another
    product's format, asserted only against a mock of itself. `path.replace('/', '-')` had never
    been true — Claude Code replaces every non-alphanumeric character — and the unit tests could
    not notice, because they built the fixture directory with the same wrong rule they were
    testing. So one test here reads the real catalogue, and the hermetic one takes its directory
    name from production code rather than restating it.
    """

    def test_the_project_folder_name_matches_the_real_claude_catalogue(self) -> None:
        """The rule against the directories Claude Code actually wrote on this host.

        Every session log records the `cwd` it was opened in, so each project directory carries the
        workspace path it was named after and the pair can be checked without guessing. Skipped
        where there is no catalogue to read — a machine without one cannot answer the question, and
        a mock of the answer would be the defect this test exists for.
        """
        root = Path.home() / ".claude" / "projects"
        if not root.is_dir():
            self.skipTest("no ~/.claude/projects on this host")
        pairs = [
            (project.name, cwd)
            for project in sorted(root.iterdir())
            if project.is_dir()
            for cwd in [_recorded_cwd(project)]
            if cwd
        ]
        if not pairs:
            self.skipTest("no Claude session log on this host records its workspace")
        underscored = [pair for pair in pairs if "_" in pair[1]]
        if not underscored:
            self.skipTest("no workspace with an underscore in the catalogue on this host")

        self.assertEqual(
            [(name, cwd) for name, cwd in pairs if claude_project_dir_name(cwd) != name], []
        )
        # And the rule that was there before really does miss those workspaces, which is why six
        # healthy heads were closed on a product whose workspaces carry one.
        self.assertEqual(
            [
                (name, cwd)
                for name, cwd in underscored
                if str(Path(cwd).resolve(strict=False)).replace("/", "-") == name
            ],
            [],
        )

    def test_an_underscore_workspace_is_confirmed_by_its_transcript_alone(self) -> None:
        """The delivery criterion, on the shape of workspace the incident was reported against.

        The pane is painting nothing this confirmation could recognise, and it does not have to:
        the user turn Claude persisted after the send is the proof, and it is read from the
        directory Claude actually writes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "codegen_orchestrator" / "codegen-orchestrator-1166"
            workspace.mkdir(parents=True)
            projects = Path(tmp) / "claude-projects"
            folder = projects / claude_project_dir_name(str(workspace))
            self.assertIn("-codegen-orchestrator-codegen-orchestrator-1166", str(folder))
            folder.mkdir(parents=True)
            (folder / "session.jsonl").write_text(
                json.dumps({
                    "type": "user",
                    "timestamp": "2099-01-02T03:04:05Z",
                    "cwd": str(workspace),
                }) + "\n",
                encoding="utf-8",
            )
            # A pane with nothing on it: no spinner, no status line, no glyph of any generation.
            def run_json(command: list[str]) -> dict:
                return {"terminal": {"tail": ["", "❯"]}}

            with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(projects)}):
                confirm = turn_started_confirm(
                    "term-claude", str(workspace), "claude", run_json=run_json
                )
                self.assertTrue(confirm(1.0))
                # Everything the criterion says is about the boundary: a turn older than the send
                # is not this delivery's, and no screen glyph can make it one.
                self.assertFalse(confirm(4102462000.0))

    def test_a_transcript_under_the_old_folder_name_is_not_read(self) -> None:
        """The path the reader used to look under is not a second place to look.

        It is a directory Claude Code never writes, so anything found there would be evidence
        somebody else planted. Keeping the old glob alive "just in case" would also keep the
        defect alive on any host where such a directory did exist.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "codegen_orchestrator" / "ws"
            workspace.mkdir(parents=True)
            projects = Path(tmp) / "claude-projects"
            stale = projects / str(workspace.resolve()).replace("/", "-")
            stale.mkdir(parents=True)
            (stale / "session.jsonl").write_text(
                json.dumps({"type": "user", "timestamp": "2099-01-02T03:04:05Z"}) + "\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(projects)}):
                self.assertIsNone(latest_claude_user_turn_for(str(workspace), 0.0))


def _recorded_cwd(project: Path) -> str:
    """The workspace one Claude project directory was named after, as its own logs record it."""
    for log in sorted(project.glob("*.jsonl")):
        try:
            with log.open(encoding="utf-8", errors="replace") as source:
                for index, line in enumerate(source):
                    if index >= 200:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = record.get("cwd") if isinstance(record, dict) else None
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return ""


class ClaudeScreenHintTests(unittest.TestCase):
    """The screen is a hint about a foreign TUI, and it is only ever read as one.

    The pattern it used to be pinned to — one of five spinner glyphs, then a word, then a
    parenthesised `(4s · ↑ 13.2k tokens)` — matched nothing Claude 2.1.227 paints, and while it
    silently matched nothing it was also the only thing masking the transcript-path defect. The
    lines below are what `orca terminal read` really returned from a working Claude pane on
    2026-08-11, alternate-screen overlay and all.
    """

    LIVE_STATUS_LINES = [
        "· Tempering…e /btw to ask a 9u ck side question without interrupting Claude's current work",
        "✽ Tempering…e /btw to ask a 5u ck side question without)interrupting Claude's current work",
        "● Tempering…e /btw5to ask a 6u ck side question without)interrupting Claude's current work",
        "✢ Tempering…e /btw5to ask a 6u ck side question without)interrupting Claude's current work",
        # The version whose suffix survives, and the one the incident report quoted.
        "✻ Forming... (4s · ↑ 13.2k tokens)",
        "●─Bloviating…──2──(12.4k tokens)",
    ]

    def screen(self, *lines: str):
        def run_json(_command: list[str]) -> dict:
            return {"terminal": {"tail": list(lines)}}

        return run_json

    def test_every_status_line_a_live_claude_pane_paints_is_a_turn(self) -> None:
        for line in self.LIVE_STATUS_LINES:
            with self.subTest(line=line):
                self.assertTrue(
                    terminal_turn_started(
                        "term-claude", adapter="claude", run_json=self.screen("Claude Code", line)
                    )
                )

    def test_a_pane_that_is_not_working_is_not_read_as_working(self) -> None:
        for line in (
            "The completed response says it was thinking while working.",
            "⏺ Read(TASK.md)",
            "  ⎿  Read 131 lines",
            "❯ ",
            "",
        ):
            with self.subTest(line=line):
                self.assertFalse(
                    terminal_turn_started(
                        "term-claude", adapter="claude", run_json=self.screen("Claude Code", line)
                    )
                )


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
    ) -> HeadCommand:
        self.heads.append(head)
        return HeadCommand(
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
        self.preflight_codex_run = accepted_transport_run  # type: ignore[method-assign]
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


class ScriptedPane:
    """One pane, answered from a script keyed on what the delivery has already done.

    `screens` are keyed by how many sends have landed, so a test says what the pane looks like
    before the payload is written, after it, and after each re-entry, which is exactly the sequence
    this boundary has to tell apart.
    """

    def __init__(
        self,
        screens: dict[int, list[str]],
        *,
        idle_after: dict[int, bool] | None = None,
        bytes_written: int = 1315,
        cursors: dict[int, str] | None = None,
        refuse: str = "",
    ) -> None:
        self.screens = screens
        self.idle_after = idle_after or {}
        self.bytes_written = bytes_written
        # What Orca answers as `nextCursor`, keyed the same way. None at all is a runtime that
        # returns no cursor, which is how every fixture that predates this looked.
        self.cursors = cursors or {}
        self.refuse = refuse
        self.calls: list[list[str]] = []
        self.sends: list[str] = []
        self.submits = 0

    def _at(self, table: dict):
        landed = self.submits
        while landed >= 0:
            if landed in table:
                return table[landed]
            landed -= 1
        return None

    def _screen(self) -> list[str]:
        return self._at(self.screens) or []

    def run_json(self, args: list[str]) -> dict:
        self.calls.append(args)
        if self.refuse and args[1:3] == ["terminal", self.refuse]:
            raise HostError(f"orca terminal {self.refuse} failed: synthetic transport refusal")
        if args[1:3] == ["terminal", "send"]:
            self.sends.append(args[args.index("--text") + 1])
            if "--enter" in args:
                self.submits += 1
            return {"send": {"accepted": True, "bytesWritten": self.bytes_written}}
        if args[1:3] == ["terminal", "wait"]:
            idle = self.idle_after.get(self.submits, True)
            return {"wait": {"condition": "tui-idle", "satisfied": idle}}
        if args[1:3] == ["terminal", "read"]:
            terminal: dict = {"tail": list(self._screen())}
            cursor = self._at(self.cursors)
            if cursor is not None:
                terminal["nextCursor"] = cursor
                terminal["latestCursor"] = cursor
            return {"terminal": terminal}
        raise AssertionError(args)


class TuiDeliveryStageTests(unittest.TestCase):
    """Delivery is staged, and a written payload is only the first stage of it.

    Every case here is one of the two production failures this boundary was rebuilt for: a Codex
    pane that answered `accepted: true` with a byte count, left the payload under a paste
    placeholder in its composer and reported `tui-idle` satisfied throughout (board 871), and an
    observer wake that reached its head and was recorded as `pane-stayed-ready` anyway because the
    turn began and ended between two probes (sprint:1402).
    """

    def deliver(self, pane: ScriptedPane, **kwargs):
        with mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_TIMEOUT_S", 0.3), \
             mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01), \
             mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0), \
             mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RETRIES", 2), \
             mock.patch("triggered_agents.runtime.agent_prompt_transport.AGENT_PROMPT_SUBMIT_DELAY_S", 0):
            return deliver_interactive_prompt(
                "term-observer", "wake the observer", run_json=pane.run_json, **kwargs
            )

    def test_a_payload_left_in_the_composer_is_not_a_delivered_prompt(self) -> None:
        pane = ScriptedPane({0: ["›"], 1: ["› [Pasted Content 1315 chars]"]})

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.reason, "payload-left-in-composer")
        self.assertEqual(evidence.stage, "payload_written")
        self.assertTrue(evidence.send_accepted)
        self.assertEqual(evidence.bytes_written, 1315)
        self.assertTrue(evidence.payload_left_in_composer)
        self.assertTrue(evidence.composer_after.startswith("paste:"))
        self.assertEqual(evidence.readiness_after, READINESS_READY)
        self.assertFalse(evidence.cursor_moved)
        # The payload is entered again rather than written again: it is already sitting there.
        self.assertEqual(pane.sends, ["wake the observer", "", "", ""])
        self.assertEqual(evidence.attempts, 3)
        self.assertEqual(evidence.resends, 2)

    def test_an_enter_that_starts_a_turn_completes_the_delivery(self) -> None:
        """The re-entry carries the prompt past whatever swallowed the first Enter."""
        pane = ScriptedPane(
            {0: ["›"], 1: ["› [Pasted Content 1315 chars]"], 2: ["thinking"]},
            idle_after={2: False},
        )

        outcome = self.deliver(pane, ack_out_of_band=True)

        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertEqual(outcome.evidence.stage, "turn_observed")
        self.assertEqual(outcome.evidence.resends, 1)
        self.assertEqual(pane.sends, ["wake the observer", "", ""])
        self.assertFalse(outcome.evidence.payload_left_in_composer)

    def test_output_cursor_progress_is_a_turn_on_a_pane_that_stayed_ready(self) -> None:
        """The sprint:1402 false failure: the head answered between two readiness probes.

        Orca calls the pane ready before the send and ready again afterwards, because the turn it
        ran fitted between them. Readiness alone therefore says the prompt was swallowed. The
        composer is empty and the pane has printed output it had not printed before, which is a
        turn, and the delivery is accepted instead of being reported as a refusal.
        """
        pane = ScriptedPane({0: ["ready", "›"], 1: ["ready", "read the card, recorded resume", "›"]})

        outcome = self.deliver(pane, ack_out_of_band=True)

        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertEqual(outcome.evidence.stage, "turn_observed")
        self.assertTrue(outcome.evidence.cursor_moved)
        self.assertEqual(outcome.evidence.readiness_after, READINESS_READY)
        self.assertEqual(pane.sends, ["wake the observer", ""])

    def test_a_pane_that_showed_nothing_for_the_prompt_is_written_again_and_then_refused(self) -> None:
        """A composer that is empty and a pane that printed nothing: the payload is gone.

        Re-entering an Enter would achieve nothing here — there is nothing in the composer to
        enter — so the payload is written again instead, and when that produces no turn either the
        delivery is refused with the stage it actually reached rather than as a delivered prompt.
        """
        pane = ScriptedPane({0: ["ready", "›"]})

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.reason, "enter-accepted-without-turn")
        self.assertFalse(evidence.cursor_moved)
        self.assertEqual(pane.sends, ["wake the observer", ""] * 3)

    def test_evidence_keeps_the_size_and_the_hash_and_never_the_prompt(self) -> None:
        pane = ScriptedPane({0: ["›"], 1: ["› [Pasted Content 1315 chars]"]})

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True, subject="observer-wake")

        evidence = raised.exception.evidence.to_json()
        self.assertEqual(evidence["subject"], "observer-wake")
        self.assertEqual(evidence["payload_bytes"], len("wake the observer"))
        self.assertEqual(len(evidence["payload_sha256"]), 16)
        self.assertNotIn("wake the observer", json.dumps(evidence))

    def test_the_reviewer_criterion_and_the_observer_rule_are_the_same_boundary(self) -> None:
        """A confirmed delivery is the out-of-band one plus the caller's own acknowledgement.

        The reviewer and worker path passes `confirm`; the observer path passes none and stops one
        stage earlier. Both see the same composer, the same output cursor and the same bounded
        re-entry, which is what "the same confirmation rule" has to mean if a reviewer prompt and
        an observer wake are to fail for the same reasons.
        """
        pane = ScriptedPane(
            {0: ["›"], 1: ["› [Pasted Content 1315 chars]"], 2: ["thinking"]},
            idle_after={2: False},
        )
        turns: list[float] = []

        outcome = self.deliver(pane, confirm=lambda sent_at: bool(turns) or turns.append(sent_at))

        self.assertEqual(outcome, DELIVERY_CONFIRMED)
        self.assertEqual(outcome.evidence.stage, "acknowledged")
        self.assertEqual(pane.sends, ["wake the observer", "", ""])

    def test_the_backend_cursor_advancing_is_a_turn_even_when_the_tail_is_unchanged(self) -> None:
        """The retained tail is bounded; Orca's cursor is not.

        A quick turn can append output that the tail window no longer shows, and a repainting TUI
        can print without the returned lines differing at all. Orca still advances `nextCursor`,
        and that is the pane's real position, so it is what the delivery compares. A digest of the
        tail would have said nothing moved and the wake would have been re-entered and refused.
        """
        pane = ScriptedPane(
            {0: ["ready", "›"]},
            cursors={0: "558", 1: "612"},
        )

        outcome = self.deliver(pane, ack_out_of_band=True)

        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        evidence = outcome.evidence
        self.assertEqual(evidence.stage, "turn_observed")
        self.assertTrue(evidence.cursor_moved)
        self.assertTrue(evidence.cursor_from_backend)
        self.assertEqual((evidence.cursor_before, evidence.cursor_after), ("orca:558", "orca:612"))
        self.assertEqual(evidence.readiness_after, READINESS_READY)
        self.assertEqual(pane.sends, ["wake the observer", ""])

    def test_a_backend_cursor_that_did_not_move_is_not_a_turn(self) -> None:
        """The same pane with the cursor standing still: nothing was printed, so nothing landed."""
        pane = ScriptedPane({0: ["ready", "›"]}, cursors={0: "558"})

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertFalse(evidence.cursor_moved)
        self.assertTrue(evidence.cursor_from_backend)
        self.assertEqual(evidence.cursor_after, "orca:558")

    def test_a_runtime_without_cursors_still_falls_back_to_the_tail(self) -> None:
        """No cursor in the answer is the only case the tail digest stands in for one, and it says
        so in the evidence rather than passing itself off as the backend's position."""
        pane = ScriptedPane({0: ["ready", "›"], 1: ["ready", "answered", "›"]})

        outcome = self.deliver(pane, ack_out_of_band=True)

        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertTrue(outcome.evidence.cursor_moved)
        self.assertFalse(outcome.evidence.cursor_from_backend)
        self.assertTrue(outcome.evidence.cursor_after.startswith("tail:"))

    def test_a_refused_send_is_a_delivery_failure_with_its_evidence(self) -> None:
        """The transport itself failing is a prompt that did not land, not a bare host error.

        Before this, a refused `terminal send` escaped the boundary as the host's own exception,
        the observer path caught it as a plain failure, and the record kept a count and a sentence
        with no terminal, payload or stage — and whatever evidence an earlier failure had left.
        """
        pane = ScriptedPane({0: ["ready", "›"]}, refuse="send")

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.reason, "transport-refused-body-write")
        self.assertEqual(evidence.handle, "term-observer")
        self.assertEqual(evidence.payload_bytes, len("wake the observer"))
        self.assertEqual(len(evidence.payload_sha256), 16)
        self.assertEqual(evidence.readiness_before, READINESS_READY)
        self.assertEqual(evidence.stage, "none")
        self.assertNotIn("wake the observer", json.dumps(evidence.to_json()))

    def test_a_refused_submit_keeps_the_accepted_framed_body_as_separate_evidence(self) -> None:
        """A body acceptance is not delivery when its one submission write is refused."""
        calls: list[list[str]] = []

        def run_json(args: list[str]) -> dict:
            calls.append(args)
            if args[1:3] == ["terminal", "wait"]:
                return {"wait": {"condition": "tui-idle", "satisfied": True}}
            if args[1:3] == ["terminal", "read"]:
                return {"terminal": {"tail": ["›"]}}
            if args[1:3] == ["terminal", "send"]:
                return {"send": {"accepted": "--enter" not in args, "bytesWritten": 7}}
            raise AssertionError(args)

        with mock.patch("triggered_agents.runtime.agent_prompt_transport.AGENT_PROMPT_SUBMIT_DELAY_S", 0):
            with self.assertRaises(TuiDeliveryError) as raised:
                deliver_interactive_prompt(
                    "term-codex", "deliver exactly once", run_json=run_json,
                    adapter="codex", ack_out_of_band=True,
                )

        evidence = raised.exception.evidence
        self.assertEqual(evidence.stage, "payload_written")
        self.assertTrue(evidence.body_write_accepted)
        self.assertFalse(evidence.submit_write_accepted)
        self.assertEqual((evidence.body_write_count, evidence.submit_count), (1, 1))
        sends = [call for call in calls if call[1:3] == ["terminal", "send"]]
        self.assertEqual(len(sends), 2)
        self.assertTrue(sends[0][sends[0].index("--text") + 1].startswith("\x1b[200~"))
        self.assertIn("--enter", sends[1])
        self.assertNotIn("deliver exactly once", json.dumps(evidence.to_json()))

    def test_a_refused_readiness_wait_is_a_delivery_failure_with_its_evidence(self) -> None:
        pane = ScriptedPane({0: ["ready", "›"]}, refuse="wait")

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.reason, "readiness-unavailable")
        self.assertEqual(evidence.readiness_state, READINESS_UNAVAILABLE)
        self.assertEqual(delivery_readiness_state(evidence), READINESS_UNAVAILABLE)
        self.assertEqual(evidence.handle, "term-observer")
        self.assertEqual(evidence.payload_bytes, len("wake the observer"))
        self.assertEqual(pane.sends, [], "nothing was written into a pane that could not be waited for")

    def test_a_refused_readiness_wait_keeps_busy_unavailable_and_stale_distinct(self) -> None:
        """The live CLI uses non-zero exits for a working pane and for broken transport alike."""
        cases = (
            (HostError(TIMEOUT_WAIT_FAILURE), READINESS_BUSY, "readiness-busy"),
            (
                HostError(
                    'orca terminal wait --for tui-idle failed: '
                    '{"result":{"wait":{"satisfied":false,"status":"running"}}}'
                ),
                READINESS_BUSY,
                "readiness-busy",
            ),
            (HostError("orca terminal wait --for tui-idle failed: connection refused"),
             READINESS_UNAVAILABLE, "readiness-unavailable"),
            (HostError(STALE_HANDLE_WAIT_FAILURE), READINESS_STALE_HANDLE, "readiness-stale_handle"),
        )
        for refusal, state, reason in cases:
            with self.subTest(state=state, refusal=str(refusal)):
                def run_json(_command: list[str]) -> dict:
                    raise refusal

                with self.assertRaises(TuiDeliveryError) as raised:
                    deliver_interactive_prompt(
                        "term-observer",
                        "wake the observer",
                        run_json=run_json,
                        adapter="codex",
                        ack_out_of_band=True,
                    )

                evidence = raised.exception.evidence
                self.assertEqual(evidence.readiness_state, state)
                self.assertEqual(evidence.reason, reason)
                self.assertEqual(delivery_readiness_state(evidence), state)
                self.assertEqual(evidence.stage, "none")
