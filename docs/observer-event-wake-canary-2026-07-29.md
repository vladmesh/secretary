# Observer event wake canary, 2026-07-29

The live installation ran `sprint:882` with `codex-observer`. Its only linked
card, `service-template-883`, was created in Ideas and moved to Blocked. It was
never claimed or promoted, and no worker, reviewer, or project checkout started.

- `14:36:44Z`: Codex wrote the initial resume entry.
- `14:38:33Z`: the linked card moved from Ideas to Blocked, event
  `evt_e01bc0a11cd9442bb9218540ee682c6d`.
- The observer's active turn reread that board state and wrote a fresh resume at
  `14:39:17Z`. The dispatcher kept the event state durable while the queue was
  active (`wake_sent: false`).
- `14:41:18Z`: one card-level confirmation event,
  `evt_6ad6921dcef24446a02b6e49d8352eaa`, arrived after Codex had completed its
  queue. The production tick returned `observer-nudged` for `codex-observer`.
- `14:42:15Z`: the nudged observer wrote its fresh acknowledgement resume. Status
  reported `resume_freshness.fresh: true`, then `idle-grace`, with no further
  observer turn or card event.

The sprint was closed after that quiet observation. The temporary Codex observer
route was restored to the installation's normal `claude-observer` route, and
`dispatcher production-observe` reported no observers.
