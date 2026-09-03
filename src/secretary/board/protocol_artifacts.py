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
    """An instruction requirement crossed an artifact's registered owner boundary."""

    artifact: ProtocolArtifact
    required_action: str
    requested_role: ArtifactOwner
    specification_revision: str | None

    @property
    def message(self) -> str:
        revision = self.specification_revision or "unresolved"
        return (
            f"{self.requested_role.value} may not {self.required_action} {self.artifact.name}: "
            f"the artifact is owned by {self.artifact.owner.value} "
            f"at specification revision {revision}"
        )


@dataclass(frozen=True)
class ArtifactRequirement:
    """One action directed at the artifact named in its own instruction clause."""

    artifact: ProtocolArtifact
    action: str
    requested_role: ArtifactOwner
    negated: bool


_ACTION = re.compile(r"\b(produce|obtain|attest)\b", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"(?:[.;\n]+|\b(?:and|but)\b)", re.IGNORECASE)
_ROLE_DIRECTION = re.compile(
    r"\b(?:require|ask|tell|instruct|have)\s+(?:the\s+)?"
    r"(?P<role>worker|dispatcher|reviewer|observer)\s+to\s+"
    r"(?:produce|obtain|attest)\b"
    r"|\b(?:the\s+)?(?P<subject>worker|dispatcher|reviewer|observer)\s+"
    r"(?:must|shall|should|needs?\s+to|will|is\s+to)\s+(?:produce|obtain|attest)\b",
    re.IGNORECASE,
)
_NEGATED_PREFIX = re.compile(r"\b(?:do\s+not|must\s+not|never)\s+$", re.IGNORECASE)


def _mentions(clause: str, artifact: ProtocolArtifact) -> bool:
    normalized = " ".join(clause.casefold().replace("-", " ").split())
    return any(" ".join(name.casefold().replace("-", " ").split()) in normalized for name in artifact.instruction_names)


def _clauses(instruction: str) -> tuple[str, ...]:
    """Split human rework prose at boundaries that cannot share an action target."""
    return tuple(clause.strip() for clause in _CLAUSE_BREAK.split(instruction) if clause.strip())


def _direction(clause: str) -> ArtifactOwner:
    """Read the actor from a requirement's direction, defaulting only an imperative to its worker."""
    match = _ROLE_DIRECTION.search(clause)
    if match is None:
        # Rework text is addressed to the retained worker, so a bare imperative is a parsed
        # implicit worker direction, not an ownership guess at the audit boundary.
        return ArtifactOwner.WORKER
    return ArtifactOwner(match.group("role") or match.group("subject"))


def parse_rework_requirements(instruction: str) -> tuple[ArtifactRequirement, ...]:
    """Parse only clause-local action/artifact pairs from worker rework prose.

    A mention in one clause cannot borrow an action from another. Negation is evaluated relative
    to the action immediately before the matched artifact clause, never over the whole body.
    """
    requirements: list[ArtifactRequirement] = []
    for clause in _clauses(instruction):
        for action in _ACTION.finditer(clause):
            prefix = clause[: action.start()]
            negated = bool(_NEGATED_PREFIX.search(prefix))
            for artifact in PROTOCOL_ARTIFACTS.values():
                if _mentions(clause, artifact):
                    requirements.append(
                        ArtifactRequirement(artifact, action.group(1).lower(), _direction(clause), negated)
                    )
    return tuple(requirements)


def validate_rework_instruction(
    instruction: str, *, specification_revision: str | None
) -> ArtifactOwnershipViolation | None:
    """Refuse a rework requirement that crosses a registered owner boundary.

    This parser binds each action to an artifact in its own clause. The ownership decision is
    always the registry lookup below, so changing prompt wording cannot assign a different owner.
    """
    for requirement in parse_rework_requirements(instruction):
        if not requirement.negated and requirement.requested_role is not requirement.artifact.owner:
            return ArtifactOwnershipViolation(
                requirement.artifact,
                requirement.action,
                requirement.requested_role,
                specification_revision,
            )
    return None
