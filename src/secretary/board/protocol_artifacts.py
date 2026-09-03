"""The protocol artifacts that cross role boundaries and the role that owns each one.

Instruction text can name an artifact, but it cannot assign its owner. Consumers resolve that
name through this registry before deciding whether an instruction is admissible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ArtifactOwner(StrEnum):
    WORKER = "worker"
    DISPATCHER = "dispatcher"
    REVIEWER = "reviewer"
    OBSERVER = "observer"


@dataclass(frozen=True)
class ProtocolArtifact:
    """One durable protocol artifact and its sole authoritative producer."""

    name: str
    owner: ArtifactOwner
    instruction_names: tuple[str, ...]


_ARTIFACTS = (
    ProtocolArtifact(
        "worker_local_broad_check_receipt",
        ArtifactOwner.WORKER,
        ("worker-local broad receipt", "worker-local broad-check receipt"),
    ),
    ProtocolArtifact(
        "dispatcher_executed_exact_sha_gate_receipt",
        ArtifactOwner.DISPATCHER,
        (
            "executed exact-sha gate receipt",
            "executed exact sha gate receipt",
            "dispatcher-owned exact-sha gate receipt",
        ),
    ),
    ProtocolArtifact(
        "independent_reviewer_verdict",
        ArtifactOwner.REVIEWER,
        ("independent reviewer verdict", "reviewer verdict"),
    ),
    ProtocolArtifact("observer_decision", ArtifactOwner.OBSERVER, ("observer decision",)),
)

# The immutable registry is the authority. In particular, no caller assigns ownership based on
# whether an instruction happened to say "dispatcher-owned" or similar prose.
PROTOCOL_ARTIFACTS: Mapping[str, ProtocolArtifact] = MappingProxyType(
    {artifact.name: artifact for artifact in _ARTIFACTS}
)


@dataclass(frozen=True)
class ArtifactOwnershipViolation(Exception):
    """A worker-directed requirement crossed an artifact's registered owner boundary."""

    artifact: ProtocolArtifact
    required_action: str
    specification_revision: str | None

    @property
    def message(self) -> str:
        revision = self.specification_revision or "unresolved"
        return (
            f"worker may not {self.required_action} {self.artifact.name}: "
            f"the artifact is owned by {self.artifact.owner.value} "
            f"at specification revision {revision}"
        )


_WORKER_ACTION = re.compile(r"\b(produce|obtain|attest)\b", re.IGNORECASE)
_NEGATED_ACTION = re.compile(r"\b(?:do\s+not|must\s+not|never)\s+(?:produce|obtain|attest)\b", re.IGNORECASE)


def _mentions(instruction: str, artifact: ProtocolArtifact) -> bool:
    normalized = " ".join(instruction.casefold().replace("-", " ").split())
    return any(" ".join(name.casefold().replace("-", " ").split()) in normalized for name in artifact.instruction_names)


def _worker_directed_action(instruction: str) -> str:
    """Return the action a rework instruction directs at its worker, if any."""
    if _NEGATED_ACTION.search(instruction):
        return ""
    match = _WORKER_ACTION.search(instruction)
    return match.group(1).lower() if match is not None else ""


def validate_rework_instruction(
    instruction: str, *, specification_revision: str | None
) -> ArtifactOwnershipViolation | None:
    """Refuse a rework that directs its worker across a registered owner boundary.

    This parser recognizes artifact names and actions only. The ownership decision is always the
    registry lookup below, so changing prompt wording cannot assign a different owner.
    """
    action = _worker_directed_action(instruction)
    if not action:
        return None
    for artifact in PROTOCOL_ARTIFACTS.values():
        if _mentions(instruction, artifact) and artifact.owner is not ArtifactOwner.WORKER:
            return ArtifactOwnershipViolation(artifact, action, specification_revision)
    return None
