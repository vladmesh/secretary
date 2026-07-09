# secretary

Product repository for the portable secretary appliance.

Phase 1 contains a CLI skeleton only. It does not own host processes, live project
bindings, board data, memory data, secrets, transcripts, or instance-specific paths.

## CLI

Run the smoke tests:

```bash
python3 -m unittest
```

Run the dry-run doctor against the mock instance fixture:

```bash
python3 -m secretary doctor --dry-run --instance tests/fixtures/mock-instance.yaml
```

The Phase 1 command surface is present, but only `doctor --dry-run` does useful
work. `reconcile`, `backup`, `restore`, `project add`, `task`, and `memory` return
an explicit `not implemented` message.
