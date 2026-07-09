# Target Layout

The portable secretary appliance is split into one product repository, one
private instance repository, and one data directory.

```text
~/secretary             # product repository
~/secretary-instance    # private config for one installation
~/secretary-data        # operational data, not a git repo
~/projects/...          # existing project repositories
```

## `secretary`

`secretary` contains product code and generic contracts:

- CLI entry points and orchestration runtime;
- task and memory protocols;
- dispatcher, board adapter and memory service;
- schemas, migrations, templates, tests and generic skills;
- product documentation;
- example configs with placeholder data only.

It must not contain connected project lists, cards, memory facts, transcripts,
host-local paths, secrets or Kanboard data.

Phase 1 keeps this repository as a skeleton. It provides schema validation and
command stubs, but it does not own any production host state.

## `secretary-instance`

`secretary-instance` is the private config repository for one secretary
installation:

```text
secretary-instance/
  instance.yaml
  persona/
  projects/
  adapters/
  policies/
  heads/
  secrets/
```

`instance.yaml` names the installation, points at `secretary-data`, and records
offsite restore-chain settings. `projects/` contains connected project bindings.
`adapters/` contains project adapters, including adapters for external projects
where service files cannot be committed to the project repo.

There is one instance repo per installation, not one per connected project. It
needs a private remote outside the VPS because restore depends on it.

## `secretary-data`

`secretary-data` is live operational state and is not a git repository:

```text
secretary-data/
  board/
  memory/
  runs/
  transcripts/
  artifacts/
  backups/
```

Backups move this data as archives, not through product or instance git history.
The planned exception is `memory/facts`, which is a local git journal inside the
data directory. It is still transported by backup, not by pushing product code.

`memory/index.sqlite` is derived state. It can be rebuilt from facts and exports,
so it is not the canonical memory source.

## Target Commands

`secretary doctor --dry-run` validates an instance tree and reports what would
need attention without changing the host. In Phase 1 it reads the mock instance
under `examples/instance` and validates `instance.yaml`, project bindings,
adapters and the data manifest.

With `--host` it also runs a Phase 2 read-only inventory: for project repos,
systemd units and Orca repo registrations it reports what is matched, described
but missing on the host, and present on the host but unmanaged. The instance
declares its owned host surface under `host` (`projects_root`, `unit_prefix`,
`units`, `orca_repos`); project names come from the bindings. The inventory only
lists resource names, never reads secrets or env files, and changes nothing.
`--host-fixture DIR` points the same comparison at a fixture host for offline use.

`secretary reconcile` will render desired process state from product and instance
config, then create or update only resources marked as managed by secretary.
Planned managed resources include systemd units, Orca bindings, generated env
files, memory indexes and board schema. In Phase 1 it is an explicit stub.

`secretary backup create` will create a consistent archive of operational data:
board export, memory facts and export, run state, transcripts, artifacts and
debug snapshots where allowed. In Phase 1 `backup` is an explicit stub.

Before full backup exists, Phase 3 exposes two narrow data commands:

```bash
secretary data init --instance ~/secretary-instance
secretary data raw-kanboard-dump --instance ~/secretary-instance
```

`data init` creates the target `secretary-data` directories and writes the
schema-validated `data-manifest.json` under `data_dir`. `data raw-kanboard-dump`
copies the Kanboard container data directory into a fresh timestamped directory
under `secretary-data/board/`; repeated runs keep earlier dumps intact. This is a
raw layer for later backup/export work, not a normalized board export.

`secretary backup verify` will check backup structure and version compatibility
before restore or offsite retention decisions. It is planned after Phase 1.

`secretary restore` will restore an installation from an archive plus the private
instance repo, rebuild derived indexes, restore board data, and hand process
state back to `reconcile`. In Phase 1 it is an explicit stub.

`secretary bootstrap --empty` will create a fresh installation target for restore
or a new empty deployment. It is planned after Phase 1.

`secretary upgrade` will update product code, check compatibility, run
migrations, refresh generated state, and leave a restore path if migration
fails. It is planned after Phase 1.

`secretary project add` will onboard a project. The target flow is deterministic
repo scanning, LLM-assisted provision analysis, adapter creation, clean worktree
validation, and enabling only after a green gate. In Phase 1 it is an explicit
stub.

`secretary task create|claim|report|comment|move|list` will be the public task
protocol over the board backend. Workers and reviewers should use it instead of
direct backend writes once the protocol exists. In Phase 1 `task` is an explicit
stub.

`secretary memory propose|commit|supersede` will be the public writer path for
memory facts. Curator code writes through this protocol, while the memory service
owns the on-disk facts and local git journal. In Phase 1 `memory` is an explicit
stub.
