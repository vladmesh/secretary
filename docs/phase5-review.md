# Phase 5 Review

Phase 5 defines the task protocol over the live Pipeline Kanboard. It does not
move the dispatcher, replace the board, or select a model family automatically.
Those changes start in Phase 7. Until then, `triggered_agents` remains the
owner of dispatch and the protocol is a compatibility layer over the same
cards.

## Normalized task

The protocol exposes one normalized task document. `id` is the stable protocol
identity; `backend.kanboard_task_id` is an adapter detail. `ref` is the current
human-facing reference and is unique within this board.

```yaml
id: task_kanboard_<task-id>
ref: secretary-467
title: Fix task contract
description: ...
state: ideas | ready | in_progress | validate | blocked | done
position: 1
project: secretary
type: code | research
blocked_by: secretary-466 | null
claim:
  worker: worker-id | null
  claimed_at: RFC3339 | null
routing:
  complexity: cheap | standard | hard | frontier
  family_preference: auto | claude | codex
  head_override: profile-id | null
  review_head_override: profile-id | none | null
  resolved_worker_family: claude | codex | null
  resolved_worker_head: profile-id | null
  resolved_review_family: claude | codex | null
  resolved_review_head: profile-id | null
  routing_reason: string | null
  quota_snapshot_at: RFC3339 | null
workspace:
  slug: string | null
  base_branch: string | null
retry:
  same: 0
  switched: 0
  heads: []
audit:
  created_at: RFC3339 | null
  updated_at: RFC3339 | null
  backend:
    kind: kanboard
    kanboard_task_id: 123
    board: Pipeline
```

`state`, enum values, timestamps and empty values are normalized by the
protocol. The adapter never leaks a Kanboard column id or an empty-string
metadata sentinel to a caller. Unknown metadata stays in `extensions.kanboard`
for read/export compatibility and is not accepted as a protocol write.

### Current Kanboard mapping

| Normalized field | Kanboard source | Existing default / compatibility rule |
| --- | --- | --- |
| `id` | derived from task `id` as `task_kanboard_<id>` | Existing task ids are preserved. |
| `ref` | `task.reference` | Empty legacy reference is shown as `""`; writes need a unique ref. |
| `title`, `description` | `task.title`, `task.description` | Description defaults to `""`. |
| `state` | column title | `Идеи`, `Ready`, `In progress`, `Validate`, `Blocked`, `Done` map to `ideas`, `ready`, `in_progress`, `validate`, `blocked`, `done`. Unknown columns are a backend-schema error. |
| `position` | `task.position` | Missing or invalid value becomes `0` on read. |
| `project` | metadata `project`; swimlane name is cross-check only | Empty for legacy/manual cards. No inferred project is written back. |
| `type` | metadata `task_type` | Empty for legacy cards; new writes require `code` or `research`. |
| `blocked_by` | metadata `blocked_by` | Empty string becomes `null`. |
| `claim.worker` | metadata `claim` | Empty string becomes `null`; existing dispatcher remains authoritative for its lifecycle. `claim.claimed_at` is `null` on legacy cards and is never inferred from comment text. |
| `workspace.slug`, `workspace.base_branch` | metadata `slug`, `base_branch` | Empty string becomes `null`; absent `base_branch` keeps manifest/default branch behavior. |
| `retry.*` | metadata `retry_same`, `retry_switch`, `retry_heads` | Missing or invalid counters read as `0`; empty heads reads as `[]`. |
| worker overrides/results | metadata `head`, `resolved_head` | `head` maps to `head_override`; absent `resolved_head` yields `null`, not the effective default profile. |
| review overrides/results | metadata `review_head`, `resolved_review_head` | Empty maps to `null`; `none` remains the explicit layer-3 review opt-out. |
| comments | `getAllComments` items `date_creation`, `comment` | Preserve content and timestamp. Parsed marker is derived from the first `[marker]` line; unmarked text remains a comment. |
| audit timestamps | `task.date_creation`, `task.date_modification`, `task.date_moved` | Unix seconds convert to RFC3339; absent or zero becomes `null`. |

The initial adapter retains the current board name, swimlanes and tags. Board
name is `Pipeline`; the swimlane remains the project display grouping. The
current generated head tags (`worker:`, `model:`, `effort:`, `reviewer:`) stay a
Kanboard presentation cache, not protocol data. `steward_report` is retained
only in `extensions.kanboard` because it is a dispatcher/steward implementation
guard, not a portable task property.

### Routing metadata

`complexity`, `family_preference` and overrides describe intent. `resolved_*`,
`routing_reason` and `quota_snapshot_at` record one dispatcher decision. A
protocol client must not overwrite resolved fields except the dispatcher path.

Backward-compatible defaults for cards that predate Phase 5 are:

```yaml
routing:
  complexity: standard
  family_preference: auto
  head_override: <existing metadata.head or null>
  review_head_override: <existing metadata.review_head or null>
  resolved_worker_family: null
  resolved_worker_head: <existing metadata.resolved_head or null>
  resolved_review_family: null
  resolved_review_head: <existing metadata.resolved_review_head or null>
  routing_reason: null
  quota_snapshot_at: null
```

`head_override` is the renamed public meaning of the current `head` metadata,
not a second competing value. The adapter reads old `head` and writes it only
when an explicit override changes. The initial Phase 5 implementation may set
only `complexity` and `family_preference`; it does not choose a family, populate
`resolved_*`, probe quota or alter current health fallback. `frontier` is valid
intent but cannot be automatically claimed.

## Commands, roles and failures

The Phase 5 public surface is:

```text
secretary task list [--state STATE] [--project PROJECT]
secretary task show --ref REF
secretary task comment --ref REF --role ROLE --body-file FILE
secretary task report --ref REF --role worker --kind done|blocked --body-file FILE
secretary task move --ref REF --role ROLE --to STATE [--reason-file FILE]
secretary task reconcile-audit
secretary task verify-audit
```

Successful commands write one JSON document to stdout. `list` returns an array
of normalized task summaries; `show` returns the full document plus comments;
the three write commands return `{action, task, event_id}`. Errors are one JSON
object on stderr and no success object on stdout:

```json
{"error":{"code":"transition_forbidden","message":"worker may not move a task"}}
```

The exit-code contract is stable for all task commands:

| Exit | Meaning | Error codes |
| --- | --- | --- |
| 0 | Request completed and its audit event was appended. | none |
| 1 | Backend unavailable or backend rejected a write before commit. | `backend_unavailable`, `backend_error` |
| 2 | Invalid CLI input or invalid normalized schema. | `usage`, `validation`, `not_found` |
| 3 | Authenticated role or state guard rejected the request. | `role_forbidden`, `transition_forbidden`, `claim_conflict`, `predecessor_open`, `capacity_reached` |
| 4 | Backend write committed but appending its normalized event failed. | `audit_pending` |

Exit 4 is deliberately not retried as a board write. The caller invokes the
idempotent audit repair using the returned task identity and backend revision.
The old `triggered_agents pipeline` command keeps its current `0/1/2/3`
contract during the bridge; it is not changed by Phase 5.

Read-only `list` and `show` need no role. `comment` is available to `po`,
`dispatcher`, `worker`, `reviewer`, `steward` and `retro`, and records that
role as the comment actor. `report` is worker-only: `blocked` requires a
non-empty body; `done` keeps the existing optional body. `move` uses the
following matrix. `in_progress` is entered from Ready only by dispatcher claim.
`Validate -> In progress` is the dispatcher-only rework transition.

| Role | Permitted transitions |
| --- | --- |
| `po` | `ideas -> ready`, `blocked -> ready` |
| `dispatcher` | `in_progress -> validate|blocked|ready`, `validate -> in_progress|blocked|done` |
| `worker`, `reviewer`, `retro` | none |
| `steward` | PO transitions; `blocked -> done`; `in_progress -> done` only for its own report task; and `ideas|ready|in_progress|validate -> blocked` |

Steward `blocked -> done` and every steward transition to `blocked` require a
non-empty reason, written as the same operation's comment. All other guards
remain as they are today: a claim requires Ready, no existing claim, a completed
predecessor, a valid head profile, the global WIP cap, and at most one active
code task per project. A transition reset to Ready clears the claim, resolved
heads and retry state.

## Normalized event and audit log

`secretary-data/board/events.ndjson` is append-only. Every protocol-observed
write produces one event; importer/bootstrap events make legacy history
explicit rather than pretending it was authored by the new protocol.

```json
{
  "event_id": "evt_01J...",
  "schema_version": 1,
  "occurred_at": "2026-07-13T12:00:00Z",
  "actor": {"role": "worker", "id": "codex-terra"},
  "kind": "commented",
  "task_id": "task_kanboard_123",
  "ref": "secretary-467",
  "backend": {"kind": "kanboard", "task_id": 123, "revision": "date_modification:..."},
  "request_id": "uuid",
  "payload": {"marker": "report:done", "body_sha256": "..."}
}
```

Event kinds are `imported`, `commented`, `reported`, `moved`, `claimed`,
`metadata_changed` and `closed`. Bodies remain in Kanboard and normalized task
exports; the event records a digest, marker and structured changed fields so it
is useful for audit without duplicating arbitrary text in a second log.

The transaction boundary is the successful backend write. The protocol first
validates role and state, performs the Kanboard mutation, reads the resulting
task/revision, then appends and fsyncs the event under a board-audit lock. It
does not roll back Kanboard if that append fails, because Kanboard has no safe
compare-and-swap rollback and another actor may have changed the card. Instead
it leaves a durable pending-audit record keyed by request id, returns exit 4,
and `task reconcile-audit` appends exactly one event without repeating the
Kanboard mutation. `task verify-audit` reports unresolved pending records;
they block normalized protocol export and backup promotion.
The event id/request id uniqueness constraint makes repair idempotent.

`backup` consumes the normalized task snapshot plus `events.ndjson` through the
protocol export. A raw Kanboard dump remains a recovery artifact, not a second
task contract.

## Compatibility bridge and rollout

1. Add read-only adapter and fixture tests. Compare protocol `list/show` with
   `triggered_agents pipeline list/show` on the live board, including legacy
   cards and comments. No agent invocation changes.
2. Add the metadata defaults and append-only audit writer. Gate writes on a
   canary project and compare every protocol mutation with the old CLI's board
   view.
3. Switch workers first to protocol `comment/report`; their old CLI commands
   continue to see the same Kanboard comments and markers. Switch reviewers,
   then PO/steward moves. Dispatcher remains on its current direct path.
4. Expose protocol `list/show/comment/report/move` as the normal role surface.
   Direct Kanboard writes remain a secretary/operator development escape hatch
   while the adapter is being completed.
5. In Phase 7, move claim and dispatcher ownership only after side-by-side
   event, transition and health comparisons are green. Automatic family
   selection starts there, not in this phase.

Each rollout step requires these live health gates: Kanboard authentication and
board schema are green; all six named columns exist once; protocol and legacy
active-task counts agree; a sampled `show` agrees on reference, column,
metadata and comment markers; event tail has no unresolved pending record; and
a normalized export parses and has one task per active Kanboard task. A failed
gate stops promotion but does not itself mark an ordinary task blocked.

Rollback is a client-routing change, not a data rollback: stop sending the
affected role to `secretary task`, return it to the old pipeline CLI, retain
the board and event log, and reconcile missing audit events from pending records.
No Kanboard card is deleted, rewritten to old metadata, or moved solely for
rollback. If the adapter's schema mapping is wrong, disable its writes, keep
read-only comparison available, fix it behind fixtures, and resume at the last
green canary step. The old dispatcher therefore never needs to stop for a
Phase 5 rollback.

## Non-goals

Phase 5 does not implement the task CLI or Kanboard adapter, migrate the
dispatcher/reviewer/curator runtime, add quota probes, or choose Claude versus
Codex automatically. It fixes the contract those implementation cards must
follow.
