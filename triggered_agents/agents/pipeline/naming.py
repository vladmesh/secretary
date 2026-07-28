"""Slug/workspace-name helpers for the task pipeline — pure functions, no I/O, no Orca.

A card's slug (validated at create time against SLUG_RE) names its worker/reviewer workspace so
GUI tabs and worktree dirs read `<id>-<slug>` instead of a bare timestamp. A card created before
the slug field existed, or by hand, carries none — fallback_slug derives one from its title
instead, so the pipeline never refuses to claim a card for lack of a slug.

Workspaces already live under the project's own directory in the session manager
(`<workspaces root>/<project>/`), so repeating the project name in the workspace itself would just echo something the path already
says. `card_id` strips the reference (`<project>-<id>`, the board-CLI identity, left untouched)
down to the numeric tail the workspace/title functions below key off.

Collision (a re-claim while the previous attempt's workspace is still alive, e.g. left on
Blocked) is resolved by `dedupe`, which takes an `exists` predicate rather than touching disk
itself — the caller (dispatcher.py) supplies worker.workspace_exists, keeping this module free of
host I/O and trivially unit-testable.
"""
from __future__ import annotations

import re
import unicodedata

SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")


def fallback_slug(title: str) -> str:
    """Best-effort slug for a card with no explicit slug: fold accents to their base letters, keep
    only [a-z0-9-], collapse runs of separators, cap at 30 chars. A title written entirely in a
    script that does not fold to ASCII yields the neutral `task` rather than an empty name, so the
    pipeline still has a workspace to create."""
    decomposed = unicodedata.normalize("NFKD", (title or "").lower())
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:30].strip("-")
    return slug or "task"


def card_slug(card: dict) -> str:
    """The card's explicit slug, or a fallback derived from its title — an old or manual card
    created before the slug field existed still claims fine."""
    slug = (card.get("slug") or "").strip()
    return slug if slug else fallback_slug(card.get("title") or card["reference"])


def card_id(reference: str) -> str:
    """The numeric tail of a `<project>-<id>` reference, e.g. `"218"` from
    `"triggered-agents-218"`. The reference itself is left untouched everywhere else (board-CLI,
    comments, claim metadata) — this is only for naming workspaces/tabs."""
    return reference.rsplit("-", 1)[-1]


def worker_workspace_base(card_id: str, slug: str) -> str:
    return f"{card_id}-{slug}"


def reviewer_workspace_base(card_id: str, slug: str) -> str:
    return f"review-{card_id}-{slug}"


def dedupe(base: str, exists) -> str:
    """`base`, or `base-2`/`base-3`/... — the first suffix for which `exists(candidate)` is
    False. `exists` is a predicate (str) -> bool, not a filesystem touch here."""
    if not exists(base):
        return base
    i = 2
    while exists(f"{base}-{i}"):
        i += 1
    return f"{base}-{i}"


def worker_title(card_id: str, card_title: str) -> str:
    return f"worker {card_id}: {card_title}"


def reviewer_title(card_id: str, card_title: str) -> str:
    return f"review {card_id}: {card_title}"


# --- git ref names (git hygiene: one ref per actor, see design-task-pipeline.md) ---------------
# Single source of truth for every actor's branch name, keyed off the card `reference` (not the
# workspace id above) so worker.py and dispatcher.py never hardcode the `pipeline/`/`review/`
# prefix in more than one place.


def worker_branch(reference: str) -> str:
    return f"pipeline/{reference}"


def reviewer_branch(reference: str) -> str:
    return f"review/{reference}"


def stand_branch(project: str) -> str:
    return f"stand/{project}"


# --- memory prompt block ---------------------------------------------------------------------
# Shared between worker's TASK.md and reviewer's REVIEW.md so the wording/order/caller contract
# stays one source of truth; the steward skill (static markdown, no per-card project) mirrors it
# by hand with the product's own project scope.


def memory_block(role: str, project: str) -> str:
    """A short block about shared memory for `role` (worker/reviewer), scoped by the card's
    `project`: the project's own scope first, then no scope; caller is mandatory; the canon wins
    over personal memory on a conflict."""
    return (
        "## Memory\n\n"
        "Before working out how the system is built from scratch, search shared memory: the "
        "`memory` MCP server, tool `memory_search(query, k, scope, caller)`. Order: "
        f'`scope="project:{project}"` first, and without a scope if that is empty. Always pass '
        f'`caller="{role}"`. When shared memory conflicts with personal memory, the canon wins.'
    )
