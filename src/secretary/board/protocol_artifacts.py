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
    """One ordered action, object, and direction parsed from a rework instruction."""

    action: str
    object_text: str
    requested_role: ArtifactOwner
    negated: bool
    artifact: ProtocolArtifact | None


_ACTION = re.compile(r"\b(produce|obtain|attest)\b", re.IGNORECASE)
_HARD_BOUNDARY = re.compile(r"[,;.\n]")
_OBJECT_CONTEXT = re.compile(
    r"\b(?:for|in|about|from|before|after|while|when|because|so|that)\b", re.IGNORECASE
)
_ROLE_DIRECTION = re.compile(
    r"\b(?:require|ask|tell|instruct|have)\s+(?:the\s+)?"
    r"(?P<role>worker|dispatcher|reviewer|observer)\s+to\b"
    r"|\b(?:the\s+)?(?P<subject>worker|dispatcher|reviewer|observer)\s+"
    r"(?:must|shall|should|needs?\s+to|will|is\s+to)\b",
    re.IGNORECASE,
)
_NEGATED_PREFIX = re.compile(r"\b(?:do\s+not|must\s+not|never)\s*$", re.IGNORECASE)


def _mentions(clause: str, artifact: ProtocolArtifact) -> bool:
    normalized = " ".join(clause.casefold().replace("-", " ").split())
    return any(" ".join(name.casefold().replace("-", " ").split()) in normalized for name in artifact.instruction_names)


def _direction_before(instruction: str, action_start: int) -> ArtifactOwner:
    """Read this action's closest direction since the preceding comma or sentence boundary."""
    boundary = max(instruction.rfind(mark, 0, action_start) for mark in ",;.\n")
    match = None
    for candidate in _ROLE_DIRECTION.finditer(instruction, boundary + 1, action_start):
        match = candidate
    if match is None:
        # Rework text is addressed to the retained worker, so a bare imperative has an implicit
        # worker direction. This is direction parsing, not an ownership guess.
        return ArtifactOwner.WORKER
    return ArtifactOwner(match.group("role") or match.group("subject"))


def _object_after(instruction: str, action_end: int, next_action: int) -> str:
    """Return the action's direct object, excluding subordinate reference context."""
    end = next_action
    boundary = _HARD_BOUNDARY.search(instruction, action_end, next_action)
    if boundary is not None:
        end = boundary.start()
    context = _OBJECT_CONTEXT.search(instruction, action_end, end)
    if context is not None:
        end = context.start()
    return instruction[action_end:end].strip(" \t,:;.-")


def _negated_before(instruction: str, previous_action_end: int, action_start: int) -> bool:
    """Negation belongs to this unit's own prefix, never to an earlier action."""
    boundary = max(instruction.rfind(mark, 0, action_start) for mark in ",;.\n")
    start = max(boundary + 1, previous_action_end)
    return bool(_NEGATED_PREFIX.search(instruction[start:action_start]))


def parse_rework_requirements(instruction: str) -> tuple[ArtifactRequirement, ...]:
    """Parse rework prose into action/object/direction units in source order.

    An artifact is recognized only in the direct object span of its own action. Commas terminate
    both object and direction context, so a later requirement cannot inherit the prior actor.
    """
    requirements: list[ArtifactRequirement] = []
    actions = tuple(_ACTION.finditer(instruction))
    for index, action in enumerate(actions):
        next_start = actions[index + 1].start() if index + 1 < len(actions) else len(instruction)
        previous_end = actions[index - 1].end() if index else 0
        object_text = _object_after(instruction, action.end(), next_start)
        artifact = next((item for item in PROTOCOL_ARTIFACTS.values() if _mentions(object_text, item)), None)
        requirements.append(
            ArtifactRequirement(
                action.group(1).lower(),
                object_text,
                _direction_before(instruction, action.start()),
                _negated_before(instruction, previous_end, action.start()),
                artifact,
            )
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
        if (
            not requirement.negated
            and requirement.requested_role is ArtifactOwner.WORKER
            and requirement.artifact is not None
            and requirement.artifact.owner is ArtifactOwner.DISPATCHER
        ):
            return ArtifactOwnershipViolation(
                requirement.artifact,
                requirement.action,
                requirement.requested_role,
                specification_revision,
            )
    return None
