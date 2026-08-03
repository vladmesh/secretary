"""Slug/workspace-name helpers for the task pipeline — pure functions, no I/O, no Orca.

A card's slug (validated at create time against SLUG_RE) names its worker/reviewer workspace so
GUI tabs and worktree dirs read `<id>-<slug>` instead of a bare timestamp. A card created before
the slug field existed, or by hand, carries none — fallback_slug derives one from its title
instead, so the pipeline never refuses to claim a card for lack of a slug.

Workspaces already live under the project's own directory in the session manager
(`<workspaces root>/<project>/`), so repeating the project name in the workspace itself would just echo something the path already
says. `card_id` strips the reference (`<project>-<id>`, the board-CLI identity, left untouched)
down to the numeric tail a workspace or tab name is keyed off.
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
