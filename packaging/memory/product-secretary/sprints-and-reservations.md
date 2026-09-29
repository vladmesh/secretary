---
id: sprints-and-reservations
title: Sprints, products, and project reservations
---

A sprint is a separate durable entity, identified as `sprint:ID`. It records a goal, Definition of Done, product and issues it serves, reserved projects, and its open, stopped, or closed status. `secretary sprint show` is the authoritative view of that contract.

An execution card belongs to an open sprint. An open sprint reserves its projects so that work proceeds under one visible plan rather than through competing writers. Create and modify sprint-linked work through the role and workflow assigned to that sprint; a PO override is an explicit, audited exception.

An optional observer follows sprint-level progress and decisions. It does not replace the worker or reviewer, and it does not claim cards. A stopped or closed sprint is not permission to continue changing its old card contract: start or reopen the appropriate planned work through the normal protocol.

Use sprint comments for durable communication about the sprint. Keep goals, Definition of Done, decisions, and blockers specific enough that another role can act on them without reconstructing context from a conversation.

Worker and reviewer local checks on the control host are limited to the project's adapter broad
check and its subsets. Integration shards, Docker/container runs, stands, provisioning and
network-heavy checks belong in CI. Exceptions come only from the sprint's creation-only
`local_run_exceptions` list of `{project, argv, rationale}` entries, scoped to registered projects
reserved by that sprint; default `[]`. Use `sprint create --local-run-exceptions-file` to declare
them. Prose and missing gate receipts grant no exception. An undeclared local heavy run is a
blocking review finding even if it passed.
