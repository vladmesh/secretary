# secretary

Product repo for the personal secretary appliance. The migration contract lives in
`control-panel/docs/design-secretary-appliance.md`.

This Phase 1 skeleton is intentionally inert. It provides a CLI shape, schemas,
example config and tests, but it does not own any live host process or data.

## Layout

- `secretary/` - Python package and CLI entrypoint.
- `schemas/` - JSON schemas for instance config, project binding, adapter and data manifest.
- `config/examples/` - mock configs for dry-run checks.
- `docs/` - product docs and target filesystem layout.
- `templates/` - empty product templates, without installation data.
- `tests/` - smoke and schema checks.

The repo must not contain connected projects, cards, memory facts, transcripts,
Kanboard dumps, secrets or host-local data. Examples may mention paths like
`~/secretary` to describe the target layout.

## Smoke

```bash
python3 -m secretary --help
python3 -m secretary doctor --dry-run
python3 -m unittest discover -s tests
python3 -m compileall -q secretary tests
```
