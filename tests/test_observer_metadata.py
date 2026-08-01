"""The sprint's declared observer: representation, migration, strict reader and fence.

The live inventory this migration was designed against is reproduced here row for row
(secretary-1027): seventeen sprints, `sprint:1024` open on `claude-observer`, thirteen closed rows
whose head is recoverable from durable lifecycle events, `sprint:818` among them because it changed
head mid-run, and `sprint:878`, `sprint:913` and `sprint:916` with no successful launch at all.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher import CutoverState, DispatcherRuntime
from secretary.dispatcher_observer import (
    ObserverRecord,
    load_observers,
    observer_decision,
    put_observers,
)
from secretary.dispatcher_observer_fence import (
    REASON_DEAD,
    REASON_NO_RECORD,
    fenced_task,
    observer_fence,
)
from secretary.observer_backfill import (
    BackfillError,
    apply_backfill,
    build_inventory,
    persist_inventory,
    plan_cutover,
    read_inventory,
    read_journal,
    recover_observer,
    run_cutover,
    scan_rows,
)
from secretary.sprint_observer import (
    ObserverMetadataError,
    REASON_HISTORICAL,
    REASON_MALFORMED,
    REASON_MISSING,
    REASON_UNKNOWN_PROFILE,
    activate_strict_reader,
    encode_observer,
    executable_observer,
    head_choice,
    historical_recovered,
    historical_unknown,
    none_choice,
    observer_choice,
    parse_observer,
    strict_reader_active,
)
from secretary.sprints import SprintReader, SprintWriter
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter

from tests.test_dispatcher import FakeCatalog, FakeHost, FakeKanboard, FakeLegacyPause
from tests.test_dispatcher_observer import DEAD_PID, install_skill_registry
from tests.test_sprints import SprintFixture


# The live inventory, as the PO audit of 2026-08-01T22:03:13Z fixed it. Closed rows carry the head
# their last successful launch named; `sprint:818` carries two launches so the latest-wins rule is
# exercised rather than asserted.
CLOSED_WITH_EVIDENCE = {
    "sprint:804": "codex-observer",
    "sprint:808": "codex-observer",
    "sprint:814": "codex-observer",
    "sprint:818": "claude-observer",
    "sprint:877": "claude-observer",
    "sprint:879": "codex-observer",
    "sprint:880": "claude-observer",
    "sprint:881": "claude-observer",
    "sprint:882": "codex-observer",
    "sprint:885": "claude-observer",
    "sprint:886": "codex-observer",
    "sprint:888": "codex-observer",
    "sprint:1007": "claude-observer",
}
CLOSED_WITHOUT_EVIDENCE = ("sprint:878", "sprint:913", "sprint:916")
OPEN_SPRINT = "sprint:1024"
OPEN_HEAD = "claude-observer"


class ObserverValueTests(unittest.TestCase):
    """Four tagged forms, and nothing that resembles one."""

    def test_the_four_forms_round_trip_byte_for_byte(self) -> None:
        for value in (
            head_choice("claude-observer"),
            none_choice(),
            historical_recovered("codex-observer", "evt_1"),
            historical_unknown(),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_observer(encode_observer(value)), value)

    def test_absent_null_empty_default_and_inherited_are_not_values(self) -> None:
        for raw in (
            None, "", "null", "{}", "default", "inherited",
            {"kind": "default"}, {"kind": "inherited"},
            {"kind": "head"}, {"kind": "head", "profile": ""},
            {"kind": "none", "profile": "codex-observer"},
            # An extra key is a shape nobody audited, not the form it resembles.
            {"kind": "head", "profile": "codex-observer", "note": "why"},
            {"kind": "historical", "profile": None, "source": "observer_lifecycle_audit"},
            {"kind": "historical", "profile": "codex-observer", "source": "migration_unknown"},
            {"kind": "historical", "profile": "x", "source": "observer_lifecycle_audit",
             "event_id": ""},
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_observer(raw))

    def test_a_historical_value_is_never_executable(self) -> None:
        for value in (historical_recovered("codex-observer", "evt_1"), historical_unknown()):
            with self.subTest(value=value):
                with self.assertRaises(ObserverMetadataError) as raised:
                    executable_observer({"ref": "sprint:1", "observer": value})
                self.assertEqual(raised.exception.reason, REASON_HISTORICAL)

    def test_missing_and_malformed_are_told_apart(self) -> None:
        with self.assertRaises(ObserverMetadataError) as missing:
            executable_observer({"ref": "sprint:1"})
        self.assertEqual(missing.exception.reason, REASON_MISSING)
        with self.assertRaises(ObserverMetadataError) as malformed:
            executable_observer({"ref": "sprint:1", "observer": None})
        self.assertEqual(malformed.exception.reason, REASON_MALFORMED)

    def test_the_operator_spells_none_or_a_profile(self) -> None:
        self.assertEqual(observer_choice("none"), none_choice())
        self.assertEqual(observer_choice(" claude-observer "), head_choice("claude-observer"))
        self.assertIsNone(observer_choice("  "))


class SprintDeclarationTests(SprintFixture):
    """Opening and reopening a sprint state its observer, or do not happen."""

    def _metadata(self, reference: str) -> dict:
        row = next(item for item in self.client.tasks if item["reference"] == reference)
        return self.client.metadata[int(row["id"])]

    def test_create_without_an_observer_is_refused_before_any_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "requires an explicit observer"):
            self.writer.create(
                role="po", actor="operator", goal="no observer", product="secretary",
                issues=["issue:open"], projects=["secretary"],
            )

        self.assertEqual(TaskAudit(self.tmp.name).events(), [])

    def test_create_records_none_as_a_value_of_its_own(self) -> None:
        result = self._create(goal="unobserved", observer=none_choice())

        self.assertEqual(result["sprint"]["observer"], none_choice())
        self.assertEqual(
            self._metadata(result["sprint"]["ref"])["sprint_observer"],
            encode_observer(none_choice()),
        )

    def test_create_refuses_provenance_and_every_absent_spelling(self) -> None:
        for observer in (
            historical_unknown(),
            historical_recovered("codex-observer", "evt_1"),
        ):
            with self.subTest(observer=observer):
                with self.assertRaisesRegex(TaskError, "migration provenance"):
                    self._create(goal="provenance", observer=observer)
        for observer in ({"kind": "default"}, {"kind": "head", "profile": ""}, ""):
            with self.subTest(observer=observer):
                with self.assertRaises(TaskError):
                    self._create(goal="not a value", observer=observer)

    def test_the_observer_lands_before_the_reference_publishes_the_row(self) -> None:
        self._create(goal="ordered", reference="sprint:ordered")

        order = [
            method for method, params in self.client.calls
            if (method == "saveTaskMetadata" and "sprint_observer" in dict(params["values"]))
            or (method == "updateTask" and params.get("reference") == "sprint:ordered")
        ]
        self.assertEqual(order[:2], ["saveTaskMetadata", "updateTask"])

    def test_reopen_requires_a_fresh_choice_and_never_inherits_the_closed_one(self) -> None:
        reference = self._create(goal="reopened", observer=head_choice("codex-observer"))["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=reference, request_id="close")

        with self.assertRaisesRegex(TaskError, "requires an explicit observer"):
            self.writer.reopen(role="po", actor="operator", reference=reference)

        reopened = self.writer.reopen(
            role="po", actor="operator", reference=reference, observer=none_choice(),
            request_id="reopen",
        )
        self.assertEqual(reopened["sprint"]["observer"], none_choice())
        self.assertEqual(reopened["sprint"]["status"], "open")

    def test_reopen_writes_the_choice_while_the_sprint_is_still_closed(self) -> None:
        reference = self._create(goal="ordered reopen")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=reference, request_id="close")
        self.client.calls.clear()

        self.writer.reopen(
            role="po", actor="operator", reference=reference,
            observer=head_choice("claude-observer"), request_id="reopen",
        )

        written = [
            sorted(dict(params["values"]))
            for method, params in self.client.calls
            if method == "saveTaskMetadata"
            and {"sprint_observer", "sprint_status"} & set(dict(params["values"]))
        ]
        self.assertEqual(written, [["sprint_observer"], ["sprint_status"]])

    def test_a_reopen_repeated_with_another_observer_is_refused(self) -> None:
        reference = self._create(goal="one reopen")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=reference, request_id="close")
        first = self.writer.reopen(
            role="po", actor="operator", reference=reference,
            observer=head_choice("codex-observer"), request_id="reopen-once",
        )

        replay = self.writer.reopen(
            role="po", actor="operator", reference=reference,
            observer=head_choice("codex-observer"), request_id="reopen-once",
        )
        self.assertEqual(replay["event_id"], first["event_id"])

        with self.assertRaises(TaskError):
            self.writer.reopen(
                role="po", actor="operator", reference=reference,
                observer=none_choice(), request_id="reopen-once",
            )


class ObserverBoard(FakeKanboard):
    """The live sprint inventory, on the fake board, with its durable lifecycle log."""

    def seed_inventory(self, data_dir: Path) -> list[dict]:
        for reference in sorted(CLOSED_WITH_EVIDENCE) + list(CLOSED_WITHOUT_EVIDENCE):
            self.add_sprint(reference, status="closed", sprint_reservations='["secretary"]')
        self.add_sprint(OPEN_SPRINT, status="open", sprint_reservations='["secretary"]')
        return _write_lifecycle_log(data_dir)


def _write_lifecycle_log(data_dir: Path) -> list[dict]:
    """The observer launch history the migration recovers provenance from.

    `sprint:818` gets two successful launches on different heads, `sprint:878` a deferral and a
    failed launch, and `sprint:913`/`sprint:916` nothing at all: the three shapes of "no head is
    recoverable" that the live board actually holds.
    """
    events: list[dict] = []
    stamp = [0]

    def event(ref: str, kind: str, head: str, outcome: str = "success") -> dict:
        stamp[0] += 1
        return {
            "event_id": f"evt_{ref.replace(':', '_')}_{stamp[0]}",
            "schema_version": 1,
            "occurred_at": f"2026-07-27T00:{stamp[0]:02d}:00Z",
            "actor": {"role": "dispatcher", "id": "dispatcher"},
            "kind": kind,
            "outcome": outcome,
            "task_id": "",
            "ref": ref,
            "backend": {"kind": "dispatcher", "task_id": None, "revision": "n/a"},
            "request_id": f"req-{ref}-{stamp[0]}",
            "payload": {"head": head, "launches": 1},
        }

    for reference, head in sorted(CLOSED_WITH_EVIDENCE.items()):
        if reference == "sprint:818":
            # Changed head mid-run: the earlier launch must lose to the later relaunch.
            events.append(event(reference, "observer_launched", "codex-observer"))
            events.append(event(reference, "observer_relaunched", head))
        else:
            events.append(event(reference, "observer_launched", head))
    events.append(event("sprint:878", "observer_launch_deferred", "codex-observer"))
    events.append(event("sprint:878", "observer_launched", "codex-observer", outcome="failure"))
    events.append(event(OPEN_SPRINT, "observer_launched", OPEN_HEAD))
    board = data_dir / "board"
    board.mkdir(parents=True, exist_ok=True)
    (board / "events.ndjson").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events), encoding="utf-8"
    )
    return events


class ObserverBackfillTests(unittest.TestCase):
    """Every one of the seventeen rows, and what a retry of the write does."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.board = ObserverBoard()
        self.events = self.board.seed_inventory(self.data_dir)
        self.reader = SprintReader(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        self.writer = SprintWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        self.audit = TaskAudit(self.data_dir)

    def inventory(self, running: dict[str, str] | None = None) -> dict:
        return build_inventory(
            self.reader.export(), self.audit.events(),
            running if running is not None else {OPEN_SPRINT: OPEN_HEAD},
        )

    def test_all_seventeen_rows_get_the_value_the_audit_fixed(self) -> None:
        rows = {row["ref"]: row["observer"] for row in self.inventory()["rows"]}

        self.assertEqual(len(rows), 17)
        self.assertEqual(rows[OPEN_SPRINT], head_choice(OPEN_HEAD))
        for reference in CLOSED_WITHOUT_EVIDENCE:
            self.assertEqual(rows[reference], historical_unknown())
        for reference, head in CLOSED_WITH_EVIDENCE.items():
            self.assertEqual(rows[reference]["kind"], "historical")
            self.assertEqual(rows[reference]["profile"], head)
            self.assertEqual(rows[reference]["source"], "observer_lifecycle_audit")
            self.assertTrue(rows[reference]["event_id"])

    def test_a_sprint_that_changed_head_recovers_its_latest_launch(self) -> None:
        recovered = recover_observer("sprint:818", self.events)

        self.assertEqual(recovered["profile"], "claude-observer")
        latest = [
            item for item in self.events
            if item["ref"] == "sprint:818" and item["kind"] == "observer_relaunched"
        ][-1]
        self.assertEqual(recovered["event_id"], latest["event_id"])

    def test_a_deferral_or_a_failed_launch_is_not_evidence(self) -> None:
        self.assertIsNone(recover_observer("sprint:878", self.events))

    def test_an_open_row_the_migration_cannot_prove_stops_it(self) -> None:
        with self.assertRaisesRegex(BackfillError, "declare its observer by hand"):
            build_inventory(self.reader.export(), [], {})

    def test_a_disagreement_between_record_and_log_stops_the_migration(self) -> None:
        with self.assertRaisesRegex(BackfillError, "resolve which head is running"):
            self.inventory(running={OPEN_SPRINT: "codex-observer"})

    def test_the_writes_are_idempotent_and_recognise_their_own_value(self) -> None:
        inventory = persist_inventory(self.data_dir, self.inventory())
        first = apply_backfill(self.writer, self.data_dir, inventory)
        writes = sum(1 for method, _ in self.board.calls if method == "saveTaskMetadata")

        second = apply_backfill(self.writer, self.data_dir, inventory)

        self.assertEqual(len(first), 17)
        self.assertEqual([row["event_id"] for row in first], [row["event_id"] for row in second])
        self.assertEqual(
            sum(1 for method, _ in self.board.calls if method == "saveTaskMetadata"), writes
        )
        self.assertEqual(scan_rows(self.reader.export()), [])

    def test_a_retry_reads_the_journal_rather_than_recomputing_provenance(self) -> None:
        inventory = persist_inventory(self.data_dir, self.inventory())
        apply_backfill(self.writer, self.data_dir, inventory)

        # The audit log has grown by this migration's own events, and a head could have been
        # relaunched since. A second run must still write what the first one selected.
        _write_lifecycle_log(self.data_dir)
        stored = read_inventory(self.data_dir)
        self.assertIsNotNone(stored)
        self.assertEqual(stored, inventory)
        journalled = {entry["ref"]: entry["observer"] for entry in read_journal(self.data_dir)}
        self.assertEqual(len(journalled), 17)
        self.assertEqual(journalled["sprint:818"]["profile"], "claude-observer")

    def test_a_second_inventory_over_a_changed_board_is_refused(self) -> None:
        persist_inventory(self.data_dir, self.inventory())
        self.board.add_sprint("sprint:1030", status="closed")

        with self.assertRaisesRegex(BackfillError, "different observer migration inventory"):
            persist_inventory(self.data_dir, self.inventory())

    def test_a_row_that_already_holds_another_value_is_never_overwritten(self) -> None:
        inventory = persist_inventory(self.data_dir, self.inventory())
        row = next(item for item in self.board.sprints if item["reference"] == "sprint:804")
        self.board.metadata[int(row["id"])]["sprint_observer"] = encode_observer(none_choice())

        with self.assertRaisesRegex(BackfillError, "not the value this migration selected"):
            apply_backfill(self.writer, self.data_dir, inventory)

    def test_the_scan_names_every_row_the_strict_reader_would_refuse(self) -> None:
        rows = self.reader.export()
        self.assertEqual(len(scan_rows(rows)), 17)

        inventory = persist_inventory(self.data_dir, self.inventory())
        apply_backfill(self.writer, self.data_dir, inventory)
        row = next(item for item in self.board.sprints if item["reference"] == OPEN_SPRINT)
        self.board.metadata[int(row["id"])]["sprint_observer"] = encode_observer(
            historical_unknown()
        )

        problems = scan_rows(self.reader.export())
        self.assertEqual(len(problems), 1)
        self.assertIn("open sprint carries non-executable migration_unknown", problems[0])


class StubPause:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def load(self) -> dict:
        return dict(self.payload)


class StubProductionState:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def load(self) -> dict:
        return dict(self.payload)

    def records(self, payload: dict) -> dict:
        from secretary.dispatcher_state import DispatcherRecord

        raw = payload.get("records") or {}
        return {ref: DispatcherRecord.from_json(value) for ref, value in raw.items()}


class StubCheckpoint:
    def __init__(self, results: list) -> None:
        self.results = results
        self.calls = 0

    def write(self):
        self.calls += 1
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return _Json(result)


class _Json:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def to_json(self) -> dict:
        return dict(self.payload)


class StubPusher:
    def __init__(self, result: dict) -> None:
        self.result = result

    def push(self, state: dict) -> dict:
        return dict(self.result)


class StubRuntime:
    def __init__(self, *, sprints, audit, pause, production_state, checkpoint, pusher) -> None:
        self.sprints = sprints
        self.audit = audit
        self.pause = pause
        self.production_state = production_state
        self.checkpoint = checkpoint
        self.checkpoint_push = pusher


class ObserverCutoverTests(unittest.TestCase):
    """The ordered sequence, and every boundary it refuses to cross."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.board = ObserverBoard()
        self.board.seed_inventory(self.data_dir)
        self.sprint_reader = SprintReader(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        self.sprint_writer = SprintWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        self.pause = StubPause({"mode": "freeze", "excluded_worker": []})
        self.state = StubProductionState({
            "observers": {OPEN_SPRINT: ObserverRecord(sprint=OPEN_SPRINT, head=OPEN_HEAD).to_json()},
            "records": {},
        })
        self.checkpoint = StubCheckpoint([{"status": "ok"}])
        self.runtime = StubRuntime(
            sprints=self.sprint_reader,
            audit=TaskAudit(self.data_dir),
            pause=self.pause,
            production_state=self.state,
            checkpoint=self.checkpoint,
            pusher=StubPusher({"status": "ok"}),
        )

    def cutover(self, **kwargs):
        return run_cutover(
            self.runtime, sprint_writer=self.sprint_writer, data_dir=self.data_dir,
            now="2026-08-02T00:00:00Z", resume=False, **kwargs,
        )

    def test_the_whole_sequence_runs_in_order_and_ends_strict(self) -> None:
        result = self.cutover()

        self.assertEqual(
            [step["step"] for step in result["steps"]],
            [
                "freeze", "heads-stopped", "pre-migration-checkpoint", "inventory", "backfill",
                "strict-scan", "post-migration-checkpoint", "strict-reader", "resume",
            ],
        )
        self.assertTrue(strict_reader_active(self.data_dir))
        self.assertEqual(len(result["rows"]), 17)

    def test_an_unfrozen_pipeline_is_refused_before_any_write(self) -> None:
        self.pause.payload = {"mode": ""}

        with self.assertRaisesRegex(BackfillError, "not frozen"):
            self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))
        self.assertIsNone(read_inventory(self.data_dir))
        self.assertEqual(scan_rows(self.sprint_reader.export()), scan_rows(self.sprint_reader.export()))

    def test_a_freeze_with_exclusions_is_refused(self) -> None:
        self.pause.payload = {"mode": "freeze", "excluded_worker": ["/work/backup"]}

        with self.assertRaisesRegex(BackfillError, "workspace exclusions"):
            self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))

    def test_a_head_the_freeze_could_not_stop_is_refused(self) -> None:
        record = ObserverRecord(sprint=OPEN_SPRINT, head=OPEN_HEAD, handle="term_1")
        self.state.payload = {"observers": {OPEN_SPRINT: record.to_json()}, "records": {}}

        with self.assertRaisesRegex(BackfillError, "heads are still running"):
            self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))

    def test_a_blocked_pre_migration_checkpoint_stops_the_cutover(self) -> None:
        self.checkpoint.results = [{"status": "blocked", "reason": "dirty"}]

        with self.assertRaisesRegex(BackfillError, "pre-migration checkpoint"):
            self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))
        self.assertIsNone(read_inventory(self.data_dir))

    def test_a_crash_after_the_writes_leaves_the_reader_tolerant_and_is_resumable(self) -> None:
        inventory = persist_inventory(
            self.data_dir,
            build_inventory(
                self.sprint_reader.export(), self.runtime.audit.events(), {OPEN_SPRINT: OPEN_HEAD},
            ),
        )
        apply_backfill(self.sprint_writer, self.data_dir, inventory)
        # Exactly the state a process killed between the last write and the activation is in.
        self.assertFalse(strict_reader_active(self.data_dir))

        result = self.cutover()

        self.assertTrue(strict_reader_active(self.data_dir))
        self.assertEqual(read_inventory(self.data_dir)["digest"], inventory["digest"])
        self.assertEqual(len(result["rows"]), 17)

    def test_a_failed_scan_never_activates_the_strict_reader(self) -> None:
        with mock.patch(
            "secretary.observer_backfill.scan_rows", return_value=["sprint:1024: no observer"]
        ):
            with self.assertRaisesRegex(BackfillError, "post-migration scan refused"):
                self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))

    def test_a_second_cutover_does_nothing_and_says_so(self) -> None:
        self.cutover()

        again = self.cutover()

        self.assertEqual(again["status"], "already-migrated")

    def test_the_resume_is_the_last_step_and_only_after_a_clean_run(self) -> None:
        with mock.patch("secretary.dispatcher_pause_ops.resume") as lifted:
            result = run_cutover(
                self.runtime, sprint_writer=self.sprint_writer, data_dir=self.data_dir,
                now="2026-08-02T00:00:00Z",
            )
        self.assertEqual(lifted.call_count, 1)
        self.assertEqual(result["steps"][-1]["step"], "resume")

    def test_a_dry_run_writes_nothing_and_needs_no_freeze(self) -> None:
        self.pause.payload = {"mode": ""}

        plan = plan_cutover(self.runtime, data_dir=self.data_dir)

        self.assertEqual(len(plan["rows"]), 17)
        self.assertFalse(plan["strict_reader_active"])
        self.assertIsNone(read_inventory(self.data_dir))
        self.assertNotIn(
            "sprint_observer",
            self.board.metadata[int(self.board.sprints[0]["id"])],
        )


class ObserverFenceFixture(unittest.TestCase):
    """A production runtime over one open sprint with a declared observer."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        install_skill_registry(self.data_dir)
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
                "SECRETARY_ROLE_SKILLS_MANIFEST": str(self.data_dir / "registry" / "manifest.toml"),
                "SECRETARY_INSTANCE": str(self.data_dir / "registry" / "instance"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (self.data_dir / "bodies").mkdir(parents=True, exist_ok=True)
        self.board = FakeKanboard()
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.catalog.profiles["claude-observer"] = {
            "adapter": "claude", "model": "opus", "resource": "claude-sub",
        }
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.runtime = DispatcherRuntime(
            TaskReader(self.board),  # type: ignore[arg-type]
            TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir),  # type: ignore[arg-type]
            TaskAudit(self.data_dir),
            CutoverState(self.data_dir),
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
            legacy_pause=FakeLegacyPause(),  # type: ignore[arg-type]
        )
        self.runtime.state.save({
            "version": 1, "phase": "cutover_committed", "pilot_ref": "secretary-510-pilot",
            "old_owner_paused": True, "records": {},
        })

    def go_strict(self) -> None:
        activate_strict_reader(
            self.data_dir, inventory_digest="test", rows=1, activated_at="2026-08-02T00:00:00Z",
        )

    def declare(self, observer, *, reference: str = "sprint:1", **metadata) -> None:
        values = {"sprint_reservations": '["secretary"]', **metadata}
        if observer is not None:
            values["sprint_observer"] = observer
        self.board.add_sprint(reference, status="open", **values)

    def fence(self) -> dict:
        return observer_fence(self.runtime, self.runtime.production_state.load())


class ObserverFenceTests(ObserverFenceFixture):
    def test_a_declared_none_passes_without_a_launch_or_a_probe(self) -> None:
        self.go_strict()
        self.declare(encode_observer(none_choice()))

        result = self.runtime.production_tick()

        self.assertEqual(self.fence()["sprints"], set())
        self.assertEqual(self.host.observers, [])
        self.assertEqual(
            [action["action"] for action in result["actions"] if action["step"] == "observer-reconcile"],
            ["observer-none"],
        )

    def _tick_twice_with_an_active_card(self, declaration: str | None) -> list[dict]:
        """Two ticks over one in-progress card linked to the sprint. The second is the evidence.

        The first tick of any declared sprint fences on `observer_not_launched`, because the head
        it declares is brought up later in that same tick. The second tick is where a working
        declaration has an adopted head and a broken one still has none.
        """
        self.declare(declaration)
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.tasks[0]["column_id"] = 3
        self.runtime.production_tick()
        return self.runtime.production_tick()["actions"]

    def test_a_sprint_redeclared_as_none_gives_its_head_back(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        self.runtime.production_tick()
        row = next(item for item in self.board.sprints if item["reference"] == "sprint:1")
        self.board.metadata[int(row["id"])]["sprint_observer"] = encode_observer(none_choice())

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in result["actions"] if action["step"] == "observer-reconcile"],
            ["observer-stopped"],
        )
        self.assertEqual(self.host.stopped_observers, ["observer:sprint:1"])
        self.assertEqual(load_observers(self.runtime.production_state.load()), {})
        self.assertEqual(self.fence()["sprints"], set())

    def test_a_corrupt_declaration_fences_before_any_card_moves(self) -> None:
        self.go_strict()

        actions = self._tick_twice_with_an_active_card("{not json")

        fenced = [action for action in actions if action["step"] == "observer-fence"]
        self.assertEqual([action["observer_reason"] for action in fenced], [REASON_MALFORMED])
        self.assertEqual(fenced[0]["status"], "critical")
        # The active card of that sprint was not advanced, no head came up, and the card was not
        # moved on the board.
        self.assertEqual([action for action in actions if action["step"] == "advance"], [])
        self.assertEqual(self.host.observers, [])
        self.assertEqual([method for method, _ in self.board.calls if method == "moveTaskPosition"], [])

    def test_a_card_fenced_only_by_its_project_keeps_its_dispatcher_record(self) -> None:
        """Dropping out of the active set is not the same as leaving the cycle.

        The card here is not linked to the sprint at all: it sits in a project the sprint
        reserved, and it has left the active cycle. Reconciliation reads such a record as orphaned
        and settles its heads, which is exactly the mutation the fence exists to prevent.
        """
        self.go_strict()
        self.declare("{not json")
        # An unlinked card of the reserved project, out of the cycle, with a live record.
        self.board.tasks[1]["column_id"] = 5
        payload = self.runtime.production_state.load()
        payload["records"] = {
            "secretary-510-neighbor": {
                "worker": "w1", "workspace": "/tmp/w1", "handle": "term_w", "head": "codex",
                "review_head": "codex-reviewer", "attempt_id": "att-1", "comment_baseline": 0,
                "review_baseline": 0, "state": "adopted", "claimed_at": time.time(),
            }
        }
        self.runtime.production_state.save(payload)

        result = self.runtime.production_tick()

        self.assertEqual(
            [action["action"] for action in result["actions"] if action["step"] == "observer-fence"],
            ["observer-fenced"],
        )
        self.assertEqual([action for action in result["actions"] if action["step"] == "advance"], [])
        self.assertIn("secretary-510-neighbor", self.runtime.production_state.load()["records"])

    def test_the_same_card_advances_once_the_declared_observer_is_adopted(self) -> None:
        """The control for the fence: without it the pass above proves nothing."""
        self.go_strict()

        actions = self._tick_twice_with_an_active_card(encode_observer(head_choice("claude-observer")))

        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "observer-fence"],
            ["observer-fence-cleared"],
        )
        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "advance"],
            ["waiting-worker-report"],
        )

    def test_a_missing_declaration_under_the_strict_reader_is_corruption(self) -> None:
        self.go_strict()
        self.declare(None)

        fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_MISSING)

    def test_the_same_row_before_the_cutover_is_left_alone(self) -> None:
        self.declare(None)

        fence = self.fence()

        self.assertEqual(fence["sprints"], set())

    def test_an_unknown_profile_fences_and_never_falls_back_to_the_role_default(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("retired-observer")))

        result = self.runtime.production_tick()

        fenced = [action for action in result["actions"] if action["step"] == "observer-fence"]
        self.assertEqual([action["observer_reason"] for action in fenced], [REASON_UNKNOWN_PROFILE])
        self.assertEqual(self.host.observers, [])
        reconcile = [
            action for action in result["actions"] if action["step"] == "observer-reconcile"
        ]
        self.assertEqual([action["action"] for action in reconcile], ["observer-declaration-invalid"])

    def test_a_declared_head_launches_on_its_own_profile_and_clears_on_a_later_tick(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))

        first = self.runtime.production_tick()
        raised = [action for action in first["actions"] if action["step"] == "observer-fence"]
        self.assertEqual([action["observer_reason"] for action in raised], [REASON_NO_RECORD])
        self.assertEqual(
            load_observers(self.runtime.production_state.load())["sprint:1"].head, "claude-observer"
        )

        second = self.runtime.production_tick()

        cleared = [action for action in second["actions"] if action["step"] == "observer-fence"]
        self.assertEqual([action["action"] for action in cleared], ["observer-fence-cleared"])
        self.assertEqual(self.fence()["sprints"], set())

    def test_a_dead_declared_head_fences_the_sprint_again(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        self.runtime.production_tick()
        self.runtime.production_tick()
        record = load_observers(self.runtime.production_state.load())["sprint:1"]
        Path(record.pid_file).write_text(str(DEAD_PID), encoding="utf-8")

        fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_DEAD)

    def test_fencing_is_project_local(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.metadata[13]["project"] = "other"

        fence = self.fence()

        self.assertEqual(fence["projects"], {"secretary"})
        self.assertTrue(fenced_task(fence, {"ref": "secretary-510-pilot", "project": "secretary"}))
        self.assertFalse(fenced_task(fence, {"ref": "secretary-510-neighbor", "project": "other"}))

    def test_a_fence_writes_its_reason_durably_once_per_reason(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("retired-observer")))

        self.runtime.production_tick()
        self.runtime.production_tick()

        raised = [
            event for event in self.runtime.audit.events("sprint:1")
            if event["kind"] == "observer_fence_raised"
        ]
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["outcome"], "critical")
        self.assertEqual(raised[0]["payload"]["observer_reason"], REASON_UNKNOWN_PROFILE)

    def test_an_unreadable_sprint_board_fences_nothing(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            fence = self.fence()

        self.assertEqual(fence["sprints"], set())
        self.assertEqual(fence["outcomes"][0]["action"], "sprint_board_unavailable")

    def test_the_decision_never_reads_the_role_default_once_strict(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        sprint = self.runtime.sprints.show("sprint:1", include_cards=False)
        self.catalog.role_defaults["observer"] = "codex-observer"

        self.assertEqual(observer_decision(self.runtime, sprint)["head"], "claude-observer")


class ObserverRecordFenceStateTests(ObserverFenceFixture):
    def test_a_stale_fence_of_a_closed_sprint_is_dropped(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        observer_fence(self.runtime, payload)
        self.assertIn("sprint:1", payload["observer_fence"])
        self.board.metadata[100]["sprint_status"] = "closed"

        outcomes = observer_fence(self.runtime, payload)["outcomes"]

        self.assertEqual([outcome["action"] for outcome in outcomes], ["observer-fence-cleared"])
        self.assertNotIn("observer_fence", payload)

    def test_a_head_that_is_not_the_declared_one_fences(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        put_observers(payload, {
            "sprint:1": ObserverRecord(
                sprint="sprint:1", head="codex-observer", handle="term_x",
                state="running", launches=1, launched_at=time.time(),
            )
        })
        self.runtime.production_state.save(payload)

        fence = observer_fence(self.runtime, self.runtime.production_state.load())

        self.assertEqual(fence["outcomes"][0]["observer_reason"], "observer_head_mismatch")


if __name__ == "__main__":
    unittest.main()
