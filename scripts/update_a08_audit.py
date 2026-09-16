from pathlib import Path

path = Path("astra_audit.md")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match, found {count}: {old[:120]!r}")
    text = text.replace(old, new, 1)


progress_anchor = "- ✅ **A07 completed across PR #443 and PR #444:** #443 introduced the canonical product-side `Role(StrEnum)`, typed role subsets, and `Role`-keyed card transitions; #444 made `Actor.role` carry `Role`, normalized TaskWriter role strings at the boundary, removed the duplicate task-side role registries, derived CLI/importer role choices from the canonical vocabulary, and replaced the temporary drift ratchet with boundary tests. Persisted/event/CLI spellings remain string-compatible. The separate `triggered_agents.runtime.role_env` registry remains intentionally owned by the later A19 namespace migration rather than by A07.\n"
replace_once(
    progress_anchor,
    progress_anchor
    + "- ✅ **A08 completed in PR #445:** introduced canonical `TaskType`, `TaskComplexity`, `FamilyPreference`, `RoutingPhase`, `BlockClassification`, and `TaskDecision` `StrEnum`s plus immutable typed `TaskRouting` / `TaskMetadata` values. Task reading, create/report/decision/routing validation, CLI choices, restore, and the board importer now consume the canonical vocabulary while projecting the same strings at Kanboard/PostgreSQL/CLI/document boundaries. The A09 private codec aliases remain only as compatibility surfaces, and `secretary.board.task_routing` is now in the incremental mypy gate.\n",
)

replace_once(
    "PR #434, PR #435, PR #436, PR #437, PR #438, PR #440, PR #441, PR #442, PR #443, and PR #444 all passed their full pull-request CI workflow and were merged into `main`. PR #443 passed typecheck and all seven test shards on exact head SHA `6ddbfb3ea8ee5d49b68468f0f8a205533f69e11c`; PR #444 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `1d92ab9533ec3b2abe66003303c34d82c46ba7eb` before squash-merge.\n",
    "PR #434, PR #435, PR #436, PR #437, PR #438, PR #440, PR #441, PR #442, PR #443, PR #444, and PR #445 all passed their full pull-request CI workflow and were merged into `main`. PR #443 passed typecheck and all seven test shards on exact head SHA `6ddbfb3ea8ee5d49b68468f0f8a205533f69e11c`; PR #444 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `1d92ab9533ec3b2abe66003303c34d82c46ba7eb`; PR #445 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `840fc38266d118d570f357036d2406f81526ae3e` before squash-merge.\n",
)

replace_once(
    "| A08 | Type task routing metadata (`task_type`, complexity, family preference, phases, decisions) | **3** |",
    "| A08 | ✅ Type task routing metadata (`task_type`, complexity, family preference, phases, decisions) — **completed in #445** | **3** |",
)

old_detail = '''### A08. Type task routing metadata — complexity 3

`tasks.py` contains many manually maintained string vocabularies:

- `_TASK_TYPES`;
- `_COMPLEXITIES`;
- `_FAMILY_PREFERENCES`;
- `_ROUTING_PHASES`;
- `_BLOCK_CLASSIFICATIONS`;
- `_DECISIONS` / `_DECISION_TARGETS`;
- editable/active state sets.

The same concepts are normalized in `restore.py`, SQL adapters, the importer, dispatcher routing, and JSON documents. At audit time `_enum_or_default` was duplicated in `tasks.py` and `restore.py`; PR #442 centralized that low-level fallback parser, but the routing vocabularies themselves are still untyped.

**Remaining fix:** define small `StrEnum`s and a typed `TaskRouting` / `TaskMetadata` value. Convert Kanboard metadata strings at the adapter boundary. The shared legacy fallback parser is already centralized by #442; this item now owns the typed domain vocabulary/value migration.
'''
new_detail = '''### A08. Type task routing metadata — complexity 3 — completed

At audit time, `tasks.py` manually maintained `_TASK_TYPES`, `_COMPLEXITIES`, `_FAMILY_PREFERENCES`, `_ROUTING_PHASES`, `_BLOCK_CLASSIFICATIONS`, `_DECISIONS` / `_DECISION_TARGETS`, and editable/active state sets. The same concepts were normalized independently across task reads/writes, restore, the importer, CLI choices, and routing/decision paths. PR #442 had already centralized the low-level legacy fallback parser, but the domain vocabulary itself was still represented as strings.

**Implemented in PR #445:** added `secretary.board.task_routing` as the canonical typed boundary, with `TaskType`, `TaskComplexity`, `FamilyPreference`, `RoutingPhase`, `BlockClassification`, and `TaskDecision` `StrEnum`s, typed `CardState` sets/decision targets, and frozen `TaskRouting` / `TaskMetadata` values. `TaskReader` now converts legacy metadata into those values once; task create/report/decision/routing validation and CLI choices consume the same vocabularies; restore uses the same typed compatibility boundary; and the board importer no longer imports the three private routing registries from the large `tasks.py` façade.

The external contract is intentionally unchanged: persisted Kanboard/PostgreSQL values, CLI spellings, public task documents, SQL CHECK vocabularies, and routing-event strings are the same. Historical `_enum_or_default` / `_enum_or_none` private aliases remain where A09 compatibility tests require them, rather than broadening A08 into an unrelated compatibility cleanup. `secretary.board.task_routing` was added to the incremental mypy gate, focused tests pin the enum/default/document behavior and absence of the duplicate task-side registries, and the full PR CI passed on exact head SHA `840fc38266d118d570f357036d2406f81526ae3e` before squash-merge.
'''
replace_once(old_detail, new_detail)

path.write_text(text, encoding="utf-8")
