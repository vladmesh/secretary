from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from secretary import tasks
from secretary.board.card_transitions import CARD_TRANSITIONS
from secretary.board.done_retention import close_old_done
from secretary.board.host import TransitionRequest
from secretary.board.kanboard import KanboardBoardHost
from secretary.board.models import Actor, CardState, EntityKind, Event, RelatedRefs
from secretary.board.steward_reports import StewardReportBoard, StewardSignalBoard
from secretary.board.transitions import TRANSITIONS, transition_for
from secretary.board_transport import BoardTransport
from secretary.cli import main
from secretary.data import export_board
from secretary.dispatcher_state import claim_mismatch
from secretary.routing_journal import (
    HeadRun,
    attempts,
    head_run_from_profile,
    launched_head_run_snapshot,
    routing_payload,
)
from secretary.sprints import refresh_active_sprint_projects
from secretary.tasks import (
    ArtifactOwnershipTaskError,
    _STATE_BY_COLUMN,
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    is_significant_observer_event,
    specification_revision,
    standing_decision,
)
from tests.fakes.tasks import FakeKanboard, WriteKanboard
from tests.observer_identity import as_observer, bind_observer, unbound_observer
from triggered_agents.runtime.head import HeadRun as LifecycleHeadRun, HeadSpec, TaskRef

CARD_STATES = ("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done")


@contextlib.contextmanager
def open_sprint(ref: str = "sprint:test", project: str = "secretary"):
    """Stand in for the open sprint every Ready card needs.

    These tests are about the create and audit path; the sprint link is a precondition of a
    create on the board, and the guard behind it is covered in tests/test_sprints.py.

    The caller is bound to the same sprint, because the observer creating a card linked to it is
    that sprint's own head; an unbound caller is refused before the create path is reached.
    """
    sprint = {"ref": ref, "status": "open", "repositories": [project], "reservations": [project]}
    with mock.patch("secretary.sprints.SprintReader.show", return_value=sprint), as_observer(ref):
        yield ref


# The sprint the assessment fixture's card belongs to.
SPRINT = "sprint:1031"


class FakeSprintReader:
    """The sprint board as the task writer's reservation guard reads it: one open sprint."""

    def __init__(self, sprint: dict[str, object]) -> None:
        self.sprint = sprint

    def list(self, **kwargs: object) -> list[dict[str, object]]:
        return [self.sprint]

    def show(self, reference: str, **kwargs: object) -> dict[str, object]:
        if reference != self.sprint["ref"]:
            raise TaskError("not_found", f"no sprint {reference}", 3)
        return self.sprint


class TaskReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeKanboard()
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]

    def test_list_normalizes_and_filters_deterministically(self) -> None:
        result = self.reader.list(states={"ready"}, project="secretary")

        self.assertEqual([task["ref"] for task in result], ["secretary-468"])
        task = result[0]
        self.assertEqual(task["id"], "task_kanboard_12")
        self.assertEqual(task["claim"], {"worker": "codex-terra", "claimed_at": None})
        self.assertEqual(task["retry"], {"same": 2, "switched": 0, "heads": ["codex-terra", "claude-opus"]})
        self.assertEqual(task["routing"]["complexity"], "standard")
        self.assertEqual(task["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(task["extensions"]["kanboard"], {"steward_report": "1", "swimlane": "Secretary"})
        self.assertNotIn("comments", task)

    def test_show_preserves_comments_and_legacy_defaults(self) -> None:
        task = self.reader.show("old-1")

        self.assertEqual(task["project"], "")
        self.assertEqual(task["type"], "")
        self.assertIsNone(task["blocked_by"])
        self.assertEqual(task["position"], 0)
        self.assertEqual(task["routing"]["family_preference"], "auto")
        self.assertEqual(task["comments"][0]["marker"], "report:done")
        self.assertEqual(task["comments"][0]["created_at"], "2024-07-03T09:47:00Z")

    def test_export_includes_archived_cards_in_one_metadata_comments_batch(self) -> None:
        self.client.tasks[1]["is_active"] = 0
        exported = self.reader.export()

        self.assertEqual([card["reference"] for card in exported], ["secretary-468", "old-1"])
        self.assertEqual(len(self.client.batch_calls), 1)
        self.assertEqual(
            self.client.batch_calls[0],
            [
                ("getTaskMetadata", {"task_id": 12}),
                ("getAllComments", {"task_id": 12}),
                ("getTaskMetadata", {"task_id": 13}),
                ("getAllComments", {"task_id": 13}),
            ],
        )
        self.assertFalse(exported[0]["closed"])
        self.assertTrue(exported[1]["closed"])
        self.assertEqual(
            exported[0]["comments"], [{"ts": "1720000020", "text": "[report:done]\nReady for review"}]
        )

    def test_show_reports_missing_task(self) -> None:
        with self.assertRaisesRegex(TaskError, "not found") as raised:
            self.reader.show("missing")
        self.assertEqual(raised.exception.code, "not_found")

    def test_show_prefers_live_duplicate_reference(self) -> None:
        archived = {
            "id": 14,
            "reference": "secretary-784",
            "title": "Archived",
            "column_id": 6,
            "position": 1,
            "swimlane_id": 4,
            "is_active": 0,
        }
        live = {
            "id": 15,
            "reference": "secretary-784",
            "title": "Live",
            "column_id": 2,
            "position": 1,
            "swimlane_id": 4,
            "is_active": 1,
        }
        self.client.tasks.extend([archived, live])
        self.client.metadata.update({14: {}, 15: {"project": "secretary"}})

        task = self.reader.show("secretary-784")

        self.assertEqual(task["id"], "task_kanboard_15")
        self.assertEqual(task["title"], "Live")
        self.assertEqual(
            [params["status_id"] for method, params in self.client.calls if method == "getAllTasks"],
            [1],
        )

    def test_show_returns_archived_reference_when_no_live_duplicate_exists(self) -> None:
        self.client.tasks[1]["is_active"] = 0

        task = self.reader.show("old-1")

        self.assertEqual(task["id"], "task_kanboard_13")

    def test_unknown_column_is_backend_error(self) -> None:
        self.client.tasks[0]["column_id"] = 999
        with self.assertRaisesRegex(TaskError, "schema") as raised:
            self.reader.list()
        self.assertEqual(raised.exception.code, "backend_error")

    def test_steward_report_read_is_bounded_and_exposes_no_backend_row(self) -> None:
        client = WriteKanboard()
        client.tasks[0]["column_id"] = 3
        client.tasks.append(
            {
                "id": 14,
                "reference": "secretary-469",
                "title": "not a report",
                "column_id": 3,
                "position": 2,
                "swimlane_id": 4,
                "date_moved": "1720000400",
            }
        )
        client.metadata[14] = {"project": "secretary", "task_type": "research"}
        reader = TaskReader(client)  # type: ignore[arg-type]

        reports = reader.steward_reports_in_progress("secretary")

        self.assertEqual(
            reports,
            [{"reference": "secretary-468", "date_moved": None, "steward_report": "1"}],
        )
        self.assertEqual(len(client.batch_calls), 1)
        self.assertEqual(
            client.batch_calls[0],
            [("getTaskMetadata", {"task_id": 12}), ("getTaskMetadata", {"task_id": 14})],
        )

    def test_steward_signal_cards_are_bounded_normalized_and_filtered(self) -> None:
        cards = self.reader.steward_signal_cards(states={"ready"}, project="secretary")

        self.assertEqual(
            cards,
            [
                {
                    "reference": "secretary-468",
                    "state": "ready",
                    "column": "Ready",
                    "project": "secretary",
                    "date_moved": None,
                    "steward_report": "1",
                }
            ],
        )
        self.assertEqual(len(self.client.batch_calls), 1)
        self.assertEqual(
            self.client.batch_calls[0],
            [("getTaskMetadata", {"task_id": 12}), ("getTaskMetadata", {"task_id": 13})],
        )
        self.assertEqual(set(cards[0]), {"reference", "state", "column", "project", "date_moved", "steward_report"})
        self.assertIsInstance(StewardSignalBoard(self.reader).active_cards(states={"ready"}), list)

    def test_steward_signal_cards_reject_invalid_backend_shapes(self) -> None:
        with self.assertRaisesRegex(TaskError, "unknown task states") as raised:
            self.reader.steward_signal_cards(states={"not-a-state"})
        self.assertEqual(raised.exception.code, "validation")
        original_call = self.client.call
        with (
            mock.patch.object(
                self.client,
                "call",
                side_effect=lambda method, **params: ["not-a-card"]
                if method == "getAllTasks"
                else original_call(method, **params),
            ),
            self.assertRaisesRegex(TaskError, "invalid task list"),
        ):
            self.reader.steward_signal_cards()
        self.client.metadata[12] = "not-a-map"  # type: ignore[assignment]
        with self.assertRaisesRegex(TaskError, "invalid task metadata"):
            self.reader.steward_signal_cards()


class TaskCliTests(unittest.TestCase):
    def test_backend_error_never_echoes_credentials(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "KANBOARD_URL": "https://board.invalid/token",
                    "KANBOARD_API_USER": "user",
                    "KANBOARD_API_TOKEN": "super-secret",
                },
                clear=False,
            ),
            mock.patch("secretary.tasks.urllib.request.urlopen", side_effect=OSError("super-secret")),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(["task", "list"])

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")
        self.assertNotIn("super-secret", errors.getvalue())

    def test_missing_runtime_configuration_is_json_error(self) -> None:
        # The instance is named explicitly and points at an empty directory. Clearing the
        # environment is not enough on its own: `DEFAULT_INSTANCE` is `Path.home()/secretary-instance`
        # resolved at import, so on the appliance host itself an unnamed run resolves the live
        # installation and reads the production board.
        output, errors = io.StringIO(), io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(["task", "show", "--ref", "secretary-468", "--instance", tmp])

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")

    def test_reads_are_bound_to_the_named_installation(self) -> None:
        """`task list`/`task show` take an instance like every write command does.

        They used to take none at all, so `_instance` fell through to the home default and no
        flag or variable could move them: a process bound to one installation read another's
        board. On the appliance host that other board is production (secretary-1026's class of
        accident, arriving through the home default rather than through ambient credentials).
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {}, clear=True):
                for argv in (
                    ["task", "list", "--instance", tmp],
                    ["task", "show", "--ref", "secretary-468", "--instance", tmp],
                ):
                    errors = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                        code = main(argv)
                    self.assertEqual(code, 1, argv)
                    self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")

            # The variable is the same source the write commands already honour.
            with mock.patch.dict("os.environ", {"SECRETARY_INSTANCE": tmp}, clear=True):
                errors = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                    code = main(["task", "list"])

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")

    def test_create_rejects_codex_mode_for_non_codex_head_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "heads").mkdir()
            (root / "instance.yaml").write_text(
                "version: 1\nname: test\ndata_dir: /tmp/data\n", encoding="utf-8"
            )
            (root / "heads" / "heads.yaml").write_text(
                "profiles:\n  claude-opus:\n    adapter: claude\nrole_defaults:\n  new_card: claude-opus",
                encoding="utf-8",
            )
            output, errors = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict("os.environ", {}, clear=True),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = main(
                    [
                        "task",
                        "create",
                        "--role",
                        "po",
                        "--instance",
                        str(root),
                        "--project",
                        "secretary",
                        "--type",
                        "code",
                        "--title",
                        "T",
                        "--head",
                        "claude-opus",
                        "--codex-mode",
                        "tui",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual(error["code"], "validation")
        self.assertIn("requires a Codex worker head", error["message"])

    def test_create_rejects_codex_mode_exec_before_the_registry_is_even_read(self) -> None:
        """`--codex-mode exec` names a launch shape the product removed.

        It is refused with that reason, before the instance registry is opened and long before any
        board call: the CLI must never accept a mode that would then have to be silently launched
        as something else. The instance path here does not exist, so reaching the registry read at
        all would fail with a different error.
        """
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "create",
                    "--role",
                    "po",
                    "--instance",
                    "/nonexistent-instance",
                    "--project",
                    "secretary",
                    "--type",
                    "code",
                    "--title",
                    "T",
                    "--codex-mode",
                    "exec",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual(error["code"], "validation")
        self.assertIn("interactive TUI only", error["message"])

    def test_archive_cli_reads_reason_file_and_closes_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reason = root / "reason.md"
            reason.write_text("backlog cleanup\n", encoding="utf-8")
            client = WriteKanboard()
            client.metadata[12]["claim"] = ""
            output, errors = io.StringIO(), io.StringIO()
            with (
                mock.patch("secretary.task_commands.KanboardClient.for_instance", return_value=client),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = main(
                    [
                        "task",
                        "archive",
                        "--role",
                        "po",
                        "--ref",
                        "secretary-468",
                        "--data-dir",
                        str(data_dir),
                        "--reason-file",
                        str(reason),
                        "--request-id",
                        "archive-cli",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["action"], "archived")
        self.assertEqual(client.tasks[0]["is_active"], 0)


class KanboardClientTests(unittest.TestCase):
    def test_rpc_error_is_sanitized(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"error":{"message":"super-secret"}}'
        response.__enter__.return_value = response
        with mock.patch("secretary.tasks.urllib.request.urlopen", return_value=response):
            client = KanboardClient(
                BoardTransport("https://board.invalid", "user", "super-secret"),
                Path.cwd(),
            )
            with self.assertRaises(TaskError) as raised:
                client.call("getAllTasks", project_id=1)
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertNotIn("super-secret", raised.exception.message)


class TaskWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.client.instance_dir = Path(self.tmpdir.name)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_forbidden_role_does_not_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.report(role="reviewer", actor="r", reference="secretary-468", kind="done", body="")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertEqual(self.client.calls, [])

    def test_stale_transition_does_not_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(role="po", actor="p", reference="secretary-468", target="ready", reason="")
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))

    def test_generic_create_keeps_in_progress_closed_to_steward_reports(self) -> None:
        self.assertNotIn("steward_report", inspect.signature(self.writer.create).parameters)
        with self.assertRaisesRegex(TaskError, "only a steward report") as raised:
            self.writer.create(
                role="steward",
                actor="dispatch",
                project="secretary",
                task_type="research",
                title="not an accounting artifact",
                target="in_progress",
                slug="not-a-report",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "createTask" for call in self.client.calls))

    def test_steward_report_create_is_audited_directly_in_progress_and_replays(self) -> None:
        self.client.tasks.append(
            {
                "id": 14,
                "reference": "secretary-700",
                "title": "Archived high-water mark",
                "column_id": 6,
                "position": 1,
                "swimlane_id": 4,
                "is_active": 0,
            }
        )
        self.client.metadata[14] = {}
        self.client.comments[14] = []

        result = self.writer.create_steward_report(
            actor="dispatch",
            project="secretary",
            title="steward: hourly sweep",
            slug="steward-sweep-20260830-120000",
            request_id="steward-report-create",
        )

        self.assertEqual(result["task"]["ref"], "secretary-701")
        self.assertEqual(result["task"]["state"], "in_progress")
        task_id = int(str(result["task"]["id"]).removeprefix("task_kanboard_"))
        self.assertEqual(
            self.client.metadata[task_id],
            {
                "record_type": "task",
                "task_type": "research",
                "project": "secretary",
                "complexity": "standard",
                "family_preference": "auto",
                "slug": "steward-sweep-20260830-120000",
                "claim": "steward-sweep-20260830-120000",
                "steward_report": "1",
            },
        )
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))
        event = self.writer.audit.committed_event("steward-report-create")
        assert event is not None
        self.assertTrue(event["payload"]["steward_report"])

        replay = self.writer.create_steward_report(
            actor="dispatch",
            project="secretary",
            title="steward: hourly sweep",
            slug="steward-sweep-20260830-120000",
            request_id="steward-report-create",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)

    def test_steward_report_pending_metadata_recovers_without_duplicate_create(self) -> None:
        self.client.fail_metadata = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.create_steward_report(
                actor="dispatch",
                project="secretary",
                title="steward: hourly sweep",
                slug="steward-sweep-20260830-120001",
                request_id="steward-report-pending",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)

        self.client.fail_metadata = False
        recovered = self.writer.create_steward_report(
            actor="dispatch",
            project="secretary",
            title="steward: hourly sweep",
            slug="steward-sweep-20260830-120001",
            request_id="steward-report-pending",
        )
        self.assertTrue(recovered["replayed"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)

    def test_steward_cannot_close_an_ordinary_in_progress_card(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.client.metadata[12].pop("steward_report")
        with self.assertRaisesRegex(TaskError, "only for its own report") as raised:
            self.writer.move(
                role="steward",
                actor="dispatch",
                reference="secretary-468",
                target="done",
                reason="close",
                request_id="ordinary-steward-close",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))

    def test_steward_can_close_its_in_progress_report(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        result = self.writer.move(
            role="steward",
            actor="dispatch",
            reference="secretary-468",
            target="done",
            reason="sweep complete",
            request_id="report-steward-close",
        )
        self.assertEqual(result["task"]["state"], "done")

    def test_steward_report_adapter_is_structural_task_reader_writer_composition(self) -> None:
        board = StewardReportBoard(self.writer.reader, self.writer, actor="dispatch")
        reference = board.create_report(
            project="secretary",
            title="steward: hourly sweep",
            slug="steward-sweep-20260830-120002",
        )

        self.assertEqual(board.in_progress_reports(project="secretary")[0]["reference"], reference)
        board.move_report(reference=reference, target="done", reason="sweep complete")
        self.assertEqual(self.writer.reader.show(reference)["state"], "done")

    def test_kanboard_host_executes_every_declared_card_edge_through_the_typed_canon(self) -> None:
        columns = {
            state: identifier
            for identifier, title in self.writer.reader._board()[1].items()
            if (state := _STATE_BY_COLUMN.get(title))
        }
        host = self.writer.board_host
        self.client.tasks[1]["swimlane_id"] = 0
        for index, declaration in enumerate(TRANSITIONS[EntityKind.CARD].values()):
            self.client.tasks[0]["column_id"] = columns[declaration.source.value]
            result = host.transition(
                TransitionRequest(
                    EntityKind.CARD,
                    "secretary-468",
                    declaration.target,
                    Actor("po", "operator"),
                    "registry contract",
                    RelatedRefs(("sprint:1031",)),
                    f"host-edge-{index}",
                )
            )
            self.assertEqual(result.entity.state, declaration.target)
            self.assertEqual(result.event.kind, declaration.event_kind)
            self.assertEqual(
                (result.event.source_state, result.event.target_state),
                (declaration.source.value, declaration.target.value),
            )
        self.assertEqual(len(host.canon.events(ref="secretary-468")), len(TRANSITIONS[EntityKind.CARD]))

    def test_typed_pending_transition_recovers_only_after_proving_the_live_target(self) -> None:
        self.client.metadata[12]["claim"] = ""
        with mock.patch.object(self.writer.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "audit repair") as raised:
                self.writer.claim(
                    role="dispatcher",
                    actor="d",
                    reference="secretary-468",
                    worker="worker-a",
                    request_id="typed-pending-claim",
                )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "in_progress")
        pending = self.writer.audit.pending_event("typed-pending-claim")
        assert pending is not None
        self.assertEqual(pending["record_type"], "board.protocol_event")
        self.assertEqual(pending["transition"], {"source": "ready", "target": "in_progress"})
        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])

        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))

    def test_the_typed_event_is_staged_exactly_once_before_the_column_effect(self) -> None:
        """Staging is a precondition of the effect, and the committed event is that same record."""
        self.client.tasks[0]["column_id"] = 3
        staged: list[dict | None] = []
        real_call = self.client.call

        def call(method: str, **params: object) -> object:
            if method == "moveTaskPosition":
                staged.append(self.writer.audit.pending_event("staged-once"))
            return real_call(method, **params)

        with mock.patch.object(self.client, "call", call):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="validate",
                reason="submit",
                request_id="staged-once",
            )

        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["record_type"], "board.protocol_event")
        self.assertEqual(staged[0]["transition"], {"source": "in_progress", "target": "validate"})
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(
            self.writer.audit.committed_event("staged-once")["event_id"],
            staged[0]["event_id"],
        )

    def _pending_typed_move(self, request_id: str, target: str = "ready") -> int:
        """Leave the supported post-effect failure: the column moved, its event did not commit."""
        self.client.tasks[0]["column_id"] = 3
        with mock.patch.object(self.writer.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.move(
                    role="dispatcher",
                    actor="d",
                    reference="secretary-468",
                    target=target,
                    reason="",
                    request_id=request_id,
                )
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], target)
        # The cleanup this edge owes the board runs inside the transition, so it is already
        # complete when only the commit fails.
        self.assertEqual(self.client.metadata[12]["claim"], "")
        return len([call for call in self.client.calls if call[0] == "moveTaskPosition"])

    def test_reconcile_publishes_a_typed_pending_transition_whose_board_work_is_done(self) -> None:
        """Recovery owes the journal the occurrence, and the board no second move."""
        moves = self._pending_typed_move("pending-ready-cleanup")

        self.assertEqual(self.writer.reconcile(), (1, 0))

        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        task = self.writer.reader.show("secretary-468")
        self.assertIsNone(task["claim"]["worker"])
        self.assertEqual(task["retry"], {"same": 0, "switched": 0, "heads": []})
        committed = self.writer.audit.committed_event("pending-ready-cleanup")
        self.assertEqual(committed["transition"], {"source": "in_progress", "target": "ready"})

    def test_recovery_refuses_a_typed_pending_transition_the_board_contradicts(self) -> None:
        moves = self._pending_typed_move("contradicted-ready")
        # Another writer moved the card on. The recorded target is no longer live, so the
        # occurrence is not proven and nothing about it may be published or re-applied.
        self.client.tasks[0]["column_id"] = 5

        self.assertEqual(self.writer.reconcile(), (0, 1))

        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        pending = self.writer.audit.pending_event("contradicted-ready")
        self.assertEqual(pending["transition"], {"source": "in_progress", "target": "ready"})

    def test_recovery_refuses_a_typed_pending_transition_whose_card_vanished(self) -> None:
        moves = self._pending_typed_move("vanished-ready")
        self.client.tasks.pop(0)

        self.assertEqual(self.writer.reconcile(), (0, 1))

        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        self.assertIsNotNone(self.writer.audit.pending_event("vanished-ready"))

    def test_a_transport_failure_after_the_move_keeps_the_typed_pending_record(self) -> None:
        """A read that could not answer is not evidence the move did not happen.

        One dropped JSON-RPC round trip after `moveTaskPosition` returned used to discard the
        staged event, leaving a moved card with nothing at all in the journal and an audit that
        reported itself clean. The confirming read is outside the discard window now, so the
        occurrence stays recoverable.
        """
        self.client.tasks[0]["column_id"] = 3
        self.client.fail_read_after_move = True

        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="validate",
                reason="submit",
                request_id="lost-read-back",
            )

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(self.client.tasks[0]["column_id"], 4)
        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])
        self.assertEqual(moves, 1)
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        pending = self.writer.audit.pending_event("lost-read-back")
        self.assertEqual(pending["record_type"], "board.protocol_event")
        self.assertEqual(pending["transition"], {"source": "in_progress", "target": "validate"})

        self.client.fail_read_after_move = False
        self.assertEqual(self.writer.reconcile(), (1, 0))

        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        self.assertEqual(
            self.writer.audit.committed_event("lost-read-back")["event_id"],
            pending["event_id"],
        )

    def test_a_state_race_after_the_move_keeps_the_typed_pending_record(self) -> None:
        """The other post-effect failure: the move landed, another writer moved it onward.

        The read back finds the wrong column, which says nothing about whether this move was
        applied. It is the same enforcement point, so the record survives and recovery decides.
        """
        self.client.tasks[0]["column_id"] = 3
        self.client.race_column_after_move = 5  # Blocked, by somebody else, between the two calls

        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="validate",
                reason="submit",
                request_id="raced-read-back",
            )

        self.assertEqual(raised.exception.code, "audit_pending")
        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])
        self.assertEqual(moves, 1)
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(
            self.writer.audit.pending_event("raced-read-back")["transition"],
            {"source": "in_progress", "target": "validate"},
        )

        # The board still contradicts the recorded target, so recovery refuses it rather than
        # publishing an occurrence it cannot prove - and it does not move the card back.
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        self.assertEqual(self.client.tasks[0]["column_id"], 5)

    def test_a_refused_card_edge_stages_no_typed_event(self) -> None:
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(
                role="po",
                actor="p",
                reference="secretary-468",
                target="ready",
                reason="",
                request_id="refused-edge",
            )

        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))
        self.assertIsNone(self.writer.audit.event("refused-edge"))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertFalse(Path(self.writer.audit.events_path).exists())

    def _released_move_record(self, request_id: str, **payload: object) -> dict[str, object]:
        """One generic `moved` record shaped exactly as the released writer wrote it."""
        return {
            "event_id": f"evt_{request_id}",
            "schema_version": 1,
            "occurred_at": "2026-08-01T00:00:00+00:00",
            "actor": {"role": "dispatcher", "id": "d"},
            "kind": "moved",
            "outcome": "success",
            "task_id": self.writer.reader.show("secretary-468")["id"],
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "r1"},
            "request_id": request_id,
            "payload": dict(payload),
        }

    def test_generic_pending_contender_cannot_be_published_as_a_typed_transition(self) -> None:
        """A released record owns its request id, and the typed request may not borrow it."""
        self.client.tasks[0]["column_id"] = 3
        contender = self._released_move_record("contended-transition", to="validate")
        contender["event_id"] = "legacy-contender"
        self.writer.audit.stage("contended-transition", contender)

        with self.assertRaisesRegex(TaskError, "another operation or payload") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="validate",
                reason="submit",
                request_id="contended-transition",
            )

        self.assertEqual(raised.exception.code, "validation")
        pending = self.writer.audit.pending_event("contended-transition")
        self.assertEqual(pending["event_id"], "legacy-contender")
        self.assertNotIn("record_type", pending)
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))

    def test_a_released_generic_move_id_still_replays_after_the_migration(self) -> None:
        """The upgrade does not turn a pre-migration move id into a validation error.

        Dispatcher move ids are deterministic per attempt, so an attempt spanning the upgrade
        re-issues one. It has to answer as the released replay it is, not as a typed request.
        """
        self.client.tasks[0]["column_id"] = 4
        released = self._released_move_record(
            "released-move",
            **{
                "from": "in_progress",
                "to": "validate",
                "reason_sha256": hashlib.sha256(b"submit").hexdigest(),
            },
        )
        self.writer.audit.stage("released-move", released)
        self.writer.audit.append("released-move", released)

        replayed = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="validate",
            reason="submit",
            request_id="released-move",
        )

        self.assertEqual(replayed["action"], "moved")
        self.assertIs(replayed["replayed"], True)
        self.assertEqual(replayed["event_id"], "evt_released-move")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_a_released_pending_generic_move_is_finished_by_its_released_cleanup(self) -> None:
        """The other released half: a pending generic move still completes its Ready reset."""
        self.client.tasks[0]["column_id"] = 2
        released = self._released_move_record(
            "released-pending-move",
            **{"from": "in_progress", "to": "ready", "reason_sha256": None},
        )
        self.writer.audit.stage("released-pending-move", released)

        replayed = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="",
            request_id="released-pending-move",
        )

        self.assertIs(replayed["replayed"], True)
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        task = self.writer.reader.show("secretary-468")
        self.assertIsNone(task["claim"]["worker"])
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertNotIn("record_type", json.loads(events.readline()))

    def test_retry_does_not_repeat_backend_write_or_event(self) -> None:
        result = self.writer.comment(
            role="worker", actor="w", reference="secretary-468", body="safe", request_id="same"
        )
        second = self.writer.comment(
            role="worker", actor="w", reference="secretary-468", body="safe", request_id="same"
        )
        self.assertEqual(result["event_id"], second["event_id"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createComment"]), 1)
        audit = TaskAudit(self.tmpdir.name)
        with open(audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_comment_scrubs_runtime_secret_before_board_and_audit(self) -> None:
        runtime = Path(self.tmpdir.name) / "external" / "runtime.env"
        secret = "opaque-token-value"
        url = "https://board.example.invalid/jsonrpc.php"
        runtime.parent.mkdir()
        runtime.write_text(f"KANBOARD_URL={url}\nKANBOARD_API_TOKEN={secret}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SECRETARY_RUNTIME_ENV_FILE": str(runtime)}):
            self.writer.comment(
                role="worker",
                actor="w",
                reference="secretary-468",
                body=f"Check {url}; token {secret}",
                request_id="scrubbed-comment",
            )

        comment = [call for call in self.client.calls if call[0] == "createComment"][-1]
        content = str(comment[1]["content"])
        self.assertIn(url, content)
        self.assertNotIn(secret, content)
        self.assertIn("«REDACTED»:env-value", content)
        events = Path(self.writer.audit.events_path).read_text(encoding="utf-8")
        self.assertNotIn(secret, events)

    def test_ordinary_long_text_is_preserved_for_board_protocol_text(self) -> None:
        ordinary = "build-attestation-" + "a" * 64

        self.writer.comment(
            role="worker",
            actor="w",
            reference="secretary-468",
            body=ordinary,
            request_id="ordinary-long-comment",
        )
        self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="blocked",
            classification="external_fact",
            body=ordinary,
            request_id="ordinary-long-report",
        )
        self.writer.verdict(
            role="reviewer",
            actor="r",
            reference="secretary-468",
            kind="red",
            body=ordinary,
            request_id="ordinary-long-verdict",
        )

        comments = [str(call[1]["content"]) for call in self.client.calls if call[0] == "createComment"]
        self.assertTrue(all(ordinary in content for content in comments[-3:]))

    def test_custom_catalog_value_is_scrubbed_before_a_board_comment(self) -> None:
        secret = "custom-catalogued-credential"
        with mock.patch("secretary.secret_store.redaction_values", return_value=(secret,)):
            self.writer.comment(
                role="worker",
                actor="w",
                reference="secretary-468",
                body=secret,
                request_id="custom-catalog-scrub",
            )

        content = str([call for call in self.client.calls if call[0] == "createComment"][-1][1]["content"])
        self.assertNotIn(secret, content)
        self.assertIn("«REDACTED»:env-value", content)

    def test_restoring_a_card_preserves_ordinary_long_text_byte_for_byte(self) -> None:
        ordinary = "restore-proof-" + "a" * 64

        result = self.writer.create(
            role="po",
            actor="operator",
            project="secretary",
            task_type="code",
            title=ordinary,
            description=ordinary,
            target="ready",
            reference="secretary-restore-long",
            request_id="restore-long",
            restoring=True,
        )

        self.assertEqual(result["task"]["title"], ordinary)
        self.assertEqual(result["task"]["description"], ordinary)

    def test_backend_failure_removes_uncommitted_pending_record(self) -> None:
        self.client.fail_comments = True
        with self.assertRaisesRegex(TaskError, "rejected"):
            self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_edit_is_po_only_and_requires_a_change(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.edit(role="worker", actor="w", reference="secretary-468", description="new spec")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertEqual(self.client.calls, [])

        with self.assertRaisesRegex(TaskError, "requires a new") as raised:
            self.writer.edit(role="po", actor="operator", reference="secretary-468")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.calls, [])

    def test_edit_refuses_active_states(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        with self.assertRaisesRegex(TaskError, "Ready or Blocked") as raised:
            self.writer.edit(role="po", actor="operator", reference="secretary-468", description="new spec")
        self.assertEqual(raised.exception.code, "edit_forbidden")
        self.assertFalse(any(call[0] == "updateTask" for call in self.client.calls))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_edit_updates_spec_and_routing_and_writes_audit(self) -> None:
        old_description = str(self.client.tasks[0]["description"])

        result = self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="revised spec",
            head="codex-terra",
            review_head="claude-opus",
            request_id="edit-once",
        )

        self.assertEqual(result["action"], "edited")
        self.assertEqual(result["task"]["description"], "revised spec")
        update = next(params for method, params in self.client.calls if method == "updateTask")
        self.assertEqual(update, {"id": 12, "description": "revised spec"})
        metadata = next(params for method, params in self.client.calls if method == "saveTaskMetadata")
        self.assertEqual(metadata["values"], {"head": "codex-terra", "review_head": "claude-opus"})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "edited")
        payload = event["payload"]
        self.assertEqual(payload["description_sha256"], hashlib.sha256(b"revised spec").hexdigest())
        self.assertEqual(
            payload["description_sha256_was"], hashlib.sha256(old_description.encode()).hexdigest()
        )
        self.assertIsNone(payload["title_sha256"])
        self.assertEqual(payload["head"], "codex-terra")
        self.assertEqual(payload["review_head"], "claude-opus")
        self.assertEqual(
            specification_revision(self.writer.audit.events("secretary-468"), "revised spec"),
            event["event_id"],
        )

    def test_edit_retry_does_not_repeat_backend_write(self) -> None:
        first = self.writer.edit(
            role="po", actor="operator", reference="secretary-468", description="v2", request_id="same-edit"
        )
        second = self.writer.edit(
            role="po", actor="operator", reference="secretary-468", description="v2", request_id="same-edit"
        )
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "updateTask"]), 1)

    def test_archive_is_po_only_and_requires_reason(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.archive(role="worker", actor="w", reference="secretary-468", reason="cleanup")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

        with self.assertRaisesRegex(TaskError, "non-empty reason") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason=" ")
        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_archive_refuses_live_work_or_active_claim(self) -> None:
        with self.assertRaisesRegex(TaskError, "active claim") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason="cleanup")
        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

        self.client.metadata[12]["claim"] = ""
        self.client.tasks[0]["column_id"] = 4
        with self.assertRaisesRegex(TaskError, "live worker or reviewer") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason="cleanup")
        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_archive_closes_card_and_writes_audit(self) -> None:
        self.client.metadata[12]["claim"] = ""

        result = self.writer.archive(
            role="po",
            actor="operator",
            reference="secretary-468",
            reason="backlog cleanup",
            request_id="archive-once",
        )

        self.assertEqual(result["action"], "archived")
        self.assertEqual(self.client.tasks[0]["is_active"], 0)
        self.assertEqual(
            [call[0] for call in self.client.calls if call[0] in {"createComment", "closeTask"}],
            ["createComment", "closeTask"],
        )
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "archived")
        self.assertEqual(event["payload"].keys(), {"reason_sha256"})
        self.assertNotIn("secretary-468", [task["ref"] for task in self.writer.reader.list()])

    def test_archive_retry_after_lost_close_reply_does_not_close_twice(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.client.fail_close = True
        original_close = self.client.call

        def close_then_lose(method: str, **params: object) -> object:
            if method == "closeTask":
                self.client.fail_close = False
                original_close(method, **params)
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return original_close(method, **params)

        with mock.patch.object(self.client, "call", side_effect=close_then_lose):
            with self.assertRaisesRegex(TaskError, "audit repair") as raised:
                self.writer.archive(
                    role="po",
                    actor="operator",
                    reference="secretary-468",
                    reason="backlog cleanup",
                    request_id="archive-retry",
                )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        closes = len([call for call in self.client.calls if call[0] == "closeTask"])

        result = self.writer.archive(
            role="po",
            actor="operator",
            reference="secretary-468",
            reason="backlog cleanup",
            request_id="archive-retry",
        )

        self.assertEqual(result["action"], "archived")
        self.assertEqual(closes, len([call for call in self.client.calls if call[0] == "closeTask"]))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_archive_retry_after_failed_comment_recreates_reason_before_close(self) -> None:
        self.client.metadata[12]["claim"] = ""
        original_call = self.client.call
        failed_once = False

        def fail_first_comment(method: str, **params: object) -> object:
            nonlocal failed_once
            if method == "createComment" and not failed_once:
                failed_once = True
                self.client.calls.append((method, params))
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=fail_first_comment):
            with self.assertRaisesRegex(TaskError, "audit repair") as raised:
                self.writer.archive(
                    role="po",
                    actor="operator",
                    reference="secretary-468",
                    reason="backlog cleanup",
                    request_id="archive-comment-retry",
                )
            self.assertEqual(raised.exception.code, "audit_pending")

            result = self.writer.archive(
                role="po",
                actor="operator",
                reference="secretary-468",
                reason="backlog cleanup",
                request_id="archive-comment-retry",
            )

        self.assertEqual(result["action"], "archived")
        self.assertEqual(self.client.tasks[0]["is_active"], 0)
        self.assertEqual(
            [comment["comment"] for comment in self.client.comments[12]],
            ["[archive]\nbacklog cleanup"],
        )
        self.assertLess(
            [call[0] for call in self.client.calls].index("createComment", 1),
            [call[0] for call in self.client.calls].index("closeTask"),
        )

    def test_archive_reconcile_without_missing_reason_does_not_close(self) -> None:
        self.client.metadata[12]["claim"] = ""
        original_call = self.client.call
        failed_once = False

        def fail_first_comment(method: str, **params: object) -> object:
            nonlocal failed_once
            if method == "createComment" and not failed_once:
                failed_once = True
                self.client.calls.append((method, params))
                raise TaskError("backend_error", "Kanboard rejected the write", 1)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=fail_first_comment):
            with self.assertRaises(TaskError):
                self.writer.archive(
                    role="po",
                    actor="operator",
                    reference="secretary-468",
                    reason="backlog cleanup",
                    request_id="archive-reconcile-missing-reason",
                )

        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertNotEqual(self.client.tasks[0].get("is_active"), 0)
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_archive_refuses_dispatcher_record_after_claim_was_cleared(self) -> None:
        self.client.metadata[12]["claim"] = ""
        state_dir = Path(self.tmpdir.name) / "dispatcher"
        state_dir.mkdir()
        (state_dir / "production-state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "phase": "production",
                    "records": {
                        "secretary-468": {
                            "worker": "worker-secretary-468",
                            "workspace": "/home/dev/orca/workspaces/secretary/468-archive",
                            "handle": "terminal-1",
                            "review_handle": "review-1",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(TaskError, "live dispatcher work") as raised:
            self.writer.archive(
                role="po",
                actor="operator",
                reference="secretary-468",
                reason="cleanup",
                request_id="archive-live-dispatcher-record",
            )

        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_pending_is_visible_and_reconciles_without_backend_retry(self) -> None:
        with mock.patch.object(self.writer.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "committed") as raised:
                self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        writes = len([call for call in self.client.calls if call[0] == "createComment"])
        self.assertEqual(self.writer.audit.reconcile(), (1, 0))
        self.assertEqual(writes, len([call for call in self.client.calls if call[0] == "createComment"]))

    def test_restore_comment_retry_after_lost_reply_does_not_duplicate_history(self) -> None:
        self.client.lose_comment_reply = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.restore_comment(
                reference="secretary-468",
                body="[report:done]\\nrestored",
                occurrence=0,
                request_id="restore-comment-lost-reply",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(len(self.client.comments[12]), 1)

        self.client.lose_comment_reply = False
        self.writer.restore_comment(
            reference="secretary-468",
            body="[report:done]\\nrestored",
            occurrence=0,
            request_id="restore-comment-lost-reply",
        )

        self.assertEqual(len(self.client.comments[12]), 1)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertNotIn("restore_body", events.read())

    def test_restore_comment_retry_uses_digest_occurrence_not_history_index(self) -> None:
        self.client.comments[12].append({"date_creation": "1720000020", "comment": "first"})
        self.client.lose_comment_reply = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.restore_comment(
                reference="secretary-468",
                body="second",
                occurrence=0,
                request_id="restore-second-lost-reply",
            )
        self.client.lose_comment_reply = False
        self.writer.restore_comment(
            reference="secretary-468",
            body="second",
            occurrence=0,
            request_id="restore-second-lost-reply",
        )
        self.assertEqual([comment["comment"] for comment in self.client.comments[12]], ["first", "second"])

        self.client.lose_comment_reply = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.restore_comment(
                reference="secretary-468",
                body="second",
                occurrence=1,
                request_id="restore-duplicate-lost-reply",
            )
        self.client.lose_comment_reply = False
        self.writer.restore_comment(
            reference="secretary-468",
            body="second",
            occurrence=1,
            request_id="restore-duplicate-lost-reply",
        )
        self.assertEqual(
            [comment["comment"] for comment in self.client.comments[12]],
            ["first", "second", "second"],
        )

    def test_partial_move_failure_keeps_pending_until_reconcile(self) -> None:
        """A follow-up that does not land keeps the pending obligation, as the released move did.

        The comment itself is never recreated - it is not idempotent, and the released
        reconciliation did not recreate one either - but the occurrence stays unpublished until
        recovery proves the state edge it names.
        """
        self.client.tasks[0]["column_id"] = 3
        self.client.fail_comments = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="validate", reason="why"
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.client.tasks[0]["column_id"], 4)
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        self.client.fail_comments = False
        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(self.client.comments[12], [])

    def test_restore_move_failure_keeps_pending_audit(self) -> None:
        self.client.fail_move = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.restore_card(
                reference="secretary-468", metadata={"claim": "restored"}, target="in_progress"
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

    def test_reconcile_finishes_pending_restore_before_auditing_success(self) -> None:
        self.client.fail_move = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.restore_card(
                reference="secretary-468", metadata={"claim": "restored"}, target="in_progress"
            )
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "ready")
        self.client.fail_move = False

        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "in_progress")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_restore_placement_uses_live_duplicate_reference(self) -> None:
        archived = self.client.tasks[0]
        archived.update({"column_id": 3, "position": 1, "is_active": 0})
        live = {
            "id": 15,
            "reference": "secretary-468",
            "title": "Live",
            "description": "",
            "column_id": 3,
            "position": 2,
            "swimlane_id": 4,
            "is_active": 1,
            "date_creation": "1720000000",
            "date_modification": "1720000000",
        }
        self.client.tasks.append(live)
        self.client.metadata[15] = {"project": "secretary", "task_type": "code"}
        self.client.comments[15] = []

        self.writer.restore_card(
            reference="secretary-468", metadata={"claim": "restored"}, target="in_progress", position=1
        )

        moves = [params for method, params in self.client.calls if method == "moveTaskPosition"]
        self.assertEqual(moves[-1]["task_id"], 15)

    def test_pending_create_repairs_legacy_orphaned_reference_by_recorded_id(self) -> None:
        self.client.fail_metadata = True
        with open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="Restore",
                    reference="secretary-restore",
                    request_id="restore-create",
                    sprint=sprint,
                )
            # A staged event left by the pre-atomic create path has the id but not the ref.
            self.client.tasks[-1]["reference"] = ""
            pending = self.writer.audit.pending_event("restore-create")
            assert pending is not None
            pending["backend"].pop("reference_assignment")
            self.writer.audit.stage("restore-create", pending)
            self.assertEqual(self.client.tasks[-1]["reference"], "")
            self.client.fail_metadata = False

            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Restore",
                reference="secretary-restore",
                request_id="restore-create",
                sprint=sprint,
            )
        self.assertEqual(result["task"]["ref"], "secretary-restore")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_reconcile_completes_stale_ready_cleanup_before_closing_pending(self) -> None:
        """A migrated move owes the same Ready reset, and does not close its event without it."""
        self.client.tasks[0]["column_id"] = 3
        self.client.fail_metadata = True
        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.move(
                role="dispatcher", actor="d", reference="secretary-468", target="ready", reason=""
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.client.tasks[0]["column_id"], 2)
        self.assertEqual(self.client.metadata[12]["claim"], "codex-terra")
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        self.client.fail_metadata = False
        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        task = self.writer.reader.show("secretary-468")
        self.assertIsNone(task["claim"]["worker"])
        self.assertEqual(task["retry"], {"same": 0, "switched": 0, "heads": []})
        event = json.loads(Path(self.writer.audit.events_path).read_text(encoding="utf-8"))
        self.assertEqual(event["record_type"], "board.protocol_event")
        self.assertEqual(event["transition"], {"source": "in_progress", "target": "ready"})

    def test_pending_ready_replay_finishes_cleanup_before_success_audit(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.client.metadata[12]["resolved_head"] = "codex-terra"
        self.client.metadata[12]["resolved_review_head"] = "codex-reviewer"
        self.client.fail_metadata = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="ready",
                reason="",
                request_id="ready-replay",
            )

        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="ready",
                reason="",
                request_id="ready-replay",
            )
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))

        self.client.fail_metadata = False
        replayed = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="",
            request_id="ready-replay",
        )
        self.assertIs(replayed["replayed"], True)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

        self.assertEqual(replayed["task"]["state"], "ready")
        task = self.writer.reader.show("secretary-468")
        self.assertIsNone(task["claim"]["worker"])
        self.assertIsNone(task["routing"]["resolved_worker_head"])
        self.assertIsNone(task["routing"]["resolved_review_head"])
        self.assertEqual(task["retry"], {"same": 0, "switched": 0, "heads": []})

    def test_dispatcher_claim_stamps_metadata_moves_and_audits(self) -> None:
        self.client.metadata[12]["claim"] = ""
        result = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            resolved_review_head="codex-reviewer",
            request_id="claim-once",
        )

        self.assertEqual(result["action"], "claimed")
        self.assertEqual(result["task"]["state"], "in_progress")
        self.assertEqual(self.client.metadata[12]["claim"], "secretary-468-runtime")
        self.assertEqual(self.client.metadata[12]["resolved_head"], "codex")
        self.assertEqual(self.client.metadata[12]["resolved_review_head"], "codex-reviewer")
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "card.started")
        self.assertEqual(event["transition"], {"source": "ready", "target": "in_progress"})

    def test_create_stores_codex_launch_mode_and_audits(self) -> None:
        with open_sprint() as sprint:
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                description="body",
                target="ready",
                reference="secretary-522",
                head="codex-extra",
                codex_launch_mode="tui",
                request_id="create-tui",
                sprint=sprint,
            )

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["task"]["ref"], "secretary-522")
        self.assertEqual(result["task"]["state"], "ready")
        self.assertEqual(result["task"]["routing"]["head_override"], "codex-extra")
        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        task_id = int(result["task"]["id"].removeprefix("task_kanboard_"))
        self.assertEqual(self.client.metadata[task_id]["codex_launch_mode"], "tui")
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            event = json.loads(events.readline())
        self.assertEqual(event["kind"], "created")
        self.assertEqual(event["payload"]["codex_launch_mode"], "tui")
        self.assertEqual(event["payload"]["head"], "codex-extra")
        self.assertIn("title_sha256", event["payload"])

    def test_auto_reference_uses_board_wide_project_high_water_mark(self) -> None:
        # The new Kanboard row will be 14, which is already a historical reference.
        self.client.tasks[0]["reference"] = "secretary-14"
        self.client.tasks.append(
            {
                "id": 10,
                "reference": "secretary-1158",
                "title": "Archived",
                "column_id": 6,
                "position": 1,
                "swimlane_id": 4,
                "is_active": 0,
            }
        )
        self.client.tasks.extend(
            [
                {
                    "id": 9,
                    "reference": "secretary-nope",
                    "title": "Malformed",
                    "column_id": 2,
                    "position": 2,
                    "swimlane_id": 4,
                },
                {
                    "id": 8,
                    "reference": "other-999",
                    "title": "Other project",
                    "column_id": 2,
                    "position": 3,
                    "swimlane_id": 4,
                },
            ]
        )
        self.client.metadata.update({8: {}, 9: {}, 10: {}})
        self.client.comments.update({8: [], 9: [], 10: []})

        with (
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            open_sprint() as sprint,
        ):
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Auto reference",
                request_id="auto-reference",
                sprint=sprint,
            )

        self.assertEqual(result["task"]["ref"], "secretary-1159")
        self.assertNotEqual(result["task"]["ref"], "secretary-14")
        self.assertEqual(
            [params["status_id"] for method, params in self.client.calls if method == "getAllTasks"][:2],
            [1, 0],
        )

    def test_auto_reference_serializes_concurrent_creates(self) -> None:
        first_create_started = threading.Event()
        release_first_create = threading.Event()
        second_reached_board = threading.Event()
        original_call = self.client.call
        first_create = True
        results: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def paused_first_create(method: str, **params: object) -> object:
            nonlocal first_create
            if method == "createTask" and first_create:
                first_create = False
                first_create_started.set()
                if not release_first_create.wait(2):
                    raise AssertionError("first create was not released")
            elif method == "getProjectByName" and first_create_started.is_set():
                second_reached_board.set()
            return original_call(method, **params)

        def create(writer: TaskWriter, request_id: str, sprint: str) -> None:
            try:
                results.append(
                    writer.create(
                        role="po",
                        actor="operator",
                        project="secretary",
                        task_type="code",
                        title=request_id,
                        target="ready",
                        request_id=request_id,
                        sprint=sprint,
                        sprint_override=True,
                        sprint_override_reason="concurrent allocation test",
                    )
                )
            except BaseException as exc:  # Preserve thread failures for the assertion below.
                failures.append(exc)

        with (
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            open_sprint() as sprint,
            mock.patch.object(self.client, "call", side_effect=paused_first_create),
        ):
            first = threading.Thread(target=create, args=(self.writer, "first-auto-reference", sprint))
            first.start()
            self.assertTrue(first_create_started.wait(2))
            second = threading.Thread(
                target=create,
                args=(TaskWriter(self.client, data_dir=self.tmpdir.name), "second-auto-reference", sprint),
            )
            second.start()
            self.assertFalse(second_reached_board.wait(0.2))
            release_first_create.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(
            sorted(result["task"]["ref"] for result in results), ["secretary-469", "secretary-470"]
        )

    def test_auto_reference_enumeration_failure_writes_no_card(self) -> None:
        original_call = self.client.call

        def invalid_task_list(method: str, **params: object) -> object:
            if method == "getAllTasks":
                return {"unexpected": "shape"}
            return original_call(method, **params)

        with (
            mock.patch.object(self.client, "call", side_effect=invalid_task_list),
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            open_sprint() as sprint,
            self.assertRaisesRegex(TaskError, "invalid task list") as raised,
        ):
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="No fallback",
                request_id="auto-reference-failure",
                sprint=sprint,
            )

        self.assertEqual(raised.exception.code, "backend_error")
        self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))

    def test_auto_reference_refuses_null_or_false_enumeration(self) -> None:
        original_call = self.client.call

        for reply in (None, False):
            with (
                self.subTest(reply=reply),
                mock.patch.object(
                    self.client,
                    "call",
                    side_effect=lambda method, reply=reply, **params: (
                        reply if method == "getAllTasks" else original_call(method, **params)
                    ),
                ),
                mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
                open_sprint() as sprint,
                self.assertRaisesRegex(TaskError, "invalid task list") as raised,
            ):
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="No fallback",
                    request_id=f"null-reference-{reply}",
                    sprint=sprint,
                )

            self.assertEqual(raised.exception.code, "backend_error")
            self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))

    def test_create_passes_reference_to_atomic_backend_write(self) -> None:
        with (
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            open_sprint() as sprint,
        ):
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Atomic reference",
                request_id="atomic-reference",
                sprint=sprint,
            )

        created = [params for method, params in self.client.calls if method == "createTask"]
        self.assertEqual(created[-1]["reference"], result["task"]["ref"])
        self.assertFalse(any(method == "updateTask" for method, _params in self.client.calls))

    def test_pending_atomic_create_without_recorded_id_stays_unresolved(self) -> None:
        original_stage = self.writer.audit.stage
        stages = 0

        def lose_backend_id_stage(request_id: str, event: dict[str, object]) -> None:
            nonlocal stages
            stages += 1
            if stages == 3:
                raise OSError("lost after create")
            original_stage(request_id, event)  # type: ignore[arg-type]

        with (
            mock.patch.object(self.writer.audit, "stage", side_effect=lose_backend_id_stage),
            open_sprint() as sprint,
            self.assertRaisesRegex(TaskError, "audit repair"),
        ):
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Crash safe",
                request_id="atomic-create-crash",
                sprint=sprint,
            )

        self.assertEqual(self.client.tasks[-1]["reference"], "secretary-469")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        with open_sprint() as sprint, self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Crash safe",
                request_id="atomic-create-crash",
                sprint=sprint,
            )

        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)
        self.assertEqual(self.client.metadata[int(self.client.tasks[-1]["id"])], {})
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

    def test_sigint_before_atomic_create_does_not_adopt_same_identity_later_reference(self) -> None:
        original_call = self.client.call

        def interrupt_create(method: str, **params: object) -> object:
            if method == "createTask":
                raise KeyboardInterrupt()
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=interrupt_create), open_sprint() as sprint:
            with self.assertRaises(KeyboardInterrupt):
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="Interrupted before create",
                    description="never reached board",
                    request_id="sigint-before-create",
                    sprint=sprint,
                )

        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.client.tasks.append(
            {
                "id": 99,
                "reference": "secretary-469",
                "title": "Interrupted before create",
                "description": "never reached board",
                "column_id": 2,
                "position": 1,
                "swimlane_id": 4,
                "is_active": 1,
            }
        )
        self.client.metadata[99] = {}
        self.client.comments[99] = []

        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.client.metadata[99], {})
        self.assertFalse(any(method == "updateTask" for method, _params in self.client.calls))

    def test_backend_ignoring_atomic_reference_leaves_pending_create_unrepaired(self) -> None:
        original_call = self.client.call

        def ignore_reference(method: str, **params: object) -> object:
            if method == "createTask":
                params.pop("reference", None)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=ignore_reference), open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="Reference must persist",
                    request_id="ignored-atomic-reference",
                    sprint=sprint,
                )

        self.assertEqual(self.client.tasks[-1]["reference"], "")
        self.assertEqual(self.client.metadata[int(self.client.tasks[-1]["id"])], {})
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertFalse(any(method == "updateTask" for method, _params in self.client.calls))

    def test_pending_create_does_not_repair_a_different_task_with_its_reference(self) -> None:
        self.client.fail_metadata = True
        with open_sprint() as sprint, self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Interrupted",
                reference="secretary-interrupted",
                request_id="interrupted-create",
                sprint=sprint,
            )
        intended = self.client.tasks[-1]
        intended["reference"] = ""
        self.client.tasks.append(
            {
                "id": 99,
                "reference": "secretary-interrupted",
                "title": "Different",
                "column_id": 2,
                "position": 1,
                "swimlane_id": 4,
                "is_active": 1,
            }
        )
        self.client.metadata[99] = {}
        self.client.comments[99] = []
        self.client.fail_metadata = False

        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertEqual(self.client.metadata[int(intended["id"])], {})
        self.assertEqual(self.client.metadata[99], {})
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

    def test_an_allocated_reference_clears_the_archived_rows_too(self) -> None:
        """An archived card keeps its reference for good, so the counter has to see it."""
        self.client.tasks.append(
            {
                "id": 900,
                "reference": "secretary-1404",
                "title": "Archived",
                "description": "",
                "column_id": 6,
                "position": 1,
                "swimlane_id": 4,
                "is_active": 0,
            }
        )
        self.client.metadata[900] = {"project": "secretary"}
        self.client.comments[900] = []

        with open_sprint() as sprint:
            created = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Next in line",
                request_id="allocate-above-archived",
                sprint=sprint,
            )

        self.assertEqual(created["task"]["ref"], "secretary-1405")

    def test_an_allocated_reference_that_is_claimed_is_refused_not_written(self) -> None:
        """The claim check is what proves a reference free; allocation only proposes one.

        An enumeration that missed a row is simulated by allocating a reference the board already
        holds: the card must not be created under someone else's reference.
        """
        with mock.patch.object(tasks, "next_project_reference", return_value="secretary-468"):
            with open_sprint() as sprint:
                with self.assertRaisesRegex(TaskError, "secretary-468 is already claimed") as raised:
                    self.writer.create(
                        role="observer",
                        actor="observer",
                        project="secretary",
                        task_type="code",
                        title="Collides",
                        request_id="allocated-collision",
                        sprint=sprint,
                    )

        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))

    def test_explicit_reference_collision_is_still_refused(self) -> None:
        with open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "secretary-468 is already claimed") as raised:
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="Duplicate",
                    reference="secretary-468",
                    request_id="explicit-collision",
                    sprint=sprint,
                )

        self.assertEqual(raised.exception.code, "validation")
        self.assertFalse(any(method == "createTask" for method, _params in self.client.calls))

    def test_pending_create_replay_restores_metadata_before_audit(self) -> None:
        self.client.fail_metadata = True
        with open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="Launch mode",
                    target="ready",
                    reference="secretary-523",
                    codex_launch_mode="tui",
                    request_id="create-replay",
                    sprint=sprint,
                )
            self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
            create_writes = len([call for call in self.client.calls if call[0] == "createTask"])

            self.client.fail_metadata = False
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                target="ready",
                reference="secretary-523",
                codex_launch_mode="tui",
                request_id="create-replay",
                sprint=sprint,
            )

        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(create_writes, len([call for call in self.client.calls if call[0] == "createTask"]))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_ready_reset_preserves_codex_launch_mode(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.client.metadata[12]["codex_launch_mode"] = "tui"

        result = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="retry",
            request_id="ready-preserves-mode",
        )

        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(self.client.metadata[12]["codex_launch_mode"], "tui")

    def test_create_rejects_invalid_codex_launch_mode_without_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "codex launch mode") as raised:
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                codex_launch_mode="shell",
            )

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertFalse(any(call[0] == "createTask" for call in self.client.calls))

    def test_create_rejects_the_retired_exec_launch_mode_without_write(self) -> None:
        """The service layer refuses it too, not only the command that usually calls it."""
        with self.assertRaisesRegex(TaskError, "codex launch mode must be tui") as raised:
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                codex_launch_mode="exec",
            )

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertFalse(any(call[0] == "createTask" for call in self.client.calls))

    def test_a_card_already_carrying_exec_reads_as_carrying_no_mode(self) -> None:
        """Legacy routing data, not authority: the field no longer names anything launchable."""
        self.client.metadata[12]["codex_launch_mode"] = "exec"

        task = self.writer.reader.show("secretary-468")

        self.assertIsNone(task["routing"]["codex_launch_mode"])

    def test_worker_create_ready_is_forbidden_without_backend_write(self) -> None:
        with self.assertRaisesRegex(TaskError, "only proposals in Issues") as raised:
            self.writer.create(
                role="worker",
                actor="w",
                project="secretary",
                task_type="code",
                title="Continuation",
                target="ready",
            )

        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertFalse(any(call[0] == "createTask" for call in self.client.calls))

    def _failed_claim(self, request_id: str, worker: str = "secretary-468-runtime") -> None:
        """A claim whose column move is refused: the whole attempt leaves nothing behind."""
        self.client.fail_move = True
        try:
            with self.assertRaisesRegex(TaskError, "rejected the move") as raised:
                self.writer.claim(
                    role="dispatcher",
                    actor="d",
                    reference="secretary-468",
                    worker=worker,
                    resolved_head="codex",
                    request_id=request_id,
                )
        finally:
            self.client.fail_move = False
        self.assertEqual(raised.exception.code, "backend_error")

    def test_a_failed_claim_move_leaves_neither_a_typed_event_nor_a_claim(self) -> None:
        """The claim write is inside the transition, so a refused move claims nothing.

        The released path wrote the claim metadata first and left a pending record for
        `reconcile` to finish by moving the card. Recovery may no longer repeat a move, so the
        claim is written only once the column effect is proven: a failed attempt has to be
        indistinguishable from one that never ran.
        """
        self.client.metadata[12]["claim"] = ""

        self._failed_claim("claim-replay")

        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])
        self.assertIsNone(task["routing"]["resolved_worker_head"])

        replayed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            request_id="claim-replay",
        )

        self.assertEqual(replayed["task"]["state"], "in_progress")
        self.assertEqual(replayed["task"]["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_reconcile_has_nothing_to_repeat_after_a_failed_claim_move(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self._failed_claim("claim-reconcile")

        self.assertEqual(self.writer.reconcile(), (0, 0))
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])

    def test_a_retry_after_a_failed_claim_move_still_meets_every_admission_guard(self) -> None:
        """A new attempt is a new admission, whoever it is: no attempt earns a skipped guard.

        The claimant id is derived from the card, so a retrying dispatcher computes the same
        worker id as the attempt that failed. That must not read as "already mine".
        """
        self.client.metadata[12]["claim"] = ""
        self._failed_claim("claim-attempt-1")
        # Another code card of the same project is claimed before the retry.
        self.client.tasks.append(
            {
                "id": 14,
                "reference": "secretary-999",
                "title": "Other code",
                "column_id": 3,
                "position": 1,
                "swimlane_id": 4,
            }
        )
        self.client.metadata[14] = {
            "project": "secretary",
            "task_type": "code",
            "claim": "other-worker",
        }
        self.client.comments[14] = []
        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])

        with self.assertRaisesRegex(TaskError, "one active code task") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex-b",
                request_id="claim-attempt-2",
            )

        self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_a_retry_after_a_failed_claim_move_records_the_head_it_asks_for(self) -> None:
        """The head a new attempt resolved is the head the card gets, not the failed one's.

        A dropped head is the disagreement `claim_mismatch` exists to catch, so a partial claim
        must not leave the next attempt launching against a head the dispatcher no longer holds.
        """
        self.client.metadata[12]["claim"] = ""
        self.client.fail_move = True
        with self.assertRaisesRegex(TaskError, "rejected the move"):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex-a",
                request_id="claim-head-1",
            )
        self.client.fail_move = False

        claimed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex-b",
            request_id="claim-head-2",
        )

        self.assertIs(claimed["replayed"], False)
        self.assertEqual(claimed["task"]["state"], "in_progress")
        self.assertEqual(claimed["task"]["routing"]["resolved_worker_head"], "codex-b")
        self.assertNotIn(
            "resolved_head",
            claim_mismatch(claimed["task"], "secretary-468-runtime", "codex-b", ""),
        )

    def test_a_claim_on_a_held_card_is_refused_even_when_it_names_the_same_worker(self) -> None:
        """A live claim closes the door, and naming its holder is not a key to it."""
        held = self.client.metadata[12]["claim"]

        with self.assertRaisesRegex(TaskError, "already claimed") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker=held,
                resolved_head="codex-b",
                request_id="claim-contender",
            )

        self.assertEqual(raised.exception.code, "claim_conflict")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))
        self.assertEqual(self.client.metadata[12]["head"], "codex-terra")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def _released_claim_record(self, request_id: str, **payload: object) -> dict[str, object]:
        """One generic `claimed` record shaped exactly as the released writer wrote it."""
        return {
            "event_id": f"evt_{request_id}",
            "schema_version": 1,
            "occurred_at": "2026-08-01T00:00:00+00:00",
            "actor": {"role": "dispatcher", "id": "d"},
            "kind": "claimed",
            "outcome": "success",
            "task_id": self.writer.reader.show("secretary-468")["id"],
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "r1"},
            "request_id": request_id,
            "payload": dict(payload),
        }

    def _released_pending_claim(self, request_id: str) -> None:
        """The released half-claim: metadata written, column move still owed."""
        self.client.metadata[12]["claim"] = "secretary-468-runtime"
        self.client.metadata[12]["resolved_head"] = "codex"
        self.writer.audit.stage(
            request_id,
            self._released_claim_record(
                request_id,
                worker="secretary-468-runtime",
                resolved_head="codex",
                resolved_review_head=None,
                slug=None,
                base_branch=None,
                cap=3,
            ),
        )

    def test_a_released_pending_claim_is_still_finished_by_reconcile(self) -> None:
        """The released recovery survives the migration for the records that need it.

        A claim written before this migration wrote its metadata before the column move, so its
        pending record is an owed move. `reconcile` still completes it exactly as the released
        code did; only the typed path forbids recovery from moving a card.
        """
        self._released_pending_claim("released-pending-claim")

        self.assertEqual(self.writer.reconcile(), (1, 0))

        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "in_progress")
        self.assertEqual(task["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            record = json.loads(events.readline())
        self.assertNotIn("record_type", record)
        self.assertEqual(record["kind"], "claimed")

    def test_a_released_pending_claim_id_replays_through_its_released_path(self) -> None:
        """Retrying that id is still the released claim, not a typed transition request."""
        self._released_pending_claim("released-claim-replay")

        replayed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            request_id="released-claim-replay",
        )

        self.assertIs(replayed["replayed"], True)
        self.assertEqual(replayed["task"]["state"], "in_progress")
        self.assertEqual(replayed["task"]["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_a_claim_whose_metadata_write_fails_keeps_its_pending_event(self) -> None:
        """The claim write is the transition's own follow-up, so it holds the event open.

        A retry of the same request id is the complete repair: it repeats no admission check and
        no column move, finishes the metadata, and only then publishes the occurrence.
        """
        self.client.metadata[12]["claim"] = ""
        self.client.fail_metadata = True

        with self.assertRaisesRegex(TaskError, "audit repair") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex",
                request_id="claim-cleanup",
            )

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "in_progress")
        self.assertIsNone(self.writer.reader.show("secretary-468")["claim"]["worker"])
        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])

        self.client.fail_metadata = False
        replayed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            request_id="claim-cleanup",
        )

        self.assertIs(replayed["replayed"], True)
        self.assertEqual(replayed["task"]["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(replayed["task"]["routing"]["resolved_worker_head"], "codex")
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_reconcile_publishes_a_proven_start_and_leaves_the_claim_to_the_dispatcher(self) -> None:
        """Recovery publishes the state edge it proved, and invents no claim it cannot know.

        Unlike the Ready reset, a claim's metadata is a dispatcher decision - the worker id and
        the resolved heads - that no reader of the card can recompute. Refusing to publish would
        make a proven start permanently unrecoverable, which is exactly what recovery is for, so
        the occurrence is published and the missing claim is left to the dispatcher's own live
        claim check, which reports it as a controlled divergence instead of launching on it.
        """
        self.client.metadata[12]["claim"] = ""
        self.client.fail_metadata = True
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex",
                request_id="claim-orphan",
            )
        moves = len([call for call in self.client.calls if call[0] == "moveTaskPosition"])

        self.assertEqual(self.writer.reconcile(), (1, 0))

        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(moves, len([call for call in self.client.calls if call[0] == "moveTaskPosition"]))
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "in_progress")
        self.assertIsNone(task["claim"]["worker"])
        self.assertIn("worker", claim_mismatch(task, "secretary-468-runtime", "codex", ""))
        event = json.loads(Path(self.writer.audit.events_path).read_text(encoding="utf-8"))
        self.assertEqual(event["kind"], "card.started")
        self.assertEqual(event["transition"], {"source": "ready", "target": "in_progress"})

    def test_claim_rejects_project_code_capacity_without_write(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.client.tasks.append(
            {
                "id": 14,
                "reference": "secretary-999",
                "title": "Other code",
                "column_id": 3,
                "position": 1,
                "swimlane_id": 4,
            }
        )
        self.client.metadata[14] = {
            "project": "secretary",
            "task_type": "code",
            "claim": "other-worker",
        }

        with self.assertRaisesRegex(TaskError, "one active code task") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
            )

        self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.client.calls))

    def test_claim_counts_a_parked_card_as_an_active_code_task(self) -> None:
        """A parked card holds a retained worker and its checkout: a second writer in the same
        project is as wrong there as it is in Validate."""
        self.client.metadata[12]["claim"] = ""
        self.client.tasks.append(
            {
                "id": 14,
                "reference": "secretary-999",
                "title": "Parked code",
                "column_id": 7,  # Assessment
                "position": 1,
                "swimlane_id": 4,
            }
        )
        self.client.metadata[14] = {
            "project": "secretary",
            "task_type": "code",
            "claim": "other-worker",
        }

        with self.assertRaisesRegex(TaskError, "one active code task") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
            )

        self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertFalse(any(call[0] == "saveTaskMetadata" for call in self.client.calls))

    def test_archive_refuses_a_parked_card(self) -> None:
        """Assessment is a wait, not a resting place: the worker and workspace are still owned."""
        self.client.metadata[12]["claim"] = ""
        self.client.tasks[0]["column_id"] = 7

        with self.assertRaisesRegex(TaskError, "live worker or reviewer") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason="cleanup")

        self.assertEqual(raised.exception.code, "live_work")
        self.assertFalse(any(call[0] == "closeTask" for call in self.client.calls))

    def test_reviewer_verdict_uses_review_marker(self) -> None:
        result = self.writer.verdict(
            role="reviewer",
            actor="r",
            reference="secretary-468",
            kind="green",
            body="ok",
            request_id="green",
        )

        self.assertEqual(result["action"], "verdict")
        comment = [call for call in self.client.calls if call[0] == "createComment"][-1]
        self.assertEqual(comment[1]["content"], "[review:green]\nok")

    def test_validate_to_in_progress_rework_is_dispatcher_only(self) -> None:
        self.client.tasks[0]["column_id"] = 4
        self.client.metadata[12]["resolved_review_head"] = "codex-reviewer"

        result = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="in_progress",
            reason="review:red",
            request_id="rework",
        )

        self.assertEqual(result["task"]["state"], "in_progress")
        self.assertEqual(self.client.metadata[12]["resolved_review_head"], "")

    def test_completed_ready_replay_does_not_reset_metadata_again(self) -> None:
        self.client.tasks[0]["column_id"] = 3
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="",
            request_id="ready-done",
        )
        metadata_writes = len([call for call in self.client.calls if call[0] == "saveTaskMetadata"])

        self.client.tasks[0]["column_id"] = 4
        self.client.metadata[12]["claim"] = "codex-terra"
        self.client.metadata[12]["resolved_head"] = "codex-terra"
        self.client.metadata[12]["resolved_review_head"] = "codex-reviewer"
        self.client.metadata[12]["retry_same"] = "1"
        second = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="",
            request_id="ready-done",
        )

        self.assertEqual(second["task"]["state"], "validate")
        self.assertEqual(
            metadata_writes, len([call for call in self.client.calls if call[0] == "saveTaskMetadata"])
        )
        self.assertEqual(self.client.metadata[12]["claim"], "codex-terra")
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            self.assertEqual(len(events.readlines()), 1)

    def test_pending_blocks_export_from_the_same_data_root(self) -> None:
        self.writer.audit.stage("pending", {"request_id": "pending", "event_id": "evt_pending"})
        with self.assertRaisesRegex(RuntimeError, "unresolved pending"):
            export_board(
                Path(self.tmpdir.name),
                instance_dir=Path(self.tmpdir.name),
                reader=mock.Mock(),
            )


class DoneRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client.instance_dir = Path(self.tmpdir.name)
        self.reader = TaskReader(self.client)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)
        self.task = self.client.tasks[0]
        self.task.update({"column_id": 6, "date_moved": 100, "is_active": 1})
        self.client.metadata[12].update({"record_type": "task", "task_type": "code"})

    def cleanup(self, *, now: float = 100 + 14 * 86400 + 1) -> dict:
        return close_old_done(self.reader, self.writer, now=now, retention_days=14)

    def test_strict_threshold_missing_timestamp_and_deterministic_order(self) -> None:
        self.client.tasks.append(
            {"id": 14, "reference": "a-old", "title": "old", "column_id": 6, "date_moved": 100, "is_active": 1}
        )
        self.client.metadata[14] = {"record_type": "task"}
        self.client.tasks.append(
            {"id": 15, "reference": "missing", "title": "missing", "column_id": 6, "is_active": 1}
        )
        self.client.metadata[15] = {"record_type": "task"}
        result = self.cleanup()
        self.assertEqual(result["closed"], ["a-old", "secretary-468"])
        self.assertEqual(result["closed_count"], 2)
        self.assertEqual(self.client.tasks[-1]["is_active"], 1)

        self.client.tasks[-1]["date_moved"] = 100
        equal = close_old_done(self.reader, self.writer, now=100 + 14 * 86400, retention_days=14)
        self.assertEqual(equal["closed"], [])

    def test_reader_includes_only_active_done_execution_candidates(self) -> None:
        self.client.tasks.append(
            {"id": 14, "reference": "product", "title": "p", "column_id": 6, "date_moved": 1, "is_active": 1}
        )
        self.client.metadata[14] = {"record_type": "product"}
        self.client.tasks.append(
            {"id": 15, "reference": "ready", "title": "r", "column_id": 2, "date_moved": 1, "is_active": 1}
        )
        self.client.metadata[15] = {"record_type": "task"}
        self.client.tasks.append(
            {"id": 16, "reference": "closed", "title": "c", "column_id": 6, "date_moved": 1, "is_active": 0}
        )
        self.client.metadata[16] = {"record_type": "task"}
        self.assertEqual(self.reader.done_retention_candidates(), [{"reference": "secretary-468", "date_moved": 100}])

    def test_product_or_issue_is_refused_without_close(self) -> None:
        for record_type in ("issue", "product"):
            self.client.metadata[12]["record_type"] = record_type
            with self.assertRaisesRegex(TaskError, "cannot be retired") as raised:
                self.writer.retire_done(
                    reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
                )
            self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(method == "closeTask" for method, _params in self.client.calls))

    def test_race_before_close_skips_changed_episode(self) -> None:
        original = self.client.call
        reads = 0

        def race(method: str, **params: object) -> object:
            nonlocal reads
            if method == "getAllTasks" and params.get("status_id") == 1:
                reads += 1
                if reads == 2:
                    self.task["date_moved"] = 200
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=race):
            result = self.writer.retire_done(
                reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
            )
        self.assertTrue(result["skipped"])
        self.assertFalse(any(method == "closeTask" for method, _params in self.client.calls))

    def test_race_before_close_skips_state_change(self) -> None:
        original = self.client.call
        reads = 0

        def race(method: str, **params: object) -> object:
            nonlocal reads
            if method == "getAllTasks" and params.get("status_id") == 1:
                reads += 1
                if reads == 2:
                    self.task["column_id"] = 2
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=race):
            result = self.writer.retire_done(
                reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
            )
        self.assertTrue(result["skipped"])
        self.assertFalse(any(method == "closeTask" for method, _params in self.client.calls))

    def test_replay_uses_episode_key_and_no_archive_comment(self) -> None:
        first = self.writer.retire_done(
            reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
        )
        second = self.writer.retire_done(
            reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
        )
        self.assertTrue(first["retired"])
        self.assertTrue(second["skipped"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "closeTask"]), 1)
        self.assertFalse(any(call[0] == "createComment" for call in self.client.calls))
        event = self.writer.audit.events("secretary-468", kind="retired")[0]
        self.assertEqual(event["actor"]["role"], "retro")
        self.assertEqual(event["payload"]["expected_date_moved"], 100)
        self.assertEqual(event["request_id"], tasks._done_retention_request_id(12, 100))

    def test_lost_close_reply_recovers_through_generic_reconcile(self) -> None:
        original = self.client.call

        def close_then_lose(method: str, **params: object) -> object:
            if method == "closeTask":
                original(method, **params)
                raise TaskError("backend_unavailable", "Kanboard backend is unavailable", 1)
            return original(method, **params)

        with (
            mock.patch.object(self.client, "call", side_effect=close_then_lose),
            self.assertRaisesRegex(TaskError, "audit repair"),
        ):
            self.writer.retire_done(
                reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
            )
        closes = len([call for call in self.client.calls if call[0] == "closeTask"])
        self.assertEqual(self.writer.audit.reconcile(), (0, 1))
        self.assertEqual(self.writer.reconcile(), (1, 0))
        self.assertEqual(closes, len([call for call in self.client.calls if call[0] == "closeTask"]))
        self.assertEqual(len(self.writer.audit.events("secretary-468", kind="retired")), 1)


class AssessmentStateTests(unittest.TestCase):
    """secretary-1025/1031: the durable wait between a reviewer verdict and the observer's decision.

    These pin the model: who may move a card in and out of the column, that the column
    round-trips through the state map, and that a card only leaves it on a decision somebody
    recorded.
    """

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)

    def reserve_project(
        self,
        *,
        card_sprint: str = SPRINT,
        project: str = "secretary",
        data_dir: str = "",
    ) -> None:
        """Put the card in an open sprint that reserves its project.

        That reservation is what entitles an observer to decide about the card, so the tests of
        the decision path set it up as the board would have it: the guard index and the live
        sprint row both naming the sprint the card is linked to.

        The caller is bound to the card's own sprint, which is the head that would be deciding
        here. A test about a caller from elsewhere binds its own.
        """
        bind_observer(self, card_sprint)
        self.client.metadata[12]["sprint_ref"] = card_sprint
        reader = FakeSprintReader({"ref": SPRINT, "status": "open", "reservations": [project]})
        patcher = mock.patch("secretary.sprints.SprintReader", return_value=reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        refresh_active_sprint_projects(data_dir or self.tmpdir.name, reader)

    def test_column_order_and_state_map(self) -> None:
        self.assertEqual(_STATE_BY_COLUMN["Assessment"], "assessment")
        self.assertEqual(
            list(_STATE_BY_COLUMN),
            ["Issues", "Ready", "In progress", "Validate", "Assessment", "Blocked", "Done"],
        )

    def test_dispatcher_transitions_are_exact(self) -> None:
        """Pinned exactly: a later card must widen this table deliberately, not by accident."""
        self.assertEqual(
            CARD_TRANSITIONS["dispatcher"],
            {
                (CardState.READY, CardState.IN_PROGRESS),
                (CardState.IN_PROGRESS, CardState.VALIDATE),
                (CardState.IN_PROGRESS, CardState.BLOCKED),
                (CardState.IN_PROGRESS, CardState.READY),
                (CardState.VALIDATE, CardState.IN_PROGRESS),
                (CardState.VALIDATE, CardState.BLOCKED),
                (CardState.VALIDATE, CardState.DONE),
                (CardState.VALIDATE, CardState.ASSESSMENT),
                (CardState.ASSESSMENT, CardState.IN_PROGRESS),
                (CardState.ASSESSMENT, CardState.DONE),
                (CardState.ASSESSMENT, CardState.BLOCKED),
            },
        )

    def test_worker_and_reviewer_stay_out_of_assessment(self) -> None:
        self.assertEqual(CARD_TRANSITIONS["worker"], frozenset())
        self.assertEqual(CARD_TRANSITIONS["reviewer"], frozenset())
        for role in ("po", "observer"):
            self.assertIn((CardState.VALIDATE, CardState.ASSESSMENT), CARD_TRANSITIONS[role])
        self.assertIn((CardState.ASSESSMENT, CardState.READY), CARD_TRANSITIONS["po"])
        self.assertEqual(
            {edge for edge in CARD_TRANSITIONS["steward"] if CardState.ASSESSMENT in edge},
            {(CardState.ASSESSMENT, CardState.BLOCKED)},
        )

    def test_the_observer_takes_no_exit_out_of_assessment(self) -> None:
        """The observer decides; the dispatcher performs. A board move by the observer would be a
        release with nothing merged, so the authority matrix has no exit for it at all."""
        self.assertEqual(
            {edge for edge in CARD_TRANSITIONS["observer"] if edge[0] is CardState.ASSESSMENT}, set()
        )
        self.assertIn((CardState.VALIDATE, CardState.ASSESSMENT), CARD_TRANSITIONS["observer"])

    def test_writer_preserves_the_complete_legacy_role_by_edge_contract(self) -> None:
        """Exercise the public writer instead of proving a copied table equals the registry."""
        dispatcher = {
            ("ready", "in_progress"),
            ("in_progress", "validate"),
            ("in_progress", "blocked"),
            ("in_progress", "ready"),
            ("validate", "in_progress"),
            ("validate", "blocked"),
            ("validate", "done"),
            ("validate", "assessment"),
            ("assessment", "in_progress"),
            ("assessment", "done"),
            ("assessment", "blocked"),
        }
        steward = {
            ("blocked", "ready"),
            ("blocked", "done"),
            ("in_progress", "done"),
            ("ready", "blocked"),
            ("in_progress", "blocked"),
            ("validate", "blocked"),
            ("assessment", "blocked"),
        }
        column_by_state = {
            state: column_id
            for column_id, title in self.writer.reader._board()[1].items()
            if (state := _STATE_BY_COLUMN.get(title))
        }
        self.client.tasks[1]["swimlane_id"] = 0

        def legacy_authorized(role: str, source: str, target: str) -> bool:
            if source == target:
                return False
            if role == "po":
                return True
            if role == "observer":
                return source != "assessment"
            if role == "dispatcher":
                return (source, target) in dispatcher
            if role == "steward":
                return (source, target) in steward
            return False

        with as_observer(SPRINT), mock.patch.object(self.writer, "_sprint_holds_project", return_value=True):
            for role in ("po", "dispatcher", "observer", "steward", "worker", "reviewer", "retro"):
                for source in CARD_STATES:
                    for target in CARD_STATES:
                        card = next(
                            task for task in self.client.tasks if task["reference"] == "secretary-468"
                        )
                        card["column_id"] = column_by_state[source]
                        try:
                            self.writer.move(
                                role=role,
                                actor=role,
                                reference="secretary-468",
                                target=target,
                                reason="authorization contract",
                                request_id=f"transition-contract-{role}-{source}-{target}",
                            )
                        except TaskError as exc:
                            admitted = exc.code != "transition_forbidden"
                        else:
                            admitted = True
                        self.assertEqual(
                            admitted, legacy_authorized(role, source, target), (role, source, target)
                        )

    def _park(self, request_id: str = "into-assessment") -> None:
        self.client.tasks[0]["column_id"] = 4  # Validate
        entered = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="assessment",
            reason="",
            request_id=request_id,
        )
        self.assertEqual(entered["task"]["state"], "assessment")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def _decide(self, kind: str, request_id: str = "") -> dict:
        if not self.client.metadata[12].get("sprint_ref"):
            self.reserve_project()
        return self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind=kind,
            body="the round converged",
            request_id=request_id or f"decision-{kind}",
        )

    def test_dispatcher_moves_a_card_into_and_out_of_assessment(self) -> None:
        self._park()
        self._decide("rework")

        left = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="in_progress",
            reason="",
            decision="rework",
            request_id="out-of-assessment",
        )
        self.assertEqual(left["task"]["state"], "in_progress")
        self.assertEqual(self.client.tasks[0]["column_id"], 3)

    def test_a_release_with_no_recorded_decision_is_refused(self) -> None:
        """The seam's whole point: nothing acts on a parked card that nobody decided about."""
        self._park()

        with self.assertRaisesRegex(TaskError, "recorded decision") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="done",
                reason="",
                request_id="undecided-release",
            )

        self.assertEqual(raised.exception.code, "decision_required")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)
        # The decision rule runs before the typed canon is touched at all: a refused release
        # leaves neither a staged event to recover nor a request id somebody has to release.
        self.assertIsNone(self.writer.audit.event("undecided-release"))
        self.assertEqual(self.writer.audit.status()["pending"], 0)

    def test_a_move_naming_a_decision_nobody_recorded_is_refused(self) -> None:
        """Carrying the word is not deciding: the audit is what the refusal reads."""
        self._park()

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="claimed-release",
            )

        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_a_decision_from_an_earlier_parking_does_not_release_a_later_one(self) -> None:
        """A decision is about the round it was written for, not about every later round."""
        self._park()
        self._decide("release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="first-release",
        )
        self._park(request_id="parked-again")

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="replayed-release",
            )

    def test_exact_decision_replay_survives_dispatcher_leaving_assessment(self) -> None:
        self._park()
        first = self._decide("release", request_id="replay-after-release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-release",
        )

        replay = self._decide("release", request_id="replay-after-release")

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(replay["task"]["state"], "done")
        self.assertEqual(
            len(
                [
                    comment
                    for comment in self.client.comments[12]
                    if comment["comment"] == "[decision:release]\nthe round converged"
                ]
            ),
            1,
        )

    def test_exact_decision_replay_uses_its_original_assessment_visit(self) -> None:
        self._park()
        first = self._decide("release", request_id="replay-after-later-visit")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-first-visit",
        )
        # A PO may return the Card to Validate before the dispatcher parks a
        # second Assessment visit.  The replay must still describe visit one.
        self.client.tasks[0]["column_id"] = 4
        self._park(request_id="park-second-visit")

        replay = self._decide("release", request_id="replay-after-later-visit")

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(
            len(
                [
                    comment
                    for comment in self.client.comments[12]
                    if comment["comment"] == "[decision:release]\nthe round converged"
                ]
            ),
            1,
        )

    def test_unowned_decision_remains_refused_after_leaving_assessment(self) -> None:
        self._park()
        self._decide("release", request_id="first-release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-first-release",
        )

        with self.assertRaisesRegex(TaskError, "only recorded on a card in Assessment") as raised:
            self._decide("release", request_id="unowned-after-release")

        self.assertEqual(raised.exception.code, "transition_forbidden")

    def test_mismatched_decision_replay_is_refused_before_assessment_admission(self) -> None:
        self._park()
        self._decide("release", request_id="mismatched-after-release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-mismatched-release",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._decide("rework", request_id="mismatched-after-release")

        self.assertEqual(raised.exception.code, "validation")

    def test_a_decision_is_recorded_on_the_card_and_in_the_audit(self) -> None:
        self._park()

        decided = self._decide("reslice")

        self.assertEqual(decided["action"], "decided")
        comment = decided["task"]["comments"][-1]
        self.assertEqual(comment["marker"], "decision:reslice")
        self.assertIn("the round converged", comment["body"])
        event = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="decided")[-1]
        self.assertEqual(event["kind"], "card.decided")
        self.assertEqual(event["data"]["decision"], "reslice")
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})

    def test_rework_refuses_a_worker_requirement_for_a_dispatcher_gate_receipt(self) -> None:
        """issue:7360d39d4956435c9cc6: the gate receipt is not worker evidence."""
        self._park()
        self.reserve_project()
        body = "Repair the local implementation and report the focused regression coverage."
        request_id = "dispatcher-receipt-rework"

        with mock.patch("secretary.tasks.specification_revision", return_value="specification-revision-1"):
            with self.assertRaises(ArtifactOwnershipTaskError) as raised:
                self.writer.decide(
                    role="observer",
                    actor="observer",
                    reference="secretary-468",
                    kind="rework",
                    body=body,
                    protocol_prerequisites=("dispatcher_executed_exact_sha_gate_receipt",),
                    request_id=request_id,
                )

            # Retrying the same denied request neither creates a second audit fact nor changes the
            # card into a worker-blocked/external-fact outcome.
            with self.assertRaises(ArtifactOwnershipTaskError) as retried:
                self.writer.decide(
                    role="observer",
                    actor="observer",
                    reference="secretary-468",
                    kind="rework",
                    body=body,
                    protocol_prerequisites=("dispatcher_executed_exact_sha_gate_receipt",),
                    request_id=request_id,
                )

        self.assertEqual(raised.exception.code, "artifact_ownership_violation")
        self.assertEqual(retried.exception.code, "artifact_ownership_violation")
        self.assertIn("owned by dispatcher", raised.exception.message)
        self.assertIn("specification-revision-1", raised.exception.message)
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "assessment")
        self.assertFalse(
            any(comment.get("marker") == "decision:rework" for comment in self.client.comments[12])
        )
        refusals = self.writer.audit.events("secretary-468", kind="card.decision_refused")
        self.assertEqual(len(refusals), 1)
        refusal = refusals[0]["data"]
        self.assertEqual(refusal["code"], "artifact_ownership_violation")
        self.assertEqual(refusal["artifact_owner"], "dispatcher")
        self.assertEqual(refusal["requested_role"], "worker")
        self.assertEqual(refusal["protocol_prerequisites"], ["dispatcher_executed_exact_sha_gate_receipt"])
        self.assertEqual(refusal["specification_revision"], "specification-revision-1")
        self.assertNotIn("external_fact", str(refusal))

        # A corrected instruction uses the normal decision path on this same Assessment visit and
        # candidate. The refusal did not consume the decision's retry identity.
        corrected = self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="rework",
            body="Repair the local implementation and report the focused regression coverage.",
            protocol_prerequisites=("worker_local_broad_check_receipt",),
            request_id=request_id,
        )
        self.assertFalse(corrected["replayed"])
        self.assertEqual(corrected["task"]["state"], "assessment")
        decisions = self.writer.audit.events("secretary-468", kind="card.decided")
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["data"]["assessment_visit"])
        self.assertEqual(decisions[0]["data"]["protocol_prerequisites"], ["worker_local_broad_check_receipt"])

    def test_normal_rework_with_reviewer_verdict_context_is_recordable(self) -> None:
        self._park()
        self.reserve_project()

        decided = self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="rework",
            body="Do not obtain an executed exact-SHA gate receipt. Address each reviewer finding.",
            protocol_prerequisites=(),
            request_id="normal-reviewer-context-rework",
        )

        self.assertFalse(decided["replayed"])
        self.assertEqual(decided["task"]["state"], "assessment")
        self.assertEqual(
            self.writer.audit.events("secretary-468", kind="card.decision_refused"), []
        )
        self.assertEqual(self.writer.audit.events("secretary-468", kind="card.decided")[0]["data"]["decision"], "rework")

    def test_assessment_visit_accepts_one_canonical_decision_across_delivery_retries(self) -> None:
        self._park()

        first = self._decide("release", request_id="decision-first-delivery")
        replay = self._decide("release", request_id="decision-retried-delivery")

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        decisions = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="decided")
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["data"]["assessment_visit"])
        with self.assertRaisesRegex(TaskError, "already has a release decision") as raised:
            self._decide("rework", request_id="decision-conflicting-delivery")
        self.assertEqual(raised.exception.code, "decision_already_recorded")

    def test_concurrent_assessment_decisions_have_one_canonical_winner(self) -> None:
        self._park()
        self.writer._guard_sprint_write = lambda **_kwargs: {}  # type: ignore[method-assign]
        self.writer._sprint_holds_project = lambda _project: True  # type: ignore[method-assign]
        gate = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []

        def decide(kind: str) -> None:
            gate.wait()
            try:
                with as_observer(SPRINT):
                    outcomes.append(
                        (
                            kind,
                            self.writer.decide(
                                role="observer",
                                actor="observer",
                                reference="secretary-468",
                                kind=kind,
                                body=kind,
                                request_id="race-" + kind,
                            ),
                        )
                    )
            except TaskError as exc:
                outcomes.append((kind, exc))

        threads = [threading.Thread(target=decide, args=(kind,)) for kind in ("release", "rework")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        decisions = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="decided")
        self.assertEqual(len(decisions), 1, outcomes)
        self.assertEqual(sum(isinstance(result, dict) for _kind, result in outcomes), 1)
        self.assertEqual(sum(isinstance(result, TaskError) for _kind, result in outcomes), 1)

    def test_concurrent_same_id_decision_mismatch_is_refused(self) -> None:
        self._park()
        self.writer._guard_sprint_write = lambda **_kwargs: {}  # type: ignore[method-assign]
        self.writer._sprint_holds_project = lambda _project: True  # type: ignore[method-assign]
        gate = threading.Barrier(2)
        outcomes: list[object] = []

        def decide(body: str) -> None:
            gate.wait()
            try:
                outcomes.append(
                    self.writer.decide(
                        role="observer",
                        actor="observer",
                        reference="secretary-468",
                        kind="release",
                        body=body,
                        request_id="same-id-different-body",
                    )
                )
            except TaskError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=decide, args=(body,)) for body in ("first body", "different body")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(isinstance(result, dict) for result in outcomes), 1)
        refused = [result for result in outcomes if isinstance(result, TaskError)]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].code, "validation")
        self.assertIn("belongs to another operation", refused[0].message)

    def test_pending_decision_blocks_conflict_until_audit_reconcile(self) -> None:
        self._park()
        real_append = self.writer.audit.append
        failed = [False]

        def fail_once(request_id: str, event: dict) -> str:
            if event.get("kind") == "card.decided" and not failed[0]:
                failed[0] = True
                raise OSError("lost audit append")
            return real_append(request_id, event)

        with mock.patch.object(self.writer.audit, "append", side_effect=fail_once):
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self._decide("release", request_id="pending-release")
        with self.assertRaisesRegex(TaskError, "unfinished release decision") as blocked:
            self._decide("rework", request_id="conflicting-after-crash")
        self.assertEqual(blocked.exception.code, "decision_pending")
        comments = [
            comment
            for comment in self.writer.reader.show("secretary-468")["comments"]
            if comment.get("marker") == "decision:release"
        ]
        self.assertEqual(len(comments), 1)

        repaired, unresolved = self.writer.reconcile()

        self.assertEqual((repaired, unresolved), (1, 0))
        decisions = self.writer.audit.events("secretary-468", kind="decided")
        self.assertEqual(len(decisions), 1)
        replay = self._decide("release", request_id="delivery-after-repair")
        self.assertTrue(replay["replayed"])

    def test_decision_staged_before_comment_fails_closed_until_original_retry(self) -> None:
        self._park()
        real_claim = self.writer.audit.claim
        interrupted = [False]

        def claim_then_crash(request_id: str, event: dict, **kwargs: object) -> object:
            claimed = real_claim(request_id, event, **kwargs)
            if event.get("kind") == "card.decided" and not interrupted[0]:
                interrupted[0] = True
                raise KeyboardInterrupt("crash after stage")
            return claimed

        with mock.patch.object(self.writer.audit, "claim", side_effect=claim_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                self._decide("release", request_id="staged-before-comment")

        self.assertFalse(
            any(
                comment.get("marker") == "decision:release"
                for comment in self.writer.reader.show("secretary-468")["comments"]
            )
        )
        repaired, unresolved = self.writer.reconcile()
        self.assertEqual((repaired, unresolved), (0, 1))
        self.assertEqual(self.writer.audit.events("secretary-468", kind="decided"), [])
        with self.assertRaisesRegex(TaskError, "recorded decision"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="done",
                reason="",
                request_id="must-not-act-on-commentless-decision",
            )

        # A staged occurrence is ambiguous evidence.  Retrying it must not
        # issue a second comment write, because the first process could have
        # reached Kanboard immediately before it died.
        with self.assertRaisesRegex(TaskError, "audit repair"):
            self._decide("release", request_id="staged-before-comment")
        self.assertEqual(self.writer.audit.events("secretary-468", kind="decided"), [])

    def test_observer_wake_predicate_excludes_routine_and_self_card_events(self) -> None:
        refs = {"secretary-468"}

        def event(kind: str, *, actor: str, ref: str = "secretary-468", payload: dict | None = None) -> dict:
            return {
                "ref": ref,
                "kind": kind,
                "outcome": "success",
                "actor": {"role": actor},
                "payload": payload or {},
            }

        for routine in (
            event("created", actor="observer"),
            event("claimed", actor="dispatcher"),
            event("reported", actor="worker"),
            event("commented", actor="dispatcher"),
            event("moved", actor="dispatcher", payload={"to": "validate"}),
            event("moved", actor="dispatcher", payload={"from": "assessment", "to": "issues"}),
            event("routing", actor="dispatcher"),
            event("decided", actor="observer", payload={"decision": "release"}),
        ):
            self.assertFalse(
                is_significant_observer_event(
                    routine,
                    linked_refs=refs,
                    sprint_ref=SPRINT,
                )
            )
        for semantic in (
            event("moved", actor="dispatcher", payload={"to": "assessment"}),
            event("moved", actor="dispatcher", payload={"to": "blocked"}),
            event("moved", actor="dispatcher", payload={"to": "done"}),
            event("moved", actor="po", payload={"from": "in_progress", "to": "issues"}),
            event("moved", actor="po", payload={"from": "validate", "to": "issues"}),
            event("moved", actor="po", payload={"from": "assessment", "to": "issues"}),
            event("moved", actor="steward", payload={"from": "assessment", "to": "issues"}),
            event("budget_recorded", actor="dispatcher", ref=SPRINT),
            event("commented", actor="po", ref=SPRINT),
        ):
            self.assertTrue(
                is_significant_observer_event(
                    semantic,
                    linked_refs=refs,
                    sprint_ref=SPRINT,
                )
            )

    def test_observer_wake_predicate_reads_typed_transitions_by_their_own_shape(self) -> None:
        """The migrated representation carries the same semantics, in its own fields.

        The predicate is told which record it holds by ``record_type``; it never reads a
        transition out of a generic payload or an outcome out of a typed one.
        """
        refs = {"secretary-468"}

        def typed(source: CardState, target: CardState, *, actor: str = "dispatcher") -> dict:
            declaration = transition_for(EntityKind.CARD, source, target)
            return Event(
                f"board-event-{actor}-{source.value}-{target.value}",
                declaration.event_kind,
                EntityKind.CARD,
                "secretary-468",
                Actor(actor, actor),
                "why",
                datetime(2026, 8, 11, tzinfo=UTC),
                source_state=source.value,
                target_state=target.value,
            ).to_record(f"request-{actor}-{source.value}-{target.value}")

        for routine in (
            typed(CardState.READY, CardState.IN_PROGRESS),
            typed(CardState.IN_PROGRESS, CardState.VALIDATE),
            typed(CardState.VALIDATE, CardState.IN_PROGRESS),
            typed(CardState.IN_PROGRESS, CardState.READY),
            # The observer's own writes never wake it again, whatever the edge says.
            typed(CardState.VALIDATE, CardState.BLOCKED, actor="observer"),
        ):
            self.assertFalse(
                is_significant_observer_event(routine, linked_refs=refs, sprint_ref=SPRINT),
                routine,
            )
        for semantic in (
            typed(CardState.VALIDATE, CardState.ASSESSMENT),
            typed(CardState.IN_PROGRESS, CardState.BLOCKED),
            typed(CardState.ASSESSMENT, CardState.DONE),
            typed(CardState.IN_PROGRESS, CardState.ISSUES, actor="po"),
        ):
            self.assertTrue(
                is_significant_observer_event(semantic, linked_refs=refs, sprint_ref=SPRINT),
                semantic,
            )

    def test_a_decision_needs_a_parked_card_a_reason_and_a_permitted_role(self) -> None:
        self._park()
        with self.assertRaisesRegex(TaskError, "non-empty reason"):
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="  ",
                request_id="empty-reason",
            )
        with self.assertRaisesRegex(TaskError, "decision must be one of"):
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="merge",
                body="ship it",
                request_id="unknown-kind",
            )
        with self.assertRaisesRegex(TaskError, "role is not permitted"):
            self.writer.decide(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="worker-decision",
            )
        # The card leaves the column. Its project stays reserved by the observer's own sprint, so
        # what refuses this is the state and not the reservation.
        self.reserve_project()
        self.client.tasks[0]["column_id"] = 4
        with self.assertRaisesRegex(TaskError, "only recorded on a card in Assessment"):
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="unparked-decision",
            )

    def test_a_blocked_escalation_out_of_assessment_needs_no_decision(self) -> None:
        """Blocked stays reachable without one: it is what rescues a card nobody decided about."""
        self._park()

        escalated = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="blocked",
            reason="the release could not land",
            request_id="parked-card-blocked",
        )

        self.assertEqual(escalated["task"]["state"], "blocked")

    def test_only_the_observer_decides(self) -> None:
        """One authority for the decision. A PO that has to intervene overrides visibly."""
        self._park()

        with self.assertRaisesRegex(TaskError, "role is not permitted"):
            self.writer.decide(
                role="po",
                actor="operator",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="po-decision",
            )

    def test_a_decision_moves_the_card_where_that_decision_goes(self) -> None:
        """A recorded release paired with a move back to In progress is a rework nobody decided."""
        self._park()
        self._decide("release")

        with self.assertRaisesRegex(TaskError, "release decision moves the card to done") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="in_progress",
                reason="",
                decision="release",
                request_id="release-to-in-progress",
            )

        self.assertEqual(raised.exception.code, "decision_mismatch")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_the_undecided_exits_from_assessment_are_closed(self) -> None:
        """Ready, Validate and Issues all leave the column with nothing decided, and Ready also
        clears the claim, which is what would let a second worker start on a reviewed checkout."""
        self._park()

        for target in ("ready", "validate", "issues"):
            with self.assertRaises(TaskError) as raised:
                self.writer.move(
                    role="dispatcher",
                    actor="d",
                    reference="secretary-468",
                    target=target,
                    reason="",
                    request_id=f"dispatcher-bypass-{target}",
                )
            self.assertIn(raised.exception.code, {"decision_required", "transition_forbidden"})
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_the_observer_may_not_perform_its_own_decision(self) -> None:
        """The observer records the decision; the dispatcher performs it.

        A matching decision is checkable, but a board move is not a release: the card would read
        Done with nothing merged, In progress with no worker relaunched. So every
        decision-carrying exit is refused to the observer, on its own sprint's card and with the
        decision standing on the card.
        """
        self._park()

        self._decide("release", request_id="decision-performed-by-observer")
        with self.assertRaises(TaskError) as raised:
            self.writer.move(
                role="observer",
                actor="observer",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="observer-performs-release",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertIn("task decide", str(raised.exception))
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

        # And the dispatcher performs the one canonical decision that is standing.
        performed = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="dispatcher-performs-release",
        )
        self.assertEqual(performed["task"]["state"], "done")

    def test_a_decision_needs_an_open_sprint_to_hold_the_project(self) -> None:
        """A decision is refused where no open sprint holds the card's project, the reservation
        `move` already checks. What it does not do is say who the caller is: see the test below.
        """
        self._park()
        # A bound caller, so what is being tested is the reservation and not the identity: this
        # observer is somebody's head, and the card it reaches for is held by no open sprint.
        bind_observer(self, SPRINT)

        with self.assertRaisesRegex(TaskError, "role is not permitted") as unheld:
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="decision-without-a-sprint",
            )
        self.assertEqual(unheld.exception.code, "role_forbidden")

        # A card linked to another sprint than the one holding its project is refused too, and
        # refused as the reservation it crosses.
        self.reserve_project(card_sprint="sprint:1030")
        with self.assertRaises(TaskError) as other:
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="decision-from-another-sprint",
            )
        self.assertEqual(other.exception.code, "sprint_write_forbidden")
        self.assertEqual(standing_decision(TaskAudit(Path(self.tmpdir.name)).events("secretary-468")), "")

    def test_the_decision_guard_also_places_the_caller(self) -> None:
        """The other half of the guard: which sprint's observer is writing.

        Every observer process still runs as `--role observer --actor observer`, so the actor id
        places nobody. The sprint its head was launched for does: the card's own observer decides,
        and a head of another sprint is refused as the identity failure it is.
        """
        self._park()
        self.reserve_project()

        decided = self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="release",
            body="deciding from this card's own head",
            request_id="decision-from-its-own-head",
        )

        self.assertEqual(decided["action"], "decided")
        event = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="decided")[-1]
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})

        self._park(request_id="park-again")
        with as_observer("sprint:2000"), self.assertRaises(TaskError) as stranger:
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="deciding about a sprint I do not observe",
                request_id="decision-from-another-head",
            )
        self.assertEqual(stranger.exception.code, "observer_sprint_mismatch")
        denial = TaskAudit(Path(self.tmpdir.name)).events("secretary-468", kind="sprint_guard_denied")[-1]
        self.assertEqual(denial["payload"]["code"], "observer_sprint_mismatch")
        self.assertEqual(denial["payload"]["sprint"], "sprint:2000")

        with self.assertRaises(TaskError) as unbound, unbound_observer():
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="deciding from a head nobody bound",
                request_id="decision-from-an-unbound-head",
            )
        self.assertEqual(unbound.exception.code, "observer_identity_unbound")

    def test_a_po_override_still_takes_a_parked_card_back_to_ready(self) -> None:
        """The escape hatch stays open, and it is recorded as the override it is."""
        self._park()

        requeued = self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-468",
            target="ready",
            reason="taking this one back by hand",
            request_id="po-requeue",
        )

        self.assertEqual(requeued["task"]["state"], "ready")

    def test_a_po_override_takes_a_parked_card_to_the_decided_targets_too(self) -> None:
        """The escape hatch is the whole exit, not the two thirds of it that need nothing decided.

        A seam stuck with no observer to release it is exactly when an operator has to finish or
        return a parked card by hand, and Done and In progress are where it would send it. Only
        the dispatcher is held to a recorded decision, because only the dispatcher performs one.
        """
        self.reserve_project()
        for target, request_id in (("done", "po-release"), ("in_progress", "po-return")):
            self._park(request_id=f"{request_id}-park")

            moved = self.writer.move(
                role="po",
                actor="operator",
                reference="secretary-468",
                target=target,
                reason="finishing this one by hand",
                sprint_override=True,
                sprint_override_reason="no observer is coming back for it",
                request_id=request_id,
            )

            self.assertEqual(moved["task"]["state"], target)

    def test_a_po_move_out_of_assessment_still_checks_a_decision_it_names(self) -> None:
        """Not being held to a decision is not licence to invent one: a decision the PO passes is
        read against the card and its destination like anybody else's."""
        self._park()

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="po",
                actor="operator",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="po-claimed-release",
            )
        self._decide("release")
        with self.assertRaises(TaskError) as mismatched:
            self.writer.move(
                role="po",
                actor="operator",
                reference="secretary-468",
                target="in_progress",
                reason="",
                decision="release",
                sprint_override=True,
                sprint_override_reason="stepping in on a reserved project",
                request_id="po-mismatched-release",
            )

        self.assertEqual(mismatched.exception.code, "decision_mismatch")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

    def test_worker_may_not_move_a_card_out_of_assessment(self) -> None:
        self.client.tasks[0]["column_id"] = 7
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(
                role="worker",
                actor="w",
                reference="secretary-468",
                target="done",
                reason="",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))

    def test_steward_escalates_an_assessment_card_with_a_reason(self) -> None:
        self.client.tasks[0]["column_id"] = 7
        with self.assertRaisesRegex(TaskError, "non-empty reason"):
            self.writer.move(
                role="steward",
                actor="s",
                reference="secretary-468",
                target="blocked",
                reason="",
            )
        escalated = self.writer.move(
            role="steward",
            actor="s",
            reference="secretary-468",
            target="blocked",
            reason="the observer never came back",
            request_id="assessment-escalation",
        )
        self.assertEqual(escalated["task"]["state"], "blocked")
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "blocked")

    def _move_cli(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "move",
                    "--ref",
                    "secretary-468",
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "data"),
                    *arguments,
                ]
            )
        return code, output.getvalue(), errors.getvalue()

    def test_cli_move_target_assessment_moves_the_card(self) -> None:
        """Criterion 3 spells this `--target`; `--to` is the same argument under another name."""
        self.client.tasks[0]["column_id"] = 4  # Validate
        code, output, errors = self._move_cli(
            "--role",
            "dispatcher",
            "--target",
            "assessment",
            "--request-id",
            "cli-target",
        )

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["action"], "moved")
        self.assertEqual(self.client.tasks[0]["column_id"], 7)

        # The way back out is the decision path, through the CLI as well: the writer checks
        # `--decision` against the audit, so the recorded decision has to come first.
        code, output, errors = self._move_cli(
            "--role",
            "dispatcher",
            "--to",
            "done",
            "--request-id",
            "cli-to-undecided",
        )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(errors)["error"]["code"], "decision_required")

        # The CLI writes its audit and its sprint guard index under its own data dir, so both the
        # decision and the reservation that authorizes it have to be set up there.
        self.reserve_project(data_dir=str(Path(self.tmpdir.name) / "data"))
        reason = Path(self.tmpdir.name) / "reason.md"
        reason.write_text("ship it", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            decided = main(
                [
                    "task",
                    "decide",
                    "--ref",
                    "secretary-468",
                    "--role",
                    "observer",
                    "--kind",
                    "release",
                    "--reason-file",
                    str(reason),
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "data"),
                    "--request-id",
                    "cli-decision",
                ]
            )
        self.assertEqual((decided, errors.getvalue()), (0, ""))
        self.assertEqual(json.loads(output.getvalue())["action"], "decided")
        code, output, errors = self._move_cli(
            "--role",
            "dispatcher",
            "--to",
            "done",
            "--decision",
            "release",
            "--request-id",
            "cli-to",
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.client.tasks[0]["column_id"], 6)

    def test_cli_move_target_assessment_is_refused_for_a_forbidden_role(self) -> None:
        self.client.tasks[0]["column_id"] = 4
        code, output, errors = self._move_cli(
            "--role",
            "worker",
            "--target",
            "assessment",
            "--request-id",
            "cli-forbidden",
        )

        self.assertEqual((code, output), (3, ""))
        self.assertEqual(json.loads(errors)["error"]["code"], "transition_forbidden")
        self.assertEqual(self.client.tasks[0]["column_id"], 4)

    def test_cli_choice_lists_accept_assessment_where_a_state_is_legal(self) -> None:
        """`list --state` and `move --to` take it; `create --state` still cannot open a card there."""
        choices = _task_state_choices()
        self.assertIn("assessment", choices[("list", "state")])
        self.assertIn("assessment", choices[("move", "to")])
        self.assertEqual(choices[("create", "state")], ("issues", "ready"))
        # One argument, two spellings: `--target` is not a second option with its own dest.
        self.assertEqual(sorted(_move_target_option_strings()), ["--target", "--to"])


def _move_target_option_strings() -> list[str]:
    """Every flag `task move` accepts for the destination state."""
    from secretary.task_commands import add_task_subcommands

    parser = argparse.ArgumentParser()
    add_task_subcommands(parser.add_subparsers(dest="command"))
    task = parser._subparsers._group_actions[0].choices["task"]  # type: ignore[union-attr]
    move = task._subparsers._group_actions[0].choices["move"]  # type: ignore[union-attr]
    return [option for action in move._actions if action.dest == "to" for option in action.option_strings]


def _task_state_choices() -> dict[tuple[str, str], tuple[str, ...]]:
    """{(task subcommand, argument dest): its choices} for every state-valued task argument."""
    from secretary.task_commands import add_task_subcommands

    parser = argparse.ArgumentParser()
    add_task_subcommands(parser.add_subparsers(dest="command"))
    task = parser._subparsers._group_actions[0].choices["task"]  # type: ignore[union-attr]
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for name, sub in task._subparsers._group_actions[0].choices.items():  # type: ignore[union-attr]
        for action in sub._actions:
            if action.dest in {"state", "to"} and action.choices:
                found[(name, action.dest)] = tuple(action.choices)
    return found


_READ_METHODS = {
    "getProjectByName",
    "getActiveSwimlanes",
    "getTaskByReference",
    "getTaskMetadata",
    "getAllComments",
    "getTask",
}


class RoutingJournalTests(unittest.TestCase):
    """secretary-716: the routing record is journal-only and must survive everything the board
    forgets: the reviewer head cleared on the way out of Validate, the routing block reset on the
    way back to Ready."""

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)
        self.audit = TaskAudit(self.tmpdir.name)

    def _run(self, role: str, head: str):
        return head_run_from_profile(
            role=role,
            head=head,
            head_source="role_default",
            profile={
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "extra",
                "resource": "openai-sub",
            },
            resources={"openai-sub": {"account": "openai-subscription"}},
        )

    def _payload(self, attempt: int, phase: str, *heads: tuple[str, str], outcome: str = "") -> dict:
        return routing_payload(
            attempt=attempt,
            attempt_id="att-1",
            phase=phase,
            heads=[self._run(role, head) for role, head in heads],
            outcome=outcome,
        )

    def test_routing_writes_the_journal_without_touching_the_board(self) -> None:
        self.writer.routing(
            role="dispatcher",
            actor="pilot",
            reference="secretary-468",
            payload=self._payload(1, "worker", ("worker", "codex")),
            request_id="routing-1",
        )

        events = self.audit.events("secretary-468", kind="routing")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["heads"][0]["head"], "codex")
        self.assertEqual(
            [call for call in self.client.calls if call[0] not in _READ_METHODS],
            [],
            "a routing record is telemetry; it must not mutate the card",
        )

    def test_repeated_routing_record_commits_once(self) -> None:
        for _ in range(2):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(1, "worker", ("worker", "codex")),
                request_id="routing-1",
            )

        self.assertEqual(len(self.audit.events("secretary-468", kind="routing")), 1)

    def test_only_the_dispatcher_may_write_routing(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted"):
            self.writer.routing(
                role="worker",
                actor="w",
                reference="secretary-468",
                payload=self._payload(1, "worker", ("worker", "codex")),
            )

    def test_routing_rejects_an_unknown_phase_and_an_empty_head_list(self) -> None:
        with self.assertRaisesRegex(TaskError, "unknown routing phase"):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload={"attempt": 1, "phase": "guess", "heads": [{"role": "worker"}]},
            )
        with self.assertRaisesRegex(TaskError, "at least one head"):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload={"attempt": 1, "phase": "worker", "heads": []},
            )

    def test_attempts_rebuild_the_pairs_and_their_outcomes(self) -> None:
        for attempt, outcome in ((1, "red"), (2, "green")):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(attempt, "worker", ("worker", "codex")),
                request_id=f"routing-worker-{attempt}",
            )
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(attempt, "review", ("reviewer", "codex-reviewer")),
                request_id=f"routing-review-{attempt}",
            )
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(
                    attempt,
                    "verdict",
                    ("worker", "codex"),
                    ("reviewer", "codex-reviewer"),
                    outcome=outcome,
                ),
                request_id=f"routing-verdict-{attempt}",
            )

        history = attempts(self.audit.events("secretary-468", kind="routing"))
        self.assertEqual([record.attempt for record in history], [1, 2])
        self.assertEqual([record.outcome for record in history], ["red", "green"])
        self.assertEqual([record.worker.head for record in history], ["codex", "codex"])
        self.assertEqual([record.reviewer.head for record in history], ["codex-reviewer"] * 2)

    def test_a_head_without_a_model_must_say_the_cli_resolved_it(self) -> None:
        """The blank-model guard: a record may only omit the model when it names the runtime that
        picked one, so `claude-default` can never be journalled as a silent empty string."""
        with self.assertRaisesRegex(ValueError, "unpinned model"):
            HeadRun(role="worker", head="claude-default", model_source="profile")

        unpinned = head_run_from_profile(
            role="reviewer",
            head="claude-default",
            head_source="card",
            profile={"adapter": "claude", "resource": "claude-sub"},
            resources={"claude-sub": {"account": "claude-subscription"}},
        )

        self.assertEqual((unpinned.model, unpinned.model_source), ("", "cli_default"))

    def test_a_codex_record_names_the_one_launch_mode_whatever_it_was_asked_for(self) -> None:
        """The journal records the mode the head actually ran in, and there is one.

        A profile that still pins the retired `exec`, and a card that still carries it, are both
        legacy routing data. Neither may put a mode in the journal that no bring-up on this
        product could have produced.
        """
        run = head_run_from_profile(
            role="worker",
            head="codex",
            head_source="card",
            profile={
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
                "resource": "openai-sub",
                "codex_mode": "exec",
            },
            resources={"openai-sub": {"account": "openai-subscription"}},
        )

        self.assertEqual(run.codex_mode, "tui")
        self.assertNotIn(
            "codex_mode",
            inspect.signature(head_run_from_profile).parameters,
            "no caller may hand the journal a launch mode of its own",
        )

    def test_an_old_journal_record_is_read_back_as_it_was_written(self) -> None:
        """History is read, not rewritten: an attempt that really ran one-shot still says so."""
        legacy = HeadRun.from_json(
            {
                "role": "worker",
                "head": "codex",
                "head_source": "card",
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "model_source": "profile",
                "effort": "default",
                "codex_mode": "exec",
                "resource": "openai-sub",
                "account": "openai-subscription",
            }
        )

        self.assertEqual(legacy.codex_mode, "exec")

    def test_claude_effort_is_part_of_the_routing_record(self) -> None:
        run = head_run_from_profile(
            role="reviewer",
            head="claude-opus-medium",
            head_source="card",
            profile={
                "adapter": "claude",
                "model": "opus",
                "effort": "medium",
                "resource": "claude-sub",
            },
            resources={"claude-sub": {"account": "claude-subscription"}},
        )

        self.assertEqual(run.effort, "medium")

    def test_head_run_round_trips_session_and_prompt_identity(self) -> None:
        run = HeadRun(
            role="worker",
            head="codex",
            adapter="codex",
            model="gpt-5.6-terra",
            model_source="profile",
            session_id="rollout-123",
            prompt_path="/workspaces/card/TASK.md",
            prompt_version="sha256:" + "a" * 64,
        )

        restored = HeadRun.from_json(run.to_json())

        self.assertEqual(restored, run)
        self.assertEqual(restored.session_id_reason, "")

    def test_launch_prompt_identity_does_not_reread_a_mutated_worker_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            document = Path(tmp) / "TASK.md"
            original = b"first launch instruction\n"
            document.write_bytes(original)
            lifecycle = LifecycleHeadRun(
                run_id="worker-launch",
                spec=HeadSpec(profile_id="codex", adapter="codex"),
                workspace=tmp,
                task_ref=TaskRef.card("secretary-1517", document=str(document)),
                role="worker",
                fanout_policy={
                    "version": 1,
                    "state": "unknown",
                    "terminal_state": "unknown",
                    "events": [],
                    "prompt_identity": {
                        "path": str(document),
                        "version": "sha256:" + hashlib.sha256(original).hexdigest(),
                    },
                },
            )
            document.write_text("later rework instruction\n", encoding="utf-8")

            snapshot = launched_head_run_snapshot(
                self._run("worker", "codex"), lifecycle_run=lifecycle.to_json()
            )

        self.assertEqual(snapshot["prompt_path"], str(document))
        self.assertEqual(snapshot["prompt_version"], "sha256:" + hashlib.sha256(original).hexdigest())


class ReportDurabilityGateTests(unittest.TestCase):
    """`report --kind done` refuses to run from a dirty workspace (secretary-653).

    The gate lives in the worker's own session so it can commit and retry, instead of
    learning from the dispatcher post-factum that the card went to blocked."""

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.workspace = Path(self.tmpdir.name) / "workspace"
        self.workspace.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "worker@example.invalid"],
            ["config", "user.name", "worker"],
        ):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True)
        (self.workspace / "code.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-qm", "work"], check=True, capture_output=True
        )
        self.writer = TaskWriter(
            self.client,  # type: ignore[arg-type]
            data_dir=str(Path(self.tmpdir.name) / "data"),
            workspace=str(self.workspace),
        )

    def _report(self, kind: str, body: str = "ready") -> dict:
        classification = "external_fact" if kind == "blocked" else ""
        return self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind=kind,
            body=body,
            classification=classification,
        )

    def test_clean_workspace_reports_done(self) -> None:
        self.assertEqual(self._report("done")["action"], "reported")

    def test_dirty_workspace_is_refused_without_touching_the_board(self) -> None:
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        with self.assertRaises(TaskError) as caught:
            self._report("done")
        self.assertEqual(caught.exception.code, "uncommitted")
        self.assertNotEqual(caught.exception.exit_code, 0)
        self.assertIn("code.py", caught.exception.message)
        self.assertIn("commit", caught.exception.message)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_exact_done_report_replays_after_the_workspace_becomes_dirty(self) -> None:
        first = self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="done",
            body="ready",
            request_id="replay-after-dirt",
        )
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")

        replay = self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="done",
            body="ready",
            request_id="replay-after-dirt",
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(len(self.client.comments[12]), 1)

    def test_untracked_file_is_refused(self) -> None:
        (self.workspace / "scratch.py").write_text("print(3)\n", encoding="utf-8")
        with self.assertRaises(TaskError) as caught:
            self._report("done")
        self.assertEqual(caught.exception.code, "uncommitted")
        self.assertIn("scratch.py", caught.exception.message)

    def test_runtime_audit_tail_does_not_block_done(self) -> None:
        board = self.workspace / "secretary-data" / "board"
        board.mkdir(parents=True)
        (board / "events.ndjson").write_text("{}\n", encoding="utf-8")
        self.assertEqual(self._report("done")["action"], "reported")

    def test_blocked_report_is_not_gated(self) -> None:
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        self.assertEqual(self._report("blocked", body="stuck on the adapter")["action"], "reported")

    def test_non_git_workspace_is_not_gated(self) -> None:
        plain = Path(self.tmpdir.name) / "plain"
        plain.mkdir()
        writer = TaskWriter(
            self.client,  # type: ignore[arg-type]
            data_dir=str(Path(self.tmpdir.name) / "data"),
            workspace=str(plain),
        )
        result = writer.report(role="worker", actor="w", reference="secretary-468", kind="done", body="ok")
        self.assertEqual(result["action"], "reported")

    def test_cwd_is_the_default_workspace(self) -> None:
        writer = TaskWriter(self.client, data_dir=str(Path(self.tmpdir.name) / "data"))  # type: ignore[arg-type]
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        with mock.patch("secretary.tasks.Path.cwd", return_value=self.workspace):
            with self.assertRaises(TaskError) as caught:
                writer.report(role="worker", actor="w", reference="secretary-468", kind="done", body="ok")
        self.assertEqual(caught.exception.code, "uncommitted")


class BlockedContractTests(unittest.TestCase):
    """Why a card is blocked, and what the observer did about it (secretary-1034).

    Both halves are recorded rather than left in prose: the worker names the kind of blocker
    it hit, and the observer's move out of Blocked carries the reason it moved.
    """

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # A workspace outside git, so the done report's durability gate is not what these
        # tests are measuring.
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir()
        self.writer = TaskWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmpdir.name,
            workspace=str(workspace),
        )

    def _events(self, kind: str) -> list[dict]:
        with open(self.writer.audit.events_path, encoding="utf-8") as events:
            typed = {"reported": "card.reported", "verdict": "card.verdict", "decided": "card.decided"}
            return [event for event in map(json.loads, events) if event["kind"] in {kind, typed.get(kind)}]

    def _reserve(self) -> None:
        bind_observer(self, SPRINT)
        self.client.metadata[12]["sprint_ref"] = SPRINT
        reader = FakeSprintReader({"ref": SPRINT, "status": "open", "reservations": ["secretary"]})
        patcher = mock.patch("secretary.sprints.SprintReader", return_value=reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        refresh_active_sprint_projects(self.tmpdir.name, reader)

    def test_a_blocked_report_without_a_classification_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "require --classification") as raised:
            self.writer.report(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="blocked",
                body="the upstream API is down",
                request_id="blocked-unclassified",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.calls, [])

    def test_an_unknown_classification_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "require --classification"):
            self.writer.report(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="blocked",
                body="stuck",
                classification="something_else",
                request_id="blocked-unknown",
            )
        self.assertEqual(self.client.calls, [])

    def test_each_classification_reaches_the_audit_and_the_card(self) -> None:
        for index, classification in enumerate(("external_fact", "wrong_task_definition")):
            with self.subTest(classification=classification):
                result = self.writer.report(
                    role="worker",
                    actor="w",
                    reference="secretary-468",
                    kind="blocked",
                    body="stuck on the adapter",
                    classification=classification,
                    request_id=f"blocked-{classification}",
                )
                self.assertEqual(result["action"], "reported")
                event = self._events("reported")[index]
                self.assertEqual(event["record_type"], "board.protocol_event")
                self.assertEqual(event["kind"], "card.reported")
                payload = event["data"]
                self.assertEqual(payload["marker"], "report:blocked")
                self.assertEqual(payload["classification"], classification)
                self.assertEqual(
                    payload["body_sha256"],
                    hashlib.sha256(b"stuck on the adapter").hexdigest(),
                )
                comment = self.client.comments[12][-1]["comment"]
                self.assertTrue(comment.startswith("[report:blocked]\n"))
                self.assertIn(f"classification: {classification}", comment)
                self.assertIn("stuck on the adapter", comment)

    def test_a_blocked_report_is_a_single_backend_write(self) -> None:
        """Two writes could disagree; the comment and the audit event cannot."""
        self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="blocked",
            body="stuck",
            classification="external_fact",
            request_id="blocked-one-write",
        )
        written = [
            method
            for method, _ in self.client.calls
            if method.startswith(("create", "save", "move", "update"))
        ]
        self.assertEqual(written, ["createComment"])

    def test_a_done_report_carries_no_classification(self) -> None:
        result = self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="done",
            body="ready",
            request_id="done-no-classification",
        )
        self.assertEqual(result["action"], "reported")
        self.assertIsNone(self._events("reported")[0]["data"]["classification"])
        self.assertNotIn("classification:", self.client.comments[12][-1]["comment"])
        with self.assertRaisesRegex(TaskError, "no classification") as raised:
            self.writer.report(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="done",
                body="ready",
                classification="external_fact",
                request_id="done-with-classification",
            )
        self.assertEqual(raised.exception.code, "validation")

    def test_the_cli_refuses_an_unclassified_blocked_report(self) -> None:
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("the upstream API is down\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "report",
                    "--role",
                    "worker",
                    "--ref",
                    "secretary-468",
                    "--kind",
                    "blocked",
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "cli"),
                    "--body-file",
                    str(body),
                    "--request-id",
                    "cli-blocked-unclassified",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")

    def test_the_cli_records_a_classified_blocked_report(self) -> None:
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("the card contradicts itself\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.KanboardClient.for_instance", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "report",
                    "--role",
                    "worker",
                    "--ref",
                    "secretary-468",
                    "--kind",
                    "blocked",
                    "--classification",
                    "wrong_task_definition",
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "cli"),
                    "--body-file",
                    str(body),
                    "--request-id",
                    "cli-blocked-classified",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["action"], "reported")
        comment = self.client.comments[12][-1]["comment"]
        self.assertIn("classification: wrong_task_definition", comment)

    def test_an_observer_moving_a_card_out_of_blocked_must_say_why(self) -> None:
        self._reserve()
        self.client.tasks[0]["column_id"] = 5  # Blocked

        with self.assertRaisesRegex(TaskError, "out of Blocked requires a non-empty reason") as raised:
            self.writer.move(
                role="observer",
                actor="observer",
                reference="secretary-468",
                target="ready",
                reason="   ",
                request_id="observer-silent-disposition",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.tasks[0]["column_id"], 5)

        reason = "the upstream fix landed, the card is workable again"
        moved = self.writer.move(
            role="observer",
            actor="observer",
            reference="secretary-468",
            target="ready",
            reason=reason,
            request_id="observer-disposition",
        )
        self.assertEqual(moved["task"]["state"], "ready")
        event = self._events("card.unblocked")[-1]
        self.assertEqual(event["transition"], {"source": "blocked", "target": "ready"})
        self.assertEqual(event["reason"], reason)
        # The sprint the card belongs to is related to its transition without the caller
        # having to say so, which is what keeps a sprint's own history complete.
        self.assertEqual(event["related_refs"], [SPRINT])
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})
        self.assertIn(reason, self.client.comments[12][-1]["comment"])

        # Every exit is guarded, not just the requeue to Ready.
        self.client.tasks[0]["column_id"] = 5
        with self.assertRaisesRegex(TaskError, "out of Blocked requires a non-empty reason"):
            self.writer.move(
                role="observer",
                actor="observer",
                reference="secretary-468",
                target="in_progress",
                reason="",
                request_id="observer-silent-resume",
            )

    def test_the_observer_may_still_move_a_card_into_blocked_without_a_reason(self) -> None:
        """Only the exit is guarded here. The entry paths are unchanged."""
        self._reserve()
        self.client.tasks[0]["column_id"] = 3  # In progress
        moved = self.writer.move(
            role="observer",
            actor="observer",
            reference="secretary-468",
            target="blocked",
            reason="",
            request_id="observer-into-blocked",
        )
        self.assertEqual(moved["task"]["state"], "blocked")

    def test_the_record_of_a_block_survives_the_card_leaving_blocked(self) -> None:
        """The classification is history, not card state: nothing on the card to go stale."""
        self._reserve()
        self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="blocked",
            body="the upstream API is down",
            classification="external_fact",
            request_id="blocked-before-requeue",
        )
        self.client.tasks[0]["column_id"] = 5  # Blocked
        requeued = self.writer.move(
            role="observer",
            actor="observer",
            reference="secretary-468",
            target="ready",
            reason="the upstream fix landed",
            request_id="observer-requeue",
        )
        self.assertEqual(requeued["task"]["state"], "ready")
        self.assertNotIn("blocked_classification", requeued["task"])
        self.assertNotIn("blocked_classification", self.client.metadata[12])
        self.assertEqual(self._events("reported")[0]["data"]["classification"], "external_fact")

    def test_the_steward_requirement_is_untouched(self) -> None:
        self.client.tasks[0]["column_id"] = 3  # In progress
        with self.assertRaisesRegex(TaskError, "this steward transition requires a non-empty reason"):
            self.writer.move(
                role="steward",
                actor="s",
                reference="secretary-468",
                target="blocked",
                reason="",
            )
        escalated = self.writer.move(
            role="steward",
            actor="s",
            reference="secretary-468",
            target="blocked",
            reason="the head went silent",
            request_id="steward-escalation",
        )
        self.assertEqual(escalated["task"]["state"], "blocked")
        # And its own exit out of Blocked keeps the shape it had: Ready needs nothing, Done does.
        self.assertEqual(
            self.writer.move(
                role="steward",
                actor="s",
                reference="secretary-468",
                target="ready",
                reason="",
                request_id="steward-requeue",
            )["task"]["state"],
            "ready",
        )


class RequestIdOwnershipTests(unittest.TestCase):
    """A request id owns the operation it committed (secretary-1060).

    A retained worker reused the previous round's report id while submitting the next
    round's body. The committed event was replayed, no comment was appended, and the
    caller was told the report succeeded, so the dispatcher waited for a marker that
    could never arrive. The id has to be refused, not replayed.
    """

    def setUp(self) -> None:
        self.client = WriteKanboard()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # Outside git, so the done report's durability gate is not what these tests measure.
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir()
        self.writer = TaskWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmpdir.name,
            workspace=str(workspace),
        )

    def _events(self, request_id: str = "") -> list[dict]:
        try:
            with open(self.writer.audit.events_path, encoding="utf-8") as events:
                recorded = [json.loads(line) for line in events if line.strip()]
        except FileNotFoundError:
            return []
        if not request_id:
            return recorded
        return [event for event in recorded if event["request_id"] == request_id]

    def _comments(self, task_id: int = 12) -> list[str]:
        return [str(comment["comment"]) for comment in self.client.comments[task_id]]

    def _report(self, **overrides: object) -> dict:
        call = {
            "role": "worker",
            "actor": "w",
            "reference": "secretary-468",
            "kind": "done",
            "body": "first round",
            "request_id": "round-1",
        }
        call.update(overrides)
        return self.writer.report(**call)  # type: ignore[arg-type]

    def test_a_reused_report_id_with_another_body_is_refused(self) -> None:
        """The live shape of issue:df7d0778b26357e60046."""
        first = self._report()
        self.client.calls.clear()

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._report(body="third round, a different report entirely")

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["event_id"], first["event_id"])
        self.assertEqual(len(self._comments()), 1)

    def test_the_same_report_under_the_same_id_stays_idempotent(self) -> None:
        first = self._report()
        second = self._report()

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["action"], "reported")
        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(len(self._comments()), 1)

    def test_structured_output_tells_a_replay_from_an_accepted_write(self) -> None:
        self.assertIs(self._report()["replayed"], False)
        self.assertIs(self._report()["replayed"], True)

    def test_a_reused_report_id_with_another_kind_is_refused(self) -> None:
        self._report()

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(kind="blocked", classification="external_fact")

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["data"]["marker"], "report:done")
        self.assertEqual(len(self._comments()), 1)

    def test_a_reused_report_id_with_another_classification_is_refused(self) -> None:
        self._report(kind="blocked", body="stuck", classification="external_fact")

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(kind="blocked", body="stuck", classification="wrong_task_definition")

        self.assertEqual(self._events("round-1")[0]["data"]["classification"], "external_fact")
        self.assertEqual(len(self._comments()), 1)

    def test_a_reused_report_id_on_another_card_is_refused(self) -> None:
        self._report()

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(reference="old-1")

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["ref"], "secretary-468")
        self.assertEqual(self._comments(13), [])

    def test_a_reused_id_from_another_write_is_refused(self) -> None:
        """The claim is over the operation, not only over the report vocabulary."""
        self.writer.comment(
            role="worker",
            actor="w",
            reference="secretary-468",
            body="a note",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report()

        self.assertEqual(self._events("round-1")[0]["kind"], "commented")
        self.assertEqual(len(self._comments()), 1)

    def _stage_pending_report(self, body: str) -> dict:
        """A report staged by a crashed attempt: written, never appended."""
        event = {
            "event_id": "evt_staged",
            "schema_version": 1,
            "occurred_at": "2026-08-03T00:00:00Z",
            "actor": {"role": "worker", "id": "w"},
            "kind": "reported",
            "outcome": "success",
            "task_id": "task_kanboard_12",
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "pending"},
            "request_id": "round-1",
            "payload": {"marker": "report:done", "body_sha256": hashlib.sha256(body.encode()).hexdigest()},
        }
        self.writer.audit.stage("round-1", event)
        return event

    def test_a_pending_report_is_owned_by_its_id_too(self) -> None:
        self._stage_pending_report("first round")

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._report(body="third round, a different report entirely")

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self._events(), [])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(self._comments(), [])

    def test_a_generic_pending_report_cannot_be_replaced_by_a_typed_owner(self) -> None:
        staged = self._stage_pending_report("first round")

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report()

        self.assertEqual(self.writer.audit.pending_event("round-1"), staged)
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(self._comments(), [])

    def test_a_reused_verdict_id_with_another_verdict_is_refused(self) -> None:
        self.writer.verdict(
            role="reviewer",
            actor="r",
            reference="secretary-468",
            kind="green",
            body="ok",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.verdict(
                role="reviewer",
                actor="r",
                reference="secretary-468",
                kind="red",
                body="the gate is red",
                request_id="round-1",
            )

        self.assertEqual(self._events("round-1")[0]["data"]["marker"], "review:green")
        self.assertEqual(len(self._comments()), 1)

    def test_the_cli_refuses_a_reused_report_id_with_exit_code_two(self) -> None:
        data_dir = str(Path(self.tmpdir.name) / "cli")
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("first round\n", encoding="utf-8")
        argv = [
            "task",
            "report",
            "--role",
            "worker",
            "--ref",
            "secretary-468",
            "--kind",
            "done",
            "--data-dir",
            data_dir,
            "--body-file",
            str(body),
            "--request-id",
            "cli-round-1",
        ]
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.KanboardClient.for_instance", return_value=self.client),
            mock.patch("secretary.tasks.workspace_dirt", return_value=[]),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(main(argv), 0)
            body.write_text("third round, a different report entirely\n", encoding="utf-8")
            code = main(argv)

        self.assertEqual(code, 2)
        self.assertIs(json.loads(output.getvalue().splitlines()[0])["replayed"], False)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")
        self.assertEqual(len(self._comments()), 1)


class TypedMarkerRecoveryTests(RequestIdOwnershipTests):
    """The three migrated marker families share one staged typed transaction."""

    def setUp(self) -> None:
        super().setUp()
        self.writer._guard_sprint_write = lambda **_kwargs: {}  # type: ignore[method-assign]
        self.writer._sprint_holds_project = lambda _project: True  # type: ignore[method-assign]

    def _write(self, family: str, request_id: str, body: str = "complete typed reason") -> dict:
        if family == "report":
            return self.writer.report(
                role="worker",
                actor="worker",
                reference="secretary-468",
                kind="done",
                body=body,
                request_id=request_id,
            )
        if family == "verdict":
            return self.writer.verdict(
                role="reviewer",
                actor="reviewer",
                reference="secretary-468",
                kind="green",
                body=body,
                request_id=request_id,
            )
        self.client.tasks[0]["column_id"] = 7  # Assessment for observer decisions.
        return self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="release",
            body=body,
            request_id=request_id,
        )

    def test_each_marker_is_rendered_from_a_complete_typed_event(self) -> None:
        expected = {
            "report": ("card.reported", "report:done"),
            "verdict": ("card.verdict", "review:green"),
            "decision": ("card.decided", "decision:release"),
        }
        for family, (kind, marker) in expected.items():
            with self.subTest(family=family):
                request_id = f"typed-{family}"
                result = self._write(family, request_id)
                event = self.writer.audit.committed_event(request_id)
                assert event is not None
                self.assertEqual(event["record_type"], "board.protocol_event")
                self.assertEqual(event["kind"], kind)
                self.assertEqual(event["subject"], {"kind": "card", "ref": "secretary-468"})
                self.assertEqual(event["reason"], "complete typed reason")
                self.assertEqual(event["data"]["marker"], marker)
                self.assertEqual(event["data"]["body"], "complete typed reason")
                self.assertEqual(event["data"]["marker_occurrence"], 1)
                self.assertEqual(
                    self.client.comments[12][-1]["comment"],
                    KanboardBoardHost.render_marker(Event.from_record(event)),
                )
                self.assertEqual(result["event_id"], event["event_id"])

    def test_post_effect_append_failure_recovers_without_a_second_comment_for_each_marker(self) -> None:
        for family in ("report", "verdict", "decision"):
            with self.subTest(family=family):
                request_id = f"pending-{family}"
                real_append = self.writer.audit.append

                def fail_marker_append(
                    request: str, event: dict, request_id: str = request_id, real_append=real_append
                ) -> str:
                    if request == request_id and event.get("record_type") == "board.protocol_event":
                        raise OSError("audit disk full")
                    return real_append(request, event)

                with mock.patch.object(self.writer.audit, "append", side_effect=fail_marker_append):
                    with self.assertRaisesRegex(TaskError, "audit repair"):
                        self._write(family, request_id, body=f"{family} pending reason")
                self.assertIsNotNone(self.writer.audit.pending_event(request_id))
                writes = len(self.client.comments[12])
                self.assertEqual(self.writer.reconcile(), (1, 0))
                self.assertEqual(len(self.client.comments[12]), writes)
                self.assertIsNotNone(self.writer.audit.committed_event(request_id))

    def test_backend_refusal_discards_each_typed_owner(self) -> None:
        self.client.fail_comments = True
        for family in ("report", "verdict", "decision"):
            with self.subTest(family=family):
                request_id = f"refused-{family}"
                with self.assertRaises(TaskError):
                    self._write(family, request_id)
                self.assertIsNone(self.writer.audit.event(request_id))

    def test_historical_identical_marker_does_not_prove_an_unavailable_new_report(self) -> None:
        body = "identical historical report"
        content = "[report:done]\n" + body
        self.client.comments[12].append({"date_creation": "1720000019", "comment": content})
        original_call = self.client.call

        def unavailable_before_write(method: str, **params: object) -> object:
            if method == "createComment":
                raise TaskError("backend_unavailable", "transport unavailable", 1)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=unavailable_before_write):
            with self.assertRaisesRegex(TaskError, "audit repair") as raised:
                self._write("report", "historical-identical-report", body)

        self.assertEqual(raised.exception.code, "audit_pending")
        pending = self.writer.audit.pending_event("historical-identical-report")
        assert pending is not None
        self.assertEqual(pending["data"]["marker_occurrence"], 2)
        self.assertEqual([comment["comment"] for comment in self.client.comments[12]], [content])
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertIsNone(self.writer.audit.committed_event("historical-identical-report"))

    def test_pending_owner_reserves_an_identical_report_or_verdict_until_recovery(self) -> None:
        """A later same-text marker cannot become proof for an earlier pending one."""
        for family in ("report", "verdict"):
            with self.subTest(family=family):
                first = f"{family}-pending-first"
                later = f"{family}-later-identical"
                body = f"{family} same marker across recovery"
                real_append = self.writer.audit.append

                def lose_event_commit(
                    request_id: str, event: dict, first: str = first, real_append=real_append
                ) -> str:
                    if request_id == first and event.get("record_type") == "board.protocol_event":
                        raise OSError("lost marker event commit")
                    return real_append(request_id, event)

                with mock.patch.object(self.writer.audit, "append", side_effect=lose_event_commit):
                    with self.assertRaisesRegex(TaskError, "audit repair"):
                        self._write(family, first, body)

                self.assertIsNotNone(self.writer.audit.pending_event(first))
                writes = len(self.client.comments[12])
                with self.assertRaisesRegex(TaskError, "audit repair") as blocked:
                    self._write(family, later, body)
                self.assertEqual(blocked.exception.code, "audit_pending")
                self.assertIsNone(self.writer.audit.event(later))
                self.assertEqual(len(self.client.comments[12]), writes)

                self.assertEqual(self.writer.reconcile(), (1, 0))
                second = self._write(family, later, body)
                self.assertFalse(second["replayed"])
                self.assertEqual(len(self.client.comments[12]), writes + 1)
                event = self.writer.audit.committed_event(later)
                assert event is not None
                self.assertEqual(event["data"]["marker_occurrence"], 2)

    def test_unavailable_pending_owner_blocks_a_later_identical_report_or_verdict(self) -> None:
        """Recovery may prove the first occurrence, never borrow the later one."""
        for family in ("report", "verdict"):
            with self.subTest(family=family):
                first = f"{family}-unavailable-first"
                later = f"{family}-unavailable-later"
                body = f"{family} unavailable then delayed delivery"
                original_call = self.client.call
                writes = len(self.client.comments[12])

                def unavailable_before_delivery(
                    method: str, original_call=original_call, **params: object
                ) -> object:
                    if method == "createComment":
                        raise TaskError("backend_unavailable", "transport unavailable", 1)
                    return original_call(method, **params)

                with mock.patch.object(self.client, "call", side_effect=unavailable_before_delivery):
                    with self.assertRaisesRegex(TaskError, "audit repair"):
                        self._write(family, first, body)

                pending = self.writer.audit.pending_event(first)
                assert pending is not None
                with self.assertRaisesRegex(TaskError, "audit repair"):
                    self._write(family, later, body)
                self.assertIsNone(self.writer.audit.event(later))
                self.assertEqual(len(self.client.comments[12]), writes)

                # Model the delayed delivery of the first RPC.  Reconciliation
                # proves that exact staged rendering before the later request is
                # admitted to write its own second occurrence.
                original_call(
                    "createComment",
                    task_id=12,
                    user_id=0,
                    content=KanboardBoardHost.render_marker(Event.from_record(pending)),
                )
                self.assertEqual(self.writer.reconcile(), (1, 0))
                self._write(family, later, body)
                self.assertEqual(len(self.client.comments[12]), writes + 2)

    def test_pending_marker_owner_blocks_an_identical_restore_comment_for_every_family(self) -> None:
        """A restore row cannot become proof for an unavailable typed occurrence."""
        for family in ("report", "verdict", "decision"):
            with self.subTest(family=family):
                request_id = f"{family}-restore-pending"
                original_call = self.client.call

                def unavailable_before_delivery(
                    method: str, original_call=original_call, **params: object
                ) -> object:
                    if method == "createComment":
                        raise TaskError("backend_unavailable", "transport unavailable", 1)
                    return original_call(method, **params)

                with mock.patch.object(self.client, "call", side_effect=unavailable_before_delivery):
                    with self.assertRaisesRegex(TaskError, "audit repair"):
                        self._write(family, request_id, body=f"{family} restore collision")

                pending = self.writer.audit.pending_event(request_id)
                assert pending is not None
                content = KanboardBoardHost.render_marker(Event.from_record(pending))
                writes = len(self.client.comments[12])
                with self.assertRaisesRegex(
                    TaskError, "identical Card marker occurrence is pending"
                ) as blocked:
                    self.writer.restore_comment(
                        reference="secretary-468",
                        body=content,
                        occurrence=0,
                        request_id=f"{family}-restore-collision",
                    )
                self.assertEqual(blocked.exception.code, "audit_pending")
                self.assertIsNone(self.writer.audit.event(f"{family}-restore-collision"))
                self.assertEqual(len(self.client.comments[12]), writes)
                self.assertEqual(self.writer.reconcile(), (0, 1))
                self.assertIsNone(self.writer.audit.committed_event(request_id))

                original_call("createComment", task_id=12, user_id=0, content=content)
                self.assertEqual(self.writer.reconcile(), (1, 0))
                self.writer.restore_comment(
                    reference="secretary-468",
                    body=content,
                    occurrence=1,
                    request_id=f"{family}-restore-after-typed-owner",
                )
                self.assertEqual(len(self.client.comments[12]), writes + 2)

    def test_pending_restore_comment_blocks_an_identical_typed_marker_for_every_family(self) -> None:
        """A typed marker cannot become proof for an unavailable restore occurrence."""
        for family in ("report", "verdict", "decision"):
            with self.subTest(family=family):
                request_id = f"{family}-restore-first"
                typed_request_id = f"{family}-typed-after-restore"
                body = f"{family} restore-first collision"
                marker = {
                    "report": "report:done",
                    "verdict": "review:green",
                    "decision": "decision:release",
                }[family]
                content = f"[{marker}]\n{body}"
                original_call = self.client.call
                writes = len(self.client.comments[12])

                def unavailable_before_delivery(
                    method: str, original_call=original_call, **params: object
                ) -> object:
                    if method == "createComment":
                        raise TaskError("backend_unavailable", "transport unavailable", 1)
                    return original_call(method, **params)

                with mock.patch.object(self.client, "call", side_effect=unavailable_before_delivery):
                    with self.assertRaisesRegex(TaskError, "audit repair"):
                        self.writer.restore_comment(
                            reference="secretary-468",
                            body=content,
                            occurrence=0,
                            request_id=request_id,
                        )

                pending = self.writer.audit.pending_event(request_id)
                assert pending is not None
                self.assertEqual(pending["payload"]["restore_body"], content)
                with self.assertRaisesRegex(TaskError, "audit repair") as blocked:
                    self._write(family, typed_request_id, body)
                self.assertEqual(blocked.exception.code, "audit_pending")
                self.assertIsNone(self.writer.audit.event(typed_request_id))
                self.assertEqual(len(self.client.comments[12]), writes)

                # The delayed restore delivery belongs only to its generic
                # owner.  Reconciliation proves that owner, then the typed
                # request may create the next identical occurrence.
                original_call("createComment", task_id=12, user_id=0, content=content)
                self.assertEqual(self.writer.reconcile(), (1, 0))
                restored = self.writer.audit.committed_event(request_id)
                assert restored is not None
                self.assertNotIn("restore_body", restored["payload"])
                self._write(family, typed_request_id, body)
                self.assertEqual(len(self.client.comments[12]), writes + 2)
                typed = self.writer.audit.committed_event(typed_request_id)
                assert typed is not None
                self.assertEqual(typed["data"]["marker_occurrence"], 2)

    def test_nonmatching_restore_comment_remains_available_while_a_typed_marker_is_pending(self) -> None:
        original_call = self.client.call

        def unavailable_before_delivery(method: str, **params: object) -> object:
            if method == "createComment":
                raise TaskError("backend_unavailable", "transport unavailable", 1)
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=unavailable_before_delivery):
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self._write("report", "pending-nonmatching-restore", "typed marker body")

        self.writer.restore_comment(
            reference="secretary-468",
            body="[historical]\nnonmatching restore body",
            occurrence=0,
            request_id="nonmatching-restore",
        )
        self.assertEqual(
            [comment["comment"] for comment in self.client.comments[12]],
            ["[historical]\nnonmatching restore body"],
        )
        self.assertEqual(self.writer.reconcile(), (0, 1))
        self.assertIsNone(self.writer.audit.committed_event("pending-nonmatching-restore"))

    def test_concurrent_identical_markers_receive_distinct_occurrence_witnesses(self) -> None:
        first_counted = threading.Event()
        second_counted = threading.Event()
        release_first = threading.Event()
        count_lock = threading.Lock()
        count_calls = 0
        original_count = self.writer.board_host._marker_occurrences
        original_call = self.client.call
        create_calls = 0

        def count_occurrences(ref: str, content: str) -> int:
            nonlocal count_calls
            count = original_count(ref, content)
            with count_lock:
                count_calls += 1
                ordinal = count_calls
            if ordinal == 1:
                first_counted.set()
                self.assertTrue(release_first.wait(2))
            else:
                second_counted.set()
            return count

        def create_then_lose_second_reply(method: str, **params: object) -> object:
            nonlocal create_calls
            if method == "createComment":
                with count_lock:
                    create_calls += 1
                    ordinal = create_calls
                if ordinal == 2:
                    raise TaskError("backend_unavailable", "transport unavailable", 1)
            return original_call(method, **params)

        outcomes: dict[str, object] = {}

        def report(request_id: str) -> None:
            try:
                outcomes[request_id] = self._write(
                    "report",
                    request_id,
                    body="same concurrent marker",
                )
            except TaskError as exc:
                outcomes[request_id] = exc

        with (
            mock.patch.object(self.writer.board_host, "_marker_occurrences", side_effect=count_occurrences),
            mock.patch.object(self.client, "call", side_effect=create_then_lose_second_reply),
        ):
            first = threading.Thread(target=report, args=("concurrent-first",))
            second = threading.Thread(target=report, args=("concurrent-second",))
            first.start()
            self.assertTrue(first_counted.wait(2))
            second.start()
            self.assertFalse(second_counted.wait(0.1))
            release_first.set()
            first.join()
            second.join()

        self.assertIsInstance(outcomes["concurrent-first"], dict)
        self.assertIsInstance(outcomes["concurrent-second"], TaskError)
        self.assertEqual(outcomes["concurrent-second"].code, "audit_pending")  # type: ignore[union-attr]
        pending = self.writer.audit.pending_event("concurrent-second")
        assert pending is not None
        self.assertEqual(pending["data"]["marker_occurrence"], 2)
        self.assertEqual(len(self.client.comments[12]), 1)
        self.assertEqual(self.writer.reconcile(), (0, 1))

    def test_generic_pending_owner_cannot_be_replaced_by_any_typed_marker(self) -> None:
        for family in ("report", "verdict", "decision"):
            with self.subTest(family=family):
                request_id = f"generic-{family}"
                generic = {
                    "event_id": request_id,
                    "request_id": request_id,
                    "kind": "commented",
                    "payload": {},
                }
                self.writer.audit.stage(request_id, generic)
                with self.assertRaisesRegex(TaskError, "belongs to another operation"):
                    self._write(family, request_id)
                self.assertEqual(self.writer.audit.pending_event(request_id), generic)

    def test_every_write_has_to_declare_what_its_id_claims(self) -> None:
        """A new caller cannot inherit the blind replay by forgetting one keyword."""
        identity = inspect.signature(TaskWriter._write).parameters["identity"]
        self.assertIs(identity.default, inspect.Parameter.empty)

    def _routing_payload(self, attempt: int, head: str = "codex-terra") -> dict:
        return routing_payload(
            attempt=attempt,
            attempt_id="att-1",
            phase="worker",
            heads=[
                head_run_from_profile(
                    role="worker",
                    head=head,
                    head_source="role_default",
                    profile={"adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra"},
                    resources={},
                )
            ],
        )

    def _routing(self, attempt: int, **overrides: object) -> dict:
        call = {
            "role": "dispatcher",
            "actor": "pilot",
            "reference": "secretary-468",
            "payload": self._routing_payload(attempt),
            "request_id": "round-1",
        }
        call.update(overrides)
        return self.writer.routing(**call)  # type: ignore[arg-type]

    def test_a_reused_routing_id_with_another_record_is_refused(self) -> None:
        """A journal-only write is caller-supplied end to end, so it owns its id too."""
        first = self._routing(1)

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._routing(2)

        self.assertEqual(raised.exception.exit_code, 2)
        recorded = self._events("round-1")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["event_id"], first["event_id"])
        self.assertEqual(recorded[0]["payload"]["attempt"], 1)

    def test_a_staged_routing_record_is_owned_by_its_id_too(self) -> None:
        staged = {
            "event_id": "evt_staged_routing",
            "schema_version": 1,
            "occurred_at": "2026-08-03T00:00:00Z",
            "actor": {"role": "dispatcher", "id": "pilot"},
            "kind": "routing",
            "outcome": "success",
            "task_id": "task_kanboard_12",
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "pending"},
            "request_id": "round-1",
            "payload": self._routing_payload(1),
        }
        self.writer.audit.stage("round-1", staged)

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._routing(2)

        self.assertEqual(self._events(), [])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        # The record its own id claims still commits.
        self.assertEqual(self._routing(1)["event_id"], "evt_staged_routing")
        self.assertEqual(self._events("round-1")[0]["payload"]["attempt"], 1)

    def test_a_reused_edit_id_with_another_spec_is_refused(self) -> None:
        self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="first spec",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.edit(
                role="po",
                actor="operator",
                reference="secretary-468",
                description="second spec",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self.client.tasks[0]["description"], "first spec")

    def test_an_edit_retried_after_it_landed_stays_idempotent(self) -> None:
        """The `_was` digests are of text the edit replaced, so a retry must not compare them."""
        first = self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="one spec",
            head="codex-terra",
            request_id="round-1",
        )
        second = self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="one spec",
            head="codex-terra",
            request_id="round-1",
        )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertIs(second["replayed"], True)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "updateTask"]), 1)

    def test_a_reused_claim_id_with_another_worker_is_refused(self) -> None:
        self.client.metadata[12]["claim"] = ""
        self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="worker-a",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="worker-b",
                request_id="round-1",
            )

        self.assertEqual(self._events("round-1")[0]["reason"], "claimed by worker-a")
        self.assertEqual(self.client.metadata[12]["claim"], "worker-a")

    def test_a_reused_move_id_with_another_destination_is_refused(self) -> None:
        self.client.tasks[0]["column_id"] = 3  # In progress
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="requeue",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="blocked",
                reason="requeue",
                request_id="round-1",
            )
        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="ready",
                reason="a different reason entirely",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["transition"]["target"], "ready")

    def test_a_move_retried_after_it_landed_stays_idempotent(self) -> None:
        """`from` is the column the move left, so a retry must not compare it."""
        self.client.tasks[0]["column_id"] = 3  # In progress
        call = {
            "role": "dispatcher",
            "actor": "d",
            "reference": "secretary-468",
            "target": "ready",
            "reason": "requeue",
            "request_id": "round-1",
        }
        first = self.writer.move(**call)  # type: ignore[arg-type]
        second = self.writer.move(**call)  # type: ignore[arg-type]

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertIs(second["replayed"], True)
        self.assertEqual(self._events("round-1")[0]["transition"]["source"], "in_progress")

    def test_a_reused_restore_id_with_another_placement_is_refused(self) -> None:
        self.writer.restore_card(
            reference="secretary-468",
            metadata={"project": "secretary"},
            target="ready",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.restore_card(
                reference="secretary-468",
                metadata={"project": "secretary"},
                target="blocked",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["payload"]["target"], "ready")

    def test_a_reused_restore_comment_id_with_another_body_is_refused(self) -> None:
        self.writer.restore_comment(
            reference="secretary-468",
            body="the original comment",
            occurrence=0,
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.restore_comment(
                reference="secretary-468",
                body="another comment entirely",
                occurrence=0,
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._comments(), ["the original comment"])

    def _create(self, **overrides: object) -> dict:
        call = {
            "role": "observer",
            "actor": "observer",
            "project": "secretary",
            "task_type": "code",
            "title": "First card",
            "target": "ready",
            "request_id": "create-1",
        }
        call.update(overrides)
        with open_sprint() as sprint:
            return self.writer.create(sprint=sprint, **call)  # type: ignore[arg-type]

    def test_a_reused_create_id_with_another_card_is_refused(self) -> None:
        created = self._create()
        cards = len(self.client.tasks)

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._create(title="A different card entirely")

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(len(self.client.tasks), cards)
        self.assertEqual(len(self._events("create-1")), 1)
        self.assertEqual(self._events("create-1")[0]["event_id"], created["event_id"])

    def test_the_same_create_under_the_same_id_stays_idempotent(self) -> None:
        first = self._create()
        cards = len(self.client.tasks)

        second = self._create()

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["task"]["ref"], second["task"]["ref"])
        self.assertIs(first["replayed"], False)
        self.assertIs(second["replayed"], True)
        self.assertEqual(len(self.client.tasks), cards)
        self.assertEqual(len(self._events("create-1")), 1)


class AuditCommittedIndexTests(unittest.TestCase):
    """committed_event читает журнал инкрементально, а не целиком на каждый вызов.

    append()/stage() зовут committed_event на каждое событие, а тот разбирал весь
    events.ndjson с начала. На восстановлении 745 карточек (~30k событий) это давало
    квадратичный прогон: ~8 МБ JSON перечитывались на каждую запись, борд при этом
    отвечал за 5-10 мс. Индекс обязан оставаться согласованным с файлом, который
    дописывает другой процесс.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.audit = TaskAudit(self.tmpdir.name)
        Path(self.audit.board_dir).mkdir(parents=True, exist_ok=True)

    def _append_raw(self, payload: dict, terminated: bool = True) -> None:
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write(line + ("\n" if terminated else ""))

    def test_missing_file_reads_as_no_event(self) -> None:
        self.assertIsNone(self.audit.committed_event("nope"))

    def test_picks_up_events_appended_after_the_first_read(self) -> None:
        self._append_raw({"request_id": "one", "event_id": "e1"})
        self.assertEqual(self.audit.committed_event("one")["event_id"], "e1")
        self.assertIsNone(self.audit.committed_event("two"))

        self._append_raw({"request_id": "two", "event_id": "e2"})
        self.assertEqual(self.audit.committed_event("two")["event_id"], "e2")
        self.assertEqual(self.audit.committed_event("one")["event_id"], "e1")

    def test_earliest_duplicate_wins(self) -> None:
        self._append_raw({"request_id": "dup", "event_id": "first"})
        self._append_raw({"request_id": "dup", "event_id": "second"})
        self.assertEqual(self.audit.committed_event("dup")["event_id"], "first")

    def test_half_written_line_is_not_consumed_until_terminated(self) -> None:
        self._append_raw({"request_id": "done", "event_id": "e1"})
        self._append_raw({"request_id": "torn", "event_id": "e2"}, terminated=False)

        self.assertEqual(self.audit.committed_event("done")["event_id"], "e1")
        self.assertIsNone(self.audit.committed_event("torn"))

        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write("\n")
        self.assertEqual(self.audit.committed_event("torn")["event_id"], "e2")

    def test_rebuilds_when_the_journal_is_replaced(self) -> None:
        self._append_raw({"request_id": "old", "event_id": "e1"})
        self.assertIsNotNone(self.audit.committed_event("old"))

        with open(self.audit.events_path, "w", encoding="utf-8") as events:
            events.write(json.dumps({"request_id": "new", "event_id": "e2"}) + "\n")

        self.assertIsNone(self.audit.committed_event("old"))
        self.assertEqual(self.audit.committed_event("new")["event_id"], "e2")

    def test_garbage_lines_are_skipped(self) -> None:
        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write("not json\n")
            events.write("[1,2,3]\n")
            events.write("\n")
        self._append_raw({"request_id": "good", "event_id": "e1"})
        self.assertEqual(self.audit.committed_event("good")["event_id"], "e1")

    def test_warm_index_answers_misses_without_reparsing_the_journal(self) -> None:
        """Суть фикса: на прогретом индексе промах не разбирает журнал заново.

        Именно этот путь исполнялся на каждой записи (append -> committed_event ->
        промах -> запись) и стоил полного json-разбора всего файла.
        """
        for index in range(20):
            self._append_raw({"request_id": f"r{index}", "event_id": f"e{index}"})
        self.assertIsNotNone(self.audit.committed_event("r0"))  # прогреваем индекс

        parsed: list[int] = []
        real_loads = json.loads

        def counting_loads(payload, *args, **kwargs):  # type: ignore[no-untyped-def]
            parsed.append(1)
            return real_loads(payload, *args, **kwargs)

        with mock.patch("secretary.tasks.json.loads", counting_loads):
            for index in range(20):
                self.assertIsNone(self.audit.committed_event(f"missing-{index}"))

        self.assertEqual(parsed, [])
