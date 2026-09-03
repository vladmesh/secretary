from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher import CommandHostRuntime, HostError
from secretary.dispatcher_tui import (
    DELIVERY_ACCEPTED,
    DELIVERY_CONFIRMED,
    DELIVERY_RECEIPT_ACCEPTED,
    DELIVERY_RECEIPT_REFUSED,
    DELIVERY_RECEIPT_UNOBSERVED,
    PRE_DELIVERY_STARTING,
    PRE_DELIVERY_UNKNOWN_DIALOG,
    PRE_DELIVERY_UPDATE_MODAL,
    READINESS_BLOCKED,
    READINESS_BUSY,
    READINESS_READY,
    READINESS_STALE_HANDLE,
    READINESS_UNAVAILABLE,
    READINESS_UNKNOWN,
    TuiDeliveryError,
    bind_claude_provider_progress_source,
    claude_project_dir_name,
    classify_pre_delivery,
    deliver_interactive_prompt,
    dialog_is_live,
    delivery_readiness_state,
    delivery_receipt_state,
    latest_claude_user_turn_for,
    live_screen,
    latest_user_turn_for,
    prepare_claude_provider_progress_source,
    provider_progress_for_run,
    provider_turn_started,
    terminal_readiness,
    terminal_turn_started,
    turn_started_confirm,
)
from secretary.dispatcher_worker_lifecycle import ContinuationProviderCondition
from tests.fakes.observer import (
    BLOCKED_PANE_WAIT_BODY,
    STALE_HANDLE_WAIT_FAILURE,
    TIMEOUT_WAIT_FAILURE,
)
from tests.fanout_fixtures import accepted_transport_run
from triggered_agents.runtime.codex_preflight import codex_provider_source_descriptor
from triggered_agents.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from triggered_agents.runtime.tui_delivery import DeliveryEvidence, composer_holds_payload


class DispatcherTuiLaunchTests(unittest.TestCase):
    def test_claude_binding_retries_the_exact_run_until_its_late_session_id_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            root = Path(tmp) / "claude-projects"
            with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(root)}):
                run = prepare_claude_provider_progress_source(
                    HeadRun(
                        run_id="claude-late",
                        spec=HeadSpec(profile_id="claude", adapter="claude"),
                        workspace=str(workspace),
                        task_ref=TaskRef.card("secretary-1517"),
                        role="worker",
                    )
                )
                run = bind_claude_provider_progress_source(run)
                self.assertEqual(
                    run.fanout_policy["provider_progress_source"]["state"], "awaiting_transcript"
                )

                transcript = root / claude_project_dir_name(str(workspace)) / "own.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
                run = bind_claude_provider_progress_source(run)
                self.assertEqual(
                    run.fanout_policy["provider_progress_source"]["state"], "awaiting_session_id"
                )

                # The selected path is fenced by its device/inode. A later same-workspace file
                # cannot become this run's conversation while Claude finishes its own header.
                foreign = transcript.with_name("foreign.jsonl")
                foreign.write_text(
                    '{"type":"assistant","sessionId":"foreign-session"}\n', encoding="utf-8"
                )
                transcript.write_text(
                    '{"type":"assistant"}\n'
                    '{"type":"assistant","sessionId":"late-own-session"}\n',
                    encoding="utf-8",
                )
                run = bind_claude_provider_progress_source(run)

            source = run.fanout_policy["provider_progress_source"]
            self.assertEqual(source["state"], "bound")
            self.assertEqual(source["session_id"], "late-own-session")
            self.assertEqual(source["path"], str(transcript.resolve()))

    def test_provider_progress_uses_only_text_free_run_bound_cursors(self) -> None:
        """Both provider shapes reject competing workspace files and expose only opaque cursors."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            claude_root = Path(tmp) / "claude-projects"
            with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(claude_root)}):
                claude_run = prepare_claude_provider_progress_source(
                    HeadRun(
                        run_id="claude-bound",
                        spec=HeadSpec(profile_id="claude", adapter="claude"),
                        workspace=str(workspace),
                        task_ref=TaskRef.card("secretary-1429"),
                        role="worker",
                    )
                )
                transcript = claude_root / claude_project_dir_name(str(workspace)) / "session.jsonl"
                foreign = claude_root / claude_project_dir_name(str(workspace)) / "foreign.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text(
                    '{"type":"assistant","sessionId":"claude-session-1","message":"secret"}\n',
                    encoding="utf-8",
                )
                claude_run = bind_claude_provider_progress_source(claude_run)
                foreign.write_text('{"type":"assistant","message":"foreign"}\n', encoding="utf-8")
                claude = provider_progress_for_run(claude_run)
            self.assertEqual(claude["state"], "observed")
            self.assertEqual(claude["source"], "claude-session")
            self.assertIn(":", claude["cursor"])
            self.assertNotIn("secret", str(claude))
            self.assertNotIn("foreign", str(claude))
            self.assertEqual(
                claude_run.fanout_policy["provider_progress_source"]["session_id"], "claude-session-1"
            )

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
            _, run_fingerprint = head_run_binding(
                HeadRun(
                    run_id="codex-bound",
                    spec=HeadSpec(profile_id="codex", adapter="codex"),
                    workspace=str(workspace),
                    task_ref=TaskRef.card("secretary-1429"),
                    role="worker",
                ).to_json()
            )
            codex_run = HeadRun(
                run_id="codex-bound",
                spec=HeadSpec(profile_id="codex", adapter="codex"),
                workspace=str(workspace),
                task_ref=TaskRef.card("secretary-1429"),
                role="worker",
            )
            source = {
                "version": 1,
                "kind": "codex_session_event_jsonl",
                "state": "bound",
                "run_id": codex_run.run_id,
                "head_run_fingerprint": run_fingerprint,
                "workspace": str(workspace.resolve()),
                "role": "worker",
                "task_ref": codex_run.task_ref.to_json(),
                "root": str(codex_root.resolve()),
                "path": str(codex_path.resolve()),
                "session_id": "own",
                "parent_thread_id": "parent",
                "initial_range": {
                    "first": {"line": 1, "digest": lines[0].digest},
                    "root": {"line": 2, "digest": lines[1].digest},
                    "last": {"line": 2, "digest": lines[1].digest},
                    "digest": _range_digest(lines),
                },
                "cursor": {"line": 2, "digest": lines[1].digest},
                "bound_at": "2026-08-13T00:00:00Z",
            }
            codex_run = codex_run.with_fanout_policy(
                {
                    **codex_run.fanout_policy,
                    "provider_source": source,
                }
            )
            foreign_codex = codex_root / "foreign.jsonl"
            foreign_codex.write_text(
                '{"type":"session_meta","payload":{"session_id":"foreign"}}\n', encoding="utf-8"
            )
            codex = provider_progress_for_run(codex_run)
            self.assertEqual(codex["state"], "observed")
            self.assertEqual(codex["source"], "codex-session")
            self.assertNotIn("foreign", str(codex))

    def test_provider_progress_keeps_an_unavailable_source_distinct_from_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = provider_progress_for_run(
                HeadRun(
                    run_id="claude-unbound",
                    spec=HeadSpec(profile_id="claude", adapter="claude"),
                    workspace=str(Path(tmp) / "workspace"),
                    task_ref=TaskRef.card("secretary-1429"),
                    role="worker",
                )
            )
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
            foreign = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "workspace": "/foreign"},
                    }
                )
            )
            malformed = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "baseline": [1]},
                    }
                )
            )
            relative_root = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "root": "relative-session-root"},
                    }
                )
            )
            relative_baseline = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {**source, "baseline": ["relative-old-session.jsonl"]},
                    }
                )
            )
            noncanonical_root = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {
                            **source,
                            "root": str(Path(tmp) / "sessions" / ".." / "sessions"),
                        },
                    }
                )
            )
            outside_baseline = provider_progress_for_run(
                run.with_fanout_policy(
                    {
                        **policy,
                        "provider_source": {
                            **source,
                            "baseline": [str((Path(tmp) / "outside.jsonl").resolve())],
                        },
                    }
                )
            )

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

    def test_delivery_with_no_criterion_at_all_is_refused_before_touching_terminal(self) -> None:
        terminal_calls: list[list[str]] = []

        def run_json(command: list[str]) -> dict:
            terminal_calls.append(command)
            return {}

        with self.assertRaisesRegex(ValueError, "requires a confirmation criterion"):
            deliver_interactive_prompt("term-observer", "wake", run_json=run_json)

        self.assertEqual(terminal_calls, [])

    def test_out_of_band_delivery_may_also_carry_a_confirmation_callback(self) -> None:
        """Either criterion is enough, because each is blind where the other sees.

        `confirm` reads what the provider persisted; the stage-3 evidence reads what the pane
        shows. Requiring both left the observer wake with no reachable way to succeed on a Codex
        pane, where the composer fingerprint reads the retained tail and Orca calls a working
        head idle.
        """
        callback_calls = [0]

        def run_json(command: list[str]) -> dict:
            return {}

        def confirm(_sent_at: float) -> bool:
            callback_calls[0] += 1
            return True

        outcome = deliver_interactive_prompt(
            "term-observer",
            "wake",
            run_json=run_json,
            confirm=confirm,
            ack_out_of_band=True,
        )

        self.assertTrue(outcome.evidence.turn_confirmed)
        self.assertGreaterEqual(callback_calls[0], 1)

    def test_claude_turn_detection_accepts_real_status_lines(self) -> None:
        def run_json(command: list[str]) -> dict:
            return {
                "terminal": {
                    "tail": [
                        "The completed response says it was thinking while working.",
                        "✻ Forming... (4s · ↑ 13.2k tokens)",
                    ]
                }
            }

        self.assertTrue(terminal_turn_started("term-claude", adapter="claude", run_json=run_json))

        def completed_run_json(command: list[str]) -> dict:
            return {
                "terminal": {
                    "tail": [
                        "The completed response says it was thinking while working.",
                    ]
                }
            }

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
            self.assertEqual(terminal_readiness("term", run_json=answer(failure)), READINESS_UNKNOWN)

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
            (workspace / "TASK.md").write_text(
                "full spec body that must not be delivered\n", encoding="utf-8"
            )
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

            with (
                mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0),
                mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01),
            ):
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

            with (
                mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_TIMEOUT_S", 0.03),
                mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01),
                mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0),
                mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RETRIES", 1),
                self.assertRaises(HostError),
            ):
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
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"cwd": str(workspace.resolve())}}),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "timestamp": "2099-01-02T03:04:05Z",
                                "payload": {"type": "user_message"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            host = RecordingTuiHost(workspace, [{"terminal": {"tail": ["idle"]}}])

            with (
                mock.patch.dict(os.environ, {"SECRETARY_CODEX_SESSIONS": str(Path(tmp) / "sessions")}),
                mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01),
            ):
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


class CodexUserTurnRecordTests(unittest.TestCase):
    """What Codex writes down when a prompt is submitted, in both shapes it writes it.

    The records below are the two real ones. `codex exec` persists `event_msg`/`user_message`;
    the interactive `codex-tui` of cli 0.147.0 persists `response_item`/`message` with
    `role: "user"` and never the first shape at all — checked against the eleven observer rollouts
    of 2026-08-15, in which `user_message` appears zero times and the user-role message 5 to 19
    times per session. Reading only the first shape made the durable half of every interactive
    delivery proof answer "no turn", for launches as much as for wakes.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()

    def write_session(self, *records: dict, name: str = "rollout.jsonl") -> None:
        lines = [
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"cwd": str(self.workspace.resolve()), "originator": "codex-tui"},
                }
            )
        ]
        lines += [json.dumps(record) for record in records]
        (self.sessions / name).write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def record(kind: str, payload: dict, *, at: str = "2099-01-02T03:04:05Z") -> dict:
        return {"type": kind, "timestamp": at, "payload": payload}

    def turn_after(self, since: float) -> float | None:
        return latest_user_turn_for(str(self.workspace), since, session_root=self.sessions)

    def test_both_shapes_of_a_submitted_prompt_are_a_user_turn(self) -> None:
        for kind, payload in (
            ("event_msg", {"type": "user_message", "message": "wake"}),
            ("response_item", {"type": "message", "role": "user", "content": [{"text": "wake"}]}),
        ):
            with self.subTest(kind=kind):
                self.write_session(self.record(kind, payload))
                self.assertIsNotNone(self.turn_after(0.0))

    def test_what_the_provider_writes_without_a_prompt_is_not_a_turn(self) -> None:
        """Everything a session holds that is not somebody submitting something."""
        self.write_session(
            self.record(
                "response_item", {"type": "message", "role": "developer", "content": [{"text": "skills"}]}
            ),
            self.record(
                "response_item", {"type": "message", "role": "assistant", "content": [{"text": "done"}]}
            ),
            self.record("response_item", {"type": "reasoning"}),
            self.record("event_msg", {"type": "task_started"}),
            self.record("event_msg", {"type": "token_count"}),
        )

        self.assertIsNone(self.turn_after(0.0))

    def test_a_turn_before_the_send_is_not_a_turn_after_it(self) -> None:
        """The window is what makes this a delivery proof rather than a session history."""
        self.write_session(
            self.record(
                "response_item",
                {"type": "message", "role": "user", "content": [{"text": "earlier"}]},
            )
        )
        recorded = self.turn_after(0.0)

        self.assertIsNotNone(recorded)
        self.assertIsNone(self.turn_after(recorded + 1))

    def test_the_journal_answers_yes_no_or_nothing_and_the_three_stay_apart(self) -> None:
        """A journal that says "not yet" is not a journal that is not there.

        The difference decides who gets to answer: only the absent journal leaves a caller with
        the screen, and the screen says yes for every pane that has ever worked.
        """
        self.write_session(
            self.record(
                "response_item",
                {"type": "message", "role": "user", "content": [{"text": "wake"}]},
            )
        )
        recorded = latest_user_turn_for(str(self.workspace), 0.0, session_root=self.sessions)

        def answer(workspace: Path, since: float) -> bool | None:
            return provider_turn_started(str(workspace), since, adapter="codex", session_root=self.sessions)

        self.assertIs(answer(self.workspace, recorded - 1), True)
        self.assertIs(answer(self.workspace, recorded + 1), False)
        self.assertIsNone(answer(self.root / "elsewhere", 1.0))
        self.assertIsNone(
            provider_turn_started(str(self.workspace), 1.0, adapter="shell", session_root=self.sessions)
        )

    def test_a_journal_that_can_answer_is_not_second_guessed_by_the_screen(self) -> None:
        """`confirm` asks the provider, and the screen only when there is no provider to ask.

        `_screen_started_turn` looks for the word `Working` anywhere in the retained window, so a
        pane that has worked once keeps saying yes forever. As a fallback for an unknown provider
        that is the best there is; as a second opinion after a journal that said "not yet" it is a
        confirmation criterion that confirms everything, which is exactly what a delivery proof
        must not be.
        """
        screen_reads = 0

        def run_json(args: list[str]) -> dict:
            nonlocal screen_reads
            if args[1:3] == ["terminal", "read"]:
                screen_reads += 1
                return {"terminal": {"tail": ["Working (12s · esc to interrupt)", "›"]}}
            raise AssertionError(args)

        self.write_session(
            self.record(
                "response_item",
                {"type": "message", "role": "user", "content": [{"text": "wake"}]},
            )
        )
        recorded = latest_user_turn_for(str(self.workspace), 0.0, session_root=self.sessions)
        confirm = turn_started_confirm(
            "term-observer",
            str(self.workspace),
            "codex",
            run_json=run_json,
            session_root=self.sessions,
        )

        self.assertTrue(confirm(recorded - 1))
        self.assertFalse(confirm(recorded + 1))
        self.assertEqual(screen_reads, 0)

        # And with no journal to read, the screen is all there is, so it is asked.
        blind = turn_started_confirm(
            "term-observer",
            str(self.root / "elsewhere"),
            "codex",
            run_json=run_json,
            session_root=self.sessions,
        )
        self.assertTrue(blind(recorded + 1))
        self.assertEqual(screen_reads, 1)


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

        self.assertEqual([(name, cwd) for name, cwd in pairs if claude_project_dir_name(cwd) != name], [])
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
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2099-01-02T03:04:05Z",
                        "cwd": str(workspace),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            # A pane with nothing on it: no spinner, no status line, no glyph of any generation.
            def run_json(command: list[str]) -> dict:
                return {"terminal": {"tail": ["", "❯"]}}

            with mock.patch.dict(os.environ, {"SECRETARY_CLAUDE_PROJECTS": str(projects)}):
                confirm = turn_started_confirm("term-claude", str(workspace), "claude", run_json=run_json)
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
            screen = list(self._screen())
            if "--limit" in args:
                # Orca answers a limited read from the bottom of the retained output, which is the
                # only reason a prompt marker in it means anything.
                screen = screen[-int(args[args.index("--limit") + 1]) :]
            terminal: dict = {"tail": screen}
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
        clock = [0.0]

        def advance_clock(seconds: float) -> None:
            clock[0] += seconds

        with (
            mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_TIMEOUT_S", 0.3),
            mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_POLL_S", 0.01),
            mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RESEND_GRACE_S", 0),
            mock.patch("triggered_agents.runtime.tui_delivery.TUI_DELIVERY_RETRIES", 2),
            mock.patch("triggered_agents.runtime.tui_delivery.time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("triggered_agents.runtime.tui_delivery.time.sleep", side_effect=advance_clock),
            mock.patch("triggered_agents.runtime.agent_prompt_transport.AGENT_PROMPT_SUBMIT_DELAY_S", 0),
        ):
            return deliver_interactive_prompt(
                "term-observer", "wake the observer", run_json=pane.run_json, **kwargs
            )

    def test_a_confirmed_delivery_does_not_keep_the_previous_probes_refusal(self) -> None:
        """The receipt is derived from the last look taken, not from the one before it.

        This is the ordinary submit-only resend: the pointer sat in the composer, a bare Enter sent
        it, and the caller's criterion answered at the top of the next iteration — before that
        iteration had looked at the pane. Keeping the earlier probe's `payload_left_in_composer`
        recorded a delivery that succeeded as a determinate refusal, which the crash window this
        boundary pins would turn into a refused adoption and a head replaced for nothing.
        """
        pane = ScriptedPane({0: ["›"], 1: ["› [Pasted Content 1315 chars]"], 2: ["› ", "· recorded resume"]})

        outcome = self.deliver(pane, confirm=lambda _since: pane.submits >= 2)

        evidence = outcome.evidence
        self.assertEqual(outcome, DELIVERY_CONFIRMED)
        self.assertEqual(evidence.resends, 1)
        self.assertFalse(evidence.payload_left_in_composer)
        self.assertEqual(evidence.to_json()["delivery_receipt"], DELIVERY_RECEIPT_ACCEPTED)

    def test_an_unconfirmed_turn_is_named_as_one_whatever_else_is_on_the_screen(self) -> None:
        """A pane that went to work is not a pane held before delivery.

        Naming the pre-delivery state ahead of the stage reported a genuine
        `turn-observed-but-unconfirmed` as `pre-delivery-starting`, and that reason is what an
        operator acts on.
        """
        pane = ScriptedPane(
            {
                0: ["›"],
                1: ["· output", "› ", "  ⏎ send   tab to queue message   ctrl+c quit"],
            },
            cursors={0: "1", 1: "2"},
        )

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, confirm=lambda _since: False)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.pre_delivery_after, PRE_DELIVERY_STARTING)
        self.assertEqual(evidence.stage, "turn_observed")
        self.assertEqual(evidence.reason, "turn-observed-but-unconfirmed")

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

    def test_a_repainting_composer_is_not_a_composer_holding_the_payload(self) -> None:
        """The 62-out-of-62 false failure, in the shape a live Codex pane produced it.

        The composer is empty on both sides of the send. What sits after the prompt marker is the
        TUI's own furniture: a hint, the model footer, and — once the head starts working — a
        counter that ticks every frame. So the fingerprint differs between the two probes while the
        composer never held anything, and the delivery used to read that difference as the payload
        being stuck. It cost sprint:1089 62 delivered wakes reported as failures and 14 head
        replacements in one day.
        """
        hint = "Improve documentation in @filename gpt-5.6-terra xhigh · ~/observers/sprint-1089"
        pane = ScriptedPane(
            {
                0: ["> read the previous card", f"› {hint}"],
                1: ["> read the previous card", "· recorded resume", f"› {hint} Working (7s)"],
            },
            cursors={0: "566", 1: "588"},
        )

        outcome = self.deliver(pane, ack_out_of_band=True)

        evidence = outcome.evidence
        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        # The fingerprints differ, and that on its own says nothing about where the payload is.
        self.assertNotEqual(evidence.composer_before, evidence.composer_after)
        self.assertFalse(evidence.payload_left_in_composer)
        self.assertTrue(evidence.cursor_moved)
        self.assertEqual(evidence.stage, "turn_observed")
        # Nothing was re-entered, so the head was woken exactly once.
        self.assertEqual(pane.sends, ["wake the observer", ""])

    def test_the_composer_is_the_bottom_of_the_pane_and_not_the_retained_history(self) -> None:
        """A marker stranded in the history is not a composer, and what follows it is not held.

        `orca terminal read` with no limit answers with 120 lines of retained output. A TUI that
        repaints its bottom block in place leaves earlier markers inside that window, and on the
        live observer pane the text sitting after the last of them was the transcript of an earlier
        wake — the payload itself, quoted back. Read that way, every wake is a payload stuck in a
        composer forever. The delivery reads a bounded window from the bottom instead.
        """
        history = ["› wake the observer and let it work"] + [f"· step {index}" for index in range(40)]
        painted = history + ["› Improve documentation in @filename gpt-5.6-terra"]
        pane = ScriptedPane({0: painted, 1: painted + ["· recorded resume"]})

        outcome = self.deliver(pane, ack_out_of_band=True)

        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertFalse(outcome.evidence.payload_left_in_composer)
        reads = [call for call in pane.calls if call[1:3] == ["terminal", "read"]]
        self.assertTrue(reads)
        for read in reads:
            self.assertIn("--limit", read)
        # And the same screen read whole is exactly the trap: the payload does follow the last
        # marker of the unbounded window, which is why the bound is the fix and not a tidy-up.
        self.assertTrue(composer_holds_payload("\n".join(painted[:1] + history[1:]), "wake the observer"))

    def test_a_composer_showing_the_payload_is_still_a_delivery_that_did_not_land(self) -> None:
        """The failure this boundary exists for, without a paste placeholder over it.

        Codex hides a large paste behind `[Pasted Content …]`; a smaller one it simply shows. Both
        are the payload sitting in a composer that never took an Enter, and the positive test has
        to catch the second as well as the first or the fix would have bought the wake by giving
        up the thing the boundary is for.
        """
        pane = ScriptedPane({0: ["›"], 1: ["› wake the observer"]})

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.reason, "payload-left-in-composer")
        self.assertTrue(evidence.payload_left_in_composer)
        # Re-entered, never rewritten: the pane is holding one copy of the prompt already.
        self.assertEqual(pane.sends, ["wake the observer", "", "", ""])

    def test_a_provider_turn_does_not_override_the_payload_still_in_the_composer(self) -> None:
        """A same-workspace turn is not proof that this payload was submitted.

        A journal can gain a user record from another turn while this delivery's bracketed paste is
        still stuck in the composer.  The direct, prompt-specific negative evidence must win; else
        the dispatcher arms an acknowledgement deadline for a wake the observer never saw.
        """
        pane = ScriptedPane({0: ["›"], 1: ["› [Pasted Content 1315 chars]"]})
        confirmations = [0]

        def confirm(_sent_at: float) -> bool:
            confirmations[0] += 1
            return True

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, confirm=confirm, ack_out_of_band=True)

        self.assertEqual(confirmations[0], 0)
        self.assertEqual(raised.exception.evidence.reason, "payload-left-in-composer")
        self.assertEqual(pane.sends, ["wake the observer", "", "", ""])

    def test_a_pane_that_printed_since_the_send_is_never_written_to_twice(self) -> None:
        """Only a pane that accounts for the payload nowhere gets it written again.

        Rewriting is the one step that can put a second copy of a prompt in front of a head. A pane
        whose composer is empty because the payload was submitted looks, to the composer alone,
        exactly like a pane the payload never reached; the output it printed since the send is what
        tells them apart, so a moved cursor buys an Enter and never a second paste.
        """
        pane = ScriptedPane(
            {0: ["ready", "›"], 1: ["ready", "· working on it", "›"]},
            cursors={0: "100", 1: "140"},
        )

        with self.assertRaises(TuiDeliveryError) as raised:
            # A caller with a criterion of its own that never fires: the loop keeps probing and
            # resending for the whole deadline, which is when a second write would happen.
            self.deliver(pane, confirm=lambda _sent_at: False)

        self.assertTrue(raised.exception.evidence.cursor_moved)
        self.assertEqual(raised.exception.evidence.resends, 2)
        # Two re-entries, and the prompt itself written exactly once.
        self.assertEqual(pane.sends, ["wake the observer", "", "", ""])
        self.assertEqual(pane.sends.count("wake the observer"), 1)

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
                    "term-codex",
                    "deliver exactly once",
                    run_json=run_json,
                    adapter="codex",
                    ack_out_of_band=True,
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
                    "orca terminal wait --for tui-idle failed: "
                    '{"result":{"wait":{"satisfied":false,"status":"running"}}}'
                ),
                READINESS_BUSY,
                "readiness-busy",
            ),
            (
                HostError("orca terminal wait --for tui-idle failed: connection refused"),
                READINESS_UNAVAILABLE,
                "readiness-unavailable",
            ),
            (HostError(STALE_HANDLE_WAIT_FAILURE), READINESS_STALE_HANDLE, "readiness-stale_handle"),
        )
        for refusal, state, reason in cases:
            with self.subTest(state=state, refusal=str(refusal)):

                def run_json(_command: list[str], refusal: BaseException = refusal) -> dict:
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


# The screens two incidents left behind, transcribed rather than paraphrased. A test that invents
# its own wording proves the regex it was written against and nothing about the pane.

# issue:e4d6f307, 2026-09-02 00:55: a `codex-high` reviewer sat 51 minutes on this, composer empty,
# provider source unbound, codex at zero CPU, and Orca answering `tui-idle` satisfied throughout.
UPDATE_MODAL_SCREEN = [
    "✨ Update available! 0.152.0 -> 0.152.1",
    "Release notes: https://github.com/openai/codex/releases/latest",
    "  1. Update now (runs `npm install -g @openai/codex@latest`)",
    "  2. Skip",
    "  3. Skip until next version",
    "Press enter to continue",
]

# issue:2fdac531, sprint:1419: Codex still starting, and Orca reporting `tui-idle/ready` for it.
# The TASK pointer went into that composer and three Enters in 12 seconds each came back
# `accepted` with one byte written, while the cursor never moved.
STARTING_SCREEN = [
    "  Starting MCP servers",
    "› ",
    "  ⏎ send   tab to queue message   ctrl+c quit",
]

# The same two states as Orca actually retains them, captured on this host on 2026-09-03 by
# creating an Orca terminal, running the interactive Codex the heads run
# (`CODEX_HOME=$HOME/.config/orca/codex-runtime-home/home codex`, v0.152.1) in it and reading the
# pane back. `terminal read` answers with retained raw output, not a rendered screen, so a screen
# above is a moment and a tail below is that moment plus everything after it. A classifier that
# cannot tell the two apart passes every test written against the screens and delivers to no head.
PANE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "panes"


def retained_tail(name: str) -> list[str]:
    """One captured `orca terminal read`, as the pane's tail lines."""
    payload = json.loads((PANE_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return [str(line) for line in payload["terminal"]["tail"]]


# A started, idle, sendable Codex pane: model resolved, both MCP servers up, composer painting its
# own hint, and the output cursor no longer moving. Its tail still carries
# `Starting MCP servers`, because that is what retention means.
STARTED_IDLE_TAIL = retained_tail("codex-started-idle")
# The same pane shape after a dialog has been settled: the update modal's six lines from
# issue:e4d6f307 sit in the tail above a live, idle composer.
SETTLED_UPDATE_MODAL_TAIL = retained_tail("codex-settled-update-modal")


class PreDeliveryStateTests(unittest.TestCase):
    """A pane that Orca calls ready is not therefore a pane that can take a prompt.

    Orca decides `tui-idle` from the pane's agent status and a quiescence window, and a TUI holding
    its own update dialog or still starting its MCP servers is perfectly quiescent. Both incidents
    below are that disagreement: `accepted: true`, bytes written, `tui-idle` satisfied, and the
    pointer never reached a provider.
    """

    def test_the_update_modal_from_the_incident_is_a_typed_pre_delivery_state(self) -> None:
        self.assertEqual(classify_pre_delivery("\n".join(UPDATE_MODAL_SCREEN)), PRE_DELIVERY_UPDATE_MODAL)

    def test_the_startup_screen_from_the_incident_is_a_typed_pre_delivery_state(self) -> None:
        self.assertEqual(classify_pre_delivery("\n".join(STARTING_SCREEN)), PRE_DELIVERY_STARTING)

    def test_an_unrecognised_dialog_is_named_and_never_guessed_at(self) -> None:
        """Codex's own modal footer under a screen none of the known patterns match."""
        screen = "Select a base branch\n  1. main\n  2. release\nPress enter to continue"
        self.assertEqual(classify_pre_delivery(screen), PRE_DELIVERY_UNKNOWN_DIALOG)

    def test_orca_naming_a_blocked_reason_is_a_dialog_even_with_no_readable_screen(self) -> None:
        self.assertEqual(classify_pre_delivery("", readiness=READINESS_BLOCKED), PRE_DELIVERY_UNKNOWN_DIALOG)

    def test_an_ordinary_codex_pane_is_in_no_pre_delivery_state(self) -> None:
        """The classification must not fire on the furniture every Codex pane paints.

        A hint, the model footer and a working counter all sit in the same region, and a screen
        that is merely quiet is the normal case this boundary delivers into thousands of times.
        """
        for screen in (
            "› ",
            "> read the previous card\n› Improve documentation in @filename gpt-5.6-terra xhigh",
            "· recorded resume\n› Working (7s)",
            "",
        ):
            with self.subTest(screen=screen):
                self.assertEqual(classify_pre_delivery(screen), "")

    def test_a_started_pane_is_sendable_though_its_tail_still_names_the_startup(self) -> None:
        """The retained tail of a pane that is ready throughout, from the real backend.

        This is the first delivery every Codex head receives. Orca keeps the pane's raw output and
        Codex redraws in place, so `Starting MCP servers` is still in
        the tail long after both servers are up — measured here with the output cursor unchanged
        across reads 20s apart. Reading that as the screen makes every Codex head permanently
        un-sendable, which is a total false negative in the boundary built to stop a false positive.
        """
        tail = "\n".join(STARTED_IDLE_TAIL)
        self.assertIn("Starting MCP servers (1/2): po_memory", tail)
        self.assertEqual(classify_pre_delivery(tail), "")
        self.assertEqual(classify_pre_delivery(tail, readiness=READINESS_READY), "")
        # And the live screen is the composer's own hint, which is what the pane is painting.
        self.assertIn("Ask Codex to do anything", live_screen(tail))

    def test_a_settled_update_modal_in_the_tail_is_not_a_dialog_and_authorises_no_key(self) -> None:
        """The second consequence of reading history as a screen, and it needs its own guard.

        Whatever settles the modal — the preflight `dismissed_version` write, a person, or this
        code's own Skip — leaves its words in the tail with a live composer painted under them.
        Typing `3` at that pane does not dismiss anything: it submits a bare `3` to the provider,
        burning a turn immediately before the task pointer arrives.
        """
        tail = "\n".join(SETTLED_UPDATE_MODAL_TAIL)
        self.assertIn("Update available! 0.152.0 -> 0.152.1", tail)
        self.assertIn("Press enter to continue", tail)
        self.assertEqual(classify_pre_delivery(tail), "")
        self.assertFalse(dialog_is_live(tail))

    def test_whether_a_dialog_is_live_is_asked_apart_from_which_dialog_it_is(self) -> None:
        """The keystroke condition is separate so a later pattern cannot inherit the hazard."""
        live = "\n".join(UPDATE_MODAL_SCREEN)
        self.assertEqual(classify_pre_delivery(live), PRE_DELIVERY_UPDATE_MODAL)
        self.assertTrue(dialog_is_live(live))
        # A pane Orca itself reports held in a dialog is showing one now, whatever the tail says.
        self.assertTrue(dialog_is_live("", readiness=READINESS_BLOCKED))
        # A screen matching the known modal's words with no footer under them is not proof that a
        # dialog is up, and the fail-closed answer to no proof is no key.
        partial = "✨ Update available! 0.152.0 -> 0.152.1   3. Skip until next version"
        self.assertEqual(classify_pre_delivery(partial), PRE_DELIVERY_UPDATE_MODAL)
        self.assertFalse(dialog_is_live(partial))

    def test_a_record_that_carried_the_derived_receipt_is_read_back_inertly(self) -> None:
        """`to_json` publishes a derivation; `from_json` restores only what is stored.

        The derived key travels as `delivery_receipt`, but a persisted payload that ever carried it
        under the property's own name must be ignored rather than raise on a read-only property.
        """
        record = DeliveryEvidence(stage="acknowledged", turn_confirmed=True).to_json()
        self.assertEqual(record["delivery_receipt"], DELIVERY_RECEIPT_ACCEPTED)
        restored = DeliveryEvidence.from_json({**record, "receipt": "refused"})
        self.assertEqual(restored.stage, "acknowledged")
        self.assertTrue(restored.turn_confirmed)
        self.assertEqual(restored.receipt, DELIVERY_RECEIPT_ACCEPTED)

    def test_the_receipt_is_asked_of_the_evidence_and_of_nothing_else(self) -> None:
        """`accepted`/`bytesWritten` and a stage are not a receipt; the composer's answer is."""
        written = {"stage": "payload_written", "send_accepted": True, "bytes_written": 1315}
        self.assertEqual(delivery_receipt_state(written), DELIVERY_RECEIPT_REFUSED)
        self.assertEqual(
            delivery_receipt_state({**written, "payload_left_in_composer": True}),
            DELIVERY_RECEIPT_REFUSED,
        )
        self.assertEqual(
            delivery_receipt_state({"stage": "acknowledged", "turn_confirmed": True}),
            DELIVERY_RECEIPT_ACCEPTED,
        )
        # A turn the provider recorded does not survive proof that the pointer is still sitting
        # in the composer: the direct, prompt-specific negative evidence wins.
        self.assertEqual(
            delivery_receipt_state(
                {"stage": "acknowledged", "turn_confirmed": True, "payload_left_in_composer": True}
            ),
            DELIVERY_RECEIPT_REFUSED,
        )
        # A bring-up that failed before a prompt existed observed no receipt either way.
        self.assertEqual(
            delivery_receipt_state({"subject": "worker-launch", "reason": "split refused"}),
            DELIVERY_RECEIPT_UNOBSERVED,
        )
        self.assertEqual(delivery_receipt_state(None), DELIVERY_RECEIPT_UNOBSERVED)


class PreDeliveryDeliveryTests(TuiDeliveryStageTests):
    """The same delivery boundary, driven at the two screens that produced the incidents."""

    def test_the_known_update_modal_is_answered_with_its_documented_skip_choice(self) -> None:
        """The modal is settled deterministically and the same pointer is then delivered once.

        The keystroke is Codex's own third choice, "Skip until next version". Upgrading is a
        separate, explicit action, so no delivery may reach for choice 1 to get past a screen. The
        modal is answered before the payload is written, so the body is written exactly once and
        the pointer that goes in is the one the caller handed over.
        """
        # The modal answer counts as a submit in this fake, so the screens key on it: after the
        # skip the pane is an ordinary composer, and after the pointer's Enter it has printed.
        pane = ScriptedPane(
            {
                0: UPDATE_MODAL_SCREEN,
                1: ["ready", "›"],
                2: ["ready", "· recorded resume", "›"],
            }
        )

        outcome = self.deliver(pane, ack_out_of_band=True)

        evidence = outcome.evidence
        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertEqual(evidence.pre_delivery_before, PRE_DELIVERY_UPDATE_MODAL)
        self.assertEqual(evidence.modal_resolution, "answered-skip")
        self.assertEqual(evidence.modal_answers, 1)
        # One "3", then the pointer, then its Enter. Nothing else was typed at the screen.
        self.assertEqual(pane.sends, ["3", "wake the observer", ""])
        self.assertEqual(pane.sends.count("wake the observer"), 1)
        self.assertEqual(evidence.body_write_count, 1)
        self.assertEqual(evidence.to_json()["delivery_receipt"], DELIVERY_RECEIPT_ACCEPTED)

    def test_readiness_is_proved_again_after_the_modal_is_answered(self) -> None:
        """The pointer is never written on the strength of the readiness proved before the dialog."""
        pane = ScriptedPane(
            {0: UPDATE_MODAL_SCREEN, 1: ["ready", "›"], 2: ["ready", "· recorded resume", "›"]}
        )

        self.deliver(pane, ack_out_of_band=True)

        waits = [call for call in pane.calls if call[1:3] == ["terminal", "wait"]]
        skip = next(index for index, call in enumerate(pane.calls) if call[1:3] == ["terminal", "send"])
        after_skip = [call for call in pane.calls[skip + 1 :] if call[1:3] == ["terminal", "wait"]]
        self.assertTrue(waits)
        self.assertTrue(after_skip, "readiness is re-proved between the modal answer and the write")

    def test_a_startup_pane_orca_calls_ready_is_refused_and_never_written_to(self) -> None:
        """issue:2fdac531, at the boundary that should have stopped it.

        Orca answers `tui-idle` satisfied for this pane. The screen says the head is still starting
        its MCP servers and its composer queues rather than submits, so nothing is written into it
        and the failure names the state that was observed.
        """
        pane = ScriptedPane({0: STARTING_SCREEN})

        with mock.patch("triggered_agents.runtime.tui_delivery.TUI_PRE_DELIVERY_TIMEOUT_S", 0):
            with self.assertRaises(TuiDeliveryError) as raised:
                self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.pre_delivery_before, PRE_DELIVERY_STARTING)
        self.assertEqual(evidence.reason, f"pre-delivery-{PRE_DELIVERY_STARTING}")
        self.assertEqual(evidence.readiness_before, READINESS_READY)
        self.assertEqual(evidence.stage, "none")
        self.assertEqual(pane.sends, [], "nothing is typed into a pane that cannot take a prompt")
        self.assertEqual(evidence.to_json()["delivery_receipt"], DELIVERY_RECEIPT_REFUSED)

    def test_a_startup_pane_that_finishes_starting_is_then_delivered_to(self) -> None:
        """The state clears on its own, which is what makes it a phase and not a dialog."""
        pane = ScriptedPane({0: STARTING_SCREEN})
        reads = [0]

        def reading(args: list[str]) -> dict:
            if args[1:3] == ["terminal", "read"]:
                reads[0] += 1
                if reads[0] == 1:
                    return {"terminal": {"tail": STARTING_SCREEN, "nextCursor": "10"}}
                return {"terminal": {"tail": ["ready", "›"], "nextCursor": str(10 + reads[0])}}
            return pane.run_json(args)

        with mock.patch("triggered_agents.runtime.tui_delivery.TUI_PRE_DELIVERY_POLL_S", 0):
            outcome = self.deliver(ScriptedPaneProxy(pane, reading), ack_out_of_band=True)

        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertEqual(outcome.evidence.pre_delivery_before, PRE_DELIVERY_STARTING)
        self.assertEqual(outcome.evidence.modal_resolution, "not-present")
        self.assertEqual(pane.sends, ["wake the observer", ""])

    def test_a_started_head_is_delivered_to_though_its_tail_names_the_startup(self) -> None:
        """The whole boundary, driven at the tail a real started Codex pane actually returns.

        With the classification reading that tail as a screen, this delivery spun out
        `TUI_PRE_DELIVERY_TIMEOUT_S` on a state that was over, wrote zero bytes and became a
        `HeadPaneBusy` deferral — for the first prompt every Codex head is ever given.
        """
        pane = ScriptedPane(
            {0: STARTED_IDLE_TAIL, 1: STARTED_IDLE_TAIL + ["· recorded resume"]},
            cursors={0: "16", 1: "17"},
        )

        outcome = self.deliver(pane, ack_out_of_band=True)

        evidence = outcome.evidence
        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertEqual(evidence.pre_delivery_before, "")
        self.assertEqual(evidence.modal_resolution, "not-present")
        self.assertEqual(pane.sends, ["wake the observer", ""])
        self.assertEqual(evidence.to_json()["delivery_receipt"], DELIVERY_RECEIPT_ACCEPTED)

    def test_a_settled_modal_in_the_tail_gets_no_keystroke_and_the_pointer_instead(self) -> None:
        """The same pane once something has settled the update modal above the composer.

        Read as a screen, this tail made the delivery type `3` with Enter into an ordinary
        composer — a bare `3` submitted to the provider immediately before its task pointer, twice,
        and then a refusal because history cannot be cleared.
        """
        pane = ScriptedPane(
            {0: SETTLED_UPDATE_MODAL_TAIL, 1: SETTLED_UPDATE_MODAL_TAIL + ["· recorded resume"]},
            cursors={0: "24", 1: "25"},
        )

        outcome = self.deliver(pane, ack_out_of_band=True)

        evidence = outcome.evidence
        self.assertEqual(outcome, DELIVERY_ACCEPTED)
        self.assertEqual(evidence.modal_answers, 0)
        self.assertEqual(evidence.modal_resolution, "not-present")
        self.assertNotIn("3", pane.sends)
        self.assertEqual(pane.sends, ["wake the observer", ""])

    def test_the_known_modal_is_answered_only_while_it_is_the_live_screen(self) -> None:
        """A pattern is never the whole authorisation for a keystroke.

        The screen below carries the modal's own words and no footer under them, so the code
        recognises the modal and still cannot prove it is up. That is a typed refusal with nothing
        typed at the pane, exactly as an unknown dialog is.
        """
        pane = ScriptedPane({0: ["✨ Update available! 0.152.0 -> 0.152.1   3. Skip until next version"]})

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.pre_delivery_before, PRE_DELIVERY_UPDATE_MODAL)
        self.assertEqual(evidence.modal_resolution, "refused-not-on-screen")
        self.assertEqual(evidence.reason, "modal-not-on-screen")
        self.assertEqual(evidence.modal_answers, 0)
        self.assertEqual(pane.sends, [])

    def test_an_unknown_dialog_fails_closed_with_no_keystrokes_at_all(self) -> None:
        """A screen this code does not recognise is one it cannot answer.

        The refusal is typed so the caller can bound its retry and escalate; what it must never be
        is an arbitrary keystroke sent at a dialog nobody read.
        """
        pane = ScriptedPane({0: ["Trust this workspace?", "  1. Yes", "  2. No", "Press enter to continue"]})

        with self.assertRaises(TuiDeliveryError) as raised:
            self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.pre_delivery_before, PRE_DELIVERY_UNKNOWN_DIALOG)
        self.assertEqual(evidence.reason, "unknown-dialog")
        self.assertEqual(evidence.modal_resolution, "refused-unknown")
        self.assertEqual(evidence.modal_answers, 0)
        self.assertEqual(pane.sends, [])

    def test_a_modal_that_does_not_clear_is_a_bounded_refusal(self) -> None:
        """Answering forever is the failure mode this bound exists to prevent."""
        pane = ScriptedPane({0: UPDATE_MODAL_SCREEN})

        with mock.patch("triggered_agents.runtime.tui_delivery.TUI_PRE_DELIVERY_POLL_S", 0):
            with self.assertRaises(TuiDeliveryError) as raised:
                self.deliver(pane, ack_out_of_band=True)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.modal_resolution, "unresolved")
        self.assertEqual(evidence.modal_answers, 2)
        self.assertEqual(pane.sends, ["3", "3"], "only the documented choice, and only twice")
        self.assertEqual(evidence.to_json()["delivery_receipt"], DELIVERY_RECEIPT_REFUSED)

    def test_the_three_facts_are_recorded_apart(self) -> None:
        """Modal resolution, delivery receipt and provider binding are not one "delivered" bit."""
        pane = ScriptedPane(
            {0: UPDATE_MODAL_SCREEN, 1: ["ready", "›"], 2: ["thinking"]}, idle_after={2: False}
        )

        outcome = self.deliver(pane, confirm=lambda _sent_at: False, ack_out_of_band=True)

        record = outcome.evidence.to_json()
        self.assertEqual(record["modal_resolution"], "answered-skip")
        self.assertEqual(record["delivery_receipt"], DELIVERY_RECEIPT_ACCEPTED)
        self.assertFalse(record["provider_bound"], "the caller's own criterion never fired")
        self.assertTrue(record["turn_confirmed"], "the pane's own evidence did")


class ScriptedPaneProxy:
    """A `ScriptedPane` whose reads are answered by a supplied function."""

    def __init__(self, pane: ScriptedPane, run_json) -> None:
        self._pane = pane
        self.run_json = run_json

    def __getattr__(self, name: str):
        return getattr(self._pane, name)
