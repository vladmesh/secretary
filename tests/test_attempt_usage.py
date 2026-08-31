"""What one finished phase cost, and what recording it must never do to the card.

Three contracts live here. The provider aggregations, because "how many tokens" is the one thing a
reader cannot recompute later from the journal and a wrong rule would be invisible. The typed
event, because the point of the record is that nobody has to reopen a session file to read it. And
the dispatcher seam, because the accounting runs on the path that accepts a worker report or a
reviewer verdict and must not be able to change what that path does.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from secretary.board.models import (
    TOKEN_DIMENSIONS,
    Actor,
    AttemptUsageOutcome,
    EntityKind,
    Event,
    EventKind,
)
from secretary.dispatch.attempt_usage import (
    CLAUDE_SOURCE_KIND,
    CODEX_SOURCE_KIND,
    claude_usage,
    codex_usage,
    collect_usage,
    provider_usage_source,
)
from secretary.tasks import is_significant_card_event
from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture

DIGEST = "a" * 64


def codex_token_count(
    *,
    input_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_output_tokens: int | None = None,
) -> dict:
    """One Codex ``token_count`` event carrying the session's running total."""
    total = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
    }
    return {
        "timestamp": "2026-08-31T12:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {name: value for name, value in total.items() if value is not None},
                "model_context_window": 272000,
            },
        },
    }


def claude_assistant(message_id: str, **usage: int) -> dict:
    """One Claude assistant record carrying that message's usage object."""
    return {
        "type": "assistant",
        "uuid": f"record-{message_id}",
        "message": {"id": message_id, "role": "assistant", "usage": dict(usage)},
    }


def bound_codex_source(path: Path, *, session_id: str = "codex-session-1") -> dict:
    """A completely bound Codex source, in the shape the runtime persists and re-validates."""
    return {
        "version": 1,
        "kind": CODEX_SOURCE_KIND,
        "state": "bound",
        "root": str(path.parent),
        "path": str(path),
        "session_id": session_id,
        "parent_thread_id": "thread-1",
        "cursor": {"line": 0, "digest": DIGEST},
        "initial_range": {
            "first": {"line": 1, "digest": DIGEST},
            "root": {"line": 1, "digest": DIGEST},
            "last": {"line": 2, "digest": DIGEST},
            "digest": DIGEST,
        },
        "bound_at": "2026-08-31T12:00:00Z",
    }


def bound_claude_source(path: Path, *, session_id: str = "claude-session-1") -> dict:
    return {
        "version": 1,
        "kind": CLAUDE_SOURCE_KIND,
        "state": "bound",
        "root": str(path.parent),
        "path": str(path),
        "session_id": session_id,
    }


def write_jsonl(path: Path, records: list, *, tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(record) + "\n" for record in records) + tail
    path.write_text(body, encoding="utf-8")
    return path


class CodexAggregationTests(unittest.TestCase):
    """Codex reports a running total, so the phase total is the last one and never their sum."""

    def test_the_last_cumulative_snapshot_is_the_whole_phase(self) -> None:
        totals, counted = codex_usage(
            [
                codex_token_count(input_tokens=100, output_tokens=10, reasoning_output_tokens=4),
                codex_token_count(input_tokens=450, output_tokens=61, reasoning_output_tokens=25),
            ]
        )

        self.assertEqual(counted, 2)
        self.assertEqual(totals.input, 450)
        self.assertEqual(totals.output, 61)
        self.assertEqual(totals.reasoning, 25)

    def test_a_repeated_snapshot_names_the_same_total_rather_than_adding_to_it(self) -> None:
        """A re-read or replayed journal reports what the phase cost, not a multiple of it."""
        snapshot = codex_token_count(input_tokens=450, output_tokens=61)

        once, _ = codex_usage([snapshot])
        again, counted = codex_usage([snapshot, snapshot, snapshot])

        self.assertEqual(once, again)
        self.assertEqual(counted, 3, "the read is still described honestly")

    def test_the_cached_share_of_input_is_read_and_cache_writes_stay_unavailable(self) -> None:
        totals, _ = codex_usage(
            [codex_token_count(input_tokens=900, cached_input_tokens=768, output_tokens=12)]
        )

        self.assertEqual(totals.cache_read_input, 768)
        self.assertIsNone(totals.cache_input, "Codex reports no separate cache write to record")

    def test_a_dimension_the_snapshot_omits_is_unavailable_rather_than_zero(self) -> None:
        totals, _ = codex_usage([codex_token_count(input_tokens=10, output_tokens=2)])

        self.assertIsNone(totals.reasoning)
        self.assertIsNone(totals.cache_read_input)
        self.assertEqual(totals.input, 10)

    def test_a_malformed_snapshot_is_not_a_usage_record(self) -> None:
        totals, counted = codex_usage(
            [
                {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                "not an object",
            ]
        )

        self.assertEqual(counted, 0)
        self.assertTrue(totals.empty)


class ClaudeAggregationTests(unittest.TestCase):
    """Claude reports per message, so the phase total is a sum over distinct messages."""

    def test_usage_objects_sum_across_the_messages_of_one_phase(self) -> None:
        totals, counted = claude_usage(
            [
                claude_assistant(
                    "msg_1",
                    input_tokens=4,
                    cache_creation_input_tokens=1200,
                    cache_read_input_tokens=0,
                    output_tokens=90,
                ),
                claude_assistant(
                    "msg_2",
                    input_tokens=6,
                    cache_creation_input_tokens=300,
                    cache_read_input_tokens=1200,
                    output_tokens=45,
                ),
            ]
        )

        self.assertEqual(counted, 2)
        self.assertEqual(totals.input, 10)
        self.assertEqual(totals.cache_input, 1500)
        self.assertEqual(totals.cache_read_input, 1200)
        self.assertEqual(totals.output, 135)
        self.assertIsNone(totals.reasoning, "Claude publishes no separate reasoning dimension")

    def test_one_message_counts_once_however_many_times_it_was_written(self) -> None:
        """A streamed message is written repeatedly and a resumed session repeats earlier ones."""
        partial = claude_assistant("msg_1", input_tokens=4, output_tokens=1)
        final = claude_assistant("msg_1", input_tokens=4, output_tokens=90)

        totals, counted = claude_usage([partial, final, final])

        self.assertEqual(counted, 1)
        self.assertEqual(totals.output, 90, "the finished record of the message, not the partial")
        self.assertEqual(totals.input, 4)

    def test_a_zero_a_provider_reports_is_kept_and_a_field_it_omits_is_not(self) -> None:
        totals, _ = claude_usage(
            [
                claude_assistant("msg_1", input_tokens=0, output_tokens=90),
                claude_assistant("msg_2", input_tokens=5, output_tokens=10),
            ]
        )

        self.assertEqual(totals.input, 5)
        self.assertEqual(totals.output, 100)
        self.assertIsNone(totals.cache_input)
        self.assertIsNone(totals.cache_read_input)

    def test_records_that_are_not_assistant_usage_are_not_counted(self) -> None:
        totals, counted = claude_usage(
            [
                {"type": "user", "message": {"role": "user", "content": "go"}},
                {"type": "assistant", "message": {"id": "msg_1"}},
                {"type": "summary"},
            ]
        )

        self.assertEqual(counted, 0)
        self.assertTrue(totals.empty)


class SourceCollectionTests(unittest.TestCase):
    """Every way a read can fail has a name, and none of them is a zero-cost phase."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def test_an_adapter_with_no_structured_records_says_so(self) -> None:
        result = collect_usage(adapter="shell", source={})

        self.assertIs(result.outcome, AttemptUsageOutcome.ADAPTER_UNSUPPORTED)
        self.assertTrue(result.tokens.empty)

    def test_a_source_that_was_never_bound_to_this_run_is_named_unavailable(self) -> None:
        result = collect_usage(
            adapter="codex", source={"version": 1, "kind": CODEX_SOURCE_KIND, "state": "unbound"}
        )

        self.assertIs(result.outcome, AttemptUsageOutcome.SOURCE_UNAVAILABLE)
        self.assertIn("unbound", result.detail)

    def test_a_bound_source_without_a_session_id_is_a_missing_session_identity(self) -> None:
        source = bound_codex_source(self.root / "rollout.jsonl")
        source["session_id"] = ""

        result = collect_usage(adapter="codex", source=source)

        self.assertIs(result.outcome, AttemptUsageOutcome.SESSION_UNAVAILABLE)

    def test_a_journal_that_cannot_be_read_is_a_read_failure_and_not_an_empty_phase(self) -> None:
        result = collect_usage(adapter="claude", source=bound_claude_source(self.root / "missing.jsonl"))

        self.assertIs(result.outcome, AttemptUsageOutcome.SOURCE_UNREADABLE)
        self.assertTrue(result.tokens.empty)

    def test_a_journal_nothing_parses_out_of_is_malformed(self) -> None:
        path = self.root / "broken.jsonl"
        path.write_text("{not json\n{also not json\n", encoding="utf-8")

        result = collect_usage(adapter="claude", source=bound_claude_source(path))

        self.assertIs(result.outcome, AttemptUsageOutcome.RECORDS_MALFORMED)
        self.assertEqual(result.skipped_records, 2)

    def test_a_readable_journal_with_no_usage_record_says_the_usage_is_absent(self) -> None:
        path = write_jsonl(self.root / "quiet.jsonl", [{"type": "user", "message": {}}])

        result = collect_usage(adapter="claude", source=bound_claude_source(path))

        self.assertIs(result.outcome, AttemptUsageOutcome.USAGE_ABSENT)

    def test_a_truncated_tail_still_reports_the_complete_records_before_it(self) -> None:
        """The normal shape of a journal read while its writer is still around."""
        path = write_jsonl(
            self.root / "rollout.jsonl",
            [codex_token_count(input_tokens=120, output_tokens=8)],
            tail='{"type": "event_msg", "payl',
        )

        result = collect_usage(adapter="codex", source=bound_codex_source(path))

        self.assertIs(result.outcome, AttemptUsageOutcome.COLLECTED)
        self.assertEqual(result.tokens.input, 120)
        self.assertEqual(result.skipped_records, 1)

    def test_a_bound_claude_journal_is_read_through_the_progress_source(self) -> None:
        path = write_jsonl(
            self.root / "session.jsonl",
            [claude_assistant("msg_1", input_tokens=3, output_tokens=77)],
        )
        run = {"fanout_policy": {"provider_progress_source": bound_claude_source(path)}}

        result = collect_usage(adapter="claude", source=provider_usage_source(run, adapter="claude"))

        self.assertIs(result.outcome, AttemptUsageOutcome.COLLECTED)
        self.assertEqual(result.tokens.output, 77)
        self.assertEqual(result.records, 1)

    def test_a_run_that_holds_no_policy_at_all_yields_no_source(self) -> None:
        self.assertEqual(provider_usage_source(None, adapter="codex"), {})
        self.assertEqual(provider_usage_source({"fanout_policy": []}, adapter="codex"), {})


def usage_event(**overrides) -> Event:
    data = {
        "attempt": 2,
        "attempt_id": "attempt-1",
        "phase": "worker",
        "role": "worker",
        "report_generation": 2,
        "head": "codex",
        "adapter": "codex",
        "model": "gpt-5.6-terra",
        "model_source": "profile",
        "session_id": "codex-session-1",
        "session_id_reason": "",
        "launch_id": "run-1",
        "outcome": "collected",
        "detail": "",
        "source_kind": CODEX_SOURCE_KIND,
        "records": 3,
        "skipped_records": 0,
        "tokens": dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 100, "output": 20},
    }
    data.update(overrides)
    return Event(
        event_id="evt_usage",
        kind=EventKind.ATTEMPT_USAGE,
        entity_kind=EntityKind.CARD,
        ref=CARD_REF,
        actor=Actor("dispatcher", "secretary-dispatcher"),
        reason="attempt.usage: worker phase of attempt 2",
        occurred_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        data=data,
    )


class AttemptUsageEventTests(unittest.TestCase):
    """The typed boundary, because the whole point is a record nobody has to re-derive."""

    def test_a_complete_occurrence_round_trips_through_the_journal_record(self) -> None:
        event = usage_event()

        restored = Event.from_record(event.to_record("request-1"))

        self.assertEqual(restored, event)
        self.assertEqual(restored.kind, EventKind.ATTEMPT_USAGE)
        self.assertEqual(restored.data["tokens"]["input"], 100)

    def test_a_degraded_outcome_may_not_carry_token_totals(self) -> None:
        """Fabricating a zero for an unreadable phase is exactly the confusion this event exists
        to remove."""
        with self.assertRaises(ValueError):
            usage_event(
                outcome="source_unreadable",
                tokens=dict.fromkeys(TOKEN_DIMENSIONS, 0),
            )

    def test_a_collected_outcome_must_report_at_least_one_dimension(self) -> None:
        with self.assertRaises(ValueError):
            usage_event(tokens=dict.fromkeys(TOKEN_DIMENSIONS, None))

    def test_the_declared_dimensions_are_the_whole_token_object(self) -> None:
        with self.assertRaises(ValueError):
            usage_event(tokens={"input": 5})
        with self.assertRaises(ValueError):
            usage_event(tokens=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": 5, "extra": 1})

    def test_a_count_is_a_non_negative_integer_or_nothing(self) -> None:
        for value in (-1, "5", True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                usage_event(tokens=dict.fromkeys(TOKEN_DIMENSIONS, None) | {"input": value})

    def test_an_absent_session_id_has_to_say_why_it_is_absent(self) -> None:
        with self.assertRaises(ValueError):
            usage_event(session_id=None, session_id_reason="")
        typed_absence = usage_event(session_id=None, session_id_reason="the pane wrote no session")
        self.assertIsNone(typed_absence.data["session_id"])

    def test_the_occurrence_names_its_round_role_and_head_configuration(self) -> None:
        for field, value in (
            ("attempt", 0),
            ("report_generation", 0),
            ("attempt_id", ""),
            ("adapter", ""),
            ("role", "observer"),
            ("phase", "verdict"),
            ("model_source", ""),
            ("outcome", "probably_fine"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                usage_event(**{field: value})

    def test_the_other_event_kinds_are_untouched_by_the_new_validation(self) -> None:
        """Historical records must keep reading exactly as they did."""
        moved = Event(
            event_id="evt_moved",
            kind=EventKind.CARD_MOVED,
            entity_kind=EntityKind.CARD,
            ref=CARD_REF,
            actor=Actor("dispatcher", "secretary-dispatcher"),
            reason="worker report:done",
            occurred_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
            source_state="in_progress",
            target_state="validate",
        )

        self.assertEqual(Event.from_record(moved.to_record("request-2")), moved)


class DispatcherAttemptUsageTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The seam: one occurrence per finished phase, bound to the run that earned it, deciding
    nothing about the card it is recorded against."""

    def usage_events(self) -> list[dict]:
        return [
            event["data"]
            for event in self.writer.audit.events(CARD_REF)
            if event.get("kind") == EventKind.ATTEMPT_USAGE.value
        ]

    def bind_source(self, role: str, journal: Path, *, adapter: str = "codex") -> str:
        """Bind a provider journal to the head that is serving this round, as a launch would.

        The session id comes from the run's own launch record rather than from the test, because a
        source that named some other session would not be the phase's source at all.
        """
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        run_key = "worker_head_run" if role == "worker" else "review_head_run"
        routing_key = "worker_run" if role == "worker" else "review_run"
        session_id = str(record[routing_key]["session_id"])
        source = (
            bound_codex_source(journal, session_id=session_id)
            if adapter == "codex"
            else bound_claude_source(journal, session_id=session_id)
        )
        run = dict(record[run_key])
        policy = dict(run.get("fanout_policy") or {})
        policy["provider_source" if adapter == "codex" else "provider_progress_source"] = source
        run["fanout_policy"] = policy
        record[run_key] = run
        self.runtime.production_state.save(payload)
        return session_id

    def codex_journal(self, name: str, **totals: int) -> Path:
        return write_jsonl(self.data_dir / "sessions" / name, [codex_token_count(**totals)])

    def claude_journal(self, name: str, message_id: str, **usage: int) -> Path:
        return write_jsonl(self.data_dir / "sessions" / name, [claude_assistant(message_id, **usage)])

    def claude_worker(self) -> None:
        """Put a Claude head on the worker role, so both adapters are exercised at this seam."""
        self.catalog.role_defaults["new_card"] = "claude-opus"

    def test_an_accepted_done_report_accounts_the_worker_phase_it_closed(self) -> None:
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal(
            "worker-1.jsonl", input_tokens=8100, cached_input_tokens=7000, output_tokens=320
        )
        session_id = self.bind_source("worker", journal)
        self._report_done()

        advanced = self.tick()

        self.assertEqual(advanced["to"], "validate")
        events = self.usage_events()
        self.assertEqual(len(events), 1)
        usage = events[0]
        self.assertEqual(usage["outcome"], AttemptUsageOutcome.COLLECTED.value)
        self.assertEqual(usage["role"], "worker")
        self.assertEqual(usage["phase"], "worker")
        self.assertEqual(usage["attempt"], 1)
        self.assertEqual(usage["report_generation"], 1)
        self.assertEqual(usage["adapter"], "codex")
        self.assertEqual(usage["model"], "gpt-5.6-terra")
        self.assertEqual(usage["model_source"], "profile")
        self.assertEqual(usage["session_id"], session_id)
        self.assertEqual(
            usage["tokens"],
            {
                "input": 8100,
                "cache_input": None,
                "cache_read_input": 7000,
                "output": 320,
                "reasoning": None,
            },
        )

    def test_a_claude_worker_phase_is_read_from_its_own_session_records(self) -> None:
        self.claude_worker()
        self.start_dispatcher()
        self.tick()
        journal = self.claude_journal(
            "worker-1.jsonl",
            "msg_1",
            input_tokens=9,
            cache_creation_input_tokens=1400,
            cache_read_input_tokens=22000,
            output_tokens=512,
        )
        session_id = self.bind_source("worker", journal, adapter="claude")
        self._report_done()

        self.assertEqual(self.tick()["to"], "validate")

        usage = self.usage_events()[0]
        self.assertEqual(usage["adapter"], "claude")
        self.assertEqual(usage["session_id"], session_id)
        self.assertEqual(usage["source_kind"], CLAUDE_SOURCE_KIND)
        self.assertEqual(
            usage["tokens"],
            {
                "input": 9,
                "cache_input": 1400,
                "cache_read_input": 22000,
                "output": 512,
                "reasoning": None,
            },
        )

    def test_a_replayed_report_and_a_re_entered_tick_keep_one_occurrence(self) -> None:
        """Recovery re-reads a session that has moved on; the phase still has one account."""
        self.start_dispatcher()
        self.tick()
        journal = self.codex_journal("worker-1.jsonl", input_tokens=100, output_tokens=10)
        self.bind_source("worker", journal)
        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        first = self.usage_events()

        write_jsonl(journal, [codex_token_count(input_tokens=999999, output_tokens=88888)])
        self.tick()
        self.tick()

        self.assertEqual(len(first), 1)
        self.assertEqual(self.usage_events(), first, "the occurrence is neither doubled nor rewritten")

    def test_an_accepted_blocked_report_accounts_the_worker_phase_too(self) -> None:
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.codex_journal("worker-1.jsonl", input_tokens=42, output_tokens=7))
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="blocked",
            body="the card contradicts itself",
            classification="wrong_task_definition",
            request_id=self._worker_report_request_id("blocked", "wrong_task_definition"),
        )

        blocked = self.tick()

        self.assertEqual(blocked["to"], "blocked")
        events = self.usage_events()
        self.assertEqual([usage["phase"] for usage in events], ["worker"])
        self.assertEqual(events[0]["tokens"]["input"], 42)

    def test_a_green_verdict_accounts_the_review_phase_beside_the_worker_one(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.tick()
        session_id = self.bind_source(
            "review", self.codex_journal("review-1.jsonl", input_tokens=5100, output_tokens=210)
        )
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="the change is right",
            request_id="review-green",
        )

        self.tick()

        events = self.usage_events()
        self.assertEqual([usage["phase"] for usage in events], ["worker", "review"])
        review = events[1]
        self.assertEqual(review["role"], "reviewer")
        self.assertEqual(review["head"], "codex-reviewer")
        self.assertEqual(review["session_id"], session_id)
        self.assertEqual(review["tokens"]["output"], 210)

    def test_a_red_verdict_and_its_rework_round_are_separate_bound_occurrences(self) -> None:
        """Relaunch and rework are exactly where a phase account could drift onto another run."""
        self.claude_worker()
        self.start_dispatcher()
        self.tick()
        first_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-1.jsonl", "msg_1", input_tokens=100, output_tokens=10),
            adapter="claude",
        )
        self._report_done()
        self.tick()
        self.tick()
        reviewer = self.bind_source(
            "review", self.codex_journal("review-1.jsonl", input_tokens=200, output_tokens=20)
        )
        self._review_red()
        self._park_and_decide("rework")
        # The rework round writes code, so its done report is not the rejected checkout again.
        self.host.commit = "rework-round-two-c0ffee"
        second_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-2.jsonl", "msg_2", input_tokens=300, output_tokens=30),
            adapter="claude",
        )
        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")

        events = self.usage_events()
        self.assertEqual(
            [(usage["role"], usage["report_generation"], usage["session_id"]) for usage in events],
            [
                ("worker", 1, first_worker),
                ("reviewer", 1, reviewer),
                ("worker", 2, second_worker),
            ],
        )
        self.assertNotEqual(first_worker, second_worker, "the rework round ran a second session")
        self.assertEqual([usage["tokens"]["input"] for usage in events], [100, 200, 300])

    def test_a_respawned_worker_is_accounted_for_the_run_that_actually_reported(self) -> None:
        """A round can bring the same role up twice; the phase belongs to the head that finished."""
        self.claude_worker()
        self.start_dispatcher()
        self.tick()
        first_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-1.jsonl", "msg_1", input_tokens=11, output_tokens=1),
            adapter="claude",
        )
        # The head's process died after the first tick, so the next one reclaims and respawns it.
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.host.head_pid = proc.pid
        record = self.runtime.production_state.records(self.runtime.production_state.load())[CARD_REF]
        self.host._write_head_pid(
            "worker", CARD_REF, head_run=record.worker_head_run, leaf=record.worker_leaf
        )
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        self.assertEqual(self.tick()["action"], "worker-respawned")
        self.host.worker_status_result = None
        second_worker = self.bind_source(
            "worker",
            self.claude_journal("worker-2.jsonl", "msg_2", input_tokens=770, output_tokens=42),
            adapter="claude",
        )
        self._report_done()

        self.assertEqual(self.tick()["to"], "validate")

        usage = self.usage_events()
        self.assertEqual(len(usage), 1, "one round, one worker phase, however often it was launched")
        self.assertNotEqual(first_worker, second_worker)
        self.assertEqual(usage[0]["session_id"], second_worker)
        self.assertEqual(usage[0]["tokens"]["input"], 770)

    def test_the_occurrence_is_not_a_semantic_event_for_a_sprint_observer(self) -> None:
        """Routing telemetry never woke an observer, and neither does phase accounting."""
        self.start_dispatcher()
        self.tick()
        self._report_done()
        self.tick()

        usage = next(
            event
            for event in self.writer.audit.events(CARD_REF)
            if event.get("kind") == EventKind.ATTEMPT_USAGE.value
        )

        self.assertFalse(is_significant_card_event(usage, linked_refs={CARD_REF}))

    def test_a_session_that_cannot_be_read_degrades_and_moves_the_card_anyway(self) -> None:
        self.start_dispatcher()
        self.tick()
        self.bind_source("worker", self.data_dir / "sessions" / "gone.jsonl")
        self._report_done()

        advanced = self.tick()

        self.assertEqual(advanced["to"], "validate", "usage collection never decides the card")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")
        usage = self.usage_events()[0]
        self.assertEqual(usage["outcome"], AttemptUsageOutcome.SOURCE_UNREADABLE.value)
        self.assertTrue(all(value is None for value in usage["tokens"].values()))

    def test_a_head_that_never_bound_a_session_still_leaves_a_typed_account(self) -> None:
        self.start_dispatcher()
        self.tick()
        self._report_done()

        self.assertEqual(self.tick()["to"], "validate")

        usage = self.usage_events()[0]
        self.assertEqual(usage["outcome"], AttemptUsageOutcome.SOURCE_UNAVAILABLE.value)
        self.assertTrue(usage["detail"])

    def test_a_refused_usage_write_leaves_the_report_and_the_transition_alone(self) -> None:
        """The one failure the seam owns itself: the journal write, not the provider read."""
        self.start_dispatcher()
        self.tick()

        def refuse(**_kwargs):
            raise OSError("the audit journal is unwritable")

        self.writer.attempt_usage = refuse  # type: ignore[method-assign]
        self._report_done()
        advanced = self.tick()

        self.assertEqual(advanced["to"], "validate")
        self.assertEqual(self.usage_events(), [])

    def test_the_occurrence_is_readable_by_kind_without_parsing_any_prose(self) -> None:
        self.start_dispatcher()
        self.tick()
        self._report_done()
        self.tick()

        by_kind = self.writer.audit.events(CARD_REF, kind=EventKind.ATTEMPT_USAGE.value)
        typed = [
            event
            for event in self.writer.board_host.canon.events(ref=CARD_REF)
            if event.kind is EventKind.ATTEMPT_USAGE
        ]

        self.assertEqual(len(by_kind), 1)
        self.assertEqual(len(typed), 1)
        self.assertEqual(typed[0].data["phase"], "worker")


if __name__ == "__main__":
    unittest.main()
