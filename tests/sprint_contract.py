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
            "test_hard_stop_pre_effect_failure_discards_its_generic_charge",
            "test_reconcile_never_publishes_a_pending_hard_charge_before_its_typed_stop",
            "test_host_sprint_replay_rejects_changed_related_refs_when_committed_or_pending",
            "test_pending_typed_close_recovers_without_repeating_the_status_write",
            "test_reopen_refuses_to_open_when_observer_read_back_is_not_proven",
        ),
        "exercises staged audit repair after a Kanboard effect; SQL must prove rollback atomicity",
    ),
    **_cases(
        "tests.test_sprints.SprintTests",
        ("test_close_repairs_an_archive_that_lost_its_backend_reply",),
        "models a lost Kanboard closeTask reply and counts the transport retry",
    ),
    **_cases(
        "tests.test_sprints.SprintTests",
        ("test_close_archives_only_its_done_tasks_and_leaves_issues_and_unlinked_cards",),
        "asserts the Kanboard closeTask ordering for done and disposed-open cards",
    ),
    **_cases(
        "tests.test_sprints.SprintAuditTraversalTests",
        (
            "test_mass_sprint_status_costs_the_same_board_round_trips_for_one_and_for_many",
            "test_an_unusable_terminal_resume_still_fails_closed_without_the_audit",
        ),
        "asserts Kanboard JSON-RPC batching or a malformed legacy metadata row SQL cannot store",
    ),
    **_cases(
        "tests.test_sprints.SprintSingleWriterGuardTests",
        (
            "test_unavailable_sprint_board_fails_closed",
            "test_pending_sprint_recovery_rebuilds_its_project_index",
            "test_guard_read_avoids_sprint_comment_history",
        ),
        "asserts a Kanboard error/recovery shape or the absence of a specific JSON-RPC read",
    ),
    **_cases(
        "tests.test_sprints.SprintSingleWriterGuardTests",
        ("test_close_releases_every_repository_and_unheld_projects_skip_sprint_board",),
        "asserts that an unheld card omits a specific Kanboard sprint-board lookup",
    ),
    **_cases(
        "tests.test_sprints.SprintSingleWriterGuardTests",
        ("test_observer_can_write_when_another_open_sprint_shares_the_repository",),
        "constructs two open Sprints sharing one live project reservation, a state SQL forbids",
    ),
    **_cases(
        "tests.test_sprints.SprintCloseDecisionTests",
        (
            "test_an_interrupted_close_continues_without_repeating_what_it_did",
            "test_no_successor_is_admitted_while_a_close_is_still_disposing_of_a_card",
            "test_an_issue_closed_from_elsewhere_leaves_the_close_recoverable",
            "test_an_issue_closed_from_elsewhere_mid_close_stops_and_is_amended",
            "test_a_card_moved_by_somebody_else_stops_the_close_and_is_confirmed",
            "test_a_step_whose_event_did_not_commit_is_repaired_not_assumed",
            "test_no_step_of_a_close_ends_on_anything_but_its_own_committed_event",
            "test_a_successor_is_refused_until_an_interrupted_close_is_finished",
            "test_every_step_of_the_terminal_phase_is_retried_where_it_stopped",
        ),
        "exercises Kanboard's staged multi-step close repair; SQL must prove one transaction atomic",
    ),
    **_cases(
        "tests.test_sprints.SprintCloseDecisionTests",
        ("test_a_retry_that_states_other_decisions_is_refused",),
        "requires a failed Kanboard effect to retain its request claim; SQL rolls the claim back",
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
    **_cases(
        "tests.test_sprint_restore.SprintRestoreTests",
        ("test_an_open_row_is_never_published_without_its_observer",),
        "asserts ordering between Kanboard metadata and reference RPCs during staged create",
    ),
    **_cases(
        "tests.test_sprint_restore.SprintRestoreTests",
        (
            "test_checkpoint_without_sprint_ownership_restores_as_it_was",
            "test_ownership_a_legacy_entity_never_had_fails_the_parity_gate",
        ),
        "constructs a legacy sprint without SQL's Product/Issue ownership constraints",
    ),
    **_cases(
        "tests.test_sprint_restore.SprintRestoreTests",
        ("test_parity_failure_leaves_recovery_incomplete_with_a_named_error",),
        "injects a lossy Kanboard saveTaskMetadata response during restore parity",
    ),
    **_cases(
        "tests.test_sprint_restore.SprintRestoreTests",
        ("test_export_without_a_sprint_board_leaves_the_target_untouched",),
        "asserts restore omits the Kanboard createProject transport when sprints.json is absent",
    ),
}
