# secretary

Portable personal appliance for running multiple AI agent heads across many projects from a remote
VPS. The repository contains the CLI, task and memory protocols, dispatcher runtime, restore logic,
schemas and generic skills.

The current production installation runs from this product repository. Private installation
configuration lives in `secretary-instance`; mutable runtime state lives in `secretary-data`.
Productization now focuses on turning the proven setup into an automated install and recovery path.

## Documentation

- [Vision](docs/VISION.md)
- [Roadmap](docs/ROADMAP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Protocols](docs/PROTOCOLS.md)
- [Operations](docs/OPERATIONS.md)

The Pipeline board is the only active backlog. Read project cards through the product protocol:

```bash
python3 -m secretary task list --project secretary
```

Install the CLI from a checkout with `python3 -m pip install .`; the memory runtime is available
with `python3 -m pip install '.[memory]'`. A supported clean-host installer is tracked in the
Roadmap.
