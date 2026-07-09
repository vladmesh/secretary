# Phase 2 Review

Phase 2 goal: describe the current secretary installation in the private
`secretary-instance` repo and use `doctor --dry-run` to compare that config with
the host without changing anything.

## Commands Run

Checked the live checkout at `/home/dev/secretary-instance`:

```bash
python3 -m secretary doctor --dry-run --host --instance /home/dev/secretary-instance
```

Output:

```text
Secretary doctor report
mode: dry-run
instance: /home/dev/secretary-instance/instance.yaml
name: vladmesh-secretary
projects: 16
adapters: 8
data manifest: absent

host inventory: read-only
projects:
  unavailable: host.projects_root not set
units:
  matched: (none)
  missing-on-host: (none)
  unmanaged-on-host: (none)
orca repos:
  matched: (none)
  missing-on-host: (none)
  unmanaged-on-host: agent-kanban, codegen_orchestrator, codemode-bench-results, control-panel, dnd-simulator, inspect_ai, inspect_notebooks, memory-mcp, orca, panelmem-kb, personal_site, public_profile, secretary, secretary-instance, service-template, triggered-agents, vladmesh
host changes: none
status: host inventory incomplete
```

Exit code: 1.

Schema validation passed on the live instance: doctor loaded `instance.yaml`,
16 project bindings and 8 adapters. The non-zero exit comes from incomplete
host inventory, not from config schema errors.

Checked the design-doc command without explicit host inventory:

```bash
python3 -m secretary doctor --dry-run --instance /home/dev/secretary-instance
```

Output:

```text
Secretary doctor report
mode: dry-run
instance: /home/dev/secretary-instance/instance.yaml
name: vladmesh-secretary
projects: 16
adapters: 8
data manifest: absent
host changes: none
status: ok
```

Exit code: 0.

This command validates the live instance but does not print the host diff.
Current CLI behavior still requires `--host` for host inventory.

Local product tests:

```bash
python3 -m unittest
```

Result: 60 tests passed.

## Acceptance Check

`secretary doctor --dry-run` shows current differences without changes.

Status: partial, with schemas green.

The live path `/home/dev/secretary-instance` now contains the filled Phase 2
instance. `doctor --dry-run --host` validates the live schemas and prints the
actual host diff without changing host state. The host comparison is incomplete
because the instance config does not declare enough host surface for a complete
matched, missing and unmanaged comparison. `doctor --dry-run` without `--host`
does not print the host diff.

Connected projects are described in the instance, not the product repo.

Status: done on the live checkout.

The live `/home/dev/secretary-instance` checkout contains 16 project bindings
under `projects/` and no live project bindings in the product repo.

External adapters cover at least `control-panel`, `triggered-agents` and one
ordinary project.

Status: done on the live checkout.

The live instance contains adapters for `control-panel`, `triggered-agents`,
`agent-kanban`, `public-profile`, `secretary`, `secretary-instance`,
`vladmesh`, plus the generic `inventory-only` adapter.

Nothing on the host was changed.

Status: done for this review.

The live instance repo was not checked out, reset, rebased or edited. Host
inventory used read-only `doctor --dry-run --host` probes. Product validation
used `python3 -m unittest`.

## Real Gaps

The live path is now populated, but the host inventory contract is still
incomplete. The live instance has no `host` block. Because of that, doctor
cannot inspect project repos (`host.projects_root not set`) and has no expected
Orca registrations, so all current Orca repos are reported as unmanaged.

The design doc says `secretary doctor --dry-run` compares instance config with
the current host, but the implemented CLI requires `--host` for that comparison.
Without `--host`, the filled instance validates and exits 0 while doing no host
diff.

No systemd unit expectations are declared. Doctor reports no unit differences,
but this is an empty comparison rather than evidence that the current process
state is represented.

There is no `data-manifest.json` in the Phase 2 instance. That is acceptable if
data layout is explicitly Phase 3, but the report should call it out so nobody
reads "data manifest: absent" as a missed Phase 2 item.

Project naming is not normalized across instance bindings and Orca registry.
Some bindings use kebab-case ids while host repos use underscores, for example
`personal-site` maps to `/home/dev/projects/personal_site`. The host inventory
derives project names from repo paths, while Orca expectations would need the
actual registry names to avoid false unmanaged entries.

The filled instance records many projects as `enabled: false` with
`inventory-only`. That is a reasonable Phase 2 inventory snapshot, but it is not
a completed onboarding gate for those projects.

## Suggested Design Doc Edits

Keep Phase 2 explicit that the filled instance must be available at the live
path `/home/dev/secretary-instance` before review. That is true now and should
remain the acceptance boundary for future phase reviews.

Align the `doctor` command contract. Either make `--host` part of the Phase 2
acceptance command or change the CLI so `secretary doctor --dry-run` includes
the host inventory by default for Phase 2.

Add the minimal required `host` block for Phase 2 inventory: `projects_root`,
`unit_prefix`, `units` and `orca_repos`. Without those fields, doctor cannot
produce a complete matched, missing and unmanaged diff.

State that `data-manifest.json` remains Phase 3 so `data manifest: absent` is
expected during Phase 2.

Separate "inventory binding exists" from "onboarding gate passed" in Phase 2.
For disabled projects using `inventory-only`, the design doc should say that
they are recorded for visibility but not yet enabled for worker routing.

Document name matching rules for host resources. Project directory names,
project ids and Orca registry names are not always identical, so the instance
needs either explicit per-kind names or a documented normalization rule.
