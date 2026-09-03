from __future__ import annotations

import unittest

from secretary.board.protocol_artifacts import ArtifactOwner, PROTOCOL_ARTIFACTS, validate_rework_instruction


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
        self.assertEqual(violation.specification_revision, "specification-revision-1")

    def test_worker_local_receipt_remains_a_valid_rework_requirement(self) -> None:
        self.assertIsNone(
            validate_rework_instruction(
                "Produce a worker-local broad receipt after the focused repair.",
                specification_revision="specification-revision-1",
            )
        )
