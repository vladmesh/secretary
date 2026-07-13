# Project Onboarding Contract

Version: 1

This is the Phase 6 contract for `secretary project add`. It defines the
artifacts exchanged between deterministic scanning, draft creation, provision
analysis and the final onboarding gate. The executable shape is
`secretary/schemas/onboarding-contract.schema.json`.

## Artifacts

`scanner` is written only by the deterministic scanner. It records facts read
from the repository: repo existence, default branch, head, clean worktree flag,
language/package-manager hints, CI files, test files and whether a project-local
adapter already exists. It does not make LLM conclusions and does not write
bindings or adapters.

`draft` is written by `project add`. It contains the first
`secretary-instance/projects/<project>.yaml` binding and
`secretary-instance/adapters/<project>.yaml` adapter draft. The binding starts
with `enabled: false`.

`provision` is written by the provision-agent. It may complete setup, smoke,
validation and artifact policy in the adapter draft. It must keep the binding at
`enabled: false` and must choose CI policy explicitly: `github`, `local` with a
command, or `none` with the missing coverage declared.

`gate` is written only by the onboarding gate. A passed gate is the only artifact
that may set the binding to `enabled: true`. A failed gate keeps
`enabled: false`.

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
