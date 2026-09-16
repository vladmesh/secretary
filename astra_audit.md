# Astra audit

Audit date: 2026-09-15  
Baseline: `main` at `419bce6decdee15dd54d7d3e7f13d662763dbe56`  
Progress updated: 2026-09-16

## Scope

This audit focuses on the areas requested by the owner:

- old/legacy code that can be deleted or isolated;
- violations of the repository's own architecture direction and common Python best practices;
- duplicated vocabularies / normalization logic;
- weak typing, especially `dict[str, Any]` used as domain objects and string literals used as closed vocabularies;
- incremental fixes that can be made without turning the cleanup into a rewrite.

I treated `dict` as a problem only when the shape is a **closed domain contract**. Raw JSON/YAML at an adapter, persistence, or schema boundary is fine; the problem is letting those untyped bags flow deep into business logic.

### Complexity scale

| Score | Meaning |
|---|---|
| **1** | Repository-local, mechanically safe cleanup; minutes, tiny blast radius. |
| **2** | Small refactor with obvious call sites and focused tests. |
| **3** | Multi-module migration, but architecture stays the same and can be done incrementally. |
| **4** | Cross-cutting domain refactor / persistence compatibility work; requires careful sequencing and broad tests. |
| **5** | Architecture migration or removal of a live compatibility subsystem; substantial staged work. |

## Executive summary

The project is already moving in the right direction: `secretary.board.models`, `secretary.board.host`, the vitality code, and the runtime `HeadRun` are examples of the target style — typed immutable values, `StrEnum`, explicit protocol boundaries, and adapters that translate raw backend data before it crosses into domain code.

The main problem is that the migration is only partly complete. The repository currently contains **two architectural generations at once**:

1. a newer typed, feature-oriented layer (`board`, `dispatch`, `webproto`, PostgreSQL store, `HeadRuntime`); and
2. a large compatibility layer (`triggered_agents`, flat `secretary/*.py` modules, Kanboard-shaped readers/writers, Orca-legacy runtime, dict-shaped dispatcher persistence).

Most technical debt is caused by data crossing between those generations through dictionaries, string vocabularies, compatibility re-exports, and private helper imports. The best cleanup strategy is therefore **not** a rewrite. Continue moving one bounded domain at a time to the typed layer and delete the compatibility seam immediately after the last caller is migrated.

## Progress

- ✅ **A01 completed in PR #434:** removed the dead `secretary._env` compatibility shim, its compatibility-only architecture assertion, and `_env.py` from `LEGACY_FLAT_MODULES`.
- ✅ **A02 completed in PR #434:** aligned the declared/tooling Python contract with the runtime by raising `requires-python` from `>=3.11` to `>=3.12` and Ruff's target from `py311` to `py312`. We intentionally did **not** add a redundant 3.11 CI run; the product now explicitly supports the Python version its CI and runtime already use.
- ✅ **A03 completed in PR #435:** added pinned `mypy==1.18.2`, a dedicated `typecheck` dependency/CI job, and an intentionally narrow first gate over already-typed leaves (`secretary.board.models`, `secretary.board.host`, `secretary.dispatch.runtime_provenance`, and `secretary.po.models`). Imported legacy modules remain outside the enforced error surface so the gate can expand incrementally instead of turning into a repository-wide migration.
- ✅ **A04 completed in PR #436:** introduced typed `BoardBackend(StrEnum)` and `BoardCapability(StrEnum)` vocabularies, kept the old public constants as string-compatible enum-member aliases, typed the backend parser/cache and capability set, and added `secretary.board.backend` to the incremental mypy gate without changing the environment/storage/serialized string contract.
- ✅ **A05 completed in PR #438:** introduced canonical `PauseMode(StrEnum)` plus typed `PauseState`, `AutoResumeStatus`, and `LegacyPauseMirror` document contracts around the dispatcher pause state. The `soft`/`hard` boundary aliases, persisted JSON keys/values, corrupt-file freeze behavior, and auto-resume semantics remain unchanged; `secretary.dispatcher_pause` is now part of the incremental mypy gate.
- ✅ **A06 completed in PR #437:** added canonical `IssueKind`, `IssuePriority`, and `IssueCloseReason` `StrEnum`s to the normalized board model. `Issue` now stores typed optional vocabulary values while accepting the existing string spellings at construction boundaries; persisted/CLI/Kanboard/PostgreSQL values remain unchanged. The staged Kanboard `pending/pending` recovery shape normalizes to absent typed metadata instead of expanding the durable vocabulary.
- ✅ **A07 completed across PR #443 and PR #444:** #443 introduced the canonical product-side `Role(StrEnum)`, typed role subsets, and `Role`-keyed card transitions; #444 made `Actor.role` carry `Role`, normalized TaskWriter role strings at the boundary, removed the duplicate task-side role registries, derived CLI/importer role choices from the canonical vocabulary, and replaced the temporary drift ratchet with boundary tests. Persisted/event/CLI spellings remain string-compatible. The separate `triggered_agents.runtime.role_env` registry remains intentionally owned by the later A19 namespace migration rather than by A07.
- ✅ **A08 completed in PR #445:** introduced canonical `TaskType`, `TaskComplexity`, `FamilyPreference`, `RoutingPhase`, `BlockClassification`, and `TaskDecision` `StrEnum`s plus immutable typed `TaskRouting` / `TaskMetadata` values. Task reading, create/report/decision/routing validation, CLI choices, restore, and the board importer now consume the canonical vocabulary while projecting the same strings at Kanboard/PostgreSQL/CLI/document boundaries. The A09 private codec aliases remain only as compatibility surfaces, and `secretary.board.task_routing` is now in the incremental mypy gate.
- ✅ **A09 completed across PR #442 and PR #446:** #442 centralized the task-side Kanboard wire-format constants and pure normalizers in `secretary.board.legacy_codec`; #446 added the typed sprint-read compatibility boundary and moved `board/import_board.py` off the remaining private sprint helpers. Historical private aliases remain only as same-module/backward-compatibility adapters, not cross-feature dependencies.
- ✅ **A10 completed in PR #441:** renamed the routing-journal domain value to canonical `RoutingHeadSnapshot`, kept the runtime lifecycle value as `HeadRun`, and added a typed launch boundary without changing routing-event JSON or persisted dispatcher state. Historical routing names and the dict adapter remain compatibility surfaces until the adjacent package/state migrations remove them.
- 🟡 **A11 partially completed across PR #446 and PR #447:** #446 added the immutable typed Sprint read boundary; #447 added typed `SprintAdmission` and immutable `SprintReservationIndex` values, moved admission collision checks and the versioned local guard index onto those types, and preserved the existing storage/wire JSON projections. The remaining A11 work is the broader sprint write-path/internal flow that still passes public dict-shaped sprint documents around beyond the now-typed read/admission/index seams.
- ✅ **A16 completed in PR #440:** made `SECRETARY_CARD_BACKEND` mandatory for live backend selection. Missing, empty, and unknown selectors now fail closed; explicit `kanboard` remains the rollback path, the test suite pins its backend explicitly, and status represents the absence of a product default as `null`.

PR #434, PR #435, PR #436, PR #437, PR #438, PR #440, PR #441, PR #442, PR #443, PR #444, PR #445, PR #446, and PR #447 all passed their full pull-request CI workflow and were merged into `main`. PR #443 passed typecheck and all seven test shards on exact head SHA `6ddbfb3ea8ee5d49b68468f0f8a205533f69e11c`; PR #444 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `1d92ab9533ec3b2abe66003303c34d82c46ba7eb`; PR #445 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `840fc38266d118d570f357036d2406f81526ae3e` before squash-merge. PR #446 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `5b26fbd8768963153f44cc42d6a70760b3d16f0b` before squash-merge. PR #447 passed typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate on head SHA `0018f63ec5a920094628593c302b99dab2a29fa0`, then squash-merged as `3215ce651402c2a300a09b497e4bffdc4cefb291`.

## Findings

| ID | Finding | Complexity |
|---|---|---:|
| A01 | ✅ Remove the dead private `_env.py` compatibility shim — **completed in #434** | **1** |
| A02 | ✅ Align declared Python support with the actual 3.12 runtime/CI — **completed in #434** | **1** |
| A03 | ✅ Add a real static type checker, initially on typed packages only — **completed in #435** | **2** |
| A04 | ✅ Replace board-backend string literals with `StrEnum` — **completed in #436** | **2** |
| A05 | ✅ Type pause mode and pause-state documents — **completed in #438** | **2** |
| A06 | ✅ Type Product/Issue kind, priority and close-reason vocabularies — **completed in #437** | **2** |
| A07 | ✅ Centralize the product role vocabulary in one `Role(StrEnum)` — **completed across #443 and #444; legacy runtime registry belongs to A19** | **3** |
| A08 | ✅ Type task routing metadata (`task_type`, complexity, family preference, phases, decisions) — **completed in #445** | **3** |
| A09 | ✅ Stop importing private task/sprint normalizers across feature boundaries — **completed across #442 and #446** | **3** |
| A10 | ✅ Rename/consolidate the two different `HeadRun` concepts — **completed in #441** | **3** |
| A11 | 🟡 Migrate legacy sprint dicts/status strings onto the normalized sprint model — **typed read boundary landed in #446; typed admission/guard-index slice landed in #447; broader write-path/internal dict flow remains** | **3** |
| A12 | Replace closed event payload dictionaries with typed payload objects | **4** |
| A13 | Break up `DispatcherRecord`'s nested `dict[str, Any]` state | **4** |
| A14 | Remove the giant `dispatcher.py` compatibility façade | **4** |
| A15 | Continue the flat-root-to-feature-package migration and split god modules | **4** |
| A16 | ✅ Make the active board backend explicit instead of silently defaulting to Kanboard — **completed in #440** | **2** |
| A17 | Retire Kanboard from the live write path after the PostgreSQL cutover window | **5** |
| A18 | Move/remove one-shot Kanboard import/cutover machinery after cutover | **3** |
| A19 | Finish migration out of the legacy `triggered_agents` namespace | **5** |
| A20 | Retire the Orca-legacy head backend after `local-pty` reaches parity | **5** |

---

## Detailed findings

### A01. Remove the dead private `_env.py` compatibility shim — complexity 1 — completed

At audit time, `src/secretary/_env.py` was only:

```python
from secretary.infra.env import positive_int
__all__ = ["positive_int"]
```

`tests/test_architecture.py` explicitly kept the old import alive and asserted that it was the same implementation. I found no production caller that needed the private `_env` path.

**Implemented in PR #434:** removed `secretary/_env.py`, removed it from `LEGACY_FLAT_MODULES`, and deleted the compatibility-only architecture assertion/imports. Full CI passed before merge.

This compatibility seam is now gone rather than being preserved indefinitely.

### A02. Align declared Python support with the actual runtime — complexity 1 — completed

At audit time, `pyproject.toml` declared `requires-python = ">=3.11"`, while GitHub Actions ran only Python 3.12. That left the package claiming a compatibility level the project did not actually test.

The original audit offered two valid fixes: add 3.11 coverage to CI, or stop declaring 3.11 support if it is intentionally unsupported. For this application, the second option is simpler and avoids paying a permanent second-version CI/compatibility cost for a Python version the controlled runtime does not need.

**Implemented in PR #434:** raised `requires-python` to `>=3.12` and Ruff's `target-version` to `py312`. CI remains on Python 3.12, so package metadata, lint semantics, CI, and the intended runtime now agree.

### A03. Add a real static type checker — complexity 2 — completed

At audit time, the repository had extensive annotations but no mypy/pyright gate. Ruff catches syntax/style classes of problems, not type mismatches between the many dict-shaped protocols.

The intended approach was incremental rather than strict checking over the whole repository at once.

**Implemented in PR #435:** added pinned `mypy==1.18.2` as a dedicated `typecheck` optional dependency, configured it for Python 3.12, and added a separate GitHub Actions `typecheck` job. The initial enforced set is deliberately small and already typed: `secretary.board.models`, `secretary.board.host`, `secretary.dispatch.runtime_provenance`, and `secretary.po.models`. Imported legacy modules are followed for type information but their pre-existing errors are not made part of this first gate.

The new typecheck passed on the first CI run, the ordinary test shards and aggregate gate also passed, and PR #435 was merged into `main`. The checked set has since grown with A04 and A05; it should continue expanding package by package as A07–A13 remove legacy string/dict surfaces.

### A04. Replace board-backend string literals with `StrEnum` — complexity 2 — completed

At audit time, `secretary.board.backend` used the closed vocabulary `"kanboard" | "postgres"` as strings (`KANBOARD`, `POSTGRES`, `CARD_BACKENDS`) and also used string capability names such as `"card"`, `"sprint"`, `"product/issue"`.

This was a textbook closed vocabulary and a good candidate for a small typed refactor.

**Implemented in PR #436:** added `BoardBackend(StrEnum)` for `kanboard` / `postgres` and `BoardCapability(StrEnum)` for `card` / `sprint` / `product/issue`. The existing `KANBOARD`, `POSTGRES`, `CARD`, `SPRINT`, and `PRODUCT_ISSUE` exports remain as string-compatible enum-member aliases, so existing callers did not need a broad import migration. `parse_card_backend()` / `card_backend()` and the process-wide cache now carry `BoardBackend`; `POSTGRES_SERVES` and `board_client(..., serves=...)` carry the capability enum. The same PR added `secretary.board.backend` to the incremental mypy gate and tightened a few overly broad annotations exposed by that gate.

The environment values, backend selection behavior, identity strings, storage shape, CLI spellings, and serialized values are unchanged. The final CI run passed typecheck, all seven test shards, and the aggregate gate before merge.

### A05. Type pause mode and pause-state documents — complexity 2 — completed

At audit time, `dispatcher_pause.py` had a small, stable domain model represented by dictionaries and strings:

- `PAUSE_MODES = ("drain", "freeze")`;
- alias maps for `soft/hard`;
- `ProductionPause.load() -> dict[str, Any]`;
- `auto_resume_status() -> dict[str, Any]`;
- `pause_payload() -> dict[str, Any]`;
- legacy mirror receipts as dicts.

**Implemented in PR #438:** added `PauseMode(StrEnum)` for the durable `drain` / `freeze` vocabulary and `TypedDict` contracts for `PauseState`, `AutoResumeStatus`, and `LegacyPauseMirror`. `ProductionPause` remains the JSON adapter boundary; it exposes the typed state internally while persisting the same JSON object shape. The existing `soft` / `hard` aliases still normalize at the input boundary, and the legacy mirror keeps its old spelling.

The PR deliberately did not turn loading into a new strict runtime validator: corrupt-file handling and the existing semantic checks stay where they were, so unreadable pause files still fail closed as a freeze and auto-resume behavior is unchanged. `secretary.dispatcher_pause` was also added to the incremental mypy gate. Full CI passed before merge, including typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate.

### A06. Type Product/Issue vocabularies — complexity 2 — completed

At audit time, `product_issues.py` defined:

- `ISSUE_KINDS = {"bug", "feature", "question", "improvement"}`;
- `ISSUE_PRIORITIES = {"P0", "P1", "P2", "P3"}`;
- `ISSUE_CLOSE_REASONS = {"resolved", "invalid", "duplicate", "wont_do"}`.

Meanwhile `board.models.Issue` was already a typed normalized value, but `priority`, `issue_kind`, and `close_reason` were plain strings.

**Implemented in PR #437:** added `IssueKind`, `IssuePriority`, and `IssueCloseReason` `StrEnum`s in `secretary.board.models` and exported them from `secretary.board`. The normalized `Issue` now stores those types (or `None` when metadata is absent) while its construction boundary accepts the existing string spellings and converts them immediately. Unknown vocabulary values are rejected rather than flowing deeper into the normalized domain model.

The existing string wire/storage contract is unchanged: `StrEnum` remains string-compatible, so CLI inputs, Kanboard metadata and PostgreSQL values keep the same spellings. The special staged Kanboard create state that historically materialized `priority="pending"` and `issue_kind="pending"` is accepted only as a paired recovery sentinel and normalizes to absent typed metadata; `pending` is not added to either durable enum. Focused tests cover string compatibility, absent metadata, staged recovery and invalid values.

The legacy validation sets in `product_issues.py` remain as compatibility/boundary validation surfaces for now; the normalized board domain contract is the enum-typed `Issue`. Full CI passed before merge.

### A07. Centralize roles in one `Role(StrEnum)` — complexity 3 — completed

The same role vocabulary is repeated in multiple places:

- `tasks.py` (`_ROLES`, `_CREATE_ROLES`, `_EDIT_ROLES`, etc.);
- `task_commands.py` argparse choices;
- `triggered_agents.runtime.role_env.BOARD_ROLES`;
- `board.card_transitions.CARD_TRANSITIONS` keys;
- `board.models.Actor.role: str`;
- dispatcher/runtime stop and routing paths.

The duplicated sets are already drifting into multiple partially-overlapping subsets.

**Implemented in PR #443 (product-side slice):** added canonical `secretary.board.roles.Role(StrEnum)` for the product board roles plus typed `BOARD_ROLES`, `CREATE_ROLES`, `PROPOSAL_CREATE_ROLES`, and `EDIT_ROLES` subsets. `CARD_TRANSITIONS` now uses `Role` keys, while `card_transition()` still accepts the existing string spellings and normalizes them at the boundary, so transition authority and external spellings are unchanged. `Role` is exported from `secretary.board`; `secretary.board.roles` and `secretary.board.card_transitions` are now in the incremental mypy gate. A focused unit ratchet asserts that the historical `tasks.py` role sets exactly match the new canonical product sets, so the compatibility surface cannot silently drift while the remaining migration is staged.

**Completed in PR #444 (boundary/removal slice):** `board.models.Actor.role` now carries `Role` while accepting the released string spellings at construction boundaries. `TaskWriter` normalizes raw role strings once and uses the canonical board/create/comment/edit/proposal subsets; the duplicated task-local `_ROLES`, `_COMMENT_ROLES`, `_CREATE_ROLES`, `_PROPOSAL_CREATE_ROLES`, and `_EDIT_ROLES` registries are gone. `task_commands.py` derives argparse role choices from the canonical enum/subsets, and the board importer derives its marker-role strings from the same vocabulary instead of importing a private task constant. The temporary compatibility ratchet from #443 was replaced with tests for Actor normalization, writer authorization, CLI projection, and the absence of the duplicate registries.

`triggered_agents.runtime.role_env.BOARD_ROLES` deliberately remains separate: `triggered_agents` is a legacy namespace with a dependency-direction test preventing new imports back into `secretary`. Moving that runtime-owned registry is part of A19, when role/runtime ownership itself leaves the legacy namespace; it is no longer an A07 tail.

### A08. Type task routing metadata — complexity 3 — completed

At audit time, `tasks.py` manually maintained `_TASK_TYPES`, `_COMPLEXITIES`, `_FAMILY_PREFERENCES`, `_ROUTING_PHASES`, `_BLOCK_CLASSIFICATIONS`, `_DECISIONS` / `_DECISION_TARGETS`, and editable/active state sets. The same concepts were normalized independently across task reads/writes, restore, the importer, CLI choices, and routing/decision paths. PR #442 had already centralized the low-level legacy fallback parser, but the domain vocabulary itself was still represented as strings.

**Implemented in PR #445:** added `secretary.board.task_routing` as the canonical typed boundary, with `TaskType`, `TaskComplexity`, `FamilyPreference`, `RoutingPhase`, `BlockClassification`, and `TaskDecision` `StrEnum`s, typed `CardState` sets/decision targets, and frozen `TaskRouting` / `TaskMetadata` values. `TaskReader` now converts legacy metadata into those values once; task create/report/decision/routing validation and CLI choices consume the same vocabularies; restore uses the same typed compatibility boundary; and the board importer no longer imports the three private routing registries from the large `tasks.py` façade.

The external contract is intentionally unchanged: persisted Kanboard/PostgreSQL values, CLI spellings, public task documents, SQL CHECK vocabularies, and routing-event strings are the same. Historical `_enum_or_default` / `_enum_or_none` private aliases remain where A09 compatibility tests require them, rather than broadening A08 into an unrelated compatibility cleanup. `secretary.board.task_routing` was added to the incremental mypy gate, focused tests pin the enum/default/document behavior and absence of the duplicate task-side registries, and the full PR CI passed on exact head SHA `840fc38266d118d570f357036d2406f81526ae3e` before squash-merge.

### A09. Stop cross-package imports of private normalizers — complexity 3 — completed

At audit time, `board/import_board.py` imported private implementation details from both `secretary.tasks` and `secretary.sprints`, including task-side `_STATE_BY_COLUMN`, `_KNOWN_METADATA`, `_enum_or_default`, `_positive_int`, `_split_heads` and sprint-side `_budget`, `_resume`, `_source_audit`, and `_json_list`.

That avoided literal duplication, but made migration semantics depend on private reader internals.

**Implemented in PR #442 (task-side slice):** added the explicit compatibility module `secretary.board.legacy_codec` for the task column/metadata vocabularies and pure legacy task parsers. `tasks.py`, `restore.py`, and `board/import_board.py` now consume the same canonical functions; the historical private names in `tasks.py` remain compatibility aliases, so callers were not forced through an unrelated broad migration. The duplicate restore `_enum_or_default` implementation was removed. Focused tests pin the released normalization behavior and prove reader/restore/importer bind to the same codec. Full CI passed before merge: typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate.

**Completed in PR #446 (sprint-side tail):** added `secretary.board.sprint_read` as the explicit typed compatibility boundary for Sprint state, budget, resume, source-audit, and legacy JSON-list metadata. `board/import_board.py` now consumes that boundary instead of importing `_budget`, `_resume`, `_source_audit`, or `_json_list` from `secretary.sprints`. The old private helpers remain thin compatibility adapters for historical same-package/test callers, so the released wire semantics are unchanged while the cross-feature private dependency is gone.

A09 is therefore complete. After Kanboard retirement, the remaining compatibility codecs/adapters can be removed together with the importer/cutover path under A18 rather than being treated as unfinished A09 work.

### A10. Rename/consolidate the two `HeadRun` concepts — complexity 3 — completed

At audit time, there were two unrelated classes named `HeadRun`:

- `triggered_agents.runtime.head.run.HeadRun`: the mutable-in-time lifecycle identity of a real launched head (`run_id`, `spec`, task ref, lifecycle, stop initiator, fanout policy);
- `secretary.routing_journal.HeadRun`: an immutable routing/telemetry snapshot (`role`, selected profile, model, effort, resource, provider session, prompt identity).

The code itself explained that these were deliberately separate, but using the same class name made imports, reviews, and the lifecycle-to-routing boundary unnecessarily ambiguous.

**Implemented in PR #441:** the routing-journal class is now canonically `RoutingHeadSnapshot`; the runtime lifecycle value remains `triggered_agents.runtime.head.HeadRun`. Routing-journal internals (`AttemptRecord`, payload typing/parsing, and run keys) use the snapshot type, `routing_head_snapshot_from_profile()` is the canonical resolved-routing constructor, and `routing_head_snapshot_from_launch(..., lifecycle_run: HeadRun)` is the typed boundary joining resolved routing to a real lifecycle run.

For compatibility, the historical `routing_journal.HeadRun` and `head_run_from_profile` spellings remain aliases, and the historical dict-returning `launched_head_run_snapshot()` remains a wrapper around the same enrichment logic. This deliberately keeps A10 separate from A13: persisted `DispatcherRecord.worker_head_run` / `review_head_run` dictionaries, routing-event JSON keys and values, and run-key semantics are unchanged.

Full CI passed before merge: typecheck, all seven test shards, and the aggregate exact-SHA evidence/coverage gate.

### A11. Migrate legacy sprint dictionaries to the normalized sprint model — complexity 3 — partially completed

`board.models` already has `Sprint` and `SprintState`, yet `sprints.py` still works mainly with `dict[str, Any]` and string sets such as `SPRINT_STATUSES = {"open", "closed", "stopped"}`. Guard indexes, budget documents, resumes, and readers pass ad-hoc dict shapes around.

**Implemented in PR #446 (first slice):** introduced `secretary.board.sprint_read` with immutable `SprintBudget`, `SprintResume`, `SprintSourceAudit`, and `SprintReadMetadata` values plus canonical `SprintState` parsing. `SprintReader` now converts the compound legacy metadata once through that boundary and projects the existing public dict shape afterwards. The string status set is derived from `SprintState`, and the same typed boundary is reused by the one-shot importer. Persisted metadata, SQL/Kanboard storage, CLI/web/checkpoint documents, and write semantics are unchanged.

**Implemented in PR #447 (second slice):** added immutable `SprintAdmission` and `SprintReservationIndex` domain values. Admission candidates/open-sprint collision checks now cross a typed boundary instead of passing raw sprint dictionaries into the conflict helpers; the local version-2 guard index is parsed, updated, and rendered through the typed index while keeping its on-disk JSON version and shape unchanged. Existing public/storage sprint dictionaries remain compatibility projections. The new admission module is included in the incremental mypy gate, focused admission/index tests were added, and the new test file is registered in the fail-closed CI shard manifest. Full PR CI passed on exact head SHA `0018f63ec5a920094628593c302b99dab2a29fa0`; the PR squash-merged as `3215ce651402c2a300a09b497e4bffdc4cefb291`.

**Remaining A11 tail:** move the broader sprint write-path/internal flow off `dict[str, Any]` where closed sprint documents still travel between writer/read helpers. Writer inputs/results and internal sprint documents should become typed values before rendering back to the established public/storage dictionaries. The guard-index and admission-candidate subproblem is now complete; keep the remaining work operation-by-operation and do not combine it with a storage migration or Kanboard retirement.

### A12. Replace closed event payload dicts with typed payload objects — complexity 4

The normalized `Event` is typed, but `Event.data` and several host operations still use `dict[str, Any]` / `dict[str, object]`. Some of those payloads are genuinely extensible, but others are tightly closed schemas that are manually validated key-by-key (for example outcome round context).

**Fix:** define typed payloads for event kinds whose schema is closed, e.g. `ReportPayload`, `VerdictPayload`, `DecisionPayload`, `AttemptUsagePayload`, and `OutcomeRoundContext`. Use a discriminated union keyed by `EventKind` where practical.

Keep an escape hatch for opaque/legacy event data; do not attempt to model every historical payload in one migration.

### A13. Break up `DispatcherRecord`'s nested dictionaries — complexity 4

`DispatcherRecord` itself is a dataclass, but many of its most important fields are still untyped bags:

- `gate_attestation`;
- `gate_pr_authorship`;
- `gate_published_ref`;
- `gate_workflow_dispatch`;
- `worker_head_run` / `review_head_run`;
- `worker_run` / `review_run`;
- `launch_intent`;
- delivery evidence and headless-state documents.

The runtime already has typed `HeadRun`, `StopInitiator`, continuation values, and vitality episode values, so keeping their persisted forms as dicts inside the in-memory domain record throws away type safety exactly where the dispatcher is most stateful.

**Fix:** deserialize JSON into typed sub-values in `DispatcherRecord.from_json()` and serialize only in `to_json()`. Start with `worker_head_run` / `review_head_run`, because a canonical runtime `HeadRun.from_json()` already exists.

This needs careful backward-compatibility tests because these fields are durable state across dispatcher restarts.

### A14. Remove the giant `dispatcher.py` compatibility façade — complexity 4

`src/secretary/dispatcher.py` is about 324 KB and begins with a very large set of imports and compatibility re-exports from `secretary.dispatch.*`, `dispatcher_*`, routing, watchdog, and host modules. It is simultaneously composition root, public compatibility surface, and implementation holder.

This makes dependency direction hard to see and encourages new callers/tests to import the old monolith even after behavior has moved elsewhere.

**Fix:**

1. inventory external/internal imports from `secretary.dispatcher`;
2. move callers to narrow feature APIs (`secretary.dispatch.*`);
3. reduce `dispatcher.py` to a thin compatibility module;
4. delete it when the last supported import path is gone.

The same principle applies to compatibility re-exports in `dispatch.host` and lazy `board.__getattr__` exports.

### A15. Continue flat-root migration and split god modules — complexity 4

The architecture document already declares the desired feature-first package layout and `tests/test_architecture.py` explicitly lists a large `LEGACY_FLAT_MODULES` allowlist. That is good containment, but the allowlist is still very large.

Several modules are large enough that they have become architecture boundaries by accident:

- `dispatcher.py` ~324 KB;
- `tasks.py` ~221 KB;
- `dispatch/host.py` ~221 KB;
- `sprints.py` ~159 KB;
- `dispatcher_observer.py` ~151 KB;
- `board/import_board.py` ~136 KB;
- `web/pages.py` ~131 KB;
- `cutover/__init__.py` ~106 KB.

**Fix:** keep using the existing architecture test as a ratchet: every cleanup PR should move a coherent slice from the flat root into the target feature package and remove that filename from `LEGACY_FLAT_MODULES`. Avoid creating new forwarding modules unless an installed/public entry point genuinely needs one.

Also move the implementation out of `cutover/__init__.py`; package `__init__` should expose a small API, not contain a 100 KB implementation.

### A16. Make the active board backend explicit — complexity 2 — completed

At audit time, production served the live board from PostgreSQL, but `board.backend.parse_card_backend()` still treated an absent/empty `SECRETARY_CARD_BACKEND` as `kanboard`. That migration default made configuration loss capable of silently selecting the legacy writer.

**Implemented in PR #440:** confirmed the installed web, dispatcher, and role launch paths all receive the instance `runtime.env` selector, then removed the implicit backend default. Missing, empty, and unknown selectors now refuse; explicit `postgres` and `kanboard` remain supported, so rollback is still an explicit configuration choice. The test suite now pins `kanboard` rather than relying on absence, and the status schema records that there is no product default.

This remains intentionally separate from deleting Kanboard itself.

### A17. Retire Kanboard from the live write path — complexity 5

The codebase currently maintains two full board implementations plus compatibility logic between them. The architecture/docs say the live production installation is PostgreSQL while Kanboard is retained as a read-only archive, but product code still contains live Kanboard readers/writers, direct Kanboard construction exceptions, transaction journals, restore paths, and host adapters.

**Fix (staged):**

1. make PostgreSQL explicit/default (A16);
2. make all production writers refuse Kanboard unless a dedicated rollback flag is set;
3. freeze a rollback deadline and export/archive requirements;
4. remove Kanboard write paths;
5. then remove Kanboard readers and compatibility parsing that no longer serves recovery/import.

Trying to delete it in one PR would be risky because backup/restore/import and historical audit compatibility still depend on it.

### A18. Move/remove one-shot Kanboard import and cutover machinery — complexity 3

`board/import_board.py` is ~136 KB and `cutover/__init__.py` is ~106 KB. Much of this exists to perform/verify a one-time storage migration. Once all supported installations are cut over and the rollback window is closed, keeping this code in the normal runtime package permanently increases the maintenance surface and forces current domain code to retain legacy normalizers.

**Fix:** after the migration lifecycle is formally closed, either delete these paths or move the importer to a versioned offline migration/tooling package that is not imported by the running product.

Before deletion, preserve the migration report/schema documentation needed to recover old archives.

### A19. Finish migration out of `triggered_agents` — complexity 5

`docs/ARCHITECTURE.md` explicitly calls `src/triggered_agents` a **legacy namespace**, yet `secretary` still imports runtime paths, role environment, head models, references, board transport, head registry, and runtime operations from it. `secretary.role_env` itself is mostly a re-export façade over `triggered_agents.runtime.role_env`.

This creates inverted ownership: the product package owns the architecture, while a legacy package still owns many of its core runtime types.

**Fix:** migrate in dependency order:

1. pure runtime types/utilities (`head`, paths, redaction, transport contracts) into `secretary.runtime` / `secretary.infra`;
2. move composition-specific runtime logic next;
3. move curator/steward/retro under `secretary.automations`;
4. leave `triggered-agents` as a temporary CLI compatibility entry point;
5. remove the package after the last installed unit/command uses the new path.

The architecture test that restricts back-imports is already a good ratchet for this work.

### A20. Retire Orca-legacy after local-pty parity — complexity 5

The head-runtime abstraction is a strong design improvement, but there are still two runtime backends and the absent/default profile backend remains `orca-legacy`. The Orca implementation has weaker stop semantics and drives a large amount of compatibility code around panes, leaves, aliasing, readiness and external session-manager behavior.

**Fix:** define explicit parity/exit criteria for `local-pty` (worker, reviewer, observer/service heads, recovery, drain/stop, operator diagnostics, rollback). Once met:

1. make `local-pty` the only default;
2. migrate installed head profiles;
3. remove `orca-legacy` selection and its adapter;
4. delete pane-specific compatibility state (`handle`/`leaf` paths that are no longer needed by any persisted old record after the support window).

This is likely the single biggest long-term simplification after the board-store migration.

---

## Suggested execution order

### Phase 1 — low-risk cleanup and guardrails

~~A01~~, ~~A02~~, ~~A03~~, ~~A04~~, ~~A05~~, ~~A06~~, ~~A16~~.

Phase 1 is complete: A01/A02 via PR #434, A03 via #435, A04 via #436, A05 via #438, A06 via #437, and A16 via #440.

### Phase 2 — collapse string/dict protocols

~~A07~~, ~~A08~~, ~~A09~~, ~~A10~~, A11.

A10 is complete via PR #441. A07 is complete across PR #443 and PR #444: product board roles now have one canonical typed vocabulary from the normalized Actor through TaskWriter, CLI choices, transitions, and importer boundaries; the separate legacy runtime registry stays with A19. A08 is complete via PR #445. A09 is complete across PR #442 and PR #446: both task and sprint cross-feature private-normalizer imports are gone. PR #446 landed the first A11 typed read-model slice, and PR #447 completed the bounded admission/reservation/guard-index slice. A11 now continues only with the broader write-path/internal sprint-document flow. The goal remains one typed vocabulary per concept and one parser at each legacy boundary.

### Phase 3 — dispatcher typing and package boundaries

A12, A13, A14, A15.

Do this incrementally; each PR should remove a compatibility surface rather than merely adding another wrapper.

### Phase 4 — delete compatibility subsystems

A17, A18, A19, A20.

These should be driven by explicit migration/rollback criteria, because they remove live compatibility behavior rather than just reorganizing code.

## General recommendation

The repository already has the right target architecture written down. The next cleanup work should optimize for **subtraction**:

- prefer deleting a compatibility path over adding another façade;
- parse untyped external data once, then use typed values internally;
- keep closed vocabularies as `StrEnum`, not sets of magic strings copied between modules;
- let `tests/test_architecture.py` keep ratcheting the old layout smaller;
- treat `triggered_agents`, Kanboard and Orca-legacy as migrations with explicit end conditions, not permanent second implementations.

With A01–A10 and A16 complete, and the A11 read plus admission/guard-index slices landed, the highest-value near-term sequence is: **finish the remaining A11 write-path/internal-flow tail → A12/A13**, keeping each migration bounded to one closed domain shape at a time. A17/A19/A20 should be planned as explicit deprecation projects rather than mixed into ordinary refactors.