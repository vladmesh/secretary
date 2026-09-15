# Astra audit

Audit date: 2026-09-15  
Baseline: `main` at `419bce6decdee15dd54d7d3e7f13d662763dbe56`

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

## Findings

| ID | Finding | Complexity |
|---|---|---:|
| A01 | Remove the dead private `_env.py` compatibility shim | **1** |
| A02 | Test the declared minimum Python version in CI | **2** |
| A03 | Add a real static type checker, initially on typed packages only | **2** |
| A04 | Replace board-backend string literals with `StrEnum` | **2** |
| A05 | Type pause mode and pause-state documents | **2** |
| A06 | Type Product/Issue kind, priority and close-reason vocabularies | **2** |
| A07 | Centralize the role vocabulary in one `Role(StrEnum)` | **3** |
| A08 | Type task routing metadata (`task_type`, complexity, family preference, phases, decisions) | **3** |
| A09 | Stop importing private task/sprint normalizers across feature boundaries | **3** |
| A10 | Rename/consolidate the two different `HeadRun` concepts | **3** |
| A11 | Migrate legacy sprint dicts/status strings onto the normalized sprint model | **3** |
| A12 | Replace closed event payload dictionaries with typed payload objects | **4** |
| A13 | Break up `DispatcherRecord`'s nested `dict[str, Any]` state | **4** |
| A14 | Remove the giant `dispatcher.py` compatibility façade | **4** |
| A15 | Continue the flat-root-to-feature-package migration and split god modules | **4** |
| A16 | Make the active board backend explicit instead of silently defaulting to Kanboard | **2** |
| A17 | Retire Kanboard from the live write path after the PostgreSQL cutover window | **5** |
| A18 | Move/remove one-shot Kanboard import/cutover machinery after cutover | **3** |
| A19 | Finish migration out of the legacy `triggered_agents` namespace | **5** |
| A20 | Retire the Orca-legacy head backend after `local-pty` reaches parity | **5** |

---

## Detailed findings

### A01. Remove the dead private `_env.py` compatibility shim — complexity 1

`src/secretary/_env.py` is now only:

```python
from secretary.infra.env import positive_int
__all__ = ["positive_int"]
```

`tests/test_architecture.py` explicitly keeps the old import alive and asserts that it is the same implementation. I found no production caller that needs the private `_env` path.

**Fix:** remove `secretary/_env.py`, remove it from `LEGACY_FLAT_MODULES`, delete the compatibility assertion, and import `secretary.infra.env` directly in the architecture test.

This is the clearest example of compatibility code that has already finished its migration and should now be deleted rather than preserved indefinitely.

### A02. Test the declared minimum Python version in CI — complexity 2

`pyproject.toml` declares `requires-python = ">=3.11"`, but GitHub Actions installs only Python 3.12. That means accidental use of a 3.12-only API can merge while the package still claims 3.11 support.

**Fix:** run at least the unit/component suites on 3.11 and keep the full integration matrix on the primary CI version if runtime cost matters. Alternatively, drop 3.11 from `requires-python` if it is intentionally unsupported.

### A03. Add a real static type checker — complexity 2

The repository has extensive annotations but no mypy/pyright gate. Ruff catches syntax/style classes of problems, not type mismatches between the many dict-shaped protocols.

**Fix:** add pyright or mypy and introduce it incrementally:

1. start with already-typed leaves such as `secretary.board.models`, `secretary.board.host`, `secretary.dispatch.head_vitality*`, `secretary.po.models`, and `triggered_agents.runtime.head`;
2. disallow new untyped functions in those packages;
3. expand package by package as legacy dict surfaces are converted.

Do **not** enable strict checking over the entire repository in one PR; that would turn this from complexity 2 into complexity 5.

### A04. Replace board-backend string literals with `StrEnum` — complexity 2

`secretary.board.backend` uses the closed vocabulary `"kanboard" | "postgres"` as strings (`KANBOARD`, `POSTGRES`, `CARD_BACKENDS`) and also uses string capability names such as `"card"`, `"sprint"`, `"product/issue"`.

This is a textbook closed vocabulary and should be typed.

**Fix:** introduce `BoardBackend(StrEnum)` and `BoardCapability(StrEnum)`. Parse environment strings once at the boundary and use enum values internally; serialize `.value` only at CLI/env/JSON boundaries.

This also makes accidental cross-use of backend names and capability names impossible to type-check.

### A05. Type pause mode and pause-state documents — complexity 2

`dispatcher_pause.py` has a small, stable domain model represented by dictionaries and strings:

- `PAUSE_MODES = ("drain", "freeze")`;
- alias maps for `soft/hard`;
- `ProductionPause.load() -> dict[str, Any]`;
- `auto_resume_status() -> dict[str, Any]`;
- `pause_payload() -> dict[str, Any]`;
- legacy mirror receipts as dicts.

**Fix:** add `PauseMode(StrEnum)`, `PauseState`, `AutoResumeStatus`, and `LegacyPauseMirror` typed values. Keep JSON serialization/deserialization in `ProductionPause`.

This removes a cluster of repeated `str(state.get(...))`, magic keys, and invalid combinations while preserving the same on-disk JSON shape.

### A06. Type Product/Issue vocabularies — complexity 2

`product_issues.py` still defines:

- `ISSUE_KINDS = {"bug", "feature", "question", "improvement"}`;
- `ISSUE_PRIORITIES = {"P0", "P1", "P2", "P3"}`;
- `ISSUE_CLOSE_REASONS = {"resolved", "invalid", "duplicate", "wont_do"}`.

Meanwhile `board.models.Issue` is already a typed normalized value, but `priority`, `issue_kind`, and `close_reason` are plain strings.

**Fix:** add `IssueKind`, `IssuePriority`, and `IssueCloseReason` `StrEnum`s in the normalized board model and let adapters parse/serialize them.

This is a contained migration because the vocabulary already has database `CHECK` constraints and validation logic.

### A07. Centralize roles in one `Role(StrEnum)` — complexity 3

The same role vocabulary is repeated in multiple places:

- `tasks.py` (`_ROLES`, `_CREATE_ROLES`, `_EDIT_ROLES`, etc.);
- `task_commands.py` argparse choices;
- `triggered_agents.runtime.role_env.BOARD_ROLES`;
- `board.card_transitions.CARD_TRANSITIONS` keys;
- `board.models.Actor.role: str`;
- dispatcher/runtime stop and routing paths.

The duplicated sets are already drifting into multiple partially-overlapping subsets.

**Fix:** define one canonical `Role(StrEnum)` in a low-level protocol module, then define permission subsets as `frozenset[Role]`. `Actor.role` should become `Role`. CLI/env boundaries parse strings into it.

This is a high-value cleanup because it removes both duplication and an entire class of typo-only runtime bugs.

### A08. Type task routing metadata — complexity 3

`tasks.py` contains many manually maintained string vocabularies:

- `_TASK_TYPES`;
- `_COMPLEXITIES`;
- `_FAMILY_PREFERENCES`;
- `_ROUTING_PHASES`;
- `_BLOCK_CLASSIFICATIONS`;
- `_DECISIONS` / `_DECISION_TARGETS`;
- editable/active state sets.

The same concepts are normalized in `restore.py`, SQL adapters, the importer, dispatcher routing, and JSON documents. `_enum_or_default` is duplicated in `tasks.py` and `restore.py`.

**Fix:** define small `StrEnum`s and a typed `TaskRouting` / `TaskMetadata` value. Convert Kanboard metadata strings at the adapter boundary. Restoration should use the same parser, not a second `_enum_or_default` implementation.

### A09. Stop cross-package imports of private normalizers — complexity 3

`board/import_board.py` explicitly imports private implementation details from `secretary.tasks` and `secretary.sprints`, including `_STATE_BY_COLUMN`, `_KNOWN_METADATA`, `_enum_or_default`, `_positive_int`, `_split_heads`, `_budget`, `_resume`, `_source_audit`, and `_json_list`.

This avoids literal code duplication, but replaces it with **semantic coupling to private internals**. Refactoring a reader now risks silently changing migration semantics.

**Fix:** extract the shared legacy wire-format parsers into an explicit module such as `secretary.board.legacy_codec` / `secretary.tasks.legacy_codec`, with public typed return values. Readers, restore, and the importer should all depend on that stable compatibility codec.

After Kanboard retirement, that whole codec can be removed together.

### A10. Rename/consolidate the two `HeadRun` concepts — complexity 3

There are two unrelated classes named `HeadRun`:

- `triggered_agents.runtime.head.run.HeadRun`: the mutable-in-time lifecycle identity of a real launched head (`run_id`, `spec`, task ref, lifecycle, stop initiator, fanout policy);
- `secretary.routing_journal.HeadRun`: an immutable routing/telemetry snapshot (`role`, selected profile, model, effort, resource, provider session, prompt identity).

The code itself explains that these are deliberately separate, but using the same name forces repeated dict conversion and makes imports/reviews error-prone.

**Fix:** keep the lifecycle type named `HeadRun`; rename the journal value to something explicit such as `RoutingHeadSnapshot` or `HeadLaunchSnapshot`, and provide one typed constructor from `HeadRun + resolved routing` rather than accepting `HeadRun | dict[str, Any]`.

### A11. Migrate legacy sprint dictionaries to the normalized sprint model — complexity 3

`board.models` already has `Sprint` and `SprintState`, yet `sprints.py` still works mainly with `dict[str, Any]` and string sets such as `SPRINT_STATUSES = {"open", "closed", "stopped"}`. Guard indexes, budget documents, resumes, and readers pass ad-hoc dict shapes around.

**Fix:** introduce typed read models for the data not represented by the minimal `board.models.Sprint` yet (`SprintBudget`, `SprintResume`, `SprintReservationSet`, etc.) and make `SprintReader` return typed values internally. Keep dict rendering at the CLI/checkpoint boundary.

This can be done operation by operation without changing storage.

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

### A16. Make the active board backend explicit — complexity 2

The documentation says production now serves the live board from PostgreSQL, but `board.backend.parse_card_backend()` still treats an absent/empty `SECRETARY_CARD_BACKEND` as `kanboard`.

That default was useful during migration, but after cutover it is a dangerous failure mode: losing one environment variable can silently select the legacy writer instead of failing closed.

**Fix:** after confirming every installed service receives `SECRETARY_CARD_BACKEND`, change absence to a configuration error (or at minimum make PostgreSQL the product default). Keep an explicit `kanboard` value for rollback while the rollback window exists.

This is intentionally separate from deleting Kanboard itself.

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

A01, A02, A03, A04, A05, A06, A16.

These improve safety immediately and make later migrations easier without changing major architecture.

### Phase 2 — collapse string/dict protocols

A07, A08, A09, A10, A11.

The goal is one typed vocabulary per concept and one parser at each legacy boundary.

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

The highest-value near-term sequence is: **A01 → A03 → A07/A08 → A09 → A13**, while A17/A19/A20 should be planned as explicit deprecation projects rather than mixed into ordinary refactors.
