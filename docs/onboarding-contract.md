# Project Onboarding Contract

Version: 1

This is the Phase 6 contract for `secretary project add`. It defines the
artifacts exchanged between deterministic scanning, draft creation, provision
analysis and the final onboarding gate. The executable shape is
`secretary/schemas/onboarding-contract.schema.json`.

## Identity

`identity` is the single source of binding identity for the whole run: `id`,
`repo`, `adapter`, `default_branch` and the optional `plane`/`policy`. It is
fixed by `project add` and never re-declared downstream. The materialized
`secretary-instance/projects/<project>.yaml` binding is `identity` plus the
current `enabled` bit.

The draft, provision and gate stages do not each carry their own copy of
identity. Their `binding` object holds only the mutable `enabled` flag. Because
the identity fields do not exist on a stage binding, and every object is
`additionalProperties: false`, a stage cannot introduce an `id`, `repo`,
`adapter` or `default_branch` that disagrees with `identity`. The forbidden
divergence is not caught by an equality check, it is impossible to write down.
A passed gate therefore enables the one binding that `project add` created and
`provision-agent` kept disabled, never a neighbouring project.

Consumers read identity from `identity` alone. They never re-derive it from a
stage snapshot, so no consumer needs its own cross-stage equality check and no
second source of identity exists.

## Artifacts

`scanner` is written only by the deterministic scanner. It records facts read
from the repository: repo existence, default branch, head, clean worktree flag,
language/package-manager hints, CI files, test files and whether a project-local
adapter already exists. It does not make LLM conclusions and does not write
bindings or adapters.

`draft` is written by `project add`. It creates the
`secretary-instance/adapters/<project>.yaml` adapter draft and records the
binding as `enabled: false`. The binding identity comes from `identity`.

`provision` is written by the provision-agent. It may complete setup, smoke,
validation and artifact policy in the adapter draft. It keeps the binding at
`enabled: false` and must choose CI policy explicitly: `github`, `local` with a
command, or `none` with the missing coverage declared.

`gate` is written only by the onboarding gate. A passed gate is the only artifact
that may record the binding as `enabled: true`. A failed gate keeps
`enabled: false`. A passed gate requires clean worktree, setup, smoke and
artifact policy checks to be `passed`; validation must be `passed` or
`declared-missing`. `declared-missing` is only for projects whose adapter
explicitly uses `validation.ci: none` with missing coverage recorded. A passed
gate with `validation.ci: github` or `validation.ci: local` must use
`validation: passed`. A passed gate also requires `scanner.status: ok`,
`provision.status: drafted` and no `severity: error` findings in scanner,
provision or gate.

## Ownership

The binding draft is owned by `project-add`. The adapter draft is owned first by
`project-add` and then by `provision-agent`. The enable transition is owned only
by `onboarding-gate`.

Forbidden enable owners are:

- `deterministic-scanner`
- `project-add`
- `provision-agent`

## Stable Codes

The contract reserves these finding codes:

| Code | Meaning |
| --- | --- |
| `repo.missing` | The requested repo path or URL cannot be resolved. |
| `scanner.failed` | Deterministic scanning could not complete. |
| `draft.invalid` | The draft binding or adapter fails schema validation. |
| `ci.undeclared` | The provision result did not choose a CI policy. |
| `gate.failed` | The clean-worktree/setup/smoke/validation gate failed. |

## Compatibility Manifest

The old dispatcher compatibility manifest is a derived transition consumer. It
is allowed to read the v1 onboarding result and render the legacy dispatcher
shape during migration, but it is not a second source of truth. If it disagrees
with this contract, the onboarding contract wins.
