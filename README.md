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

The Phase 1 command surface is present, but only `doctor --dry-run` does useful work.
`reconcile`, `backup`, `restore`, `project add`, `task`, and `memory` return an explicit
`not implemented` message.
