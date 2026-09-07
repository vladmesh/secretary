"""§8's mapping, as unit tests over a synthetic board.

Everything here is a pure function of a :class:`BoardSource` built in memory: no Kanboard, no
PostgreSQL, no driver.  That is the point — ``import_board.plan`` is where §8 either holds or
does not, and it should be arguable without a container.  The integration suite
(``tests/test_board_import_integration.py``) then executes the same plan against a real
``postgres:16``, because a mapping that satisfies these assertions and not a `CHECK` constraint
is still wrong.

The synthetic board is deliberately awkward in the ways the live board turned out to be: a
duplicated reference, a card whose project metadata is empty, a `blocked_by` pointing at a card
nobody has, a sprint repository that is a project id rather than a path, and a comment whose
first line looks like a marker but is not one.
"""

from __future__ import annotations

import unittest

from secretary.board import import_board
from secretary.board.import_board import BoardSource, RegistryEntry, SourceRow

COLUMNS = {1: "Issues", 2: "Ready", 3: "In progress", 6: "Blocked", 7: "Done"}
SWIMLANES = {1: "Default swimlane", 2: "secretary", 3: "other"}


def row(
    identifier: int,
    reference: str,
    *,
    title: str = "a title",
    description: str = "",
    column: int = 2,
    swimlane: int = 2,
    active: int = 1,
    position: int = 0,
    created: int = 1_700_000_000,
    modified: int = 1_700_000_100,
    meta: dict[str, str] | None = None,
    comments: tuple[dict[str, object], ...] = (),
) -> SourceRow:
    return SourceRow(
        raw={
            "id": identifier,
            "reference": reference,
            "title": title,
            "description": description,
            "column_id": column,
            "swimlane_id": swimlane,
            "is_active": active,
            "position": position,
            "date_creation": created,
            "date_modification": modified,
        },
        meta=dict(meta or {}),
        comments=comments,
    )


def comment(body: str, created: int = 1_700_000_050) -> dict[str, object]:
    return {"id": created, "date_creation": created, "comment": body, "user_id": 0}


def card_meta(**overrides: str) -> dict[str, str]:
    base = {"record_type": "task", "project": "secretary", "task_type": "code"}
    base.update(overrides)
    return base


def source(
    *,
    pipeline: tuple[SourceRow, ...] = (),
    sprints: tuple[SourceRow, ...] = (),
    registry: tuple[RegistryEntry, ...] = (),
    budget_records: tuple[dict[str, object], ...] = (),
    transaction_documents: tuple[dict[str, object], ...] = (),
) -> BoardSource:
    return BoardSource(
        pipeline=pipeline,
        sprints=sprints,
        pipeline_columns=COLUMNS,
        pipeline_swimlanes=SWIMLANES,
        registry=registry,
        budget_records=budget_records,
        transaction_documents=transaction_documents,
    )


def registry_entry(project_id: str, repo: str, **overrides: object) -> RegistryEntry:
    fields: dict[str, object] = {
        "project_id": project_id,
        "repo": repo,
        "remote": None,
        "default_branch": "main",
        "adapter": None,
        "orca_binding": None,
        "enabled": True,
        "plane": "project",
        "curator_roots": (),
    }
    fields.update(overrides)
    return RegistryEntry(**fields)  # type: ignore[arg-type]


PRODUCT = row(
    1,
    "product:secretary",
    title="Secretary",
    column=1,
    meta={"record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]'},
)


class MarkerRuleTests(unittest.TestCase):
    """§8.1: the prefix is stripped once, and only for a token in the vocabulary."""

    def test_a_role_marker_is_recognized_and_stripped_once(self) -> None:
        self.assertEqual(import_board.parse_marker("[po]\nbody\n[po]\nmore"), ("po", "body\n[po]\nmore"))

    def test_every_prefixed_family_the_document_names_is_in_the_vocabulary(self) -> None:
        for token in ("report:done", "review:red", "decision:reslice", "issue:closed"):
            with self.subTest(token=token):
                self.assertEqual(import_board.parse_marker(f"[{token}]\ntext"), (token, "text"))

    def test_the_three_exact_tokens_are_in_the_vocabulary(self) -> None:
        for token in ("sprint:resume", "archive", "rejected"):
            with self.subTest(token=token):
                self.assertEqual(import_board.parse_marker(f"[{token}]\ntext"), (token, "text"))

    def test_a_bracketed_first_line_outside_the_vocabulary_keeps_the_whole_body(self) -> None:
        body = "[validate:ci-green]\nthe run was green"
        self.assertEqual(import_board.parse_marker(body), (None, body))
        self.assertEqual(import_board.marker_token(body), "validate:ci-green")

    def test_prose_that_merely_starts_with_a_bracket_is_never_eaten(self) -> None:
        body = "[see the note] and then the rest\nsecond line"
        self.assertEqual(import_board.parse_marker(body), (None, body))

    def test_a_marker_with_no_body_leaves_an_empty_body_not_a_missing_one(self) -> None:
        self.assertEqual(import_board.parse_marker("[po]"), ("po", ""))

    def test_a_bare_prefix_with_no_suffix_is_not_a_marker(self) -> None:
        self.assertEqual(import_board.parse_marker("[report:]\nx"), (None, "[report:]\nx"))


class CardMappingTests(unittest.TestCase):
    def plan_of(self, **kwargs):
        return import_board.plan(source(**kwargs))

    def test_a_card_becomes_a_tasks_row_with_its_state_from_the_column(self) -> None:
        result = self.plan_of(
            pipeline=(PRODUCT, row(2, "secretary-10", column=6, meta=card_meta())),
            registry=(registry_entry("secretary", "/home/dev/secretary"),),
        )
        (task,) = result.rows["tasks"]
        self.assertEqual(task["task_ref"], "secretary-10")
        self.assertEqual(task["task_number"], 10)
        self.assertEqual(task["state"], "blocked")
        self.assertFalse(task["archived"])

    def test_an_archived_card_is_a_row_with_archived_true_and_is_never_dropped(self) -> None:
        result = self.plan_of(pipeline=(PRODUCT, row(2, "secretary-10", active=0, meta=card_meta())))
        (task,) = result.rows["tasks"]
        self.assertTrue(task["archived"])

    def test_a_card_without_project_metadata_is_refused_and_named(self) -> None:
        result = self.plan_of(pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta(project=""))))
        self.assertEqual(result.rows["tasks"], [])
        (refusal,) = result.report.records_not_imported
        self.assertEqual(refusal["ref"], "secretary-10")
        self.assertIn("tasks.project_id is NOT NULL", refusal["reason"])

    def test_a_task_type_outside_the_vocabulary_is_refused_rather_than_defaulted(self) -> None:
        result = self.plan_of(pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta(task_type="chore"))))
        self.assertEqual(result.rows["tasks"], [])
        self.assertIn("outside the CHECK vocabulary", result.report.records_not_imported[0]["reason"])

    def test_a_duplicated_reference_keeps_the_live_row_and_names_the_other_one(self) -> None:
        result = self.plan_of(
            pipeline=(
                PRODUCT,
                row(2, "secretary-10", title="live", meta=card_meta()),
                row(3, "secretary-10", title="archived", active=0, meta=card_meta()),
            )
        )
        (task,) = result.rows["tasks"]
        self.assertEqual(task["title"], "live")
        (refusal,) = result.report.records_not_imported
        self.assertIn("kanboard task 3", refusal["ref"])

    def test_retired_launch_modes_normalize_to_null_exactly_as_the_reader_reports_them(self) -> None:
        result = self.plan_of(
            pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta(codex_launch_mode="exec")))
        )
        (task,) = result.rows["tasks"]
        self.assertIsNone(task["codex_launch_mode"])
        self.assertEqual(task["extensions"]["kanboard"]["codex_launch_mode"], "exec")

    def test_retry_heads_become_ordered_rows(self) -> None:
        result = self.plan_of(
            pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta(retry_heads="a,b,c")))
        )
        self.assertEqual(
            [(head["ordinal"], head["head"]) for head in result.rows["task_retry_heads"]],
            [(0, "a"), (1, "b"), (2, "c")],
        )

    def test_a_dependency_on_a_card_nobody_has_is_named_not_dropped(self) -> None:
        result = self.plan_of(
            pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta(blocked_by="ghost-1")))
        )
        self.assertEqual(result.rows["task_dependencies"], [])
        (link,) = result.report.links_not_imported
        self.assertEqual((link["kind"], link["target"]), ("task_dependencies", "ghost-1"))

    def test_a_dependency_on_a_card_that_exists_is_a_row_on_both_sides(self) -> None:
        result = self.plan_of(
            pipeline=(
                PRODUCT,
                row(2, "secretary-10", meta=card_meta()),
                row(3, "secretary-11", meta=card_meta(blocked_by="secretary-10")),
            )
        )
        self.assertEqual(
            result.rows["task_dependencies"], [{"task_ref": "secretary-11", "depends_on": "secretary-10"}]
        )


class ExtensionsTests(unittest.TestCase):
    """§8.2: an unrecognized key survives, and record_type is not one of them."""

    def test_an_unknown_metadata_key_lands_in_extensions_and_is_counted_per_key(self) -> None:
        result = import_board.plan(
            source(
                pipeline=(
                    PRODUCT,
                    row(2, "secretary-10", meta=card_meta(steward_report="1", model="x")),
                    row(3, "secretary-11", meta=card_meta(steward_report="1")),
                )
            )
        )
        counted = {item["key"]: item["rows"] for item in result.report.extensions_keys}
        self.assertEqual(counted["steward_report"], 2)
        self.assertEqual(counted["model"], 1)
        self.assertNotIn("record_type", counted)

    def test_record_type_is_table_identity_and_never_reaches_extensions(self) -> None:
        result = import_board.plan(source(pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta()))))
        (task,) = result.rows["tasks"]
        self.assertNotIn("record_type", task["extensions"].get("kanboard", {}))

    def test_a_lane_that_disagrees_with_the_product_derived_one_is_kept(self) -> None:
        result = import_board.plan(
            source(
                pipeline=(PRODUCT, row(2, "secretary-10", swimlane=3, meta=card_meta())),
                registry=(registry_entry("secretary", "/home/dev/secretary"),),
            )
        )
        (task,) = result.rows["tasks"]
        self.assertEqual(task["extensions"]["kanboard"]["swimlane"], "other")

    def test_a_lane_that_agrees_is_not_carried_as_provenance(self) -> None:
        result = import_board.plan(
            source(pipeline=(PRODUCT, row(2, "secretary-10", swimlane=2, meta=card_meta())))
        )
        (task,) = result.rows["tasks"]
        self.assertEqual(task["extensions"], {})


class IssueAndProductTests(unittest.TestCase):
    def issue(self, reference: str, **meta: str) -> SourceRow:
        base = {
            "record_type": "issue",
            "issue_product": "secretary",
            "issue_kind": "bug",
            "issue_priority": "P1",
        }
        base.update(meta)
        return row(5, reference, column=1, meta=base)

    def test_an_open_issue_imports_with_no_close_reason(self) -> None:
        result = import_board.plan(source(pipeline=(PRODUCT, self.issue("issue:" + "a" * 20))))
        (stored,) = result.rows["issues"]
        self.assertEqual((stored["state"], stored["close_reason"]), ("open", None))

    def test_a_closed_issue_without_a_reason_is_refused_by_the_state_check(self) -> None:
        card = self.issue("issue:" + "b" * 20)
        card.raw["is_active"] = 0
        result = import_board.plan(source(pipeline=(PRODUCT, card)))
        self.assertEqual(result.rows["issues"], [])
        self.assertIn("issue_close_reason_matches_state", result.report.records_not_imported[0]["reason"])

    def test_an_issues_metadata_key_the_table_cannot_hold_is_named(self) -> None:
        result = import_board.plan(
            source(pipeline=(PRODUCT, self.issue("issue:" + "c" * 20, slug="left-over")))
        )
        named = {item["key"] for item in result.report.fields_without_a_column}
        self.assertIn("slug", named)

    def test_a_comment_on_an_issue_has_no_table_and_the_report_says_so(self) -> None:
        card = self.issue("issue:" + "d" * 20)
        card = SourceRow(raw=card.raw, meta=card.meta, comments=(comment("[po]\nnote"),))
        result = import_board.plan(source(pipeline=(PRODUCT, card)))
        self.assertEqual(result.rows["task_comments"], [])
        (refusal,) = [item for item in result.report.records_not_imported if "comment" in item["kind"]]
        self.assertIn("no table", refusal["reason"])

    def test_a_product_carries_its_projects_as_rows(self) -> None:
        result = import_board.plan(
            source(pipeline=(PRODUCT,), registry=(registry_entry("secretary", "/home/dev/secretary"),))
        )
        self.assertEqual(
            result.rows["product_projects"], [{"product_id": "secretary", "project_id": "secretary"}]
        )


def sprint_row(
    identifier: int,
    number: int,
    *,
    active: int = 1,
    comments: tuple[dict[str, object], ...] = (),
    **meta: str,
) -> SourceRow:
    base = {
        "sprint_goal": f"goal {number}",
        "sprint_definition_of_done": f"dod {number}",
        "sprint_status": "open",
        "sprint_observer": '{"kind":"head","profile":"claude-observer"}',
    }
    base.update(meta)
    return row(
        identifier,
        f"sprint:{number}",
        title=f"sprint {number}",
        column=2,
        active=active,
        meta=base,
        comments=comments,
    )


class SprintMappingTests(unittest.TestCase):
    def test_a_sprint_carries_goal_dod_status_and_a_tagged_observer(self) -> None:
        result = import_board.plan(source(sprints=(sprint_row(9, 100),)))
        (stored,) = result.rows["sprints"]
        self.assertEqual(stored["sprint_number"], 100)
        self.assertEqual(stored["goal"], "goal 100")
        self.assertEqual(stored["observer"], {"kind": "head", "profile": "claude-observer"})
        self.assertIsNone(stored["closed_at"])

    def test_a_closed_sprint_gets_the_time_its_check_requires_and_the_report_calls_it_approximate(self) -> None:
        result = import_board.plan(source(sprints=(sprint_row(9, 100, sprint_status="closed"),)))
        (stored,) = result.rows["sprints"]
        self.assertIsNotNone(stored["closed_at"])
        (approximate,) = [
            item for item in result.report.approximate_values if item["field"] == "sprints.closed_at"
        ]
        self.assertEqual(approximate["rows"], 1)

    def test_a_reference_that_is_not_a_number_is_refused_because_the_key_is_an_integer(self) -> None:
        result = import_board.plan(
            source(sprints=(row(9, "sprint:canary-20260813", meta={"sprint_goal": "g"}),))
        )
        self.assertEqual(result.rows["sprints"], [])
        self.assertIn("sprint:<N>", result.report.records_not_imported[0]["reason"])

    def test_the_current_task_cursor_is_scoped_to_the_sprints_own_cards(self) -> None:
        result = import_board.plan(
            source(
                pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta(sprint_ref="sprint:100"))),
                sprints=(sprint_row(9, 100, sprint_current_task="secretary-10"),),
            )
        )
        self.assertEqual(result.rows["sprints"][0]["current_task_ref"], "secretary-10")

    def test_a_cursor_naming_another_sprints_card_is_refused_and_named(self) -> None:
        result = import_board.plan(
            source(
                pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta(sprint_ref="sprint:101"))),
                sprints=(sprint_row(9, 100, sprint_current_task="secretary-10"), sprint_row(8, 101)),
            )
        )
        stored = {item["sprint_number"]: item for item in result.rows["sprints"]}
        self.assertIsNone(stored[100]["current_task_ref"])
        (link,) = [
            item for item in result.report.links_not_imported if item["kind"] == "sprints.current_task_ref"
        ]
        self.assertIn("belongs to another sprint", link["reason"])

    def test_a_closed_sprints_reservation_is_released_rather_than_left_live(self) -> None:
        result = import_board.plan(
            source(
                sprints=(
                    sprint_row(9, 100, sprint_status="closed", sprint_reservations='["secretary"]'),
                    sprint_row(8, 101, sprint_reservations='["secretary"]'),
                ),
                registry=(registry_entry("secretary", "/home/dev/secretary"),),
            )
        )
        held = {item["sprint_number"]: item for item in result.rows["sprint_projects"]}
        self.assertFalse(held[100]["reserved"])
        self.assertIsNotNone(held[100]["released_at"])
        self.assertTrue(held[101]["reserved"])

    def test_a_second_live_reservation_of_one_project_is_refused_by_name(self) -> None:
        result = import_board.plan(
            source(
                sprints=(
                    sprint_row(9, 100, sprint_reservations='["secretary"]'),
                    sprint_row(8, 101, sprint_reservations='["secretary"]'),
                ),
                registry=(registry_entry("secretary", "/home/dev/secretary"),),
            )
        )
        self.assertEqual(len(result.rows["sprint_projects"]), 1)
        (link,) = [item for item in result.report.links_not_imported if item["kind"] == "sprint_projects"]
        self.assertIn("sprint_projects_one_live_reservation", link["reason"])

    def test_a_resume_with_all_six_fields_becomes_a_row(self) -> None:
        resume = (
            '{"selected_step":"a","selected_why":"b","rejected_alternatives":"c",'
            '"current_task":"d","dod_state":"e","next_safe_step":"f",'
            '"recorded_at":"2026-09-01T00:00:00Z"}'
        )
        result = import_board.plan(source(sprints=(sprint_row(9, 100, sprint_resume=resume),)))
        (stored,) = result.rows["sprint_resumes"]
        self.assertEqual(stored["selected_step"], "a")
        self.assertEqual(stored["sprint_number"], 100)

    def test_a_resume_missing_a_required_field_is_refused_not_padded(self) -> None:
        result = import_board.plan(source(sprints=(sprint_row(9, 100, sprint_resume='{"selected_step":"a"}'),)))
        self.assertEqual(result.rows["sprint_resumes"], [])
        self.assertIn("RESUME_FIELDS", result.report.records_not_imported[0]["reason"])

    def test_a_sprint_comment_is_marked_and_stripped_like_a_card_comment(self) -> None:
        result = import_board.plan(
            source(sprints=(sprint_row(9, 100, comments=(comment("[sprint:resume]\nwhat happened"),)),))
        )
        (stored,) = result.rows["sprint_comments"]
        self.assertEqual((stored["marker"], stored["body"]), ("sprint:resume", "what happened"))
        self.assertIsNone(stored["actor_role"])


class BudgetTests(unittest.TestCase):
    """§8.7: the counters become rows, and the sums have to reconcile exactly."""

    def budget(self, **by_type: int) -> str:
        import json

        return json.dumps({"by_type": by_type})

    def test_each_counted_charge_becomes_one_row_and_one_claimed_request(self) -> None:
        result = import_board.plan(
            source(sprints=(sprint_row(9, 100, sprint_budget=self.budget(red_review=2, blocked=1)),))
        )
        self.assertEqual(len(result.rows["sprint_budget_events"]), 3)
        self.assertEqual(len(result.rows["requests"]), 3)
        self.assertTrue(result.report.budget["sums_reconcile"])
        self.assertEqual(result.report.budget["per_type"]["red_review"], 2)

    def test_an_uncharged_type_is_a_row_that_spends_nothing(self) -> None:
        result = import_board.plan(
            source(
                sprints=(
                    sprint_row(
                        9,
                        100,
                        sprint_budget=self.budget(),
                        sprint_budget_uncharged='{"infrastructure_blocked": 2}',
                    ),
                )
            )
        )
        self.assertEqual([item["charged"] for item in result.rows["sprint_budget_events"]], [False, False])

    def test_a_journal_record_supplies_the_real_request_id_and_timestamp(self) -> None:
        record = {
            "kind": "budget_recorded",
            "ref": "sprint:100",
            "request_id": "sprint-budget-evt_1",
            "event_id": "evt_1",
            "occurred_at": "2026-08-01T10:00:00Z",
            "payload": {"event_type": "red_review"},
        }
        result = import_board.plan(
            source(
                sprints=(sprint_row(9, 100, sprint_budget=self.budget(red_review=1)),),
                budget_records=(record,),
            )
        )
        (stored,) = result.rows["sprint_budget_events"]
        self.assertEqual(stored["request_id"], "sprint-budget-evt_1")
        self.assertEqual(stored["occurred_at"].isoformat(), "2026-08-01T10:00:00+00:00")
        self.assertEqual(result.report.budget["approximate"], 0)

    def test_a_charge_with_no_journal_record_is_synthesized_and_marked_approximate(self) -> None:
        result = import_board.plan(
            source(sprints=(sprint_row(9, 100, sprint_budget=self.budget(red_ci=1)),))
        )
        (stored,) = result.rows["sprint_budget_events"]
        self.assertIn("approximate", stored["reason"])
        self.assertEqual(result.report.budget["approximate"], 1)
        (item,) = [
            entry
            for entry in result.report.approximate_values
            if entry["field"] == "sprint_budget_events.occurred_at"
        ]
        self.assertEqual(item["rows"], 1)

    def test_a_journal_holding_more_charges_than_the_counter_is_reported_as_a_mismatch(self) -> None:
        records = tuple(
            {
                "kind": "budget_recorded",
                "ref": "sprint:100",
                "request_id": f"sprint-budget-evt_{index}",
                "event_id": f"evt_{index}",
                "occurred_at": f"2026-08-0{index + 1}T10:00:00Z",
                "payload": {"event_type": "red_review"},
            }
            for index in range(3)
        )
        result = import_board.plan(
            source(
                sprints=(sprint_row(9, 100, sprint_budget=self.budget(red_review=1)),),
                budget_records=records,
            )
        )
        self.assertEqual(len(result.rows["sprint_budget_events"]), 1)
        (mismatch,) = result.report.budget["mismatches"]
        self.assertEqual((mismatch["counter"], mismatch["audit_journal"]), (1, 3))


class ProjectRepositoryTests(unittest.TestCase):
    """§8.3: every record survives, and the absences are recorded as absences."""

    def test_a_repository_path_no_registry_entry_claims_still_gets_a_row(self) -> None:
        result = import_board.plan(
            source(
                sprints=(sprint_row(9, 100, sprint_repositories='["/tmp/nowhere"]'),),
                registry=(registry_entry("secretary", "/home/dev/secretary"),),
            )
        )
        stored = {item["path"]: item for item in result.rows["repositories"]}
        self.assertIsNone(stored["/tmp/nowhere"]["project_id"])
        self.assertEqual(len(result.rows["sprint_repositories"]), 1)
        self.assertIn(
            "/tmp/nowhere", result.report.project_repository_mismatch["repository_paths_without_project"]
        )

    def test_a_repository_entry_that_is_a_project_id_is_kept_and_named(self) -> None:
        result = import_board.plan(source(sprints=(sprint_row(9, 100, sprint_repositories='["secretary"]'),)))
        self.assertIn(
            "secretary",
            result.report.project_repository_mismatch["repository_entries_that_are_not_absolute_paths"],
        )
        self.assertEqual(len(result.rows["sprint_repositories"]), 1)

    def test_a_project_history_names_but_the_registry_lost_is_registry_present_false(self) -> None:
        result = import_board.plan(
            source(
                pipeline=(PRODUCT, row(2, "gone-10", meta=card_meta(project="gone"))),
                registry=(registry_entry("secretary", "/home/dev/secretary"),),
            )
        )
        stored = {item["project_id"]: item for item in result.rows["projects"]}
        self.assertFalse(stored["gone"]["registry_present"])
        self.assertTrue(stored["secretary"]["registry_present"])
        self.assertIn("gone", result.report.project_repository_mismatch["project_ids_without_registry_file"])

    def test_a_curator_root_is_a_second_row_for_the_same_project(self) -> None:
        result = import_board.plan(
            source(
                registry=(
                    registry_entry("secretary", "/home/dev/secretary", curator_roots=("/home/dev/wt",)),
                )
            )
        )
        roles = {item["path"]: item["role"] for item in result.rows["repositories"]}
        self.assertEqual(roles, {"/home/dev/secretary": "primary", "/home/dev/wt": "curator_root"})


class DecisionTests(unittest.TestCase):
    """§8.5: recovered where a document survives, never invented where none does."""

    def document(self) -> dict[str, object]:
        return {
            "version": 1,
            "request_id": "close-100",
            "kind": "closed",
            "intent": {"reference": "sprint:100"},
            "event": {
                "ref": "sprint:100",
                "occurred_at": "2026-09-01T12:00:00Z",
                "payload": {
                    "decisions": {
                        "issues": [
                            {"ref": "issue:" + "a" * 20, "verdict": "resolved", "reason": "shipped"}
                        ],
                        "cards": [{"ref": "secretary-10", "verdict": "done", "reason": "landed"}],
                    }
                },
            },
        }

    def board(self, documents):
        issue = row(
            5,
            "issue:" + "a" * 20,
            column=1,
            meta={
                "record_type": "issue",
                "issue_product": "secretary",
                "issue_kind": "bug",
                "issue_priority": "P1",
            },
        )
        return source(
            pipeline=(PRODUCT, issue, row(2, "secretary-10", meta=card_meta(sprint_ref="sprint:100"))),
            sprints=(
                sprint_row(
                    9,
                    100,
                    sprint_status="closed",
                    sprint_issues='["issue:' + "a" * 20 + '"]',
                ),
            ),
            transaction_documents=documents,
        )

    def test_a_surviving_document_materializes_both_subject_kinds(self) -> None:
        result = import_board.plan(self.board((self.document(),)))
        kinds = {item["subject_kind"]: item for item in result.rows["sprint_decisions"]}
        self.assertEqual(set(kinds), {"issue", "card"})
        self.assertEqual(kinds["issue"]["verdict"], "resolved")
        self.assertEqual(kinds["card"]["task_ref"], "secretary-10")
        self.assertEqual(result.report.sprints_without_recoverable_decisions, [])
        self.assertIn("close-100", {item["request_id"] for item in result.rows["requests"]})

    def test_a_sprint_with_no_document_is_named_and_nothing_is_invented(self) -> None:
        result = import_board.plan(self.board(()))
        self.assertEqual(result.rows["sprint_decisions"], [])
        self.assertEqual(result.report.sprints_without_recoverable_decisions, ["sprint:100"])

    def test_a_decision_for_an_issue_the_sprint_did_not_declare_is_refused(self) -> None:
        document = self.document()
        document["event"]["payload"]["decisions"]["issues"][0]["ref"] = "issue:" + "z" * 20
        result = import_board.plan(self.board((document,)))
        self.assertEqual([item["subject_kind"] for item in result.rows["sprint_decisions"]], ["card"])
        (link,) = [
            item for item in result.report.links_not_imported if item["kind"] == "sprint_decisions.issue"
        ]
        self.assertIn("decided_issue_is_declared_by_this_sprint", link["reason"])


class ReportShapeTests(unittest.TestCase):
    def test_the_two_structural_zeroes_are_always_explained(self) -> None:
        result = import_board.plan(source(pipeline=(PRODUCT,)))
        explained = {item["table"]: item["reason"] for item in result.report.expected_zero}
        self.assertIn("task_issues", explained)
        self.assertIn("no source field exists", explained["task_issues"])
        self.assertIn("board_events", explained)

    def test_every_table_the_import_fills_has_a_count(self) -> None:
        result = import_board.plan(source(pipeline=(PRODUCT,)))
        self.assertEqual(set(result.report.counts), set(import_board.TABLE_ORDER))

    def test_the_report_is_json_serializable_and_renders(self) -> None:
        import json

        result = import_board.plan(source(pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta()))))
        result.report.parity = import_board.parity(
            source(pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta()))),
            result.rows,
            result.report,
        )
        json.dumps(result.report.as_dict(), default=str)
        self.assertIn("parity: PASS", import_board.render(result.report))

    def test_parity_fails_loudly_when_a_row_is_dropped_without_a_refusal(self) -> None:
        board = source(pipeline=(PRODUCT, row(2, "secretary-10", meta=card_meta())))
        result = import_board.plan(board)
        result.rows["tasks"] = []
        checks = import_board.parity(board, result.rows, result.report)
        self.assertFalse(checks["ok"])
        self.assertTrue(any(not check["ok"] for check in checks["identifiers"]))
