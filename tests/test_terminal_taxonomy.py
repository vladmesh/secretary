"""Forward terminal taxonomy contracts independent of lifecycle authority."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from secretary.board.models import Actor, EntityKind, Event, EventKind
from secretary.board.terminal_taxonomy import (
    TerminalTaxonomyValidationError,
    budget_event_type,
    normalize_terminal_taxonomy,
    read_terminal_taxonomy,
)


class TerminalTaxonomyTests(unittest.TestCase):
    def test_all_dispositions_are_independent_and_nonblocked_have_no_reason(self) -> None:
        for disposition in ("release", "rework", "reslice", "drop", "operator_stop"):
            with self.subTest(disposition=disposition):
                taxonomy = normalize_terminal_taxonomy(disposition=disposition)
                self.assertEqual((taxonomy.disposition, taxonomy.blocked_reason), (disposition, None))

    def test_forward_task_contract_mapping_keeps_the_worker_evidence(self) -> None:
        taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason="wrong_task_definition")
        self.assertEqual(taxonomy.blocked_reason, "task_contract")
        self.assertEqual(taxonomy.source_evidence, "wrong_task_definition")

    def test_all_forward_blocked_reasons_are_typed(self) -> None:
        for reason in (
            "implementation",
            "review",
            "task_contract",
            "gate",
            "provider",
            "infrastructure",
            "operator",
            "other",
        ):
            with self.subTest(reason=reason):
                taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason=reason)
                self.assertEqual((taxonomy.blocked_reason, taxonomy.source_evidence), (reason, reason))

    def test_external_fact_remains_evidence_without_invented_precision(self) -> None:
        taxonomy = normalize_terminal_taxonomy(disposition="blocked", blocked_reason="external_fact")
        self.assertEqual(taxonomy.blocked_reason, "other")
        self.assertEqual(taxonomy.source_evidence, "external_fact")

    def test_invalid_new_values_are_rejected_at_the_named_boundary(self) -> None:
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "disposition"):
            normalize_terminal_taxonomy(disposition="merge")
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "blocked reason"):
            normalize_terminal_taxonomy(disposition="blocked", blocked_reason="free-form prose")
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "exactly"):
            normalize_terminal_taxonomy(disposition="release", blocked_reason="operator")

    def test_missing_and_legacy_reads_are_explicitly_legacy_other(self) -> None:
        taxonomy = read_terminal_taxonomy({}, disposition="blocked")
        self.assertEqual(
            (taxonomy.blocked_reason, taxonomy.source_evidence, taxonomy.provenance),
            ("other", None, "legacy"),
        )

    def test_malformed_forward_record_fails_closed(self) -> None:
        with self.assertRaisesRegex(TerminalTaxonomyValidationError, "field set"):
            read_terminal_taxonomy(
                {"terminal_taxonomy": {"version": 1, "disposition": "blocked"}},
                disposition="blocked",
            )

    def test_forward_reslice_reads_its_committed_disposition_not_blocked_target(self) -> None:
        taxonomy = read_terminal_taxonomy(
            {
                "terminal_taxonomy": normalize_terminal_taxonomy(disposition="reslice").to_record(),
            },
            disposition=None,
        )
        self.assertEqual(taxonomy.disposition, "reslice")
        self.assertIsNone(budget_event_type(taxonomy))

    def test_typed_event_boundary_rejects_malformed_forward_taxonomy(self) -> None:
        with self.assertRaisesRegex(ValueError, "terminal taxonomy"):
            Event(
                event_id="evt-bad-taxonomy",
                kind=EventKind.CARD_BLOCKED,
                entity_kind=EntityKind.CARD,
                ref="secretary-1533",
                actor=Actor("dispatcher", "dispatcher"),
                reason="blocked",
                occurred_at=datetime.now(UTC),
                source_state="in_progress",
                target_state="blocked",
                data={"terminal_taxonomy": {"version": 1}},
            )

    def test_budget_charge_uses_the_same_normalized_reason(self) -> None:
        infrastructure = normalize_terminal_taxonomy(disposition="blocked", blocked_reason="infrastructure")
        task_contract = normalize_terminal_taxonomy(
            disposition="blocked", blocked_reason="wrong_task_definition"
        )
        self.assertEqual(budget_event_type(infrastructure), "infrastructure_blocked")
        self.assertEqual(budget_event_type(task_contract), "blocked")
