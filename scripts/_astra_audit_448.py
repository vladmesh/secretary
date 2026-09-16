from pathlib import Path

path = Path("astra_audit.md")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match, found {count}: {old[:120]!r}")
    text = text.replace(old, new, 1)


replace_once(
    "- 🟡 **A11 partially completed across PR #446 and PR #447:** #446 added the immutable typed Sprint read boundary; #447 added typed `SprintAdmission` and immutable `SprintReservationIndex` values, moved admission collision checks and the versioned local guard index onto those types, and preserved the existing storage/wire JSON projections. The remaining A11 work is the broader sprint write-path/internal flow that still passes public dict-shaped sprint documents around beyond the now-typed read/admission/index seams.",
    "- 🟡 **A11 non-close flow completed across PR #446, PR #447, and PR #448:** #446 added the immutable typed Sprint read boundary; #447 added typed `SprintAdmission` and immutable `SprintReservationIndex` values for admission and the guard index; #448 moved create/restore-create, reopen, generic simple mutations, budget/resume/restore handling, and mutation receipts onto typed write-side values while preserving the released storage/wire/event projections. The only remaining A11 tail is `SprintWriter.close()` and its nested decisions/conflicts/closeout transaction, intentionally left separate because it overlaps the event-payload work in A12.",
)

replace_once(
    "PR #447 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `0018f63ec5a920094628593c302b99dab2a29fa0`, then squash-merged as `3215ce651402c2a300a09b497e4bffdc4cefb291`.",
    "PR #447 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `0018f63ec5a920094628593c302b99dab2a29fa0`, then squash-merged as `3215ce651402c2a300a09b497e4bffdc4cefb291`. PR #448 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `65236262ab65ff64a43918ce244a54dc9316e167`, then squash-merged as `e964d79f520a1f1584f760439acc9f043d2ad8a9`.",
)

replace_once(
    "| A11 | 🟡 Migrate legacy sprint dicts/status strings onto the normalized sprint model — **typed read boundary landed in #446; typed admission/guard-index slice landed in #447; broader write-path/internal dict flow remains** | **3** |",
    "| A11 | 🟡 Migrate legacy sprint dicts/status strings onto the normalized sprint model — **read boundary #446; admission/guard index #447; all non-close write flow #448; only the `SprintWriter.close()` transaction remains** | **3** |",
)

replace_once(
    "### A11. Migrate legacy sprint dictionaries to the normalized sprint model — complexity 3 — partially completed",
    "### A11. Migrate legacy sprint dictionaries to the normalized sprint model — complexity 3 — partially completed; non-close flow complete",
)

replace_once(
    "**Remaining A11 tail:** move the broader sprint write-path/internal flow off `dict[str, Any]` where closed sprint documents still travel between writer/read helpers. Writer inputs/results and internal sprint documents should become typed values before rendering back to the established public/storage dictionaries. The guard-index and admission-candidate subproblem is now complete; keep the remaining work operation-by-operation and do not combine it with a storage migration or Kanboard retirement.",
    "**Implemented in PR #448 (third slice, all non-close write flow):** added immutable `SprintCreateIntent`, `SprintReopenIntent`, `SprintWriteSnapshot`, and `SprintMutationReceipt` values in `secretary.board.sprint_write`. Create/restore-create now keep typed intent through validation/admission and render the historical dict only at transaction/event boundaries; reopen uses typed replay identity and sprint/admission values; the generic simple-mutation path, budget hard-stop bookkeeping, resume, restore, current-task handling, audit/result projection, and reservation-index update consume typed Sprint values instead of carrying the public sprint dict through business logic. `secretary.board.sprint_write` is in the incremental mypy gate and focused tests pin the old document projections. PostgreSQL/Kanboard storage, transaction JSON, event vocabularies, CLI/web documents, and public Sprint result shapes are unchanged. Full PR CI passed on exact head SHA `65236262ab65ff64a43918ce244a54dc9316e167`; the PR squash-merged as `e964d79f520a1f1584f760439acc9f043d2ad8a9`.\n\n**Remaining A11 tail:** only `SprintWriter.close()` and its close-specific transaction graph remain on the legacy nested dictionary flow: decisions, targets, conflicts, closeout state, staged retries, and the PostgreSQL close payload. PR #448 deliberately left that path untouched. Keep it as its own bounded migration because typing those nested payloads overlaps A12; do not mix it with storage migration or Kanboard retirement.",
)

path.write_text(text, encoding="utf-8")
