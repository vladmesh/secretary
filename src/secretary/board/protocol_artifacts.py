"""The protocol artifacts that cross role boundaries and their authoritative owners."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ArtifactOwner(StrEnum):
    WORKER = "worker"
    DISPATCHER = "dispatcher"
    REVIEWER = "reviewer"
    OBSERVER = "observer"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ProtocolArtifact:
    """One durable protocol artifact and its sole authoritative producer."""

    name: str
    owner: ArtifactOwner
    worker_label: str


_ARTIFACTS = (
    ProtocolArtifact(
        "worker_local_broad_check_receipt",
        ArtifactOwner.WORKER,
        "worker-local broad-check receipt",
    ),
    ProtocolArtifact("external_dependency", ArtifactOwner.EXTERNAL, "external dependency"),
    ProtocolArtifact(
        "dispatcher_executed_exact_sha_gate_receipt",
        ArtifactOwner.DISPATCHER,
        "dispatcher-owned exact-SHA gate receipt",
    ),
    ProtocolArtifact("independent_reviewer_verdict", ArtifactOwner.REVIEWER, "independent reviewer verdict"),
    ProtocolArtifact("observer_decision", ArtifactOwner.OBSERVER, "observer decision"),
)

# The immutable registry is the authority. Decision prose is never consulted when resolving a
# protocol prerequisite.
PROTOCOL_ARTIFACTS: Mapping[str, ProtocolArtifact] = MappingProxyType(
    {artifact.name: artifact for artifact in _ARTIFACTS}
)


@dataclass(frozen=True)
class ArtifactOwnershipViolation(Exception):
    """A declared worker prerequisite crossed a registered owner boundary."""

    artifact: ProtocolArtifact
    requested_role: ArtifactOwner
    specification_revision: str | None

    @property
    def message(self) -> str:
        revision = self.specification_revision or "unresolved"
        return (
            f"{self.requested_role.value} may not receive {self.artifact.name} as a protocol "
            f"prerequisite: the artifact is owned by {self.artifact.owner.value} "
            f"at specification revision {revision}"
        )


def resolve_protocol_prerequisites(declared: Iterable[str]) -> tuple[ProtocolArtifact, ...]:
    """Resolve one decision's declared artifact names through the registry.

    This deliberately accepts no prose aliases. The API boundary carries stable registry names,
    so punctuation or wording in the explanatory decision cannot create, remove, or rename a
    prerequisite.
    """
    resolved: list[ProtocolArtifact] = []
    seen: set[str] = set()
    for name in declared:
        if not isinstance(name, str) or not name:
            raise ValueError("protocol prerequisites must be non-empty artifact names")
        if name in seen:
            raise ValueError(f"protocol prerequisite {name!r} is declared more than once")
        artifact = PROTOCOL_ARTIFACTS.get(name)
        if artifact is None:
            raise ValueError(f"unknown protocol prerequisite {name!r}")
        seen.add(name)
        resolved.append(artifact)
    return tuple(resolved)


def validate_rework_prerequisites(
    declared: Iterable[str], *, specification_revision: str | None
) -> tuple[ProtocolArtifact, ...]:
    """Resolve and admit only worker-local or genuinely external prerequisites for rework."""
    prerequisites = resolve_protocol_prerequisites(declared)
    for artifact in prerequisites:
        if artifact.owner not in {ArtifactOwner.WORKER, ArtifactOwner.EXTERNAL}:
            raise ArtifactOwnershipViolation(artifact, ArtifactOwner.WORKER, specification_revision)
    return prerequisites
