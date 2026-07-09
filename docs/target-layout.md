# Target layout

The secretary appliance is split by ownership and durability.

```text
~/secretary             # product code
~/secretary-instance    # private config for one installation
~/secretary-data        # operational data, not a git repo
~/projects/...          # connected project repos
```

Phase 1 creates only `~/secretary`. The repo contains product code, schemas,
generic templates, tests and docs. It does not contain live projects, cards,
memory facts, transcripts, Kanboard dumps, secrets or host state.

The first useful host-facing command is dry-run only:

```bash
python3 -m secretary doctor --dry-run
```

It reads the mock instance config under `config/examples/` and reports no host
changes.
