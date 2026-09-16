from pathlib import Path

path = Path("astra_audit.md")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match, got {count}: {old[:120]!r}")
    text = text.replace(old, new, 1)


replace_once(
    "- 🟡 **A11 non-close flow completed across PR #446, PR #447, and PR #448:** #446 added the immutable typed Sprint read boundary; #447 added typed `SprintAdmission` and immutable `SprintReservationIndex` values for admission and the guard index; #448 moved create/restore-create, reopen, generic simple mutations, budget/resume/restore handling, and mutation receipts onto typed write-side values while preserving the released storage/wire/event projections. The only remaining A11 tail is `SprintWriter.close()` and its nested decisions/conflicts/closeout transaction, intentionally left separate because it overlaps the event-payload work in A12.",
    "- ✅ **A11 completed across PR #446, PR #447, PR #448, and PR #449:** #446 added the immutable typed Sprint read boundary; #447 added typed `SprintAdmission` and immutable `SprintReservationIndex` values for admission and the guard index; #448 moved create/restore-create, reopen, generic simple mutations, budget/resume/restore handling, and mutation receipts onto typed write-side values; #449 moved the remaining `SprintWriter.close()` decisions, targets, recoverable conflicts, closeout plan, retry/amendment checks, and result projection onto immutable typed close-domain values. Released storage/event/PostgreSQL/public document shapes and the public dict-returning `parse_close_decisions()` compatibility API remain unchanged.",
)

replace_once(
    "| A11 | 🟡 Migrate legacy sprint dicts/status strings onto the normalized sprint model — **read boundary #446; admission/guard index #447; all non-close write flow #448; only the `SprintWriter.close()` transaction remains** | **3** |",
    "| A11 | ✅ Migrate legacy sprint dicts/status strings onto the normalized sprint model — **completed across #446, #447, #448, and #449** | **3** |",
)

replace_once(
    "### A11. Migrate legacy sprint dictionaries to the normalized sprint model — complexity 3 — partially completed; non-close flow complete",
    "### A11. Migrate legacy sprint dictionaries to the normalized sprint model — complexity 3 — completed",
)

replace_once(
    "**Remaining A11 tail:** only `SprintWriter.close()` and its close-specific transaction graph remain on the legacy nested dictionary flow: decisions, targets, conflicts, closeout state, staged retries, and the PostgreSQL close payload. PR #448 deliberately left that path untouched. Keep it as its own bounded migration because typing those nested payloads overlaps A12; do not mix it with storage migration or Kanboard retirement.",
    "**Implemented in PR #449 (final slice):** added `secretary.board.sprint_close` with immutable typed values for close intent, explicit issue/card decisions, the frozen task target set, recoverable conflicts, the Sprint close snapshot, and the closeout plan. `SprintWriter.close()` now normalizes those closed domain shapes once and keeps typed values through planning, retry/amendment checks, issue/card disposition, closeout writing, and result projection; dictionaries remain only at the released audit/transaction/event/PostgreSQL/public boundaries. The public `parse_close_decisions()` API still returns its historical dict shape, while its validated contents pass through the typed model internally. `secretary.board.sprint_close` is part of the incremental mypy gate, focused close regressions passed before the PR, and the full pull-request CI passed before merge. A11 is now complete; broader event-payload typing remains A12 rather than being folded into this migration.",
)

replace_once(
    "PR #448 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `65236262ab65ff64a43918ce244a54dc9316e167`, then squash-merged as `e964d79f520a1f1584f760439acc9f043d2ad8a9`.",
    "PR #448 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `65236262ab65ff64a43918ce244a54dc9316e167`, then squash-merged as `e964d79f520a1f1584f760439acc9f043d2ad8a9`. PR #449 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `d1fd5ba7b0fc450e5b64ef97d24ffbdea9841a43`, then squash-merged as `6178c4a25d040f97f207a211bddd6beef801723c`.",
)

replace_once(
    "~~A07~~, ~~A08~~, ~~A09~~, ~~A10~~, A11.",
    "~~A07~~, ~~A08~~, ~~A09~~, ~~A10~~, ~~A11~~.\n\nPhase 2 is complete: A07–A11 now have typed domain boundaries while preserving the released compatibility/storage projections. The next cleanup tier starts with the bounded A12 event-payload work rather than reopening Sprint migration.",
)

if "🟡 **A11" in text or "| A11 | 🟡" in text or "**Remaining A11 tail:**" in text:
    raise RuntimeError("stale A11 in-progress wording remains")
if "PR #449" not in text or "6178c4a25d040f97f207a211bddd6beef801723c" not in text:
    raise RuntimeError("PR #449 evidence missing")

path.write_text(text, encoding="utf-8")
