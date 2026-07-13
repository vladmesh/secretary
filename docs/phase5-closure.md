# Phase 5 closure check

Checked on 2026-07-13 against the deployed `/home/dev/secretary` checkout at
`1113a81` and pilot card `secretary-471`.

Implementation PR: https://github.com/vladmesh/secretary/pull/22

## Acceptance status

| Criterion | Status | Evidence |
| --- | --- | --- |
| Pilot card uses old dispatcher and new worker path | done | Protocol comment is visible through both task readers. |
| Role guards do not damage the card | done | Forbidden reviewer report returned exit 3 before the permitted comment. |
| Normalized export contains the pilot and passes live checks | not done | Repeat after the worker-mode rollout gate is fixed. |
| Worker rollback preserves events | not done | Legacy reader sees the protocol comment, but launch templates cannot select rollback mode. |
| Closure report and secretary-only design record | done | This report and the pending-cleanup reconciliation test are in `secretary`. |

## Protocol live e2e

`secretary task show --ref secretary-471` returned the claimed In progress
pilot card. A worker `secretary task comment` added the marker and body
`Phase 5 pilot protocol comment: worker comment path verified before closure.`
The same comment and `In progress` column were then visible through legacy
`triggered_agents pipeline --role worker show --ref secretary-471`.

The negative role check
`secretary task report --ref secretary-471 --role reviewer --kind done`
returned `role_forbidden` (exit 3). The pilot's comment count was unchanged
until the permitted worker comment.

## Rollback and audit

The deployed audit checks returned:

```json
{"ok":true,"pending":0}
{"repaired":0,"unresolved":0}
```

This confirms that routing a worker back to the legacy read/write client does
not require a backend switch or event deletion. The protocol comment remains
on the Kanboard card and is readable by that legacy client.

The local Phase 5 repair test covers a dispatcher `in_progress -> ready` move
where the column move commits and claim/retry cleanup fails. Reconciliation
leaves the pending audit record open while cleanup keeps failing, then retries
the idempotent cleanup and appends the event only after normalized claim,
resolved-head, and retry fields are clear.

## Normalized export

`secretary data export-board --instance /home/dev/secretary-instance/instance.yaml
--data-dir /home/dev/secretary-data` is the live export command. Successful
publication must contain
`secretary-471` in `board/cards.json` and matching `card_count` values in
`cards.json` and `export.json`.

The same consistency rule is covered locally by
`test_export_board_records_matching_active_raw_count_when_dump_exists` and
`test_pending_blocks_export_from_the_same_data_root`.

## Closure blocker

Phase 5 is not closed. The deployed `triggered-agents` worker templates still
always instruct workers to use `triggered_agents pipeline`: both
`taskdoc.render()` and `worker._worker_prompt()` omit
`TA_WORKER_LEGACY_BOARD_WRITES`. Therefore neither the default nor rollback
mode can prove matching TASK.md and launch-prompt instructions.

The live export is intentionally not counted as a green closure result while
this rollout gate remains open. It must be repeated after the worker-mode
templates are fixed, alongside the protocol, rollback, and audit checks.

This requires a separate `triggered-agents` card: make both worker templates
select `secretary task` by default and the legacy pipeline path when
`TA_WORKER_LEGACY_BOARD_WRITES=1`, with tests for both modes. This card does
not change that neighbouring repository.
