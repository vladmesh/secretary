"""Contract coverage for the launch-bound Codex provider-event journal ingress."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from secretary.codex_provider_events import (
    CodexProviderEventIngress,
    CodexProviderSourceError,
)
from triggered_agents.runtime import codex_preflight
from triggered_agents.runtime.head import HeadRun, HeadSpec, TaskRef


class CodexProviderEventIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\nprintf 'codex 9.9.9\\n'\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.sessions = self.root / "sessions"
        self.source = self.sessions / "2026" / "08" / "13" / "run.jsonl"
        self.source.parent.mkdir(parents=True)
        self.written: list[HeadRun] = []
        self.stops: list[tuple[str, str]] = []
        self.blocks: list[dict] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self) -> HeadRun:
        run = HeadRun(
            run_id="run-1",
            spec=HeadSpec(profile_id="codex-extra", adapter="codex", model="gpt-5.6-terra"),
            workspace=str(self.workspace),
            task_ref=TaskRef.card("secretary-1428", document=str(self.workspace / "TASK.md")),
            role="worker",
        )
        tools: list[dict] = []
        attestation = {
            "version": codex_preflight.FANOUT_ATTESTATION_VERSION,
            "role": run.role,
            "model": run.spec.model,
            "binary_digest": codex_preflight._file_digest(self.binary),
            "cli_version": "codex 9.9.9",
            "tools": tools,
            "tool_schema_digest": codex_preflight._json_digest(tools),
            "provider_schema_verdict": codex_preflight.FANOUT_SCHEMA_ALLOWED,
        }
        allowed = codex_preflight.attest_codex_fanout(
            {}, run, schema_attestation=attestation, binary_path=str(self.binary)
        )
        return allowed.with_fanout_policy({
            **allowed.fanout_policy,
            "provider_source": {
                "version": 1,
                "kind": "codex_session_event_jsonl",
                "state": "unbound",
                "root": str(self.sessions),
                "baseline": [],
            },
        })

    def _write_source(self, *events: object) -> None:
        records = [
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "thread.started", "thread_id": "parent-1"},
            *events,
        ]
        self._write_records(*records)

    def _write_records(self, *records: object) -> None:
        self.source.write_text(
            "\n".join(
                value if isinstance(value, str) else json.dumps(value) for value in records
            ) + "\n",
            encoding="utf-8",
        )

    def _append_records(self, *records: object) -> None:
        body = self.source.read_text(encoding="utf-8")
        self.source.write_text(
            body + "\n".join(
                value if isinstance(value, str) else json.dumps(value) for value in records
            ) + "\n",
            encoding="utf-8",
        )

    def _ingress(self, run: HeadRun | None = None) -> CodexProviderEventIngress:
        return CodexProviderEventIngress(
            run or self._run(),
            self.written.append,
            stop=lambda current, reason: self.stops.append((current.run_id, reason)),
            block=self.blocks.append,
        )

    def test_binds_new_session_and_cursor_before_any_provider_event(self) -> None:
        self._write_source()
        ingress = self._ingress()

        ingress.bind_before_delivery()

        bound = self.written[-1]
        source = bound.fanout_policy["provider_source"]
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(source["parent_thread_id"], "parent-1")
        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        self.assertEqual(source["initial_range"]["first"]["line"], 1)
        self.assertEqual(source["initial_range"]["root"]["line"], 2)
        self.assertEqual(source["initial_range"]["last"]["line"], 2)
        self.assertEqual(source["cursor"]["line"], 2)
        self.assertEqual(self.stops, [])

    def test_collaboration_call_is_persisted_then_fenced_and_blocked(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        self._write_source({
            "type": "item.completed",
            "item": {"type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "parent-1"},
        })

        with self.assertRaises(CodexProviderSourceError):
            ingress.poll()

        recorded = self.written[-1]
        event = recorded.fanout_policy["events"][-1]
        self.assertEqual(event["type"], "collaboration_call")
        raw_line = self.source.read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(event["raw_event_digest"], hashlib.sha256(raw_line.encode("utf-8")).hexdigest())
        self.assertEqual(recorded.fanout_policy["terminal_state"], "violation")
        self.assertEqual(self.stops[0][0], "run-1")
        self.assertEqual(self.blocks[-1]["state"], "violation")

    def test_retained_tui_tool_only_envelope_reaches_the_same_recorder(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        self._write_source({
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "parent-1",
                "item": {
                    "type": "CollabAgentToolCall",
                    "tool": "wait",
                    "sender_thread_id": "parent-1",
                    "receiver_thread_ids": [],
                },
            },
        })

        with self.assertRaises(CodexProviderSourceError):
            ingress.poll()

        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "collaboration_call")
        self.assertEqual(event["parent_thread_id"], "parent-1")
        self.assertEqual(event["tool_name"], "wait")
        self.assertEqual(self.stops[0][0], "run-1")

    def test_child_edge_unknown_thread_and_malformed_line_are_typed(self) -> None:
        cases = (
            (
                {"type": "event_msg", "payload": {
                    "type": "item_completed", "thread_id": "parent-1", "item": {
                        "type": "CollabAgentToolCall", "tool": "spawn_agent",
                        "sender_thread_id": "parent-1", "receiver_thread_ids": ["child-1"],
                    },
                }},
                "child_thread_edge", "violation",
            ),
            ({"type": "thread.started", "thread_id": "foreign-thread"}, "unknown_thread_edge", "unknown"),
            ("{not-json", "unparseable_provider_event", "unknown"),
        )
        for event, expected_type, expected_state in cases:
            with self.subTest(expected_type=expected_type):
                self.written.clear()
                self.stops.clear()
                self.blocks.clear()
                self._write_source()
                ingress = self._ingress()
                ingress.bind_before_delivery()
                self._write_source(event)

                with self.assertRaises(CodexProviderSourceError):
                    ingress.poll()

                self.assertEqual(self.written[-1].fanout_policy["events"][-1]["type"], expected_type)
                self.assertEqual(self.written[-1].fanout_policy["terminal_state"], expected_state)
                self.assertEqual(self.stops[0][0], "run-1")

    def test_recovery_cursor_mismatch_blocks_without_attributing_another_run(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        # The durable parent cursor no longer names the same raw line.  It is not a reason to
        # redirect the stop to some later session or regenerate an identity.
        self.source.write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}})
            + "\n" + json.dumps({"type": "thread.started", "thread_id": "changed-parent"}) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(CodexProviderSourceError):
            ingress.poll()

        self.assertEqual(self.stops, [("run-1", "Codex provider source identity no longer matches this HeadRun")])
        self.assertEqual(self.blocks[-1]["state"], "unknown")

    def test_clean_ordinary_prebind_lines_are_durably_advanced_before_delivery(self) -> None:
        self._write_source(
            {"type": "event_msg", "payload": {"type": "agent_message", "thread_id": "parent-1"}},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        source = self.written[-1].fanout_policy["provider_source"]
        self.assertEqual(source["cursor"]["line"], 4)
        self.assertEqual(self.written[-1].fanout_policy["events"], [])
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_malformed_post_root_prebind_line_is_recorded_then_stopped_and_blocked(self) -> None:
        self._write_source("{not-json")
        ingress = self._ingress()

        with self.assertRaises(CodexProviderSourceError):
            ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 3)
        self.assertEqual(self.stops[0][0], "run-1")
        self.assertEqual(self.blocks[-1]["state"], "unknown")

    def test_tui_policy_event_in_post_root_prebind_tail_blocks_before_delivery(self) -> None:
        self._write_source({
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "parent-1",
                "item": {
                    "type": "CollabAgentToolCall",
                    "tool": "spawn_agent",
                    "sender_thread_id": "parent-1",
                    "receiver_thread_ids": ["child-1"],
                },
            },
        })
        ingress = self._ingress()

        with self.assertRaises(CodexProviderSourceError):
            ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "child_thread_edge")
        self.assertEqual(event["child_thread_id"], "child-1")
        self.assertEqual(self.blocks[-1]["state"], "violation")

    def test_unrecognised_collaboration_shape_is_unknown_not_a_clean_cursor_advance(self) -> None:
        self._write_source({
            "type": "event_msg",
            "payload": {
                "type": "item_completed", "thread_id": "parent-1",
                "item": {"type": "CollabAgentStatus", "sender_thread_id": "parent-1"},
            },
        })
        ingress = self._ingress()

        with self.assertRaises(CodexProviderSourceError):
            ingress.bind_before_delivery()

        self.assertEqual(self.written[-1].fanout_policy["events"][-1]["type"], "unknown_thread_edge")
        self.assertEqual(self.written[-1].fanout_policy["terminal_state"], "unknown")

    def test_malformed_pre_root_line_is_recorded_from_the_selected_source_range(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            "{partially-written",
            {"type": "thread.started", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        with self.assertRaises(CodexProviderSourceError):
            ingress.bind_before_delivery()

        selected = self.written[0].fanout_policy["provider_source"]
        self.assertEqual(selected["cursor"]["line"], 0)
        self.assertEqual(selected["initial_range"]["first"]["line"], 1)
        self.assertEqual(selected["initial_range"]["root"]["line"], 3)
        self.assertEqual(selected["initial_range"]["last"]["line"], 3)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 2)
        self.assertEqual(self.stops[0][0], "run-1")
        self.assertEqual(self.blocks[-1]["state"], "unknown")

    def test_clean_recognised_pre_root_records_cross_the_root_in_one_scanner(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        self.assertEqual(
            [run.fanout_policy["provider_source"]["cursor"]["line"] for run in self.written],
            [0, 1, 2, 3, 4],
        )
        self.assertEqual(self.written[-1].fanout_policy["events"], [])
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_policy_pre_root_record_is_fenced_after_source_selection(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CollabAgentToolCall",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent-1",
                        "receiver_thread_ids": ["child-1"],
                    },
                },
            },
            {"type": "thread.started", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        with self.assertRaises(CodexProviderSourceError):
            ingress.bind_before_delivery()

        selected = self.written[0].fanout_policy["provider_source"]
        self.assertEqual(selected["initial_range"]["root"]["line"], 3)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "child_thread_edge")
        self.assertEqual(event["source_sequence"], 2)
        self.assertEqual(event["child_thread_id"], "child-1")
        self.assertEqual(self.blocks[-1]["state"], "violation")

    def test_recovery_reuses_the_persisted_full_range_before_later_events(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()
        ingress.bind_before_delivery()
        persisted = HeadRun.from_json(self.written[-1].to_json())
        writes_after_bind = len(self.written)
        recovered = self._ingress(persisted)

        recovered.poll()

        self.assertEqual(len(self.written), writes_after_bind)
        self._append_records("{not-json")
        with self.assertRaises(CodexProviderSourceError):
            recovered.poll()

        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 5)
        self.assertEqual(self.stops[-1][0], "run-1")

    def test_recovery_fences_a_changed_record_inside_the_persisted_initial_range(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()
        ingress.bind_before_delivery()
        persisted = HeadRun.from_json(self.written[-1].to_json())
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message_rewritten"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )

        with self.assertRaises(CodexProviderSourceError):
            self._ingress(persisted).poll()

        self.assertEqual(self.stops[-1][0], "run-1")
        self.assertEqual(self.blocks[-1]["state"], "unknown")

    def test_prebind_cursor_persistence_failure_blocks_without_delivery(self) -> None:
        self._write_source({"type": "turn.completed", "thread_id": "parent-1"})

        def fail_after_binding(run: HeadRun) -> None:
            self.written.append(run)
            if len(self.written) > 1:
                raise OSError("disk full")

        ingress = CodexProviderEventIngress(
            self._run(),
            fail_after_binding,
            stop=lambda current, reason: self.stops.append((current.run_id, reason)),
            block=self.blocks.append,
        )

        with self.assertRaises(codex_preflight.CodexFanoutRecordingError):
            ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["state"], "bound")
        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        self.assertEqual(self.stops[0][0], "run-1")
        self.assertEqual(self.blocks[-1]["state"], "unknown")

    def test_failed_durable_event_write_blocks_and_never_silently_drops_the_event(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        self._write_source({
            "type": "item.completed",
            "item": {"type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "parent-1"},
        })

        def fail(_run: HeadRun) -> None:
            raise OSError("disk full")

        ingress.persist = fail
        with self.assertRaises(codex_preflight.CodexFanoutRecordingError):
            ingress.poll()

        self.assertEqual(self.stops[0][0], "run-1")
        self.assertEqual(self.blocks[-1]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
