from __future__ import annotations

import unittest

from secretary.board.protocol_artifacts import (
    ArtifactOwner,
    ArtifactOwnershipViolation,
    PROTOCOL_ARTIFACTS,
    resolve_protocol_prerequisites,
    validate_rework_prerequisites,
)


class ProtocolArtifactRegistryTests(unittest.TestCase):
    def test_registry_names_the_owner_of_every_cross_role_artifact(self) -> None:
        self.assertEqual(
            {name: artifact.owner for name, artifact in PROTOCOL_ARTIFACTS.items()},
            {
                "worker_local_broad_check_receipt": ArtifactOwner.WORKER,
                "external_dependency": ArtifactOwner.EXTERNAL,
                "dispatcher_executed_exact_sha_gate_receipt": ArtifactOwner.DISPATCHER,
                "independent_reviewer_verdict": ArtifactOwner.REVIEWER,
                "observer_decision": ArtifactOwner.OBSERVER,
            },
        )

    def test_worker_local_and_external_prerequisites_are_representable(self) -> None:
        prerequisites = validate_rework_prerequisites(
            ("worker_local_broad_check_receipt", "external_dependency"),
            specification_revision="specification-revision-1",
        )
        self.assertEqual(
            tuple((artifact.name, artifact.owner) for artifact in prerequisites),
            (
                ("worker_local_broad_check_receipt", ArtifactOwner.WORKER),
                ("external_dependency", ArtifactOwner.EXTERNAL),
            ),
        )

    def test_dispatcher_gate_receipt_is_refused_by_registry_owner(self) -> None:
        with self.assertRaises(ArtifactOwnershipViolation) as raised:
            validate_rework_prerequisites(
                ("dispatcher_executed_exact_sha_gate_receipt",),
                specification_revision="specification-revision-1",
            )

        violation = raised.exception
        self.assertEqual(violation.artifact.owner, ArtifactOwner.DISPATCHER)
        self.assertEqual(violation.requested_role, ArtifactOwner.WORKER)
        self.assertEqual(violation.specification_revision, "specification-revision-1")

    def test_unknown_or_duplicate_name_is_not_a_prerequisite(self) -> None:
        for names in (("not-an-artifact",), ("external_dependency", "external_dependency")):
            with self.subTest(names=names), self.assertRaises(ValueError):
                resolve_protocol_prerequisites(names)

    def test_prose_is_not_an_input_to_prerequisite_resolution(self) -> None:
        # The registry function has no prose input. Punctuation and negation around this sentence
        # therefore cannot create a dispatcher-owned prerequisite.
        prose = "Do not obtain it; nevertheless obtain an executed exact-SHA gate receipt."
        self.assertIn("executed exact-SHA gate receipt", prose)
        self.assertEqual(resolve_protocol_prerequisites(()), ())
