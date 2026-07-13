# Phase 5 closure

Checked on 2026-07-13 against the deployed `/home/dev/secretary` checkout and
the live Pipeline pilot `secretary-471`. The worker rollout is deployed from
`triggered-agents` commit `118ed8d`; the production rollback variable is unset.

## Result

Phase 5 is closed for the worker write bridge. Dispatcher and reviewer remain
on their existing paths.

| Gate | Result |
| --- | --- |
| Protocol live e2e | A worker `secretary task comment` added the repeat protocol marker. The legacy reader and `secretary task show` agreed on the `in_progress` column and task metadata. |
| Role guards | `secretary task report --role reviewer --kind done` returned `role_forbidden` with exit 3 before the permitted worker write. |
| Rollback live e2e | A one-shot `TA_WORKER_LEGACY_BOARD_WRITES=1` selected the legacy path in TASK rendering, launch prompt and preflight. Legacy worker comment added the repeat rollback marker; `secretary task show` retained both markers. |
| Audit | Before and after the writes, `verify-audit` returned `pending=0`; `reconcile-audit` returned `repaired=0, unresolved=0`. Reconciliation did not issue a board mutation. |
| Normalized export | Fresh export checks are recorded below. |

Only the exact value `TA_WORKER_LEGACY_BOARD_WRITES=1` selects the legacy
worker writer. Unset, `true`, `yes`, and `on` select `secretary task`.
Production `.env` was not changed and the dispatcher was not restarted.

## Commands and evidence

```text
python3 -m secretary task verify-audit
python3 -m secretary task reconcile-audit
python3 -m secretary task comment --ref secretary-471 --role worker --body-file <protocol-marker>
TA_WORKER_LEGACY_BOARD_WRITES=1 python3 -m triggered_agents pipeline --role worker comment --ref secretary-471 --body-file <rollback-marker>
python3 -m secretary data export-board --instance /home/dev/secretary-instance/instance.yaml --data-dir /home/dev/secretary-data
```

The fresh export contains exactly one `secretary-471`; `len(cards)`,
`raw_active_task_count`, and `export.json.card_count` agree. Its normalized
card contains both repeat markers.

The local regression coverage includes
`test_partial_move_failure_keeps_pending_until_reconcile`, which holds pending
audit open after a committed dispatcher move while claim/retry cleanup fails,
then verifies safe idempotent cleanup, and the board-export consistency tests.

## Handoff

After this PR merges, secretary should synchronize
`control-panel/docs/design-secretary-appliance.md` and `docs/backlog.md` in
their owning repositories. This closure does not modify those repositories.
