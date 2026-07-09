# Phase 1 Review

Phase 1 goal: create `~/secretary` as a side-by-side product repository with a
CLI skeleton, config schemas, mock instance, schema tests and documentation. It
must not take ownership of the live pipeline.

## Acceptance Check

`secretary doctor --dry-run` reads a mock instance and does not touch the host.

Status: done.

Checked with:

```bash
python3 -m secretary doctor --dry-run --instance examples/instance
python3 -m unittest
```

Evidence in repo:

- `examples/instance/instance.yaml`
- `examples/instance/projects/example-project.yaml`
- `examples/instance/adapters/example-project.yaml`
- `examples/instance/data-manifest.json`
- `tests/test_cli.py`
- `tests/test_schemas.py`

The doctor reports `mode: dry-run`, validates the mock instance tree and prints
`host changes: none`.

The current pipeline continues to work through the old components.

Status: done for Phase 1 scope.

Checked by repository boundary. This repo contains only the new product skeleton
and does not include dispatcher migration, systemd units, Orca bindings, Kanboard
state, memory storage or production project bindings. The active task pipeline
for this card still runs outside the new `secretary` product repo.

`secretary` contains no live projects, cards, memory or secrets.

Status: done.

Checked with:

```bash
rg -n "vladmesh|/home/dev|dnd-simulator|personal_site|kanboard|password|token|secret|BEGIN .*PRIVATE|sk-" .
```

The only expected matches are generic documentation wording, schema field names,
test sentinel strings that assert error output does not leak secrets, and
`TASK.md`, which is task-pipeline metadata and must not be committed.

## Scope Check

CLI skeleton: done. `secretary/cli.py` exposes `doctor`, `reconcile`, `backup`,
`restore`, `project add`, `task` and `memory`. Only `doctor --dry-run` performs
Phase 1 work. Other commands return an explicit Phase 1 `not implemented`
message.

Schemas: done. JSON Schemas exist for instance config, project binding, adapter
and data manifest under `secretary/schemas/`.

Empty target commands: done for the Phase 1 command surface in the current code.
`reconcile`, `backup`, `restore` and `project add` are stubs. `task` and `memory`
are also present as stubs because the target design names them as public
protocols.

Schema tests: done. `tests/test_schemas.py` validates good examples, rejects bad
shapes and checks that error messages do not echo secret-like values.

Target layout docs: done. See `docs/target-layout.md`.

## Differences From The Design Doc

The Phase 1 scope lists empty commands `doctor`, `reconcile`, `backup`,
`restore` and `project add`. The current skeleton also includes `task` and
`memory` stubs. This matches later target command design, but the Phase 1 list
could mention that extra protocol stubs are acceptable when they are inert.

The design doc mentions product commands such as `backup create`,
`backup verify`, `bootstrap --empty` and `upgrade`. Phase 1 currently exposes a
top-level `backup` stub, not nested `backup create` or `backup verify`, and it
does not expose `bootstrap` or `upgrade`. That is acceptable for the skeleton,
but the design doc should say whether Phase 1 must reserve every future command
name or only the first command surface needed by early phases.

The current example instance has one placeholder project binding with
`enabled: true`. This is safe because it points at `/srv/projects/example-project`
and is covered by tests that reject live vladmesh bindings. If the design wants
examples to model onboarding more strictly, the placeholder binding should be
`enabled: false` until a documented green gate exists.

The Phase 1 acceptance phrase "current pipeline continues to work" is checked by
non-ownership in this repo, not by an end-to-end run of the old pipeline. That is
the only practical Phase 1 check from the product repo. The card report should
record that boundary explicitly.

## Suggested Design Doc Edits

Clarify that Phase 1 may include inert stubs for later public protocols such as
`task` and `memory`.

Clarify whether Phase 1 must reserve all target command names, including
`bootstrap`, `upgrade`, `backup create` and `backup verify`, or whether those
arrive in the phases that implement them.

Clarify how to verify "current pipeline continues to work" during a side-by-side
product skeleton card. A repository-boundary check is enough for this phase, but
later phases need live pilot checks.
