# secretary

Product repository for the portable secretary appliance. It contains the CLI, schemas, task and
memory protocols, dispatcher runtime, restore logic and generic skills. The private installation
configuration is in `secretary-instance`; mutable state is in `secretary-data`.

The product is currently prepared side-by-side. Installing or testing it does not switch the live
board, dispatcher, services or timers.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Protocols](docs/PROTOCOLS.md)
- [Operations](docs/OPERATIONS.md)
- [Roadmap](docs/ROADMAP.md)

Install the CLI with `python3 -m pip install .`; the memory runtime is available with
`python3 -m pip install '.[memory]'`.
