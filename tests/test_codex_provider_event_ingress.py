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
from secretary.dispatcher import CommandHostRuntime
from secretary.dispatcher_launch import REVIEW_ROLE, WORKER_ROLE, confirm_launch_intent
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_tui import provider_progress_for_run
from secretary.dispatcher_types import HostError
from secretary.dispatcher_observer import ObserverRecord, _bind_codex_provider_ingress
from secretary.dispatcher_worker_lifecycle import WorkerContinuationLiveness
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

    def _attested_run(
        self, *, run_id: str = "run-1", role: str = "worker",
    ) -> tuple[HeadRun, dict[str, object]]:
        run = HeadRun(
            run_id=run_id,
            spec=HeadSpec(profile_id="codex-extra", adapter="codex", model="gpt-5.6-terra"),
            workspace=str(self.workspace),
            task_ref=TaskRef.card("secretary-1428", document=str(self.workspace / "TASK.md")),
            role=role,
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
        return allowed, attestation

    def _run(self) -> HeadRun:
        allowed, _attestation = self._attested_run()
        return allowed.with_fanout_policy({
            **allowed.fanout_policy,
            "provider_source": {
                "version": 1,
                "kind": "codex_session_event_jsonl",
                "state": "unbound",
                **codex_preflight.codex_provider_source_descriptor(allowed),
                "root": str(self.sessions),
                "baseline": [],
            },
        })

    def _preflight_run(self, *, run_id: str = "run-1", role: str = "worker") -> HeadRun:
        """The production preflight path, before the provider creates its new journal."""
        run, attestation = self._attested_run(run_id=run_id, role=role)
        return codex_preflight.preflight_codex_launch(
            {"codex_home": str(self.root)},
            str(self.workspace),
            run,
            schema_attestation=attestation,
            binary_path=str(self.binary),
            config=self.root / "config.toml",
        )

    def _write_source(self, *events: object, source: Path | None = None) -> None:
        records = [
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "thread.started", "thread_id": "parent-1"},
            *events,
        ]
        self._write_records(*records, source=source)

    def _write_records(self, *records: object, source: Path | None = None) -> None:
        target = source or self.source
        target.write_text(
            "\n".join(
                value if isinstance(value, str) else json.dumps(value) for value in records
            ) + "\n",
            encoding="utf-8",
        )

    def _append_records(self, *records: object, source: Path | None = None) -> None:
        target = source or self.source
        body = target.read_text(encoding="utf-8")
        target.write_text(
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

    def test_preflight_descriptor_survives_real_bind_and_admits_new_progress(self) -> None:
        """The launch source reaches liveness through binding without losing its HeadRun fence."""
        preflight = self._preflight_run()
        original = dict(preflight.fanout_policy["provider_source"])
        self._write_source()
        ingress = self._ingress(preflight)

        ingress.bind_before_delivery()

        bound = ingress.run
        source = bound.fanout_policy["provider_source"]
        for field in ("run_id", "head_run_fingerprint", "workspace", "role", "task_ref", "root", "baseline"):
            self.assertEqual(source[field], original[field])
        baseline = provider_progress_for_run(bound)
        self.assertEqual(baseline["state"], "observed")
        self.assertEqual(baseline["admission"], "accepted")
        liveness = WorkerContinuationLiveness.begin(bound.to_json())
        self.assertEqual(liveness.observe_provider(baseline, 10.0, head_run=bound.to_json()), "baseline")

        self._append_records({"type": "turn.completed", "thread_id": "parent-1"})
        ingress.poll()
        progressed_run = ingress.run
        progress = provider_progress_for_run(progressed_run)

        self.assertTrue(progressed_run.same_run(preflight))
        self.assertNotEqual(progress["cursor"], baseline["cursor"])
        self.assertEqual(progress["state"], "observed")
        self.assertEqual(liveness.observe_provider(
            progress, 20.0, head_run=progressed_run.to_json(),
        ), "progressed")
        self.assertEqual(liveness.busy_attempts, 0)

    def test_real_bound_sources_refresh_the_shared_worker_and_reviewer_progress_seam(self) -> None:
        worker_preflight = self._preflight_run()
        self._write_source()
        worker_ingress = self._ingress(worker_preflight)
        worker_ingress.bind_before_delivery()

        review_source = self.source.with_name("review.jsonl")
        review_preflight = self._preflight_run(run_id="review-run", role="reviewer")
        self._write_source(source=review_source)
        review_ingress = self._ingress(review_preflight)
        review_ingress.bind_before_delivery()

        record = DispatcherRecord(
            worker="secretary-1428-worker",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex-extra",
            review_head="codex-extra",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
        )
        record.worker_head_run = worker_ingress.run.to_json()
        record.review_head_run = review_ingress.run.to_json()
        host = object.__new__(CommandHostRuntime)
        task = {"ref": "secretary-1428"}

        worker_baseline = CommandHostRuntime.provider_progress(host, task, record, "worker")
        reviewer_progress = CommandHostRuntime.provider_progress(host, task, record, "review")
        self.assertEqual(worker_baseline["state"], "observed")
        self.assertEqual(worker_baseline["admission"], "accepted")
        self.assertEqual(reviewer_progress["state"], "observed")
        self.assertEqual(reviewer_progress["admission"], "accepted")

        liveness = WorkerContinuationLiveness.begin(record.worker_head_run)
        self.assertEqual(liveness.observe_provider(
            worker_baseline, 10.0, head_run=record.worker_head_run,
        ), "baseline")
        self._append_records({"type": "turn.completed", "thread_id": "parent-1"})
        worker_ingress.poll()
        record.worker_head_run = worker_ingress.run.to_json()
        worker_progress = CommandHostRuntime.provider_progress(host, task, record, "worker")

        self.assertTrue(worker_ingress.run.same_run(worker_preflight))
        self.assertNotEqual(worker_progress["cursor"], worker_baseline["cursor"])
        self.assertEqual(worker_progress["admission"], "accepted")
        self.assertEqual(liveness.observe_provider(
            worker_progress, 20.0, head_run=record.worker_head_run,
        ), "progressed")

    def test_incomplete_or_foreign_bound_descriptor_cannot_admit_progress(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        bound = ingress.run
        source = dict(bound.fanout_policy["provider_source"])
        for field, value in (("run_id", ""), ("workspace", "/foreign")):
            with self.subTest(field=field):
                damaged = bound.with_fanout_policy({
                    **bound.fanout_policy,
                    "provider_source": {**source, field: value},
                })
                progress = provider_progress_for_run(damaged)
                self.assertEqual(progress["state"], "identity_mismatch")
                self.assertNotEqual(progress.get("admission"), "accepted")

    def test_launch_intent_handoff_keeps_the_real_bound_source_for_worker_and_reviewer(self) -> None:
        """The later launcher result may add pane/lifecycle facts but cannot restore `unbound`."""
        class Runtime:
            def save_records(self, _payload, _records) -> None:
                return None

        for intent_role, run_role in ((WORKER_ROLE, "worker"), (REVIEW_ROLE, "reviewer")):
            with self.subTest(role=intent_role):
                self.source.unlink(missing_ok=True)
                preflight = self._preflight_run(run_id=f"{intent_role}-run", role=run_role)
                self._write_source()
                ingress = self._ingress(preflight)
                ingress.bind_before_delivery()
                bound = ingress.run
                stale = preflight.rebound(f"term-{intent_role}", leaf=f"leaf-{intent_role}").working()
                record = DispatcherRecord(
                    worker="worker-1",
                    workspace=str(self.workspace),
                    handle="",
                    head="codex-extra",
                    review_head="codex-extra",
                    attempt_id="attempt-1",
                    comment_baseline=0,
                    review_baseline=0,
                    state="claimed",
                    claimed_at=0.0,
                )
                record.launch_intent = {"role": intent_role, "head_run": bound.to_json()}
                if intent_role == REVIEW_ROLE:
                    record.review_head_run = bound.to_json()
                else:
                    record.worker_head_run = bound.to_json()
                records = {"secretary-1428": record}

                confirm_launch_intent(
                    Runtime(), {}, records, "secretary-1428", record,
                    handle=stale.handle, leaf=stale.leaf, head_run=stale.to_json(),
                )

                stored = record.review_head_run if intent_role == REVIEW_ROLE else record.worker_head_run
                source = stored["fanout_policy"]["provider_source"]
                self.assertEqual(source["state"], "bound")
                self.assertEqual(source["session_id"], "session-1")
                self.assertEqual(stored["lifecycle"], "working")
                self.assertEqual(stored["handle"], stale.handle)
                self.assertEqual(record.launch_intent["head_run"], stored)

    def test_launch_intent_handoff_refuses_a_conflicting_bound_source(self) -> None:
        class Runtime:
            def save_records(self, _payload, _records) -> None:
                return None

        preflight = self._preflight_run()
        self._write_source()
        ingress = self._ingress(preflight)
        ingress.bind_before_delivery()
        bound = ingress.run
        conflicting_source = {
            **bound.fanout_policy["provider_source"], "session_id": "foreign-session",
        }
        conflicting = bound.with_fanout_policy({
            **bound.fanout_policy, "provider_source": conflicting_source,
        })
        record = DispatcherRecord(
            worker="worker-1", workspace=str(self.workspace), handle="", head="codex-extra",
            review_head="codex-extra", attempt_id="attempt-1", comment_baseline=0,
            review_baseline=0, state="claimed", claimed_at=0.0,
        )
        record.launch_intent = {"role": WORKER_ROLE, "head_run": bound.to_json()}
        record.worker_head_run = bound.to_json()
        before = record.launch_intent["head_run"]

        with self.assertRaisesRegex(HostError, "bound provider sources conflict"):
            confirm_launch_intent(
                Runtime(), {}, {"secretary-1428": record}, "secretary-1428", record,
                head_run=conflicting.to_json(),
            )

        self.assertEqual(record.launch_intent["head_run"], before)

    def test_observer_writer_keeps_the_bound_delivery_handoff_over_a_stale_launch_copy(self) -> None:
        class State:
            def save(self, _payload) -> None:
                return None

        class Host:
            def configure_codex_provider_ingress(self, _run, *, persist, stop, block) -> None:
                self.persist = persist

        class Runtime:
            def __init__(self) -> None:
                self.host = Host()
                self.production_state = State()

        self.source.unlink(missing_ok=True)
        preflight = self._preflight_run(run_id="observer-run", role="observer")
        self._write_source()
        ingress = self._ingress(preflight)
        ingress.bind_before_delivery()
        bound = ingress.run
        stale = preflight.rebound("term-observer", leaf="leaf-observer")
        record = ObserverRecord(
            sprint="sprint:1", workspace=str(self.workspace), head="codex-extra",
            head_run=bound.to_json(),
        )
        runtime = Runtime()
        observers = {"sprint:1": record}

        _bind_codex_provider_ingress(runtime, {}, observers, "sprint:1", record)
        runtime.host.persist(stale)

        source = record.head_run["fanout_policy"]["provider_source"]
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(record.handle, "term-observer")
        self.assertEqual(observers["sprint:1"].head_run, record.head_run)

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
