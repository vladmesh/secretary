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

    def test_comma_separated_requirement_keeps_its_own_direction_and_object(self) -> None:
        instruction = (
            "The dispatcher must produce the plan, worker must obtain an executed exact-SHA gate receipt."
        )
        requirements = parse_rework_requirements(instruction)
        violation = validate_rework_instruction(instruction, specification_revision="specification-revision-1")

        self.assertEqual(
            [(item.action, item.object_text, item.requested_role, item.artifact) for item in requirements],
            [
                ("produce", "the plan", ArtifactOwner.DISPATCHER, None),
                (
                    "obtain",
                    "an executed exact-SHA gate receipt",
                    ArtifactOwner.WORKER,
                    PROTOCOL_ARTIFACTS["dispatcher_executed_exact_sha_gate_receipt"],
                ),
            ],
        )
        assert violation is not None
        self.assertEqual(violation.required_action, "obtain")
        self.assertEqual(violation.requested_role, ArtifactOwner.WORKER)

    def test_subordinate_reviewer_context_is_not_an_action_object(self) -> None:
        for instruction in (
            "Address each blocker in the reviewer verdict, then produce focused regression coverage.",
            "Produce focused regression coverage for every point in the reviewer verdict.",
            "Fix the flaky test and attest the results in the reviewer verdict thread.",
        ):
            with self.subTest(instruction=instruction):
                requirements = parse_rework_requirements(instruction)
                self.assertTrue(requirements)
                self.assertTrue(all(item.artifact is None for item in requirements))
                self.assertIsNone(
                    validate_rework_instruction(instruction, specification_revision="specification-revision-1")
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
        instruction = "Do not attest anything about CI. Obtain an executed exact-SHA gate receipt before reporting."
        requirements = parse_rework_requirements(instruction)
        violation = validate_rework_instruction(instruction, specification_revision="specification-revision-1")

        assert violation is not None
        self.assertEqual(
            [(item.action, item.object_text, item.requested_role, item.negated, item.artifact) for item in requirements],
            [
                ("attest", "anything", ArtifactOwner.WORKER, True, None),
                (
                    "obtain",
                    "an executed exact-SHA gate receipt",
                    ArtifactOwner.WORKER,
                    False,
                    PROTOCOL_ARTIFACTS["dispatcher_executed_exact_sha_gate_receipt"],
                ),
            ],
        )
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

    def test_direct_forbidden_requirement_is_the_single_refusal_shape(self) -> None:
        instruction = "Require the worker to attest an executed exact-SHA gate receipt."
        requirement = parse_rework_requirements(instruction)[0]
        violation = validate_rework_instruction(instruction, specification_revision="specification-revision-1")

        self.assertEqual(requirement.action, "attest")
        self.assertEqual(requirement.object_text, "an executed exact-SHA gate receipt")
        self.assertEqual(requirement.requested_role, ArtifactOwner.WORKER)
        self.assertIs(requirement.artifact, PROTOCOL_ARTIFACTS["dispatcher_executed_exact_sha_gate_receipt"])
        assert violation is not None
