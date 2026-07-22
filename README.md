# secretary

Portable personal appliance for running multiple AI agent heads across many projects from a remote
VPS. The repository contains the CLI, task and memory protocols, dispatcher runtime, restore logic,
schemas and generic skills.

The current production installation runs from this product repository. Private installation
configuration and the portable Git-backed recovery checkpoint live in `secretary-instance`;
local mutable and derived runtime state lives in `secretary-data`. The supported Git-backed
install/recovery path is documented in [Recovery](docs/RECOVERY.md).

## Documentation

- [Vision](docs/VISION.md)
- [Roadmap](docs/ROADMAP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Protocols](docs/PROTOCOLS.md)
- [Operations](docs/OPERATIONS.md)
- [Recovery](docs/RECOVERY.md)

The Pipeline board is the only active backlog. Read project cards through the product protocol:

```bash
python3 -m secretary task list --project secretary
```

Install the CLI from a checkout with `python3 -m pip install .`; the memory runtime is available
with `python3 -m pip install '.[memory]'`. `secretary install` and `secretary recover` require an
already installed Kanboard and Orca runtime.
