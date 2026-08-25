"""Agent proposal route (secretary-900, secretary-901).

Reviewer, retro, and steward file proposal cards into the board's first column, `Issues`. A PO may
create a task there explicitly through `create --column ...`. The card is stamped record_type=task,
so it is an untriaged execution card and never a Product issue, which no agent may create. The
default create column stays `Ready`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from triggered_agents.agents.pipeline import cli, model, ops


# The rows a create counts references over: an open card and an archived one, the way the board
# answers a reference enumeration.
EXISTING_ROWS = {
    1: [{"id": 41, "reference": "secretary-901"}],
    0: [{"id": 12, "reference": "secretary-902"}],
}


def _fake_board(columns, calls):
    def fake_call(method, **params):
        calls.append((method, params))
        if method == "getAllProjects":
            return [{"id": 2, "name": ops.model.BOARD_NAME}]
        if method == "getColumns":
            return columns
        if method == "getActiveSwimlanes":
            return [{"id": 1, "name": "secretary"}]
        if method == "getAllTasks":
            return [dict(row) for row in EXISTING_ROWS[params["status_id"]]]
        if method == "getTaskByReference":
            return next(
                (dict(row) for rows in EXISTING_ROWS.values() for row in rows
                 if row["reference"] == params["reference"]),
                None,
            )
        if method == "createTask":
            return 41
        if method in ("updateTask", "saveTaskMetadata"):
            return True
        if method == "getTaskTags":
            return {}
        raise AssertionError(f"unexpected call {method} {params}")

    return fake_call


BOARD_COLUMNS = [
    {"id": 1, "title": "Issues", "position": 1}, {"id": 2, "title": "Ready", "position": 2},
    {"id": 3, "title": "In progress", "position": 3}, {"id": 4, "title": "Validate", "position": 4},
    {"id": 5, "title": "Blocked", "position": 5}, {"id": 6, "title": "Done", "position": 6},
]
# A board nobody reconciled: the first column is not the one the route writes into.
UNKNOWN_FIRST_COLUMN = [dict(BOARD_COLUMNS[0], title="Backlog"), *BOARD_COLUMNS[1:]]


class ProposalRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        # A create takes the board's allocation lock, which lives in the installation's data plane.
        # The suite is hermetic, so it points at a temporary one rather than the live host's.
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        patched = mock.patch.dict(os.environ, {"SECRETARY_DATA_DIR": data_dir.name})
        patched.start()
        self.addCleanup(patched.stop)

    def test_first_column_takes_agent_proposals_typed_as_tasks(self):
        for file_proposal in (ops.reviewer_idea, ops.retro_idea, ops.steward_idea):
            with self.subTest(route=file_proposal.__name__):
                calls = []
                with mock.patch.object(ops, "call", side_effect=_fake_board(BOARD_COLUMNS, calls)), \
                     mock.patch.object(ops, "_sync_head_tags"):
                    result = file_proposal(
                        project="secretary", title="retro: looping head",
                        description="Pattern: looping", ref="secretary-950",
                    )

                self.assertEqual(result["action"], "created")
                self.assertEqual(result["column"], "Issues")
                created = next(params for method, params in calls if method == "createTask")
                self.assertEqual(created["column_id"], 1)
                values = next(params for method, params in calls if method == "saveTaskMetadata")["values"]
                self.assertEqual(values[model.META_RECORD_TYPE], model.RECORD_TASK)
                self.assertEqual(values[model.META_PROJECT], "secretary")

    def test_unreconciled_board_fails_closed_and_names_the_column(self):
        for file_proposal in (ops.reviewer_idea, ops.retro_idea, ops.steward_idea):
            with self.subTest(route=file_proposal.__name__):
                calls = []
                with mock.patch.object(ops, "call", side_effect=_fake_board(UNKNOWN_FIRST_COLUMN, calls)):
                    with self.assertRaises(model.GuardError) as raised:
                        file_proposal(project="secretary", title="proposal", description="body")

                message = str(raised.exception)
                self.assertIn("first column is 'Backlog'", message)
                self.assertIn("pipeline setup", message)
                self.assertNotIn("createTask", [method for method, _ in calls])
                self.assertNotIn("saveTaskMetadata", [method for method, _ in calls])

    def test_steward_moves_its_own_proposal_to_blocked_with_a_reason_and_to_ready(self):
        task = {
            "id": 41, "reference": "secretary-901", "column_id": 1, "swimlane_id": 1,
        }
        metadata = {}
        calls = []
        # The board holds no such card until this test's create writes it, which is what makes the
        # reference free to take.
        rows: list[dict] = []

        def fake_call(method, **params):
            calls.append((method, params))
            if method == "getAllProjects":
                return [{"id": 2, "name": ops.model.BOARD_NAME}]
            if method == "getColumns":
                return BOARD_COLUMNS
            if method == "getActiveSwimlanes":
                return [{"id": 1, "name": "secretary"}]
            if method == "getAllTasks":
                return [dict(row) for row in rows] if params["status_id"] == 1 else []
            if method == "createTask":
                rows.append(task)
                return 41
            if method == "getTaskByReference":
                return task if any(
                    params["reference"] == row["reference"] for row in rows
                ) else None
            if method == "getTaskMetadata":
                return metadata.copy()
            if method == "saveTaskMetadata":
                metadata.update(params["values"])
                return True
            if method == "getTaskTags":
                return {}
            if method == "moveTaskPosition":
                task["column_id"] = params["column_id"]
                return True
            if method == "createComment":
                return 1
            raise AssertionError(f"unexpected call {method} {params}")

        with mock.patch.object(ops, "call", side_effect=fake_call), \
             mock.patch.object(ops, "_sync_head_tags"):
            created = ops.steward_idea(
                project="secretary", title="unresolved anomaly", description="analysis",
                ref="secretary-901",
            )
            self.assertEqual(created["column"], "Issues")
            self.assertEqual(metadata[model.META_RECORD_TYPE], model.RECORD_TASK)

            with self.assertRaisesRegex(model.GuardError, "non-empty escalation reason"):
                ops.move_card("steward", "secretary-901", "Blocked")
            self.assertEqual(task["column_id"], 1)

            moved = ops.move_card(
                "steward", "secretary-901", "Blocked", reason="needs an owner decision",
            )

        self.assertEqual(moved, {
            "action": "moved", "reference": "secretary-901", "from": "Issues", "to": "Blocked",
        })
        self.assertEqual(task["column_id"], 5)
        comment = next(params for method, params in calls if method == "createComment")
        self.assertIn("needs an owner decision", comment["content"])

        # The other half of the steward's two accesses: its own proposal back out to Ready.
        task["column_id"] = 1
        with mock.patch.object(ops, "call", side_effect=fake_call), \
             mock.patch.object(ops, "_sync_head_tags"):
            promoted = ops.move_card("steward", "secretary-901", "Ready")
        self.assertEqual(promoted["from"], "Issues")
        self.assertEqual(task["column_id"], 2)

    def test_po_can_explicitly_create_a_proposal_task_in_the_first_column(self):
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(BOARD_COLUMNS, calls)), \
             mock.patch.object(ops, "_sync_head_tags"):
            result = ops.create_card(
                project="secretary", task_type="code", title="agent idea",
                column="Issues", role="po",
            )

        self.assertEqual(result["column"], "Issues")
        created = next(params for method, params in calls if method == "createTask")
        self.assertEqual(created["column_id"], 1)
        values = next(params for method, params in calls if method == "saveTaskMetadata")["values"]
        self.assertEqual(values[model.META_RECORD_TYPE], model.RECORD_TASK)

    def test_only_po_can_create_in_the_first_column(self):
        for role in (*model.ROLES, None):
            if role == "po":
                continue
            with self.subTest(role=role):
                calls = []
                with mock.patch.object(ops, "call", side_effect=_fake_board(BOARD_COLUMNS, calls)):
                    with self.assertRaisesRegex(model.GuardError, "created only in 'Ready'"):
                        ops.create_card(
                            project="secretary", task_type="code", title="t",
                            column="Issues", role=role,
                        )
                self.assertNotIn("createTask", [method for method, _ in calls])

    def test_po_proposal_create_fails_closed_on_an_unreconciled_board(self):
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(UNKNOWN_FIRST_COLUMN, calls)):
            with self.assertRaisesRegex(model.GuardError, "first column is 'Backlog'"):
                ops.create_card(
                    project="secretary", task_type="code", title="agent idea", column="Issues", role="po",
                )
        self.assertNotIn("createTask", [method for method, _ in calls])
        self.assertNotIn("saveTaskMetadata", [method for method, _ in calls])

    def test_create_default_remains_ready_and_is_typed(self):
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(BOARD_COLUMNS, calls)), \
             mock.patch.object(ops, "_sync_head_tags"):
            result = ops.create_card(project="secretary", task_type="code", title="approved", role="po")
        self.assertEqual(result["column"], "Ready")
        created = next(params for method, params in calls if method == "createTask")
        self.assertEqual(created["column_id"], 2)
        values = next(params for method, params in calls if method == "saveTaskMetadata")["values"]
        self.assertEqual(values[model.META_RECORD_TYPE], model.RECORD_TASK)

    def test_cli_po_creates_a_proposal_and_defaults_to_ready(self):
        for column, expected in (("Issues", "Issues"), (None, "Ready")):
            with self.subTest(column=column):
                calls = []
                argv = [
                    "--role", "po", "create", "--project", "secretary", "--type", "code",
                    "--title", "agent idea", "--description", "body",
                ]
                if column:
                    argv.extend(("--column", column))
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(ops, "call", side_effect=_fake_board(BOARD_COLUMNS, calls)), \
                     mock.patch.object(ops, "_sync_head_tags"), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(argv)
                self.assertEqual(code, 0, err.getvalue())
                self.assertEqual(json.loads(out.getvalue())["column"], expected)

        out, err, calls = io.StringIO(), io.StringIO(), []
        with mock.patch.object(ops, "call", side_effect=_fake_board(UNKNOWN_FIRST_COLUMN, calls)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([
                "--role", "po", "create", "--project", "secretary", "--type", "code",
                "--title", "agent idea", "--column", "Issues", "--description", "body",
            ])
        self.assertEqual(code, 3)
        self.assertIn("first column is 'Backlog'", err.getvalue())
        self.assertNotIn("createTask", [method for method, _ in calls])


    def test_cli_idea_reports_the_first_column_and_a_guard_exit_without_one(self):
        for role in ("reviewer", "retro", "steward"):
            with self.subTest(role=role):
                argv = [
                    "--role", role, "idea", "--project", "secretary",
                    "--title", "proposal", "--description", "body",
                ]
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(ops, "call", side_effect=_fake_board(BOARD_COLUMNS, [])), \
                     mock.patch.object(ops, "_sync_head_tags"), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(argv)
                self.assertEqual(code, 0, err.getvalue())
                self.assertEqual(json.loads(out.getvalue())["column"], "Issues")

            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(ops, "call", side_effect=_fake_board(UNKNOWN_FIRST_COLUMN, [])), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main([
                    "--role", role, "idea", "--project", "secretary",
                    "--title", "proposal", "--description", "body",
                ])
            self.assertEqual(code, 3)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("first column is 'Backlog'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
