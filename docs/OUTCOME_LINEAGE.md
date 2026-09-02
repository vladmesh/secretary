# Outcome lineage

Worker and reviewer launches create a stable durable round handoff. The dispatcher adds exact
report, verdict, and decision event identities when it consumes each source. The terminal
transition only reads that handoff to freeze the specification revision, sources, taxonomy, and
usage identities in its durable `attempt_outcome_owed` record. Recovery publishes only that
record. It does not reopen a card or infer a link from a request id, comment, timestamp, or
journal order.

The live canary evidence is the offline projection of a copied sealed production checkpoint for
`codegen-orchestrator-1239`. It is evidence from that copy, not a live-board query. The repeatable
hermetic CI regression is `secretary-1537`; it is deliberately distinct from the live canary.
