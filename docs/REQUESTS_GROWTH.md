# Growth policy of `requests`

Decided 2026-09-21 (secretary-1658): **keep everything.** No row of `requests` is rotated,
archived or deleted by the product. The table grows with the installation's history, and that
is the intended shape.

## The facts it was decided on

On production, 2026-09-21: 33,675 committed rows, 42 MB of `intent` JSON, 71 MB in total, growing
by about 3,000–6,000 rows a week since 2026-07-13. Before this card every caller of
`SqlTaskAudit.events` read and sorted the whole table and filtered it in Python, so every read
cost the whole history, on the dispatcher tick and on web requests alike.

## Why keep everything

1. **With indexed, filtered reads the cost follows the slice, not the history.** `SqlTaskAudit`
   narrows by ref, set of refs, kind, settle-time window and page in SQL, and every query it
   issues is served by an index of `0012_request_read_indexes` (`docs/BOARD_STORE.md` §7.3). A
   sprint's read costs that sprint's rows and a task's read costs that task's rows, however much
   unrelated history sits beside them. Deleting history would therefore buy almost nothing on
   the paths that matter.
2. **`requests` is what recovery replays.** It is the audit canon on PostgreSQL
   (`docs/BOARD_STORE.md` §7.3): the checkpoint exports it whole as `audit.json`/`audit.ndjson`
   (`secretary/data.py`), `restore` reads it back, and request-id replay answers, budget and usage
   accounting and observer cursors all resolve against it. Rotating it would add a data-loss risk
   to recovery — a replay, a restore or a cursor resolving against rows that are no longer there —
   in exchange for disk that is not scarce.

## The backing

`tests/test_sql_audit_reads.py::SliceCostTests` reads one sprint's events and one task's events
over N and over 10×N unrelated records and requires the rows touched to be equal. The same module
proves that every query the audit issues has an index (`IndexAvailabilityTests`) and that the
narrowed reads answer exactly what the old read-all-then-filter answered (`SameAnswersTests`).
The budget pass's cursor page is measured the same way in `SliceCostTests`.

## What would make us revisit it

- A read on the tick or the web request path that has to see the whole history again. The
  known whole-history readers are off those paths (`restore`, the checkpoint export) or out of
  this card's scope (the sprint pages' journal read in `webproto/sprint_reads.py`); a new one
  should be narrowed first, and only if it cannot be should this policy be reopened.
- `requests` reaching a size where its disk footprint, backup time or `restore` replay time is
  itself an operational problem — as an order of magnitude, several GB or a backup that no
  longer fits its window.
- The budget pass's deferred set growing without bound. The pass reads the audit through one
  durable cursor, a page per tick (`dispatch/production.py::_reconcile_sprint_budget`), and an
  event whose card cannot be looked up waits in the cursor's deferred set, retried by request id
  each tick, rather than holding the cursor back. A set that keeps growing means cards that stay
  unreadable, and its retries become the cost that follows history.

Any of these reopens the question as a separate decision. Rotation or archiving would then need
its own design for what recovery replays from, and is not done piecemeal.
