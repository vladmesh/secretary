from __future__ import annotations

import unittest

from secretary.board.protocol_artifacts import (
    ArtifactOwner,
    PROTOCOL_ARTIFACTS,
    parse_rework_requirements,
    validate_rework_instruction,
)


class ProtocolArtifactRegistryTests(unittest.TestCase):
    def test_registry_names_the_owner_of_every_cross_role_artifact(self) -> None:
        self.assertEqual(
            {name: artifact.owner for name, artifact in PROTOCOL_ARTIFACTS.items()},
            {
                "worker_local_broad_check_receipt": ArtifactOwner.WORKER,
                "dispatcher_executed_exact_sha_gate_receipt": ArtifactOwner.DISPATCHER,
                "independent_reviewer_verdict": ArtifactOwner.REVIEWER,
                "observer_decision": ArtifactOwner.OBSERVER,
            },
        )

    def test_gate_receipt_owner_comes_from_the_registry_not_owner_wording(self) -> None:
        violation = validate_rework_instruction(
            "Obtain an executed exact-SHA gate receipt before reporting the repair.",
            specification_revision="specification-revision-1",
        )

        assert violation is not None
        self.assertEqual(violation.artifact.name, "dispatcher_executed_exact_sha_gate_receipt")
        self.assertEqual(violation.artifact.owner, ArtifactOwner.DISPATCHER)
        self.assertEqual(violation.requested_role, ArtifactOwner.WORKER)
        self.assertEqual(violation.specification_revision, "specification-revision-1")

    def test_action_cannot_borrow_a_reviewer_verdict_from_another_clause(self) -> None:
        self.assertIsNone(
            validate_rework_instruction(
                "Address each blocker in the reviewer verdict and produce focused regression coverage.",
                specification_revision="specification-revision-1",
            )
        )

    def test_correct_worker_local_and_dispatcher_boundary_instruction_is_recordable(self) -> None:
        self.assertIsNone(
            validate_rework_instruction(
                "Produce a worker-local broad receipt; the dispatcher's executed exact-SHA gate "
                "receipt is not the worker's job.",
                specification_revision="specification-revision-1",
            )
        )

    def test_negation_applies_only_to_its_own_artifact_clause(self) -> None:
        violation = validate_rework_instruction(
            "Do not attest anything about CI. Obtain an executed exact-SHA gate receipt before reporting.",
            specification_revision="specification-revision-1",
        )

        assert violation is not None
        self.assertEqual(violation.artifact.name, "dispatcher_executed_exact_sha_gate_receipt")
        self.assertEqual(violation.required_action, "obtain")
        self.assertEqual(violation.requested_role, ArtifactOwner.WORKER)

    def test_parser_carries_the_requirement_direction(self) -> None:
        requirements = parse_rework_requirements(
            "Require the worker to attest an executed exact-SHA gate receipt."
        )

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].requested_role, ArtifactOwner.WORKER)
        self.assertEqual(requirements[0].action, "attest")
        dispatcher_requirement = parse_rework_requirements(
            "The dispatcher must obtain an executed exact-SHA gate receipt."
        )
        self.assertEqual(dispatcher_requirement[0].requested_role, ArtifactOwner.DISPATCHER)

    def test_worker_local_receipt_remains_a_valid_rework_requirement(self) -> None:
        self.assertIsNone(
            validate_rework_instruction(
                "Produce a worker-local broad receipt after the focused repair.",
                specification_revision="specification-revision-1",
            )
        )
