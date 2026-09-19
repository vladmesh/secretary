"""Canonical product-side board role vocabulary.

The product protocol owns these values. Legacy runtime launch roles still live under
``triggered_agents`` until that namespace is migrated; importing product code back into
that legacy package would invert the dependency direction guarded by the architecture
tests.
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
PROPOSAL_CREATE_ROLES: frozenset[Role] = frozenset(
    {
        Role.WORKER,
        Role.REVIEWER,
        Role.RETRO,
    }
)
EDIT_ROLES: frozenset[Role] = frozenset(
    {
        Role.PO,
        Role.DISPATCHER,
        Role.OBSERVER,
    }
)
