"""Slug/workspace-name helpers for the task pipeline — pure functions, no I/O, no Orca.

A card's slug (validated at create time against SLUG_RE) names its worker/reviewer workspace so
GUI tabs and worktree dirs read `<id>-<slug>` instead of a bare timestamp. A card created before
the slug field existed, or by hand, carries none — fallback_slug transliterates its title into one
instead, so the pipeline never refuses to claim a card for lack of a slug.

Workspaces already live under the project's own directory in Orca (`~/orca/workspaces/<project>/`),
so repeating the project name in the workspace itself would just echo something the path already
says. `card_id` strips the reference (`<project>-<id>`, the board-CLI identity, left untouched)
down to the numeric tail the workspace/title functions below key off.

Collision (a re-claim while the previous attempt's workspace is still alive, e.g. left on
Blocked) is resolved by `dedupe`, which takes an `exists` predicate rather than touching disk
itself — the caller (dispatcher.py) supplies worker.workspace_exists, keeping this module free of
host I/O and trivially unit-testable.
"""
from __future__ import annotations

import re

SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")

_CYRILLIC_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def fallback_slug(title: str) -> str:
    """Best-effort slug for a card with no explicit slug: transliterate Cyrillic, keep only
    [a-z0-9-], collapse runs of separators, cap at 30 chars. Never empty."""
    translit = "".join(_CYRILLIC_TRANSLIT.get(ch, ch) for ch in (title or "").lower())
    slug = re.sub(r"[^a-z0-9]+", "-", translit).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:30].strip("-")
    return slug or "task"


def card_slug(card: dict) -> str:
    """The card's explicit slug, or a transliterated fallback from its title — an old/manual card
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


# --- memory prompt block (design-политика memory-mcp, «приоритет общей памяти чтение личная
# память») -----------------------------------------------------------------------------------
# Shared between worker's TASK.md and reviewer's REVIEW.md so the wording/order/caller contract
# stays one source of truth; the steward skill (static markdown, no per-card project) mirrors it
# by hand with scope="project:triggered-agents".


def memory_block(role: str, project: str) -> str:
    """Короткий блок про общую память для роли `role` (worker/reviewer), скоуп из карточки
    (`project`): сначала scope своего проекта, потом без scope; caller обязателен; при
    конфликте с личной памятью верен канон."""
    return (
        "## Память\n\n"
        "Прежде чем разбираться в устройстве системы с нуля — поищи в общей памяти: MCP "
        "`memory`, тул `memory_search(query, k, scope, caller)`. Порядок: сначала "
        f'`scope="project:{project}"`, если пусто — без scope. `caller="{role}"` — передавай '
        "всегда. При конфликте с личной памятью верен канон (общая память приоритетнее)."
    )
