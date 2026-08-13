"""Contract coverage for the launch-bound Codex provider-event journal ingress."""

from __future__ import annotations

import json
import hashlib
import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import dispatcher_launch, dispatcher_observer, dispatcher_review
from secretary.codex_provider_events import (
    CodexProviderEventIngress,
)
from secretary.dispatcher import CommandHostRuntime, DispatcherRuntime
from secretary.dispatcher_launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
    confirm_launch_intent,
    resolve_launch_intent,
    write_launch_intent,
)
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_tui import provider_progress_for_run
from secretary.dispatcher_types import HostError
from secretary.dispatcher_observer import (
    OBSERVER_ROLE,
    ObserverRecord,
    _adopt_launch_intent as adopt_observer_launch_intent,
    _bind_codex_provider_ingress,
    _write_launch_intent as write_observer_launch_intent,
)
from secretary.head_health import HeadReadiness
from secretary.tasks import TaskAudit
from secretary.dispatcher_worker_lifecycle import WorkerContinuationLiveness
from triggered_agents.runtime import codex_preflight
from triggered_agents.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef

from tests.test_head_operations import FakeSessionHost


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

    def test_binds_codex_0147_session_meta_without_thread_started(self) -> None:
        """The live TUI journal uses session_meta as its root identity anchor."""
        self._write_records(
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "cwd": str(self.workspace)},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "thread_id": "session-1"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "thread_id": "session-1"},
            },
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        source = self.written[-1].fanout_policy["provider_source"]
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(source["parent_thread_id"], "session-1")
        self.assertEqual(source["initial_range"]["root"]["line"], 1)
        self.assertEqual(source["cursor"]["line"], 3)
        recovered = self._ingress(self.written[-1])
        recovered.poll()
        self.assertEqual(recovered.run.fanout_policy["provider_source"]["state"], "bound")

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

    def test_collaboration_call_is_persisted_as_advisory_telemetry(self) -> None:
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
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

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

        ingress.poll()

        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "collaboration_call")
        self.assertEqual(event["parent_thread_id"], "parent-1")
        self.assertEqual(event["tool_name"], "wait")
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

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

                ingress.poll()

                self.assertEqual(self.written[-1].fanout_policy["events"][-1]["type"], expected_type)
                self.assertEqual(self.written[-1].fanout_policy["terminal_state"], expected_state)
                self.assertEqual(self.stops, [])
                self.assertEqual(self.blocks, [])

    def test_recovery_cursor_mismatch_is_non_fatal_diagnostic(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        # The durable parent cursor no longer names the same raw line.  It is not a reason to
        # redirect the record to some later session or regenerate an identity.
        self.source.write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}})
            + "\n" + json.dumps({"type": "thread.started", "thread_id": "changed-parent"}) + "\n",
            encoding="utf-8",
        )

        ingress.poll()

        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])
        self.assertEqual(ingress.run.fanout_policy["terminal_state"], "unknown")

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

    def test_malformed_post_root_prebind_line_is_advisory_telemetry(self) -> None:
        self._write_source("{not-json")
        ingress = self._ingress()

        ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 3)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_tui_policy_event_in_post_root_prebind_tail_is_advisory(self) -> None:
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

        ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "child_thread_edge")
        self.assertEqual(event["child_thread_id"], "child-1")
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_unrecognised_collaboration_shape_is_unknown_not_a_clean_cursor_advance(self) -> None:
        self._write_source({
            "type": "event_msg",
            "payload": {
                "type": "item_completed", "thread_id": "parent-1",
                "item": {"type": "CollabAgentStatus", "sender_thread_id": "parent-1"},
            },
        })
        ingress = self._ingress()

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

        ingress.bind_before_delivery()

        selected = self.written[0].fanout_policy["provider_source"]
        self.assertEqual(selected["cursor"]["line"], 0)
        self.assertEqual(selected["initial_range"]["first"]["line"], 1)
        self.assertEqual(selected["initial_range"]["root"]["line"], 3)
        self.assertEqual(selected["initial_range"]["last"]["line"], 3)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 2)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

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

    def test_policy_pre_root_record_is_telemetry_after_source_selection(self) -> None:
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

        ingress.bind_before_delivery()

        selected = self.written[0].fanout_policy["provider_source"]
        self.assertEqual(selected["initial_range"]["root"]["line"], 3)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "child_thread_edge")
        self.assertEqual(event["source_sequence"], 2)
        self.assertEqual(event["child_thread_id"], "child-1")
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

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
        recovered.poll()

        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 5)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

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

        recovered = self._ingress(persisted)
        recovered.poll()

        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])
        self.assertEqual(recovered.run.fanout_policy["terminal_state"], "unknown")

    def test_prebind_cursor_persistence_failure_is_non_fatal(self) -> None:
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

        ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["state"], "bound")
        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_failed_durable_event_write_is_non_fatal(self) -> None:
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
        ingress.poll()

        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])
        self.assertEqual(ingress.run.fanout_policy["events"], [])


class ProductionPostDeliveryHandoffContractTests(unittest.TestCase):
    """Drive the dispatcher routes that own the post-delivery write, not their merge helpers."""

    class WorkingSession(FakeSessionHost):
        def __init__(self, on_ready) -> None:
            super().__init__()
            self.on_ready = on_ready

        def wait_idle(self, handle: str, *, timeout_ms: int):
            self.on_ready()
            return super().wait_idle(handle, timeout_ms=timeout_ms)

        def read(self, handle: str, *, limit: int | None = None):
            return {"terminal": {"tail": ["working", "› "], "nextCursor": "turn-started"}}

    class Catalog:
        def __init__(self, fixture: "ProductionPostDeliveryHandoffContractTests") -> None:
            self.fixture = fixture

        def head_profile(self, _head: str) -> dict[str, str]:
            return {
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "medium",
                "codex_mode": "tui",
                "codex_home": str(self.fixture.root),
            }

        def head_launch(self, _head: str, _prompt_file: str, **_kwargs) -> HeadCommand:
            return HeadCommand("run-codex", prompt_after_start=True, adapter="codex")

        def default_branch(self, _project: str, override: str | None) -> str:
            return override or "main"

        def head_run(self, task: dict, *, role: str, head: str, workspace: str, **_kwargs) -> HeadRun:
            return HeadRun(
                run_id=f"routing-{role}",
                spec=HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra"),
                workspace=workspace,
                task_ref=TaskRef.card(str(task["ref"])),
                role=role,
            )

        def observer_run(self, head: str, *, workspace: str):
            return HeadRun(
                run_id="observer-routing",
                spec=HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra"),
                workspace=workspace,
                task_ref=TaskRef.sprint("sprint:contract"),
                role=OBSERVER_ROLE,
            )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\nprintf 'codex 9.9.9\\n'\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.sessions = self.root / "sessions"
        self.source = self.sessions / "2026" / "08" / "13" / "run.jsonl"
        self.source.parent.mkdir(parents=True)
        self.snapshots: list[dict[str, dict]] = []
        self.moves: list[dict] = []
        self.source_emitted = False
        self.source_events: list[object] = []
        self.session = self.WorkingSession(self._emit_source)
        self.host = CommandHostRuntime(self.Catalog(self), self.root / "data", mode="real")  # type: ignore[arg-type]
        self.host.preflight_codex_run = self._real_preflight  # type: ignore[method-assign]
        self.host._run = self._run  # type: ignore[method-assign]
        self.host._run_json = self._run_json  # type: ignore[method-assign]
        self.session_patch = mock.patch.object(
            CommandHostRuntime, "session", property(lambda _host: self.session)
        )
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
                "SECRETARY_DISPATCHER_PROMPT_DIR": str(self.root / "prompts"),
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, args: list[str], _label: str, **_kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout="contract-sha\n", stderr="")

    def _run_json(self, args: list[str]) -> dict:
        if args[:3] == ["orca", "terminal", "wait"]:
            self._emit_source()
            return {"wait": {"satisfied": True}}
        if args[:3] == ["orca", "terminal", "read"]:
            return {"terminal": {"tail": ["working", "› "], "nextCursor": "turn-started"}}
        if args[:3] == ["orca", "terminal", "send"]:
            return {"accepted": True, "bytesWritten": len(str(args[args.index("--text") + 1]).encode())}
        if args[:3] == ["orca", "worktree", "show"]:
            raise HostError("selector_not_found")
        if args[:3] == ["orca", "worktree", "create"]:
            workspace = self.host.observer_workspace("sprint:contract")
            Path(workspace).mkdir(parents=True, exist_ok=True)
            return {"worktree": {"path": workspace}}
        return {}

    def _real_preflight(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: TaskRef,
        pid_file: str,
        run_id: str,
    ) -> HeadRun:
        run = HeadRun(
            run_id=run_id,
            spec=HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra"),
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            pid_file=pid_file,
        )
        # The production path has no independently captured provider schema.  Advisory fan-out
        # telemetry must therefore carry ``schema_absent`` through all three real launch routes.
        return codex_preflight.preflight_codex_launch(
            {"codex_home": str(self.root)},
            workspace,
            run,
            binary_path=str(self.binary),
            config=self.root / "config.toml",
        )

    def _emit_source(self) -> None:
        if self.source_emitted:
            return
        self.source_emitted = True
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": self.source_workspace}},
            {"type": "thread.started", "thread_id": "parent-1"},
            *self.source_events,
        )

    @staticmethod
    def _fail_recorder(recorder: codex_preflight.CodexProviderEventRecorder, *_args, **_kwargs):
        raise codex_preflight.CodexFanoutRecordingError(
            "recorder storage unavailable", run=recorder.run, event={},
        )

    def _add_recording_failure_event(self) -> None:
        self.source_events = [{
            "type": "event_msg",
            "payload": {
                "type": "collab_tool_call",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "sender_thread_id": "parent-1",
                    "receiver_thread_ids": [],
                },
            },
        }]

    def _write_records(self, *records: object) -> None:
        self.source.write_text(
            "\n".join(
                value if isinstance(value, str) else json.dumps(value) for value in records
            ) + "\n",
            encoding="utf-8",
        )

    def _append_records(self, *records: object) -> None:
        self.source.write_text(
            self.source.read_text(encoding="utf-8") + "\n".join(
                value if isinstance(value, str) else json.dumps(value) for value in records
            ) + "\n",
            encoding="utf-8",
        )

    @property
    def source_workspace(self) -> str:
        return str(getattr(self, "_source_workspace", self.workspace))

    def _arm_source(self, workspace: str) -> None:
        self._source_workspace = workspace
        self.source_emitted = False
        self.source.unlink(missing_ok=True)

    def _runtime(self):
        runtime = object.__new__(DispatcherRuntime)
        runtime.host = self.host
        runtime.owner = "contract-dispatcher"
        runtime.writer = SimpleNamespace(move=lambda **kwargs: self.moves.append(dict(kwargs)))
        runtime.head_readiness = lambda _head: HeadReadiness("test", "ready", "", 0.0)
        runtime.record_worker_routing = lambda *_args: None
        runtime.record_review_routing = lambda *_args: None
        runtime.open_worker_round = lambda *_args, **_kwargs: None

        def save(_payload: dict, records: dict[str, DispatcherRecord]) -> None:
            self.snapshots.append({ref: copy.deepcopy(record.to_json()) for ref, record in records.items()})

        runtime.save_records = save
        return runtime

    @staticmethod
    def _record(workspace: str) -> DispatcherRecord:
        return DispatcherRecord(
            worker="contract-worker",
            workspace=workspace,
            handle="",
            head="codex-contract",
            review_head="codex-contract",
            attempt_id="attempt-contract",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

    def _assert_bound(self, run: dict, *, role: str) -> None:
        self.assertEqual(run["fanout_policy"]["state"], codex_preflight.FANOUT_SCHEMA_ABSENT)
        source = run["fanout_policy"]["provider_source"]
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(source["role"], role)
        self.assertEqual(source["path"], str(self.source))
        self.assertEqual(source["run_id"], run["run_id"])
        self.assertEqual(source["task_ref"], run["task_ref"])

    def test_worker_route_persists_the_callback_run_before_confirm_then_adopts_and_refreshes(self) -> None:
        runtime = self._runtime()
        task = {"ref": "secretary-1428", "project": "secretary", "workspace": {"base_branch": "main"}}
        record = self._record(str(self.workspace))
        records = {task["ref"]: record}
        payload: dict = {}
        self._arm_source(record.workspace)
        self._add_recording_failure_event()

        self.assertIsNone(write_launch_intent(
            runtime, payload, records, task["ref"], record,
            role=WORKER_ROLE, action="rework", head=record.head, workspace=record.workspace,
        ))
        with mock.patch.object(
            codex_preflight.CodexProviderEventRecorder, "record", new=self._fail_recorder,
        ):
            launched, failure = runtime._bring_up_worker_head(
                task, record, records, payload, record.attempt_id,
                step="advance", blocked_reason="contract", blocked_request_id="contract-worker",
            )

        self.assertIsNone(failure)
        assert launched is not None
        self._assert_bound(record.worker_head_run, role="worker")
        self.assertEqual(record.worker_head_run["fanout_policy"]["events"], [])
        self.assertEqual(record.worker_head_run, record.launch_intent["head_run"])
        callback_snapshot = next(
            snapshot[task["ref"]]
            for snapshot in self.snapshots
            if snapshot[task["ref"]]["worker_head_run"].get("fanout_policy", {}).get("provider_source", {}).get("state") == "bound"
            and not snapshot[task["ref"]]["launch_intent"].get("launched")
        )
        self.assertEqual(callback_snapshot["launch_intent"]["head_run"], callback_snapshot["worker_head_run"])

        # A competing, incomplete, or foreign writer reaches the configured production ingress and
        # is fenced without changing the retained descriptor.
        ingress = self.host._codex_provider_ingress(HeadRun.from_json(record.worker_head_run))
        assert ingress is not None
        before = copy.deepcopy(record.worker_head_run)
        source = dict(before["fanout_policy"]["provider_source"])
        foreign = HeadRun.from_json(before).with_fanout_policy({
            **before["fanout_policy"], "provider_source": {**source, "session_id": "foreign-session"},
        })
        with self.assertRaisesRegex(HostError, "bound provider sources conflict"):
            ingress.commit_run(foreign)
        self.assertEqual(record.worker_head_run, before)
        incomplete = HeadRun.from_json(before).with_fanout_policy({
            **before["fanout_policy"],
            "provider_source": {key: value for key, value in source.items() if key != "session_id"},
        })
        ingress.commit_run(incomplete)
        self.assertEqual(record.worker_head_run, before, "an incomplete writer cannot clobber a binding")
        record.worker_head_run = incomplete.to_json()
        self.assertIn(
            self.host.provider_progress(task, record, "worker")["state"],
            {"unavailable", "identity_mismatch"},
        )
        record.worker_head_run = before

        baseline = self.host.provider_progress(task, record, "worker")
        self._append_records({"type": "turn.completed", "thread_id": "parent-1"})
        self.assertIsNone(runtime.poll_codex_provider_ingress(
            record, records, payload, reference=task["ref"],
        ))
        progress = self.host.provider_progress(task, record, "worker")
        self.assertEqual(progress["admission"], "accepted")
        self.assertNotEqual(progress["cursor"], baseline["cursor"])
        with mock.patch.object(
            dispatcher_review, "_head_run_process_status",
            return_value={"known": True, "match": True, "state": "live-match", "stopped": False},
        ):
            status = dispatcher_review.command_terminal_status(self.host, task, record, kind="worker")
        self.assertEqual(status["provider_progress"]["cursor"], progress["cursor"])
        self.assertGreater(status["last_activity"], 0.0)
        self.assertEqual(self.moves, [])
        self.assertEqual(len(self.session.sent), 2, "progress did not nudge or replace the worker")

        # This is the crash boundary after confirmation: adoption reads the exact source already
        # committed to the launch intent rather than reconstructing an unbound local copy.
        with mock.patch.object(
            dispatcher_launch, "head_process_status", return_value={"known": True, "match": True},
        ):
            adopted = resolve_launch_intent(runtime, task, records, payload)
        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self._assert_bound(record.worker_head_run, role="worker")

    def test_reviewer_route_uses_the_external_document_identity_and_adopts_the_bound_run(self) -> None:
        runtime = self._runtime()
        task = {"ref": "secretary-1428", "project": "secretary", "workspace": {"base_branch": "main"}}
        record = self._record(str(self.workspace))
        record.state = "review_starting"
        records = {task["ref"]: record}
        payload: dict = {}
        self._arm_source(record.workspace)
        self._add_recording_failure_event()
        calls = [0]

        def crash_after_confirm(*_args) -> None:
            calls[0] += 1
            if calls[0] == 1:
                raise OSError("dispatcher crashed after reviewer confirmation")

        runtime.record_review_routing = crash_after_confirm
        with mock.patch.object(
            codex_preflight.CodexProviderEventRecorder, "record", new=self._fail_recorder,
        ):
            with self.assertRaisesRegex(OSError, "crashed after reviewer confirmation"):
                dispatcher_review.start_review(
                    runtime, task, records, record, record.attempt_id,
                    action="review-started", payload=payload,
                )

        self._assert_bound(record.review_head_run, role="reviewer")
        self.assertEqual(record.review_head_run["fanout_policy"]["events"], [])
        self.assertEqual(record.review_head_run, record.launch_intent["head_run"])
        self.assertEqual(
            record.review_head_run["task_ref"]["document"],
            str(self.host._prompt_document_path(REVIEW_ROLE, task["ref"], record.review_baseline)),
        )
        with mock.patch.object(
            dispatcher_launch, "head_process_status", return_value={"known": True, "match": True},
        ):
            adopted = resolve_launch_intent(runtime, task, records, payload)
        self.assertEqual(adopted["action"], "review-launch-adopted")
        self._assert_bound(record.review_head_run, role="reviewer")

    def test_observer_prepare_callback_persists_the_bound_run_for_watchdog_adoption(self) -> None:
        runtime = SimpleNamespace()
        runtime.host = self.host
        runtime.owner = "contract-dispatcher"
        runtime.production_state = SimpleNamespace(save=lambda _payload: None)
        observers: dict[str, ObserverRecord] = {}
        payload: dict = {}
        record = ObserverRecord(sprint="sprint:contract")

        self.assertIsNone(write_observer_launch_intent(
            runtime, payload, observers, record.sprint, record, "codex-contract", 1,
        ))
        self._arm_source(record.workspace)
        self._add_recording_failure_event()
        _bind_codex_provider_ingress(runtime, payload, observers, record.sprint, record)
        with mock.patch.object(
            codex_preflight.CodexProviderEventRecorder, "record", new=self._fail_recorder,
        ):
            launched = self.host.prepare_observer(
                {"ref": record.sprint}, "codex-contract", prompt="# Sprint\n",
                heartbeat_run_id=str(record.head_run["run_id"]),
            )

        self.assertEqual(record.head_run, launched["head_run"])
        self._assert_bound(record.head_run, role=OBSERVER_ROLE)
        self.assertEqual(record.head_run["fanout_policy"]["events"], [])
        self.assertEqual(record.pending_launch, 1, "the watchdog sees the crash-era intent")
        with mock.patch.object(
            dispatcher_observer, "observer_alive", return_value={"alive": True, "pid_known": True},
        ):
            adopted = adopt_observer_launch_intent(
                runtime, payload, observers, record.sprint, record,
            )
        self.assertEqual(adopted["action"], "observer-adopted")
        self._assert_bound(observers[record.sprint].head_run, role=OBSERVER_ROLE)
        self.assertEqual(len(self.session.sent), 0, "watchdog adoption did not redeliver or replace")


if __name__ == "__main__":
    unittest.main()
