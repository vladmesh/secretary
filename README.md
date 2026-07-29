# secretary

Portable personal appliance for running multiple AI agent heads across many projects from a remote
VPS. The repository contains the CLI, the task and memory protocols, the dispatcher runtime, restore
logic, schemas and generic skills.

The product repository holds no installation data. Private installation configuration and the
portable Git-backed recovery checkpoint live in a separate private instance repository; local mutable
and derived runtime state lives in a local data directory. The current Git-backed install and recovery
path is documented in [Recovery](docs/RECOVERY.md).

## Documentation

- [Vision](docs/VISION.md) — what the appliance is for and who it is for
- [Roadmap](docs/ROADMAP.md) — product states, milestones and open questions
- [Architecture](docs/ARCHITECTURE.md) — storage boundary, runtime flow, security model
- [Protocols](docs/PROTOCOLS.md) — command contracts for tasks, sprints, memory and secrets
- [Operations](docs/OPERATIONS.md) — runbooks for a running installation
- [Recovery](docs/RECOVERY.md) — the checkpoint contract, fresh install and restore

## Install

The host bootstrap currently supports Ubuntu 24.04. Install the CLI and memory runtime from a checkout:

```bash
python3 -m pip install '.[memory]'
```

Bootstrap the host first:

```bash
sudo secretary bootstrap --instance-remote REMOTE --instance-dir INSTANCE \
  --installation-user INSTALL_USER
```

For a new installation, continue with:

```bash
sudo secretary install --instance-remote REMOTE --instance-dir INSTANCE \
  --installation-user INSTALL_USER
```

To rebuild an existing installation from its private checkpoint, use `recover` instead of `install`:

```bash
sudo secretary recover --instance-remote REMOTE --instance-dir INSTANCE \
  --installation-user INSTALL_USER
```

Bootstrap pins the board and session-manager transports, generates the local board credentials in
`INSTANCE/runtime.env` with mode `0600`, and creates the Pipeline board from the instance registry.

## Status

The project is pre-1.0 and moving quickly. It is developed against one opinionated deployment
profile: a single trusted owner running one appliance on one host. See
[SECURITY.md](SECURITY.md) for the boundaries of that model.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
