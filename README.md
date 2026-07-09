# secretary

Product repository for the portable secretary appliance.

Phase 1 contains a CLI skeleton and the config schemas only. It does not own host
processes, live project bindings, board data, memory data, secrets, transcripts, or
instance-specific paths.

## Config schemas

The contract between the product and a future `secretary-instance` repo lives in
`secretary/schemas/` as JSON Schema (draft 2020-12):

- `instance.schema.json` — top-level `instance.yaml`
- `project-binding.schema.json` — one connected project under `projects/`
- `adapter.schema.json` — a project adapter under `adapters/`
- `data-manifest.schema.json` — the `secretary-data` layout descriptor

`examples/instance/` is a complete, valid instance tree with placeholder-only data
(no live bindings). `doctor --dry-run` and the tests validate against it.

## CLI

Install dependencies and run the tests:

```bash
python3 -m pip install .
python3 -m unittest
```

Run the dry-run doctor against the example instance:

```bash
python3 -m secretary doctor --dry-run --instance examples/instance
```

`doctor --dry-run` validates `instance.yaml` plus every binding, adapter and the data
manifest it finds, and never touches the host. `--instance` accepts an instance
directory or a direct path to an `instance.yaml`. An invalid config prints one problem
per line with a path to the offending field, and exits non-zero:

```text
secretary doctor: 1 config problem(s):
  example-project.yaml: id: 'Bad_Id' does not match '^[a-z0-9][a-z0-9-]*$'
```

## Host inventory

`doctor --dry-run --host` adds a read-only comparison of the instance against the
live host. For project repos, systemd units and Orca repo registrations it prints
three sets:

- `matched` — described in the instance and present on the host;
- `missing-on-host` — described in the instance but not found;
- `unmanaged-on-host` — present on the host but not described (reconcile would
  leave these alone).

What the instance owns is declared under `host` in `instance.yaml`: `projects_root`
(where repos live), `unit_prefix` (the systemd namespace secretary manages), `units`
and `orca_repos`. Expected project names come from the bindings under `projects/`.
Declaring `units` requires `unit_prefix`: without a namespace to enumerate, doctor
cannot see host units that the instance does not describe, so it would not compute
`unmanaged-on-host` for units.

The inventory is strictly read-only: it enumerates resource names only and never
opens env files, reads secrets, or changes host state. `--host-fixture DIR` runs the
same comparison against a fixture host directory instead of the live host, so it can
run offline and under test:

```bash
python3 -m secretary doctor --dry-run --instance examples/instance \
  --host-fixture tests/fixtures/host
```

The Phase 1 command surface is present, but only `doctor --dry-run` does useful work.
`reconcile`, `backup`, `restore`, `project add`, `task`, and `memory` return an explicit
`not implemented` message.

## Documentation

- `docs/target-layout.md` describes the target `secretary`, `secretary-instance`
  and `secretary-data` split, plus the purpose of each target command.
- `docs/phase1-review.md` records the Phase 1 acceptance check and differences
  between the current skeleton and the design doc.
