"""Inventory and backend boundary for the reusable sprint contract tests.

Every test method in the four historical sprint suites is classified exactly once by
``test_sprint_fixture_guards``.  The entries below are not a list of inconvenient fixtures.  They
are the cases whose subject is specifically Kanboard transport, a legacy row shape PostgreSQL
constraints cannot represent, or recovery from a half-applied filesystem transaction.  SQL must
instead prove those mutations atomic in its own positive transaction tests.
"""

from __future__ import annotations

from typing import Final


def _cases(owner: str, names: tuple[str, ...], reason: str) -> dict[str, str]:
    return {f"{owner}.{name}": reason for name in names}


KANBOARD_ONLY: Final[dict[str, str]] = {
    **_cases(
        "tests.test_sprints.SprintOwnershipTests",
        (
            "test_a_refused_metadata_write_leaves_no_row_and_stays_repairable",
            "test_a_refused_reference_write_leaves_no_row_without_a_reference",
            "test_a_staged_create_is_resumed_before_any_live_check",
            "test_a_staged_create_that_lost_the_slot_publishes_nothing",
            "test_a_staged_create_never_takes_over_a_sprint_sharing_its_reference",
            "test_a_refused_create_whose_row_survives_is_answered_as_repairable",
            "test_a_refused_reopen_stays_repairable_and_reports_no_transition",
            "test_a_staged_reopen_is_resumed_before_any_live_check",
            "test_a_staged_reopen_that_lost_the_slot_publishes_nothing",
            "test_a_refused_reopen_puts_back_the_observer_its_attempt_wrote",
            "test_a_refused_reopen_that_cannot_put_the_observer_back_stays_repairable",
        ),
        "models a Kanboard false write reply or repairs its half-applied filesystem transaction",
    ),
    **_cases(
        "tests.test_sprints.SprintOwnershipTests",
        (
            "test_a_sprint_without_ownership_gains_none_in_show_status_or_export",
            "test_reopen_of_a_legacy_sprint_fails_closed_without_filling_fields",
        ),
        "constructs a legacy sprint row without SQL's Product/Issue ownership constraints",
    ),
    **_cases(
        "tests.test_sprints.SprintOwnershipTests",
        ("test_board_layout_without_issues_column_fails_closed",),
        "removes a Kanboard transport column and asserts that malformed board layout",
    ),
    **_cases(
        "tests.test_sprints.SprintTests",
        (
            "test_board_creation_is_idempotent",
            "test_read_only_sprint_list_does_not_create_a_board_or_claim_resume_freshness",
        ),
        "asserts creation or non-creation of the Kanboard project transport",
    ),
    **_cases(
        "tests.test_sprints.SprintTests",
        (
            "test_missing_metadata_reads_as_empty_contract_values",
            "test_metadata_less_sprint_still_closes_through_the_host",
        ),
        "constructs a metadata-less legacy Kanboard row that SQL NOT NULL constraints cannot store",
    ),
    **_cases(
        "tests.test_sprints.SprintTests",
        ("test_export_reads_records_without_the_board_or_the_linked_cards",),
        "asserts the absence and ordering of Kanboard board RPCs during export",
    ),
    **_cases(
        "tests.test_sprints.SprintTests",
        (
            "test_hard_stop_stages_typed_then_persists_budget_and_state_together",
            "test_hard_stop_post_effect_failure_retries_its_typed_owner_before_charge",
            "test_host_sprint_replay_rejects_changed_related_refs_when_committed_or_pending",
            "test_reopen_refuses_to_open_when_observer_read_back_is_not_proven",
        ),
        "exercises staged audit repair after a Kanboard effect; SQL must prove rollback atomicity",
    ),
    **_cases(
        "tests.test_sprints.SprintAuditTraversalTests",
        (
            "test_an_unusable_terminal_resume_still_fails_closed_without_the_audit",
        ),
        "asserts Kanboard JSON-RPC batching or a malformed legacy metadata row SQL cannot store",
    ),
    **_cases(
        "tests.test_sprints.SprintSingleWriterGuardTests",
        (
            "test_guard_read_avoids_sprint_comment_history",
        ),
        "asserts a Kanboard error/recovery shape or the absence of a specific JSON-RPC read",
    ),
    **_cases(
        "tests.test_sprints.SprintCloseDecisionTests",
        (
            "test_an_issue_closed_from_elsewhere_mid_close_stops_and_is_amended",
        ),
        "exercises Kanboard's staged multi-step close repair; SQL must prove one transaction atomic",
    ),
    **_cases(
        "tests.test_sprint_executors.SprintExecutorPinTests",
        (
            "test_a_sprint_that_pins_neither_role_says_so_and_writes_no_field",
            "test_each_role_is_pinned_on_its_own",
        ),
        "asserts the presence or absence of specific Kanboard metadata keys, not the normalized pin",
    ),
    **_cases(
        "tests.test_sprint_executors.SprintCardExecutorTests",
        ("test_a_pin_that_cannot_be_read_stops_the_card_instead_of_being_ignored",),
        "injects malformed Kanboard metadata that the SQL executor columns cannot store",
    ),
    **_cases(
        "tests.test_sprint_listing_budget.SprintListingBudgetTests",
        (
            "test_the_listing_costs_the_same_whether_it_lists_two_sprints_or_forty",
            "test_the_listing_is_one_pass_per_board_and_batched_metadata",
            "test_one_listing_traverses_the_committed_audit_at_most_once",
            "test_watching_one_sprint_costs_what_listing_them_all_does",
        ),
        "measures Kanboard requests and JSON-RPC batches; the normalized listing is covered elsewhere",
    ),
}


#: The cases whose body writes or claims a card. Cards have one implementation, PostgreSQL
#: (secretary-1669), so the Sprint Kanboard fixture cannot serve them: each is skipped on that
#: fixture and runs on the store through its twin in tests/test_sprints_sql_backend.py. None of
#: them is in KANBOARD_ONLY, so every one still runs on the backend production ships.
CARD_STORE_ONLY: Final[frozenset[str]] = frozenset(
    {
        "tests.test_sprint_executors.CardEditExecutorTests.test_an_edit_cannot_clear_the_pin_off_the_card",
        "tests.test_sprint_executors.CardEditExecutorTests.test_an_edit_may_restate_the_pin_and_may_not_replace_it",
        "tests.test_sprint_executors.CardEditExecutorTests.test_an_unpinned_sprint_edits_exactly_as_before",
        "tests.test_sprint_executors.SprintCardExecutorTests.test_a_card_asking_for_another_profile_is_refused_by_name",
        "tests.test_sprint_executors.SprintCardExecutorTests.test_a_card_of_a_pinned_sprint_runs_the_pinned_profiles",
        "tests.test_sprint_executors.SprintCardExecutorTests.test_a_sprint_that_pins_nothing_adds_no_check",
        "tests.test_sprint_executors.SprintCardExecutorTests.test_the_reviewer_pin_is_held_on_its_own",
        "tests.test_sprint_executors.SprintExecutorRecoveryTests.test_a_pin_that_is_not_a_profile_stops_the_restore_before_the_first_write",
        "tests.test_sprint_executors.SprintExecutorRecoveryTests.test_absence_comes_back_as_absence",
        "tests.test_sprint_executors.SprintExecutorRecoveryTests.test_each_pin_comes_back_as_the_profile_it_was",
        "tests.test_sprint_executors.SprintExecutorRecoveryTests.test_the_recovered_row_normalizes_back_to_the_record_it_came_from",
        "tests.test_sprint_restore.SprintRestoreTests.test_a_declared_head_the_registry_no_longer_has_is_refused",
        "tests.test_sprint_restore.SprintRestoreTests.test_a_second_disaster_keeps_the_declared_observer",
        "tests.test_sprint_restore.SprintRestoreTests.test_an_export_missing_an_observer_is_refused",
        "tests.test_sprint_restore.SprintRestoreTests.test_an_invalid_observer_value_stops_the_restore_before_the_first_write",
        "tests.test_sprint_restore.SprintRestoreTests.test_an_open_row_is_refused_when_its_export_carries_provenance",
        "tests.test_sprint_restore.SprintRestoreTests.test_closed_sprint_is_rebuilt_field_by_field_in_an_empty_backend",
        "tests.test_sprint_restore.SprintRestoreTests.test_export_carries_the_sprint_set_next_to_the_cards",
        "tests.test_sprint_restore.SprintRestoreTests.test_foreign_sprint_on_the_target_board_stops_the_restore",
        "tests.test_sprint_restore.SprintRestoreTests.test_invalid_sprint_export_is_refused_before_any_live_write",
        "tests.test_sprint_restore.SprintRestoreTests.test_pipeline_cards_still_restore_alongside_the_entities",
        "tests.test_sprint_restore.SprintRestoreTests.test_recovery_interrupted_before_the_sprint_step_reports_it_unfinished",
        "tests.test_sprint_restore.SprintRestoreTests.test_repeated_restore_creates_one_entity_and_no_duplicate_records",
        "tests.test_sprint_restore.SprintRestoreTests.test_restore_at_the_pilot_limit_judges_the_open_set_by_the_same_rules",
        "tests.test_sprint_restore.SprintRestoreTests.test_restore_holds_the_admission_lock_from_its_check_to_its_write",
        "tests.test_sprint_restore.SprintRestoreTests.test_restore_refuses_a_lone_open_row_whose_root_is_not_canonical",
        "tests.test_sprint_restore.SprintRestoreTests.test_restore_refuses_a_second_open_row_that_declares_no_product",
        "tests.test_sprint_restore.SprintRestoreTests.test_restore_refuses_an_export_of_open_sprints_admission_would_have_refused",
        "tests.test_sprint_restore.SprintRestoreTests.test_restore_refuses_an_open_row_whose_root_is_not_canonical",
        "tests.test_sprint_restore.SprintRestoreTests.test_second_disaster_restores_from_the_checkpoint_of_the_first_recovery",
        "tests.test_sprint_restore.SprintRestoreTests.test_sprint_comments_have_only_the_shared_restore_representation",
        "tests.test_sprint_restore.SprintRestoreTests.test_the_observer_declaration_survives_a_round_trip",
        "tests.test_sprints.SprintAuditTraversalTests.test_every_terminal_transition_freezes_freshness_on_the_sprint_record",
        "tests.test_sprints.SprintAuditTraversalTests.test_mass_sprint_status_reads_the_audit_once_for_one_and_for_many_sprints",
        "tests.test_sprints.SprintCloseDecisionTests.test_a_close_short_of_a_disposition_names_the_cards_and_their_states",
        "tests.test_sprints.SprintCloseDecisionTests.test_cli_close_reads_its_decisions_from_a_file",
        "tests.test_sprints.SprintCloseDecisionTests.test_dispositions_take_every_card_into_a_recorded_end",
        "tests.test_sprints.SprintReservedProjectGuardTests.test_a_project_no_sprint_reserves_is_unaffected",
        "tests.test_sprints.SprintReservedProjectGuardTests.test_a_stale_repository_keyed_index_is_rebuilt_before_it_answers",
        "tests.test_sprints.SprintReservedProjectGuardTests.test_observer_may_not_move_a_card_of_an_unreserved_project",
        "tests.test_sprints.SprintReservedProjectGuardTests.test_observer_moves_and_edits_a_card_of_its_reserved_project",
        "tests.test_sprints.SprintReservedProjectGuardTests.test_the_reservation_guard_denies_an_unauthorized_write",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_a_granted_override_is_recorded_once_and_survives_reconcile",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_denied_create_request_can_succeed_after_sprint_closes",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_dispatcher_cycle_and_observer_move_are_allowed",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_every_move_path_through_the_sprint_guard_records_its_decision",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_linked_task_still_obeys_the_held_project_guard",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_missing_index_bootstraps_from_live_open_sprints",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_observer_must_link_to_its_open_sprint_and_other_roles_are_denied",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_override_retry_reuses_the_denied_request_id_for_the_write",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_po_cannot_edit_a_held_card_without_an_audited_override",
        "tests.test_sprints.SprintSingleWriterGuardTests.test_po_override_requires_reason_and_is_audited_once",
        "tests.test_sprints.SprintStatusHeadlessCommandTests.test_the_command_names_the_sprints_headless_cards",
        "tests.test_sprints.SprintStatusHeadlessCommandTests.test_the_command_says_nothing_when_every_card_owns_its_worker",
        "tests.test_sprints.SprintTests.test_cli_observer_can_set_current_task",
        "tests.test_sprints.SprintTests.test_close_propagates_a_terminal_archive_refusal_without_leaving_a_transaction",
        "tests.test_sprints.SprintTests.test_hard_stop_replay_keeps_its_stored_related_refs_after_card_archive",
        "tests.test_sprints.SprintTests.test_naive_resume_timestamp_is_rejected_and_legacy_data_fails_closed",
        "tests.test_sprints.SprintTests.test_resume_freshness_ignores_denied_and_failed_card_events",
        "tests.test_sprints.SprintTests.test_resume_requires_all_fields_and_staleness_uses_card_audit",
        "tests.test_sprints.SprintTests.test_task_creation_requires_an_open_reserved_sprint_and_rejects_priority_before_writes",
        "tests.test_sprints.SprintTests.test_task_link_is_live_metadata_and_closed_sprint_rejects_writes",
        "tests.test_sprints.TwoOpenSprintIsolationTests.test_a_bound_head_still_writes_its_own_sprint",
        "tests.test_sprints.TwoOpenSprintIsolationTests.test_a_card_of_the_remaining_sprints_project_is_still_guarded_after_the_close",
        "tests.test_sprints.TwoOpenSprintIsolationTests.test_a_head_nobody_bound_writes_nothing_at_all",
        "tests.test_sprints.TwoOpenSprintIsolationTests.test_an_observer_of_one_sprint_writes_nothing_of_the_other",
    }
)
