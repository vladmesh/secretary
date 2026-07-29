# Observer event wake canary, 2026-07-29

`sprint:888` ran against candidate `5ff60e2` with `codex-observer`. Its sole
linked card, `service-template-889`, stayed out of the execution pipeline: no
worker, reviewer, claim, or project checkout started.

- `16:38:36Z`: the card was created in Ideas, event
  `evt_2110c796b8a94607a2b11fa130f1d166`.
- `16:39:46Z`: Codex wrote the initial resume while that card was still in
  Ideas (`evt_879d3d8308244054b100fc9b0d779ef7`). It completed its queue before
  the test transition.
- `16:43:26Z`: the PO moved the linked card from Ideas to Blocked, event
  `evt_806e767f653f433d8b1ab45c72523df0`.
- `16:43:42Z`: the production tick delivered exactly one Codex nudge with
  `delivery_id` `delivery-325e8ee4cc704ee7ac7243b73fce2659` and immutable
  `through_event` `evt_806e767f653f433d8b1ab45c72523df0`.
- `16:44:19Z`: the observer reread the live board and wrote
  `resume_recorded` `evt_1939aaeba2744f62ae4b012dd0f056d6`. Its audit payload
  has that exact `delivery_id` and `through_event`.
- `16:45:07Z`: the next production tick retained the causal acknowledgement
  in delivery state: `acknowledged_delivery_id` is
  `delivery-325e8ee4cc704ee7ac7243b73fce2659`, `acknowledged_through` is
  `evt_806e767f653f433d8b1ab45c72523df0`, and
  `acknowledged_resume_id` is `evt_1939aaeba2744f62ae4b012dd0f056d6`; active
  delivery state returned to `idle`.

The observer completed the acknowledgement turn. A further 35-second quiet
check added no card event, no delivery, no launch, and no terminal output;
`resume_freshness` remained fresh. The sprint was then closed, and the
production observer list was empty after the stop tick.
