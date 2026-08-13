"""Focused contract tests for the fail-closed Codex provider fan-out boundary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from secretary.dispatcher import CommandHostRuntime
from secretary.dispatcher_types import HostError
from triggered_agents.runtime import codex_preflight
from triggered_agents.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef, spawn
from triggered_agents.runtime.pane_host import Pane


class _Host:
    def __init__(self) -> None:
        self.opened = 0

    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        self.opened += 1
        return Pane(handle="pane", leaf="leaf")

    def split_pane(self, handle: str, command: str) -> Pane:  # pragma: no cover - protocol shape
        raise AssertionError("not used")

    def rename_pane(self, handle: str, title: str) -> None:  # pragma: no cover - protocol shape
        raise AssertionError("not used")

    def close_pane(self, handle: str) -> None:  # pragma: no cover - protocol shape
        raise AssertionError("not used")

    def panes(self, workspace: str) -> list[Pane]:  # pragma: no cover - protocol shape
        return []

    def stop_workspace(self, workspace: str) -> None:  # pragma: no cover - protocol shape
        raise AssertionError("not used")

    def send(self, handle: str, text: str, *, enter: bool):  # pragma: no cover - protocol shape
        raise AssertionError("not used")

    def read(self, handle: str, *, limit: int | None = None):  # pragma: no cover - protocol shape
        return {}

    def wait_idle(self, handle: str, *, timeout_ms: int):  # pragma: no cover - protocol shape
        return {"satisfied": True}


class CodexFanoutPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\nprintf 'codex 9.9.9\\n'\n", encoding="utf-8")
        self.binary.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, role: str = "worker") -> HeadRun:
        return HeadRun(
            run_id="run-1",
            spec=HeadSpec(profile_id="codex-extra", adapter="codex", model="gpt-5.6-terra"),
            workspace=str(self.workspace),
            task_ref=TaskRef.card("secretary-1428", document=str(self.workspace / "TASK.md")),
            role=role,
        )

    def _schema(self, run: HeadRun) -> dict:
        tools: list[dict] = []
        return {
            "version": codex_preflight.FANOUT_ATTESTATION_VERSION,
            "role": run.role,
            "model": run.spec.model,
            "binary_digest": codex_preflight._file_digest(self.binary),
            "cli_version": "codex 9.9.9",
            "tools": tools,
            "tool_schema_digest": codex_preflight._json_digest(tools),
            "provider_schema_verdict": codex_preflight.FANOUT_SCHEMA_ALLOWED,
        }

    def _allowed_run(self) -> HeadRun:
        run = self._run()
        return codex_preflight.attest_codex_fanout(
            {}, run, schema_attestation=self._schema(run), binary_path=str(self.binary)
        )

    def test_schema_absent_and_schema_unknown_are_distinct_and_never_clean(self) -> None:
        absent = codex_preflight.attest_codex_fanout({}, self._run(), binary_path=str(self.binary))
        unknown = codex_preflight.attest_codex_fanout(
            {}, self._run(), schema_attestation={"version": 999}, binary_path=str(self.binary)
        )

        self.assertEqual(absent.fanout_policy["state"], codex_preflight.FANOUT_SCHEMA_ABSENT)
        self.assertEqual(unknown.fanout_policy["state"], codex_preflight.FANOUT_SCHEMA_UNKNOWN)
        self.assertFalse(absent.fanout_clean)
        self.assertFalse(unknown.fanout_clean)

    def test_a_profile_claim_cannot_supply_provider_schema_evidence(self) -> None:
        run = self._run()
        profile_claim = {"codex_provider_schema_attestation": self._schema(run)}

        attested = codex_preflight.attest_codex_fanout(
            profile_claim, run, binary_path=str(self.binary)
        )

        self.assertEqual(attested.fanout_policy["state"], codex_preflight.FANOUT_SCHEMA_ABSENT)
        self.assertFalse(attested.fanout_clean)

    def test_refused_preflight_never_touches_the_codex_trust_config(self) -> None:
        config = self.root / "codex.toml"

        with self.assertRaises(codex_preflight.CodexFanoutPolicyError):
            codex_preflight.preflight_codex_launch(
                {}, str(self.workspace), self._run(), binary_path=str(self.binary), config=config
            )

        self.assertFalse(config.exists())

    def test_allowed_preflight_writes_trust_only_after_schema_attestation(self) -> None:
        run = self._run()
        config = self.root / "codex.toml"

        allowed = codex_preflight.preflight_codex_launch(
            {},
            str(self.workspace),
            run,
            schema_attestation=self._schema(run),
            binary_path=str(self.binary),
            config=config,
        )

        self.assertTrue(allowed.fanout_clean)
        self.assertEqual(allowed.fanout_policy["provider_source"]["state"], "unbound")
        self.assertEqual(allowed.fanout_policy["provider_source"]["kind"], "codex_session_event_jsonl")
        trusted = config.read_text(encoding="utf-8")
        self.assertIn("trust_level = \"trusted\"", trusted)

    def test_a_non_spawning_model_response_is_not_schema_evidence(self) -> None:
        written: list[HeadRun] = []
        recorder = codex_preflight.CodexProviderEventRecorder(
            self._allowed_run(), written.append, expected_parent_thread_id="parent"
        )
        outcome = recorder.record(
            {"type": "model_response", "text": "I will not spawn an agent."},
            source_sequence=1,
            source_location="session.jsonl:1",
        )

        self.assertEqual(outcome.event["type"], codex_preflight.EVENT_UNPARSEABLE_PROVIDER_EVENT)
        self.assertEqual(outcome.terminal_state, codex_preflight.FANOUT_TERMINAL_UNKNOWN)
        self.assertEqual(written[-1].fanout_policy["terminal_state"], "unknown")

    def test_every_declared_provider_event_type_is_durable_before_its_result(self) -> None:
        cases = {
            codex_preflight.EVENT_COLLABORATION_CALL: {
                "type": "collaboration_call", "parent_thread_id": "parent", "tool": "spawn_agent"
            },
            codex_preflight.EVENT_CHILD_THREAD_EDGE: {
                "type": "child_thread_edge", "parent_thread_id": "parent", "child_thread_id": "child"
            },
            codex_preflight.EVENT_UNKNOWN_THREAD_EDGE: {
                "type": "unknown_thread_edge", "parent_thread_id": "parent"
            },
            codex_preflight.EVENT_UNPARSEABLE_PROVIDER_EVENT: {
                "type": "unparseable_provider_event", "parent_thread_id": "parent"
            },
        }
        for expected, raw in cases.items():
            with self.subTest(expected=expected):
                written: list[HeadRun] = []
                outcome = codex_preflight.CodexProviderEventRecorder(
                    self._allowed_run(), written.append, expected_parent_thread_id="parent"
                ).record(raw, source_sequence=2, source_location="stream:2")
                self.assertEqual(outcome.event["type"], expected)
                self.assertEqual(written[-1].fanout_policy["events"][-1], outcome.event)

    def test_spawn_and_unknown_edges_fail_closed(self) -> None:
        recorder = codex_preflight.CodexProviderEventRecorder(
            self._allowed_run(), lambda _run: None, expected_parent_thread_id="parent"
        )
        spawned = recorder.record(
            {"type": "child_thread_edge", "parent_thread_id": "parent", "child_thread_id": "child"},
            source_sequence=3, source_location="stream:3",
        )
        unknown = codex_preflight.CodexProviderEventRecorder(
            self._allowed_run(), lambda _run: None, expected_parent_thread_id="parent"
        ).record(
            {"type": "child_thread_edge", "parent_thread_id": "wrong", "child_thread_id": "child"},
            source_sequence=4, source_location="stream:4",
        )
        self.assertEqual(spawned.terminal_state, codex_preflight.FANOUT_TERMINAL_VIOLATION)
        self.assertEqual(unknown.terminal_state, codex_preflight.FANOUT_TERMINAL_UNKNOWN)

    def test_recorder_failure_blocks_after_attempting_the_durable_write(self) -> None:
        order: list[str] = []

        def persist(_run: HeadRun) -> None:
            order.append("persist")
            raise OSError("disk full")

        recorder = codex_preflight.CodexProviderEventRecorder(
            self._allowed_run(), persist, expected_parent_thread_id="parent"
        )
        with self.assertRaises(codex_preflight.CodexFanoutRecordingError):
            codex_preflight.enforce_provider_event(
                recorder,
                {"type": "collaboration_call", "parent_thread_id": "parent", "tool": "spawn_agent"},
                source_sequence=5,
                source_location="stream:5",
                stop=lambda _run, _reason: order.append("stop"),
                block=lambda _evidence: order.append("block"),
            )
        self.assertEqual(order, ["persist", "stop", "block"])

    def test_head_run_round_trip_and_historical_record_remain_non_clean(self) -> None:
        allowed = self._allowed_run()
        restored = HeadRun.from_json(allowed.to_json())
        historical = HeadRun.from_json({
            key: value for key, value in allowed.to_json().items() if key != "fanout_policy"
        })
        malformed = HeadRun.from_json({**allowed.to_json(), "fanout_policy": {"version": 1}})

        self.assertTrue(restored.fanout_clean)
        self.assertFalse(historical.fanout_clean)
        self.assertFalse(malformed.fanout_clean)
        self.assertEqual(historical.fanout_policy_state, "unknown")

    def test_malformed_provider_source_remains_unknown_on_recovery(self) -> None:
        allowed = self._allowed_run()
        malformed = HeadRun.from_json({
            **allowed.to_json(),
            "fanout_policy": {
                **allowed.fanout_policy,
                "provider_source": {"version": 1, "state": "bound"},
            },
        })

        self.assertFalse(malformed.fanout_clean)
        self.assertEqual(malformed.fanout_policy_state, "unknown")
        self.assertTrue(malformed.fanout_policy["provider_source_required"])
        self.assertEqual(malformed.fanout_policy["provider_source"], {})

    def test_bound_source_without_a_full_range_anchor_remains_unknown_on_recovery(self) -> None:
        allowed = self._allowed_run()
        incomplete = HeadRun.from_json({
            **allowed.to_json(),
            "fanout_policy": {
                **allowed.fanout_policy,
                "provider_source_required": True,
                "provider_source": {
                    "version": 1,
                    "kind": "codex_session_event_jsonl",
                    "state": "bound",
                    "root": "/sessions",
                    "path": "/sessions/run.jsonl",
                    "session_id": "session-1",
                    "parent_thread_id": "parent-1",
                    "cursor": {"line": 2, "digest": "0" * 64},
                    "bound_at": "2026-08-13T00:00:00Z",
                },
            },
        })

        self.assertFalse(incomplete.fanout_clean)
        self.assertEqual(incomplete.fanout_policy_state, "unknown")
        self.assertTrue(incomplete.fanout_policy["provider_source_required"])
        self.assertEqual(incomplete.fanout_policy["provider_source"], {})

    def test_missing_required_provider_source_remains_an_ingress_fenced_unknown(self) -> None:
        allowed = self._allowed_run()
        missing = HeadRun.from_json({
            **allowed.to_json(),
            "fanout_policy": {**allowed.fanout_policy, "provider_source_required": True},
        })

        self.assertFalse(missing.fanout_clean)
        self.assertEqual(missing.fanout_policy_state, "unknown")
        self.assertEqual(missing.fanout_policy["provider_source"], {})

    def test_worker_reviewer_and_observer_preflight_refuse_before_pane_creation(self) -> None:
        for role in ("worker", "reviewer", "observer"):
            with self.subTest(role=role):
                host = _Host()
                run = self._run(role)
                with self.assertRaises(codex_preflight.CodexFanoutPolicyError):
                    spawn(
                        run.spec,
                        run.workspace,
                        run.task_ref,
                        host=host,
                        command="codex",
                        title=f"{role} head",
                        run=run,
                        role=role,
                        preflight=lambda candidate: codex_preflight.preflight_codex_launch(
                            {}, candidate.workspace, candidate, binary_path=str(self.binary)
                        ),
                    )
                self.assertEqual(host.opened, 0)

    def test_dispatcher_worker_reviewer_and_observer_refuse_before_a_pane(self) -> None:
        class Catalog:
            profile = {"adapter": "codex", "model": "gpt-5.6-terra", "codex_mode": "tui"}

            def head_profile(self, _head: str) -> dict:
                return dict(self.profile)

            def head_launch(self, *_args, **_kwargs) -> HeadCommand:
                return HeadCommand("codex", adapter="codex")

            def observer_run(self, _head: str, *, workspace: str):
                return type("Run", (), {"to_json": lambda _self: {"adapter": "codex"}})()

        catalog = Catalog()
        runtime = CommandHostRuntime(catalog, self.root / "data", mode="noop")  # type: ignore[arg-type]
        opens: list[object] = []
        runtime._create_terminal = lambda *_args: opens.append(_args)  # type: ignore[method-assign]
        task = {"ref": "secretary-1428", "project": "secretary"}
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                with self.assertRaises(HostError):
                    runtime._launch(
                        str(self.workspace), f"{role} title", "codex-extra", "TASK.md",
                        role=role, env_name="SECRETARY_UNSET_COMMAND", task=task,
                    )
        # The observer creates its worktree/prompt before it asks for a terminal.  It still fails
        # at the same shared policy boundary, with no call to the terminal constructor.
        with self.assertRaises(HostError):
            runtime.prepare_observer({"ref": "sprint:1428"}, "codex-extra", prompt="# Sprint")
        self.assertEqual(opens, [])


if __name__ == "__main__":
    unittest.main()
