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
        self.source.write_text(
            "\n".join(
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

        ingress.poll()

        recorded = self.written[-1]
        event = recorded.fanout_policy["events"][-1]
        self.assertEqual(event["type"], "collaboration_call")
        raw_line = self.source.read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(event["raw_event_digest"], hashlib.sha256(raw_line.encode("utf-8")).hexdigest())
        self.assertEqual(recorded.fanout_policy["terminal_state"], "violation")
        self.assertEqual(self.stops[0][0], "run-1")
        self.assertEqual(self.blocks[-1]["state"], "violation")

    def test_codex_session_event_msg_envelope_reaches_the_same_recorder(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        self._write_source({
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "parent-1",
                "item": {"type": "collab_tool_call", "tool": "spawn_agent"},
            },
        })

        ingress.poll()

        self.assertEqual(self.written[-1].fanout_policy["events"][-1]["type"], "collaboration_call")
        self.assertEqual(self.stops[0][0], "run-1")

    def test_child_edge_unknown_thread_and_malformed_line_are_typed(self) -> None:
        cases = (
            (
                {"type": "item.completed", "item": {
                    "type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "parent-1",
                    "receiver_thread_ids": ["child-1"],
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
