# Outcome lineage

Worker and reviewer launches create a durable round handoff. The dispatcher adds the exact report,
verdict and decision event identities as it consumes each source. The terminal transition only reads
that handoff to freeze the specification revision, sources, taxonomy and usage identities into its
durable `attempt_outcome_owed` record. Recovery publishes only that record. It never reopens a card or
infers a link from a request id, comment, timestamp or journal order.

When a worker report is accepted, the dispatcher persists the typed terminal path
(`outcome_terminal_path`) before it looks up that report's event. Validate, gate, reviewer launch,
reviewer wait and post-gate terminal all read that path. A missing report handoff stays a named
incomplete lineage obligation; it cannot turn the path into one that claims no report was consumed.

The `attempt.outcome` record itself is specified in
[Protocols](PROTOCOLS.md#attempt-outcome-ledger-v1).
