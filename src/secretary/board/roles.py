"""Canonical product-side board role vocabulary.

The product protocol owns these values. Runtime launch roles live in ``secretary.runtime``,
which does not import the board; importing board code into it would invert the dependency
direction guarded by the architecture tests. The background agents' CLI, built on top of
``secretary``, receives its board ports from its own composition root.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """A role that may act on the normalized product board protocol."""

    PO = "po"
    DISPATCHER = "dispatcher"
    WORKER = "worker"
    REVIEWER = "reviewer"
    OBSERVER = "observer"
    STEWARD = "steward"
    RETRO = "retro"


BOARD_ROLES: frozenset[Role] = frozenset(Role)
COMMENT_ROLES: frozenset[Role] = BOARD_ROLES
CREATE_ROLES: frozenset[Role] = frozenset(
    {
        Role.PO,
        Role.STEWARD,
        Role.WORKER,
        Role.REVIEWER,
        Role.RETRO,
        Role.OBSERVER,
    }
)
# The steward is here too; its report card is the one create outside Issues it may make,
# and `TaskWriter` names that exception explicitly.
PROPOSAL_CREATE_ROLES: frozenset[Role] = frozenset(
    {
        Role.WORKER,
        Role.REVIEWER,
        Role.RETRO,
        Role.STEWARD,
    }
)
EDIT_ROLES: frozenset[Role] = frozenset(
    {
        Role.PO,
        Role.DISPATCHER,
        Role.OBSERVER,
    }
)
