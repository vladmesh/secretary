"""Give Sprint rows their disjoint, indexed board-client transport key."""

from __future__ import annotations

import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = "0006_sprint_transport_key"
down_revision = "0005_sprint_sql"
branch_labels = None
depends_on = None

_SPRINT_KEY_BASE = 2_000_000_000
_SPRINT_NUMBER_KEY_SPAN = 500_000_000
_ASCII_NUMBERED_SPRINT_REF = re.compile(r"^sprint:([0-9]+)$")


def _sprint_transport_key(reference: str) -> int:
    """The immutable key formula used when this historical revision was released."""
    match = _ASCII_NUMBERED_SPRINT_REF.fullmatch(reference)
    if match is not None:
        number = int(match.group(1))
        if reference != f"sprint:{number}":
            raise ValueError(f"non-canonical numbered Sprint reference {reference!r}")
        if number >= _SPRINT_NUMBER_KEY_SPAN:
            raise ValueError(f"numbered Sprint exceeds frozen key range: {number}")
        return _SPRINT_KEY_BASE + number
    digest = hashlib.sha256(f"sprint:{reference}".encode()).digest()
    return (
        _SPRINT_KEY_BASE
        + _SPRINT_NUMBER_KEY_SPAN
        + int.from_bytes(digest[:8], "big") % _SPRINT_NUMBER_KEY_SPAN
    )


def upgrade() -> None:
    op.create_check_constraint(
        "task_number_is_in_card_key_range", "tasks", "task_number < 2000000000"
    )
    op.add_column("sprints", sa.Column("board_key", sa.BigInteger(), nullable=True))
    connection = op.get_bind()
    for reference, in connection.execute(sa.text("SELECT ref FROM sprints")):
        connection.execute(
            sa.text("UPDATE sprints SET board_key=:key WHERE ref=:ref"),
            {"key": _sprint_transport_key(str(reference)), "ref": reference},
        )
    op.alter_column("sprints", "board_key", nullable=False)
    op.create_unique_constraint("uq_sprints_board_key", "sprints", ["board_key"])


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
