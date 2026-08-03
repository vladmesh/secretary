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

from secretary.dispatcher import DispatcherRuntime
from secretary.dispatcher_observer import (
    ObserverRecord,
    load_observers,
    observer_alive,
    observer_decision,
    put_observers,
)
from secretary.dispatcher_observer_fence import (
    REASON_DEAD,
    REASON_NOT_ADOPTED,
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
    cutover_in_flight,
    encode_observer,
    executable_observer,
    head_choice,
    historical_recovered,
    historical_unknown,
    none_choice,
    observer_choice,
    forget_migration_state,
    migration_recorded,
    parse_observer,
    strict_marker_present,
    strict_reader_active,
)
from secretary.dispatcher_production import _reconcile_production
from secretary.sprints import SprintReader, SprintWriter
from secretary.tasks import TaskAudit, TaskError, TaskReader, TaskWriter

from tests.test_dispatcher import (
    FakeCatalog,
    FakeHost,
    FakeKanboard,
    TwoOpenSprintAdmission,
)
from tests.test_dispatcher_observer import DEAD_PID, install_skill_registry
from tests.test_sprints import SprintFixture, _write_head_registry


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

    def test_create_refuses_a_head_the_registry_does_not_have(self) -> None:
        """A sprint may not be opened on a head that does not exist.

        The fence would stop its projects on the very first tick, and an operator would be reading
        a critical outcome about a sprint they had just opened instead of a validation error.
        """
        with self.assertRaisesRegex(TaskError, "not a profile of this installation"):
            self._create(goal="ghost head", observer=head_choice("retired-observer"))

        self.assertEqual(TaskAudit(self.tmp.name).events(), [])

    def test_reopen_refuses_a_head_the_registry_does_not_have(self) -> None:
        reference = self._create(goal="reopen onto a ghost")["sprint"]["ref"]
        self.writer.close(role="po", actor="operator", reference=reference, request_id="close")

        with self.assertRaisesRegex(TaskError, "not a profile of this installation"):
            self.writer.reopen(
                role="po", actor="operator", reference=reference,
                observer=head_choice("retired-observer"), request_id="reopen",
            )

        self.assertEqual(
            self.writer.reader.show(reference, include_cards=False)["status"], "closed"
        )

    def test_an_unreadable_registry_refuses_rather_than_accepting_the_declaration(self) -> None:
        (self.instance / "heads" / "heads.yaml").write_text("profiles: []\n", encoding="utf-8")

        with self.assertRaisesRegex(TaskError, "head registry"):
            self._create(goal="no registry", observer=head_choice("codex-observer"))

    def test_none_needs_no_profile(self) -> None:
        (self.instance / "heads" / "heads.yaml").unlink()

        result = self._create(goal="unobserved", observer=none_choice())

        self.assertEqual(result["sprint"]["observer"], none_choice())

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
        self.path = Path("/nonexistent/dispatcher/production-state.json")

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
    """The checkpoint pusher, recording what the cutover asked it to do.

    The real pusher answers a call inside its 30-minute window by handing the previous state back
    untouched. The cutover has to bypass that window, so the calls are kept: a cutover that relied
    on the scheduler would show up here as a call carrying the tick's push state.
    """

    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"status": "pushed", "commit": "abc123"}
        self.calls: list[dict] = []

    def push(self, state: dict) -> dict:
        self.calls.append(dict(state))
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)


class WindowedStubPusher:
    """`CheckpointPusher`'s scheduling, which is the part the cutover has to get past.

    The real one returns the state it was handed, untouched, whenever its 30-minute window is not
    due (`checkpoint.py`, `_due`). That returned state carries the *previous* run's status, so a
    caller that reads it as its own result believes in a push that never happened.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def push(self, state: dict) -> dict:
        if state.get("attempted_epoch"):
            return dict(state)
        self.attempts += 1
        return {"status": "pushed", "commit": "abc123", "attempted_epoch": 2.0}


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
        self.instance = Path(self.tmp.name) / "instance"
        _write_head_registry(self.instance)
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
            pusher=StubPusher(),
        )

    def cutover(self, **kwargs):
        return run_cutover(
            self.runtime, sprint_writer=self.sprint_writer, data_dir=self.data_dir,
            instance=self.instance, now="2026-08-02T00:00:00Z", resume=False, **kwargs,
        )

    def metadata_writes(self) -> int:
        return sum(
            1 for method, params in self.board.calls
            if method == "saveTaskMetadata" and "sprint_observer" in dict(params["values"])
        )

    def observers_on_board(self) -> dict[str, dict]:
        return {
            str(sprint["ref"]): sprint["observer"]
            for sprint in self.sprint_reader.export()
            if "observer" in sprint
        }

    def test_the_whole_sequence_runs_in_order_and_ends_strict(self) -> None:
        result = self.cutover()

        self.assertEqual(
            [step["step"] for step in result["steps"]],
            [
                "freeze", "heads-stopped", "pre-migration-checkpoint", "inventory", "backfill",
                "strict-scan", "migration-completed", "post-migration-checkpoint",
                "migration-activated", "strict-reader", "resume",
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

    def test_an_unreadable_production_state_is_refused(self) -> None:
        """An empty decode is not evidence that every head is stopped.

        `pause freeze` sets the flag and stops nothing when the records are unreadable, so this is
        exactly the state in which a live head is most likely and least visible.
        """
        self.state.payload = {"version": 1, "phase": "unavailable"}

        with self.assertRaisesRegex(BackfillError, "production state cannot be read"):
            self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))
        self.assertIsNone(read_inventory(self.data_dir))
        self.assertEqual(self.metadata_writes(), 0)
        self.assertEqual(self.runtime.checkpoint.calls, 0)
        self.assertEqual(self.pause.payload["mode"], "freeze")

    def test_every_head_identity_a_freeze_can_leave_behind_is_refused(self) -> None:
        """A handle is not the only thing that names a head, and it is not the likeliest one.

        A head adopted from a launch intent never had a handle; a stop the host refused leaves the
        record pointing at its head deliberately. `pause freeze` stops by all of these identities,
        so the migration has to confirm all of them stopped or it rewrites the board under a live
        head.
        """
        base = {
            "worker": "w1", "workspace": "/tmp/w1", "handle": "", "head": "codex",
            "review_head": "codex-reviewer", "attempt_id": "att-1", "comment_baseline": 0,
            "review_baseline": 0, "state": "adopted", "claimed_at": 0.0,
        }
        identities = {
            "worker pid heartbeat": {"worker_pid_file": "/tmp/w.pid"},
            "worker pane leaf": {"worker_leaf": "leaf-1"},
            "reviewer pid heartbeat": {"review_pid_file": "/tmp/r.pid"},
            "reviewer pane leaf": {"review_leaf": "leaf-2"},
            "reviewer handle": {"review_handle": "term_r"},
            "unresolved launch intent": {
                "launch_intent": {"role": "worker", "action": "claim", "workspace": "/tmp/w1"},
            },
        }
        for name, extra in identities.items():
            with self.subTest(identity=name):
                self.setUp()
                self.state.payload = {
                    "records": {"secretary-1": {**base, **extra}}, "observers": {},
                }

                with self.assertRaisesRegex(BackfillError, "still running under the freeze"):
                    self.cutover()

                self.assertEqual(self.metadata_writes(), 0)
                self.assertIsNone(read_inventory(self.data_dir))
                self.assertFalse(strict_reader_active(self.data_dir))

    def test_an_observer_head_without_a_handle_is_refused(self) -> None:
        """A bring-up that registered its workspace before the host answered still owns a head."""
        for name, record in {
            "workspace of an unresolved bring-up": ObserverRecord(
                sprint=OPEN_SPRINT, head=OPEN_HEAD, head_possible=True,
                workspace="/tmp/observer-ws",
            ),
            "abandoned terminal": ObserverRecord(
                sprint=OPEN_SPRINT, head=OPEN_HEAD, handle="term_o", abandoned_handle=True,
            ),
        }.items():
            with self.subTest(identity=name):
                self.setUp()
                self.state.payload = {"observers": {OPEN_SPRINT: record.to_json()}, "records": {}}

                with self.assertRaisesRegex(BackfillError, "still running under the freeze"):
                    self.cutover()

                self.assertEqual(self.metadata_writes(), 0)

    def test_an_observer_pid_that_is_still_alive_is_refused(self) -> None:
        pid_file = self.data_dir / "observer.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        record = ObserverRecord(sprint=OPEN_SPRINT, head=OPEN_HEAD, pid_file=str(pid_file))
        self.state.payload = {"observers": {OPEN_SPRINT: record.to_json()}, "records": {}}

        with self.assertRaisesRegex(BackfillError, "still running under the freeze"):
            self.cutover()

        self.assertEqual(self.metadata_writes(), 0)

    def test_a_settled_record_with_no_identity_left_does_not_block(self) -> None:
        """The other half of the rule: a freeze that confirmed its stops clears the way."""
        self.state.payload = {
            "records": {
                "secretary-1": {
                    "worker": "w1", "workspace": "/tmp/w1", "handle": "", "head": "codex",
                    "review_head": "codex-reviewer", "attempt_id": "att-1",
                    "comment_baseline": 0, "review_baseline": 0, "state": "adopted",
                    "claimed_at": 0.0,
                }
            },
            "observers": {OPEN_SPRINT: ObserverRecord(sprint=OPEN_SPRINT, head=OPEN_HEAD).to_json()},
        }

        result = self.cutover()

        self.assertEqual(len(result["rows"]), 17)

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

    def test_a_head_that_left_the_registry_stops_the_scan_before_activation(self) -> None:
        """Registry drift between the freeze and the cutover is caught by the rescan.

        Activating the strict reader over a row it would immediately fence is not a successful
        migration, so the scan resolves an open row's head the same way the reader will.
        """
        _write_head_registry(self.instance)
        registry = (self.instance / "heads" / "heads.yaml").read_text(encoding="utf-8")
        (self.instance / "heads" / "heads.yaml").write_text(
            registry.replace("  claude-observer:\n    adapter: claude\n    resource: claude-sub\n", ""),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(BackfillError, "post-migration scan refused"):
            self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))
        self.assertFalse(strict_marker_present(self.data_dir))

    def test_an_unreadable_registry_stops_the_cutover_before_any_write(self) -> None:
        (self.instance / "heads" / "heads.yaml").write_text("profiles: []\n", encoding="utf-8")

        with self.assertRaisesRegex(BackfillError, "cannot validate declared heads"):
            self.cutover()

        self.assertEqual(self.metadata_writes(), 0)
        self.assertIsNone(read_inventory(self.data_dir))
        self.assertEqual(self.runtime.checkpoint.calls, 0)

    def test_the_dry_run_names_a_head_that_has_left_the_registry(self) -> None:
        registry = (self.instance / "heads" / "heads.yaml").read_text(encoding="utf-8")
        (self.instance / "heads" / "heads.yaml").write_text(
            registry.replace("  claude-observer:\n    adapter: claude\n    resource: claude-sub\n", ""),
            encoding="utf-8",
        )

        plan = plan_cutover(self.runtime, data_dir=self.data_dir, instance=self.instance)

        self.assertFalse(plan["ok"])
        self.assertEqual(len(plan["refusals"]), 1)
        self.assertIn("claude-observer", plan["refusals"][0])

    def test_a_second_cutover_does_nothing_and_says_so(self) -> None:
        self.cutover()

        again = self.cutover()

        self.assertEqual(again["status"], "already-migrated")

    def test_the_resume_is_the_last_step_and_only_after_a_clean_run(self) -> None:
        with mock.patch("secretary.dispatcher_pause_ops.resume") as lifted:
            result = run_cutover(
                self.runtime, sprint_writer=self.sprint_writer, data_dir=self.data_dir,
                instance=self.instance, now="2026-08-02T00:00:00Z",
            )
        self.assertEqual(lifted.call_count, 1)
        self.assertEqual(result["steps"][-1]["step"], "resume")

    # crash boundaries -------------------------------------------------------
    #
    # One test per point the process can die, each asserting the same two things: the strict
    # reader is not on, and the rerun finishes the cutover with the values the first attempt
    # selected rather than a second set derived from a world that has moved.

    def test_both_checkpoints_are_pushed_past_the_windowed_scheduler(self) -> None:
        """A recovery point that never left the machine is not one.

        The ordinary pusher answers a call inside its 30-minute window by handing back the state
        it was given, without attempting anything, and that stale state still says `pushed`. A
        cutover that handed it the tick's push state would read that as its own push having
        landed. It passes an empty state instead, which is what makes the window not apply.
        """
        pusher = WindowedStubPusher()
        self.runtime.checkpoint_push = pusher
        # The tick pushed five minutes ago, so the window is not due for anyone who asks with it.
        self.state.payload["checkpoint_push"] = {"status": "pushed", "attempted_epoch": 1.0}

        result = self.cutover()

        self.assertEqual(pusher.attempts, 2)
        self.assertEqual(
            [
                step["push"]["status"] for step in result["steps"]
                if step["step"].endswith("-migration-checkpoint")
            ],
            ["pushed", "pushed"],
        )

    def test_a_checkpoint_that_did_not_reach_the_remote_stops_the_cutover(self) -> None:
        for outcome in ({"status": "skipped", "reason": "no remote"}, {"status": "failed"},
                        {"status": "diverged"}, {}):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.runtime.checkpoint_push.result = outcome

                with self.assertRaisesRegex(BackfillError, "was not pushed"):
                    self.cutover()

                self.assertFalse(strict_reader_active(self.data_dir))
                self.assertIsNone(read_inventory(self.data_dir))
                self.assertEqual(self.metadata_writes(), 0)

    def test_a_runtime_without_a_pusher_cannot_run_the_cutover(self) -> None:
        self.runtime.checkpoint_push = None

        with self.assertRaisesRegex(BackfillError, "no checkpoint pusher"):
            self.cutover()

        self.assertFalse(strict_reader_active(self.data_dir))

    def test_a_crash_after_the_inventory_before_any_write_resumes_on_it(self) -> None:
        inventory = persist_inventory(
            self.data_dir,
            build_inventory(
                self.sprint_reader.export(), self.runtime.audit.events(), {OPEN_SPRINT: OPEN_HEAD},
            ),
        )
        self.assertEqual(self.observers_on_board(), {})

        result = self.cutover()

        self.assertEqual(result["digest"], inventory["digest"])
        self.assertEqual(len(self.observers_on_board()), 17)
        self.assertTrue(strict_reader_active(self.data_dir))

    def test_a_crash_between_two_per_ref_writes_resumes_without_writing_twice(self) -> None:
        inventory = persist_inventory(
            self.data_dir,
            build_inventory(
                self.sprint_reader.export(), self.runtime.audit.events(), {OPEN_SPRINT: OPEN_HEAD},
            ),
        )
        real = self.sprint_writer.backfill_observer
        seen: list[str] = []

        def die_after_five(*, reference: str, value: dict, request_id: str):
            if len(seen) >= 5:
                raise RuntimeError("the process died mid-backfill")
            seen.append(reference)
            return real(reference=reference, value=value, request_id=request_id)

        with mock.patch.object(self.sprint_writer, "backfill_observer", die_after_five):
            with self.assertRaises(RuntimeError):
                apply_backfill(self.sprint_writer, self.data_dir, inventory)
        self.assertEqual(len(self.observers_on_board()), 5)
        self.assertFalse(strict_reader_active(self.data_dir))
        partial_writes = self.metadata_writes()

        result = self.cutover()

        self.assertEqual(len(result["rows"]), 17)
        # The five rows already carrying their value are recognised, not rewritten.
        self.assertEqual(self.metadata_writes(), partial_writes + 12)
        self.assertEqual(len(self.observers_on_board()), 17)
        self.assertEqual({entry["ref"] for entry in read_journal(self.data_dir)}, set(
            row["ref"] for row in inventory["rows"]
        ))

    def test_a_crash_after_the_completion_event_is_not_yet_strict(self) -> None:
        """Strict follows the order, not the event alone.

        The completion event is the durable signal a recovered host reads, but on the host running
        the cutover it exists before the post-migration checkpoint has been taken and pushed.
        Strict there would be strict before the recovery point the order requires, so the interval
        is tolerant and the rerun finishes the sequence.
        """
        self.checkpoint.results = [{"status": "ok"}, RuntimeError("host died")]
        with self.assertRaisesRegex(BackfillError, "post-migration checkpoint"):
            self.cutover()

        self.assertTrue(migration_recorded(self.data_dir))
        self.assertTrue(cutover_in_flight(self.data_dir))
        self.assertFalse(strict_marker_present(self.data_dir))
        self.assertFalse(strict_reader_active(self.data_dir))
        checkpoints_before = self.checkpoint.calls

        self.checkpoint.results = [{"status": "ok"}]
        self.checkpoint.calls = 0
        result = self.cutover()

        self.assertGreater(self.checkpoint.calls, 0)
        self.assertNotEqual(result["status"], "already-migrated")
        self.assertTrue(strict_marker_present(self.data_dir))
        self.assertTrue(strict_reader_active(self.data_dir))
        self.assertFalse(cutover_in_flight(self.data_dir))
        self.assertGreater(checkpoints_before, 0)

    def test_the_interval_before_the_post_migration_push_is_tolerant_too(self) -> None:
        self.runtime.checkpoint_push.result = {"status": "failed", "reason": "remote refused"}
        # The pre-migration push has to land, so it is allowed through and only the second fails.
        real = self.runtime.checkpoint_push.push
        calls: list[dict] = []

        def second_push_fails(state: dict) -> dict:
            calls.append(state)
            if len(calls) == 1:
                return {"status": "pushed", "commit": "abc123"}
            return {"status": "failed", "reason": "remote refused"}

        self.runtime.checkpoint_push.push = second_push_fails  # type: ignore[method-assign]
        self.assertIsNotNone(real)

        with self.assertRaisesRegex(BackfillError, "was not pushed"):
            self.cutover()

        self.assertTrue(migration_recorded(self.data_dir))
        self.assertFalse(strict_reader_active(self.data_dir))

    def test_a_crash_after_the_post_migration_checkpoint_resumes_to_the_marker(self) -> None:
        with mock.patch(
            "secretary.observer_backfill.activate_strict_reader",
            side_effect=RuntimeError("host died"),
        ):
            with self.assertRaises(RuntimeError):
                self.cutover()
        self.assertFalse(strict_marker_present(self.data_dir))

        result = self.cutover()

        self.assertTrue(strict_marker_present(self.data_dir))
        self.assertEqual(len(result["rows"]), 17)

    def test_a_crash_after_activation_before_resume_leaves_the_freeze_in_force(self) -> None:
        self.cutover()  # resume=False is exactly "died before the resume ran"

        self.assertTrue(strict_marker_present(self.data_dir))
        self.assertEqual(self.pause.payload["mode"], "freeze")
        with mock.patch("secretary.dispatcher_pause_ops.resume") as lifted:
            again = run_cutover(
                self.runtime, sprint_writer=self.sprint_writer, data_dir=self.data_dir,
                instance=self.instance, now="2026-08-02T00:00:00Z",
            )
        # A finished cutover is not re-run, so the operator lifts the freeze themselves.
        self.assertEqual(again["status"], "already-migrated")
        self.assertEqual(lifted.call_count, 0)

    def test_a_retry_writes_what_the_first_attempt_selected_not_what_the_log_now_says(self) -> None:
        """The journal, not the log, is what a retry reads."""
        inventory = persist_inventory(
            self.data_dir,
            build_inventory(
                self.sprint_reader.export(), self.runtime.audit.events(), {OPEN_SPRINT: OPEN_HEAD},
            ),
        )
        selected = {row["ref"]: row["observer"] for row in inventory["rows"]}
        self.assertEqual(selected["sprint:818"]["profile"], "claude-observer")

        # The world moves between the two attempts: a later relaunch would now win the recovery.
        events = (self.data_dir / "board" / "events.ndjson")
        events.write_text(
            events.read_text(encoding="utf-8")
            + json.dumps({
                "event_id": "evt_818_later", "schema_version": 1,
                "occurred_at": "2026-07-30T00:00:00Z",
                "actor": {"role": "dispatcher", "id": "dispatcher"},
                "kind": "observer_relaunched", "outcome": "success", "task_id": "",
                "ref": "sprint:818",
                "backend": {"kind": "dispatcher", "task_id": None, "revision": "n/a"},
                "request_id": "req-818-later", "payload": {"head": "codex-observer"},
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            recover_observer("sprint:818", self.runtime.audit.events())["profile"],
            "codex-observer",
        )

        result = self.cutover()

        self.assertEqual(result["digest"], inventory["digest"])
        self.assertEqual(self.observers_on_board()["sprint:818"]["profile"], "claude-observer")

    def test_a_dry_run_writes_nothing_and_needs_no_freeze(self) -> None:
        self.pause.payload = {"mode": ""}

        plan = plan_cutover(self.runtime, data_dir=self.data_dir, instance=self.instance)

        self.assertEqual(len(plan["rows"]), 17)
        self.assertFalse(plan["strict_reader_active"])
        self.assertIsNone(read_inventory(self.data_dir))
        self.assertNotIn(
            "sprint_observer",
            self.board.metadata[int(self.board.sprints[0]["id"])],
        )


class MigrationDurabilityTests(unittest.TestCase):
    """The strict state has to survive the boundary `docs/RECOVERY.md` actually promises."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(forget_migration_state)
        forget_migration_state()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.instance = Path(self.tmp.name) / "instance"
        _write_head_registry(self.instance)
        self.board = ObserverBoard()
        self.board.seed_inventory(self.data_dir)
        self.sprint_reader = SprintReader(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]
        self.runtime = StubRuntime(
            sprints=self.sprint_reader,
            audit=TaskAudit(self.data_dir),
            pause=StubPause({"mode": "freeze", "excluded_worker": []}),
            production_state=StubProductionState({
                "observers": {
                    OPEN_SPRINT: ObserverRecord(sprint=OPEN_SPRINT, head=OPEN_HEAD).to_json()
                },
                "records": {},
            }),
            checkpoint=StubCheckpoint([{"status": "ok"}]),
            pusher=StubPusher(),
        )
        run_cutover(
            self.runtime,
            sprint_writer=SprintWriter(self.board, data_dir=self.data_dir),  # type: ignore[arg-type]
            data_dir=self.data_dir, instance=self.instance, now="2026-08-02T00:00:00Z",
            resume=False,
        )

    def recovered_data_dir(self) -> Path:
        """A replacement host: only the checkpoint canon, nothing else from the old machine.

        `docs/RECOVERY.md` calls the host runtime local and non-canonical and lists exactly these
        board entries as what comes back. Anything the old data directory held outside them — the
        strict marker, the migration inventory and journal — is gone by contract.
        """
        recovered = Path(self.tmp.name) / "recovered"
        (recovered / "board").mkdir(parents=True)
        for name in ("cards.ndjson", "sprints.ndjson", "events.ndjson", "export.json"):
            source = self.data_dir / "board" / name
            if source.is_file():
                (recovered / "board" / name).write_text(
                    source.read_text(encoding="utf-8"), encoding="utf-8"
                )
        return recovered

    def test_the_migrated_installation_is_strict(self) -> None:
        self.assertTrue(strict_reader_active(self.data_dir))
        self.assertTrue(migration_recorded(self.data_dir))

    def test_a_replacement_host_comes_back_strict_without_the_marker(self) -> None:
        recovered = self.recovered_data_dir()

        self.assertFalse(strict_marker_present(recovered))
        self.assertFalse((recovered / "sprints" / "observer-migration").exists())
        self.assertTrue(strict_reader_active(recovered))

    def test_a_damaged_latch_does_not_take_strictness_away(self) -> None:
        """The latch is a convenience over a durable fact, never the thing holding it up.

        The inventory is deliberately retained after a successful cutover — a retry reads it
        instead of recomputing provenance — so "an inventory exists" cannot mean "in flight". The
        activation event in the append-only log is what closed the interval, and a local file that
        is lost or corrupted cannot unsay it.
        """
        marker = self.data_dir / "sprints" / "observer-strict.json"
        self.assertTrue(marker.is_file())
        self.assertTrue((self.data_dir / "sprints" / "observer-migration" / "inventory.json").is_file())
        self.assertTrue(strict_reader_active(self.data_dir))

        for damage in ("", "{not json", '{"version": 1, "strict": false}'):
            with self.subTest(damage=damage):
                marker.write_text(damage, encoding="utf-8")
                forget_migration_state(self.data_dir)
                self.assertFalse(strict_marker_present(self.data_dir))
                self.assertFalse(cutover_in_flight(self.data_dir))
                self.assertTrue(strict_reader_active(self.data_dir))

        marker.unlink()
        forget_migration_state(self.data_dir)
        self.assertTrue(strict_reader_active(self.data_dir))

    def test_a_corrupt_row_still_fences_after_the_latch_is_lost(self) -> None:
        """The consequence that matters: no missing field reaches the role default."""
        (self.data_dir / "sprints" / "observer-strict.json").unlink()
        forget_migration_state(self.data_dir)
        row = next(item for item in self.board.sprints if item["reference"] == OPEN_SPRINT)
        self.board.metadata[int(row["id"])].pop("sprint_observer")

        with self.assertRaises(ObserverMetadataError) as raised:
            executable_observer(self.sprint_reader.show(OPEN_SPRINT, include_cards=False))

        self.assertEqual(raised.exception.reason, REASON_MISSING)
        self.assertEqual(scan_rows(self.sprint_reader.export()), [f"{OPEN_SPRINT}: no observer metadata"])

    def test_a_host_recovered_from_a_pre_migration_checkpoint_stays_tolerant(self) -> None:
        pre = Path(self.tmp.name) / "pre"
        (pre / "board").mkdir(parents=True)
        (pre / "board" / "events.ndjson").write_text(
            "".join(
                line + "\n"
                for line in (self.data_dir / "board" / "events.ndjson")
                .read_text(encoding="utf-8").splitlines()
                if "observer_migration_completed" not in line
            ),
            encoding="utf-8",
        )

        self.assertFalse(strict_reader_active(pre))

    def test_half_a_backfill_in_the_log_does_not_read_as_migrated(self) -> None:
        """The completion event, never a single row's write, is what turns the reader strict."""
        partial = Path(self.tmp.name) / "partial"
        (partial / "board").mkdir(parents=True)
        lines = (self.data_dir / "board" / "events.ndjson").read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if "observer_migration_completed" not in line]
        self.assertTrue(any("observer_backfilled" in line for line in kept))
        (partial / "board" / "events.ndjson").write_text(
            "".join(line + "\n" for line in kept), encoding="utf-8"
        )

        self.assertFalse(strict_reader_active(partial))


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
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
        )

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

    def test_a_fence_that_cannot_stage_its_outcome_stops_the_whole_tick(self) -> None:
        """An empty fence is not "nothing may be decided", it is "everything may move".

        The staging write is a separate path from the production-state guard, so a full volume or
        a permissions change can take the audit while the state stays writable. The tick has to end
        there rather than fall back to a fence that permits every card.
        """
        self.go_strict()
        self.declare("{not json")
        self.board.metadata[12]["sprint_ref"] = "sprint:1"
        self.board.tasks[0]["column_id"] = 3

        with mock.patch(
            "secretary.dispatcher_observer_fence.stage_event", side_effect=OSError("disk full"),
        ):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["action"], "observer-fence-unavailable")
        self.assertEqual(result["actions"], [])
        self.assertIn("disk full", result["reason"])
        self.assertEqual(result["errors"][0]["code"], "unexpected_error")
        # Nothing ran after the fence: no advance, no claim, no observer launch, no board move.
        self.assertEqual([method for method, _ in self.board.calls if method == "moveTaskPosition"], [])
        self.assertEqual(self.host.observers, [])
        telemetry = self.runtime.production_state.load()["tick_telemetry"]
        self.assertFalse(telemetry["last"]["healthy"])

    def test_one_failed_card_list_does_not_let_reconciliation_settle_a_fenced_record(self) -> None:
        """Backend reads fail independently, so the fence's own read can fail and the rest recover.

        An empty ref set there would hand reconciliation a fenced project's record as an orphan and
        it would stop its heads and remove it — the exact mutation the fence exists to prevent.
        """
        self.go_strict()
        self.declare("{not json")
        # An unlinked card of the reserved project, out of the active cycle, with a live record:
        # what reconciliation settles when nothing tells it the card is fenced.
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
        real_list = self.runtime.reader.list
        failures = [TaskError("backend_error", "transient", 1)]

        def list_once_broken(*args, **kwargs):
            if failures:
                raise failures.pop()
            return real_list(*args, **kwargs)

        with mock.patch.object(self.runtime.reader, "list", side_effect=list_once_broken):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["action"], "observer-fence-unavailable")
        self.assertIn(
            "secretary-510-neighbor", self.runtime.production_state.load()["records"]
        )
        self.assertEqual(
            [action for action in result["actions"] if action.get("step") == "production-reconcile"],
            [],
        )

    def test_reconciliation_classifies_the_card_it_reads_against_the_fence(self) -> None:
        """The second line: a card absent from the fence's inventory is still fenced by its sprint."""
        self.go_strict()
        self.declare("{not json")
        self.board.tasks[1]["column_id"] = 5
        self.board.metadata[13]["sprint_ref"] = "sprint:1"
        payload = self.runtime.production_state.load()
        payload["records"] = {
            "secretary-510-neighbor": {
                "worker": "w1", "workspace": "/tmp/w1", "handle": "term_w", "head": "codex",
                "review_head": "codex-reviewer", "attempt_id": "att-1", "comment_baseline": 0,
                "review_baseline": 0, "state": "adopted", "claimed_at": time.time(),
            }
        }
        self.runtime.production_state.save(payload)
        fence = observer_fence(self.runtime, self.runtime.production_state.load())
        fence["refs"] = set()  # the inventory misses it; the sprint link still holds

        outcomes = _reconcile_production(
            self.runtime, self.runtime.production_state.records(payload), payload,
            set(), fenced_refs=set(), fence=fence,
        )

        self.assertEqual(outcomes, [])

    def test_a_launched_head_that_has_not_written_its_pid_keeps_the_fence_up(self) -> None:
        """A pid that is merely not written yet is not confirmed adoption.

        The lifecycle grace window reads it as alive so a head that has just started is not
        relaunched. Releasing another role's cards on that is different: the terminal may have
        died before it ever reached the observer prompt.
        """
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        self.runtime.production_tick()
        record = load_observers(self.runtime.production_state.load())["sprint:1"]
        Path(record.pid_file).unlink()
        self.assertEqual(
            observer_alive(record), {"alive": True, "reason": "pid-not-written-yet", "pid_known": False}
        )

        fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_NOT_ADOPTED)

    def test_a_record_that_names_no_head_is_a_mismatch(self) -> None:
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        put_observers(payload, {
            "sprint:1": ObserverRecord(
                sprint="sprint:1", head="", handle="term_x", state="running",
                launches=1, launched_at=time.time(),
            )
        })
        self.runtime.production_state.save(payload)

        fence = observer_fence(self.runtime, self.runtime.production_state.load())

        self.assertEqual(fence["outcomes"][0]["observer_reason"], "observer_head_mismatch")

    def test_the_card_of_an_unadopted_observer_does_not_advance(self) -> None:
        self.go_strict()
        actions = self._tick_twice_with_an_active_card(
            encode_observer(head_choice("claude-observer"))
        )
        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "advance"],
            ["waiting-worker-report"],
        )
        record = load_observers(self.runtime.production_state.load())["sprint:1"]
        Path(record.pid_file).unlink()

        blind = self.runtime.production_tick()["actions"]

        self.assertEqual([action for action in blind if action["step"] == "advance"], [])
        self.assertEqual(
            [action["observer_reason"] for action in blind if action["step"] == "observer-fence"],
            [REASON_NOT_ADOPTED],
        )

    def test_a_fenced_sprint_makes_the_tick_report_unhealthy(self) -> None:
        """A stopped project with a healthy-looking tick is how the last outage stayed invisible."""
        self.go_strict()
        self.declare(encode_observer(head_choice("retired-observer")))

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "degraded")
        telemetry = self.runtime.production_state.load()["tick_telemetry"]
        self.assertFalse(telemetry["last"]["healthy"])
        self.assertIn(
            "sprint:1", [row["ref"] for row in telemetry["last"]["degradations"]],
        )

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

    def test_an_unreadable_sprint_board_fences_what_it_last_saw(self) -> None:
        """The Pipeline board can answer while the sprint board cannot: fail closed, not open."""
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        payload = self.runtime.production_state.load()
        observer_fence(self.runtime, payload)  # one sighted pass, to take the snapshot
        self.runtime.production_state.save(payload)
        self.board.metadata[13]["project"] = "other"

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            fence = self.fence()

        self.assertEqual(fence["sprints"], {"sprint:1"})
        self.assertEqual(fence["projects"], {"secretary"})
        self.assertEqual(fence["outcomes"][0]["action"], "sprint_board_unavailable")
        self.assertEqual(fence["outcomes"][0]["status"], "critical")
        # Project-local even when blind: the reserved project stops, another project does not.
        self.assertTrue(fenced_task(fence, {"ref": "secretary-510-pilot", "project": "secretary"}))
        self.assertFalse(fenced_task(fence, {"ref": "secretary-510-neighbor", "project": "other"}))

    def test_a_blind_tick_still_fences_a_sprint_it_never_saw(self) -> None:
        """A sprint opened since the last snapshot is caught through its cards' own link."""
        self.go_strict()
        self.declare(encode_observer(head_choice("claude-observer")))
        self.board.metadata[12]["sprint_ref"] = "sprint:1"

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            fence = self.fence()

        self.assertIn("secretary-510-pilot", fence["refs"])
        self.assertTrue(fenced_task(fence, {"ref": "secretary-510-pilot", "project": "secretary"}))
        self.assertFalse(fenced_task(fence, {"ref": "secretary-510-neighbor", "project": "other"}))

    def test_an_unreadable_sprint_board_does_not_advance_the_sprints_cards(self) -> None:
        self.go_strict()
        actions = self._tick_twice_with_an_active_card(
            encode_observer(head_choice("claude-observer"))
        )
        self.assertEqual(
            [action["action"] for action in actions if action["step"] == "advance"],
            ["waiting-worker-report"],
        )

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            blind = self.runtime.production_tick()["actions"]

        self.assertEqual([action for action in blind if action["step"] == "advance"], [])
        self.assertEqual(
            [action["action"] for action in blind if action["step"] == "observer-fence"],
            ["sprint_board_unavailable"],
        )

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


class TwoOpenSprintFenceTests(ObserverFenceFixture, TwoOpenSprintAdmission):
    """The fence with two sprints open at once: it holds one sprint's work, not the tick's.

    The pair is opened through `SprintWriter.create` under the pilot setting, so the rows the
    fence reads are rows admission produced. A broken declaration is then written over the
    persisted value of an already-open sprint, which is how a live installation reaches one: no
    create would admit it.
    """

    HELD = "the sprint holding this project has no working declared observer"

    def open_pair(self, *, observer=None, second_observer=None) -> None:
        self.go_strict()
        self.sprint_writer = self.admit_two_open_sprints(
            observer=observer or head_choice("claude-observer"),
            second_observer=second_observer,
        )
        self.link_pair_cards()

    def in_flight_pair(self) -> None:
        """The pair with its head adopted and one card of each sprint in flight.

        The first tick fences `sprint:1` on its unlaunched head, so the card claimed there is
        the other sprint's; the second tick, with the head adopted, claims `sprint:1`'s own.
        """
        self.open_pair()
        self.assertEqual(self._claim(self.runtime.production_tick()), "secretary-510-neighbor")
        self.assertEqual(self._claim(self.runtime.production_tick()), "secretary-510-pilot")

    def _fence_steps(self, result: dict) -> list[dict]:
        return [action for action in result["actions"] if action["step"] == "observer-fence"]

    def _claim(self, result: dict) -> str:
        claims = [action for action in result["actions"] if action["step"] == "claim"]
        return claims[0]["pilot_ref"] if claims else ""

    def _skipped(self, result: dict) -> list[dict]:
        for action in result["actions"]:
            if action.get("step") in {"claim", "production-claim"}:
                return list(action.get("skipped_ready") or [])
        return []

    def assert_only_the_first_sprint_is_held(self, result: dict, reason: str) -> None:
        """Its cards neither advance nor are claimed; the other sprint's do both."""
        self.assertEqual(
            [(action["sprint"], action["observer_reason"]) for action in self._fence_steps(result)],
            [(self.FIRST, reason)],
        )
        self.assertEqual(
            [action["pilot_ref"] for action in result["actions"] if action["step"] == "advance"],
            ["secretary-510-neighbor"],
        )
        self.assertEqual(self._claim(result), "third-1")
        self.assertEqual(self._skipped(result), [{"ref": "fourth-1", "reason": self.HELD}])
        self.assertEqual(self.runtime.reader.show("fourth-1")["state"], "ready")
        self.assertEqual(self.runtime.reader.show("third-1")["state"], "in_progress")
        self.assertEqual(self.runtime.reader.show("secretary-510-pilot")["state"], "in_progress")

    def test_a_dead_declared_head_holds_its_own_sprint_and_leaves_the_other_running(self) -> None:
        self.in_flight_pair()
        record = load_observers(self.runtime.production_state.load())[self.FIRST]
        Path(record.pid_file).write_text(str(DEAD_PID), encoding="utf-8")

        # Read before the tick: the tick relaunches the dead head, so this is the state the
        # cards are judged against while it is still dead.
        fence = self.fence()
        result = self.runtime.production_tick()

        self.assert_only_the_first_sprint_is_held(result, REASON_DEAD)
        self.assertEqual(fence["sprints"], {self.FIRST})
        self.assertEqual(fence["projects"], {"secretary", "fourth"})
        self.assertEqual(fence["refs"], {"secretary-510-pilot", "fourth-1"})
        self.assertTrue(fenced_task(
            fence, {"ref": "secretary-510-pilot", "sprint": self.FIRST, "project": "secretary"},
        ))
        self.assertFalse(fenced_task(
            fence, {"ref": "secretary-510-neighbor", "sprint": self.SECOND, "project": "other"},
        ))

    def test_an_unresolvable_profile_holds_its_own_sprint_only(self) -> None:
        """The declared head left the registry after the sprint was opened on it."""
        self.in_flight_pair()
        self.rewrite_observer(self.FIRST, encode_observer(head_choice("retired-observer")))

        result = self.runtime.production_tick()

        self.assert_only_the_first_sprint_is_held(result, REASON_UNKNOWN_PROFILE)
        self.assertEqual(
            [
                action["action"] for action in result["actions"]
                if action["step"] == "observer-reconcile" and action.get("sprint") == self.FIRST
            ],
            ["observer-declaration-invalid"],
        )

    def test_a_corrupt_declaration_holds_its_own_sprint_only(self) -> None:
        self.in_flight_pair()
        self.rewrite_observer(self.FIRST, "{not json")

        result = self.runtime.production_tick()

        self.assert_only_the_first_sprint_is_held(result, REASON_MALFORMED)

    def test_the_fence_state_and_its_durable_events_name_the_fenced_sprint_only(self) -> None:
        self.in_flight_pair()
        self.rewrite_observer(self.FIRST, "{not json")

        self.runtime.production_tick()

        payload = self.runtime.production_state.load()
        self.assertEqual(set(payload["observer_fence"]), {self.FIRST})
        self.assertEqual(payload["observer_fence"][self.FIRST]["reason"], REASON_MALFORMED)
        raised = [
            event for event in self.runtime.audit.events()
            if event["kind"] == "observer_fence_raised"
        ]
        # One sprint, however many reasons it has been fenced for: the first tick fenced it on
        # its unlaunched head, this one on the declaration that was broken since.
        self.assertEqual({event["ref"] for event in raised}, {self.FIRST})
        self.assertEqual(raised[-1]["payload"]["observer_reason"], REASON_MALFORMED)

    def test_the_snapshot_holds_each_open_sprints_own_reservations(self) -> None:
        """Keyed by sprint, so a blind pass fences each sprint on what that sprint holds.

        Every open sprint is in it, fenced or not: the snapshot is what a pass that cannot
        read the sprint board falls back on, and a pass that could not check any declaration
        fences all of them. What names the fenced sprint alone is the fence state above.
        """
        self.open_pair()
        self.rewrite_observer(self.FIRST, "{not json")

        self.runtime.production_tick()

        self.assertEqual(
            self.runtime.production_state.load()["observer_fence_snapshot"],
            {self.FIRST: ["fourth", "secretary"], self.SECOND: ["other", "third"]},
        )

    def test_an_unreadable_sprint_board_fences_both_sprints_by_their_own_reservations(self) -> None:
        """The blind path is installation-wide by design, and still project-local per sprint."""
        self.open_pair()
        payload = self.runtime.production_state.load()
        observer_fence(self.runtime, payload)  # one sighted pass, to take the snapshot
        self.runtime.production_state.save(payload)

        with mock.patch.object(
            self.runtime.sprints, "list", side_effect=TaskError("backend_error", "down", 1)
        ):
            fence = self.fence()

        self.assertEqual(fence["sprints"], {self.FIRST, self.SECOND})
        self.assertEqual(fence["projects"], {"secretary", "fourth", "other", "third"})
        self.assertEqual(fence["outcomes"][0]["action"], "sprint_board_unavailable")
        # The blind outcome names the sprints, which is what an operator has to act on. It
        # used to name the fenced cards there, because the linked-card index is keyed by card.
        self.assertEqual(fence["outcomes"][0]["sprints"], [self.FIRST, self.SECOND])
        self.assertFalse(fenced_task(fence, {"ref": "loose-1", "sprint": "", "project": "loose"}))

    def _orphan_records(self) -> None:
        """Both cards out of the active cycle with a live record behind them."""
        self.board.tasks[0]["column_id"] = 5
        self.board.tasks[1]["column_id"] = 5
        payload = self.runtime.production_state.load()
        payload["records"] = {
            reference: {
                "worker": "w", "workspace": "/tmp/w", "handle": "term_" + reference, "head": "codex",
                "review_head": "codex-reviewer", "attempt_id": "att-" + reference,
                "comment_baseline": 0, "review_baseline": 0, "state": "adopted",
                "claimed_at": time.time(),
            }
            for reference in ("secretary-510-pilot", "secretary-510-neighbor")
        }
        self.runtime.production_state.save(payload)

    def test_reconciliation_settles_the_unfenced_sprints_record_and_holds_the_fenced_one(self) -> None:
        """Both records are orphaned by the same tick; only the fenced sprint's survives it."""
        self.open_pair()
        self.rewrite_observer(self.FIRST, "{not json")
        self._orphan_records()

        result = self.runtime.production_tick()

        reconciled = [
            action for action in result["actions"] if action["step"] == "production-reconcile"
        ]
        self.assertEqual(
            [(action["ref"], action["action"]) for action in reconciled],
            [("secretary-510-neighbor", "record-removed")],
        )
        records = set(self.runtime.production_state.load()["records"])
        self.assertIn("secretary-510-pilot", records)
        self.assertNotIn("secretary-510-neighbor", records)

    def test_the_same_pair_with_no_fence_settles_both_records(self) -> None:
        """The control: without the fence the pass above would have removed both."""
        self.open_pair(observer=none_choice())
        self._orphan_records()

        result = self.runtime.production_tick()

        self.assertEqual(
            sorted(
                action["ref"] for action in result["actions"]
                if action["step"] == "production-reconcile"
            ),
            ["secretary-510-neighbor", "secretary-510-pilot"],
        )
        records = set(self.runtime.production_state.load()["records"])
        self.assertNotIn("secretary-510-pilot", records)
        self.assertNotIn("secretary-510-neighbor", records)

    def test_one_sprints_broken_head_does_not_fence_the_other_sprints_head(self) -> None:
        """Both sprints declaring a head: the fence is still one sprint's, not the pair's.

        The scenario admission used to refuse. Each sprint is judged on its own declaration and
        its own record, so the sprint whose head is fine keeps its projects and its cards.
        """
        self.open_pair(second_observer=head_choice("codex-observer"))
        self.runtime.production_tick()  # both heads launched and adopted
        self.rewrite_observer(self.FIRST, "{not json")

        fence = self.fence()

        self.assertEqual(fence["sprints"], {self.FIRST})
        self.assertEqual(fence["projects"], {"secretary", "fourth"})
        self.assertEqual(fence["refs"], {"secretary-510-pilot", "fourth-1"})
        self.assertEqual(fence["outcomes"][0]["observer_reason"], REASON_MALFORMED)
        self.assertFalse(fenced_task(
            fence, {"ref": "secretary-510-neighbor", "sprint": self.SECOND, "project": "other"},
        ))
        self.assertFalse(fenced_task(
            fence, {"ref": "third-1", "sprint": self.SECOND, "project": "third"},
        ))
        # The second sprint's head is not what the fence was about, and it is still alive.
        self.assertTrue(observer_alive(load_observers(self.runtime.production_state.load())[
            self.SECOND
        ])["alive"])


if __name__ == "__main__":
    unittest.main()
