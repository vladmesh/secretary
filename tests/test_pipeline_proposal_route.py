"""Agent proposal route (secretary-900, secretary-901).

Reviewer, retro, and steward file proposal cards only on the legacy board layout, where the first
column is still `Ideas`. A PO may also explicitly create a task there through `create --column Ideas`.
On a migrated board `Issues` is the Product backlog and no agent may create a Product issue, so the
routes fail closed instead of guessing a column. The default create column stays `Ready`.
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from triggered_agents.agents.pipeline import cli, model, ops


def _fake_board(columns, calls):
    def fake_call(method, **params):
        calls.append((method, params))
        if method == "getAllProjects":
            return [{"id": 2, "name": ops.model.BOARD_NAME}]
        if method == "getColumns":
            return columns
        if method == "getActiveSwimlanes":
            return [{"id": 1, "name": "secretary"}]
        if method == "createTask":
            return 41
        if method in ("updateTask", "saveTaskMetadata"):
            return True
        if method == "getTaskTags":
            return {}
        raise AssertionError(f"unexpected call {method} {params}")

    return fake_call


LEGACY_COLUMNS = [
    {"id": 1, "title": "Ideas", "position": 1}, {"id": 2, "title": "Ready", "position": 2},
    {"id": 3, "title": "In progress", "position": 3}, {"id": 4, "title": "Validate", "position": 4},
    {"id": 5, "title": "Blocked", "position": 5}, {"id": 6, "title": "Done", "position": 6},
]
UNTRANSLATED_COLUMNS = [
    dict(LEGACY_COLUMNS[0], title=next(iter(model.LEGACY_ISSUE_COLUMNS - {"Ideas"}))),
    *LEGACY_COLUMNS[1:],
]
MIGRATED_COLUMNS = [dict(LEGACY_COLUMNS[0], title="Issues"), *LEGACY_COLUMNS[1:]]
# A migrated board someone extended by hand: `Issues` leads, an old `Ideas` column survives later
# in the order. The PO decision opened the route only while the first column itself is legacy.
MIGRATED_WITH_LATER_IDEAS = [
    *MIGRATED_COLUMNS[:2],
    {"id": 7, "title": "Ideas", "position": 3},
    *[dict(column, position=int(column["position"]) + 1) for column in MIGRATED_COLUMNS[2:]],
]


class ProposalRouteTests(unittest.TestCase):
    def test_legacy_board_takes_agent_proposals_typed_as_tasks(self):
        for columns in (LEGACY_COLUMNS, UNTRANSLATED_COLUMNS):
            for file_proposal in (ops.reviewer_idea, ops.retro_idea, ops.steward_idea):
                with self.subTest(column=columns[0]["title"], route=file_proposal.__name__):
                    calls = []
                    with mock.patch.object(ops, "call", side_effect=_fake_board(columns, calls)), \
                         mock.patch.object(ops, "_sync_head_tags"):
                        result = file_proposal(
                            project="secretary", title="retro: looping head",
                            description="Pattern: looping", ref="secretary-901",
                        )

                    self.assertEqual(result["action"], "created")
                    self.assertEqual(result["column"], columns[0]["title"])
                    created = next(params for method, params in calls if method == "createTask")
                    self.assertEqual(created["column_id"], 1)
                    values = next(params for method, params in calls if method == "saveTaskMetadata")["values"]
                    self.assertEqual(values[model.META_RECORD_TYPE], model.RECORD_TASK)
                    self.assertEqual(values[model.META_PROJECT], "secretary")

    def test_migrated_board_fails_closed_and_names_the_po_decision(self):
        # Including the hand-extended layout: an `Ideas` column that is no longer the first one is
        # not the legacy layout, so the route stays closed there too.
        for columns in (MIGRATED_COLUMNS, MIGRATED_WITH_LATER_IDEAS):
            for file_proposal in (ops.reviewer_idea, ops.retro_idea, ops.steward_idea):
                with self.subTest(layout=[c["title"] for c in columns], route=file_proposal.__name__):
                    calls = []
                    with mock.patch.object(ops, "call", side_effect=_fake_board(columns, calls)):
                        with self.assertRaises(model.GuardError) as raised:
                            file_proposal(project="secretary", title="proposal", description="body")

                    message = str(raised.exception)
                    self.assertIn("first column is not the legacy 'Ideas'", message)
                    self.assertIn("A PO has to decide", message)
                    self.assertNotIn("createTask", [method for method, _ in calls])
                    self.assertNotIn("saveTaskMetadata", [method for method, _ in calls])

    def test_steward_can_escalate_its_legacy_proposal_to_blocked_with_a_reason(self):
        task = {
            "id": 41, "reference": "secretary-901", "column_id": 1, "swimlane_id": 1,
        }
        metadata = {}
        calls = []

        def fake_call(method, **params):
            calls.append((method, params))
            if method == "getAllProjects":
                return [{"id": 2, "name": ops.model.BOARD_NAME}]
            if method == "getColumns":
                return LEGACY_COLUMNS
            if method == "getActiveSwimlanes":
                return [{"id": 1, "name": "secretary"}]
            if method == "createTask":
                return 41
            if method == "getTaskByReference":
                return task if params["reference"] == task["reference"] else None
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
            self.assertEqual(created["column"], "Ideas")
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

    def test_po_can_explicitly_create_a_legacy_ideas_task(self):
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(LEGACY_COLUMNS, calls)), \
             mock.patch.object(ops, "_sync_head_tags"):
            result = ops.create_card(
                project="secretary", task_type="code", title="agent idea", column="Ideas", role="po",
            )

        self.assertEqual(result["column"], "Ideas")
        created = next(params for method, params in calls if method == "createTask")
        self.assertEqual(created["column_id"], 1)
        values = next(params for method, params in calls if method == "saveTaskMetadata")["values"]
        self.assertEqual(values[model.META_RECORD_TYPE], model.RECORD_TASK)

    def test_only_po_can_create_in_legacy_ideas(self):
        for role in (*model.ROLES, None):
            if role == "po":
                continue
            with self.subTest(role=role):
                calls = []
                with mock.patch.object(ops, "call", side_effect=_fake_board(LEGACY_COLUMNS, calls)):
                    with self.assertRaisesRegex(model.GuardError, "created only in 'Ready'"):
                        ops.create_card(
                            project="secretary", task_type="code", title="t", column="Ideas", role=role,
                        )
                self.assertNotIn("createTask", [method for method, _ in calls])

    def test_po_legacy_ideas_create_fails_closed_on_a_migrated_board(self):
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(MIGRATED_COLUMNS, calls)):
            with self.assertRaisesRegex(model.GuardError, "first column is not the legacy 'Ideas'"):
                ops.create_card(
                    project="secretary", task_type="code", title="agent idea", column="Ideas", role="po",
                )
        self.assertNotIn("createTask", [method for method, _ in calls])
        self.assertNotIn("saveTaskMetadata", [method for method, _ in calls])

    def test_create_default_remains_ready(self):
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(LEGACY_COLUMNS, calls)), \
             mock.patch.object(ops, "_sync_head_tags"):
            result = ops.create_card(project="secretary", task_type="code", title="approved", role="po")
        self.assertEqual(result["column"], "Ready")
        created = next(params for method, params in calls if method == "createTask")
        self.assertEqual(created["column_id"], 2)

    def test_cli_po_creates_legacy_ideas_and_defaults_to_ready(self):
        for column, expected in (("Ideas", "Ideas"), (None, "Ready")):
            with self.subTest(column=column):
                calls = []
                argv = [
                    "--role", "po", "create", "--project", "secretary", "--type", "code",
                    "--title", "agent idea", "--description", "body",
                ]
                if column:
                    argv.extend(("--column", column))
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(ops, "call", side_effect=_fake_board(LEGACY_COLUMNS, calls)), \
                     mock.patch.object(ops, "_sync_head_tags"), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(argv)
                self.assertEqual(code, 0, err.getvalue())
                self.assertEqual(json.loads(out.getvalue())["column"], expected)

        out, err, calls = io.StringIO(), io.StringIO(), []
        with mock.patch.object(ops, "call", side_effect=_fake_board(MIGRATED_COLUMNS, calls)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([
                "--role", "po", "create", "--project", "secretary", "--type", "code",
                "--title", "agent idea", "--column", "Ideas", "--description", "body",
            ])
        self.assertEqual(code, 3)
        self.assertIn("first column is not the legacy 'Ideas'", err.getvalue())
        self.assertNotIn("createTask", [method for method, _ in calls])


    def test_cli_idea_reports_success_on_legacy_and_a_guard_exit_on_a_migrated_board(self):
        for role in ("reviewer", "retro", "steward"):
            with self.subTest(role=role):
                argv = [
                    "--role", role, "idea", "--project", "secretary",
                    "--title", "proposal", "--description", "body",
                ]
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(ops, "call", side_effect=_fake_board(LEGACY_COLUMNS, [])), \
                     mock.patch.object(ops, "_sync_head_tags"), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(argv)
                self.assertEqual(code, 0, err.getvalue())
                self.assertEqual(json.loads(out.getvalue())["column"], "Ideas")

                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(ops, "call", side_effect=_fake_board(MIGRATED_COLUMNS, [])), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(argv)
                self.assertEqual(code, 3)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("first column is not the legacy 'Ideas'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
