"""Agent proposal route (secretary-900, secretary-901).

Reviewer, retro, and steward file proposal cards only on the legacy board layout, where the first
column is still `Ideas`. On a migrated board `Issues` is the Product backlog and no agent may
create a Product issue, so the route fails closed instead of guessing a column. The `Ready`-only
guard on every other create path stays untouched.
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

    def test_ready_stays_the_only_other_column_a_card_is_created_in(self):
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(LEGACY_COLUMNS, calls)):
            for column in ("Ideas", "Issues", "In progress", "Done"):
                with self.subTest(column=column), self.assertRaisesRegex(
                    model.GuardError, "created only in 'Ready'"
                ):
                    ops.create_card(project="secretary", task_type="code", title="t", column=column)
        self.assertNotIn("createTask", [method for method, _ in calls])

    def test_the_proposal_exception_is_not_reachable_through_the_public_create_card(self):
        # The exception belongs to the two proposal helpers, not to the shared operation: no
        # caller can ask create_card for a legacy column, whatever it passes.
        calls = []
        with mock.patch.object(ops, "call", side_effect=_fake_board(LEGACY_COLUMNS, calls)):
            with self.assertRaises(TypeError):
                ops.create_card(project="secretary", task_type="code", title="t",
                                column="Ideas", proposal=True)
            for column in sorted(model.LEGACY_ISSUE_COLUMNS):
                with self.subTest(column=column), self.assertRaisesRegex(
                    model.GuardError, "created only in 'Ready'"
                ):
                    ops.create_card(project="secretary", task_type="code", title="t", column=column)
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
