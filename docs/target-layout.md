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
    facts/          # local git journal, git root is here
      global/
      <project-dir>/
    export.ndjson   # derived, not in the git journal
    index.sqlite    # derived, not in the git journal
    manifest.json   # derived, not in the git journal
  runs/
  transcripts/
  artifacts/
  backups/
```

Backups move this data as archives, not through product or instance git history.
The exception is `memory/facts`, which is a local git journal inside the data
directory. It has no remote and is still transported by backup, not by pushing
product code. Fact files keep the existing flat layout:
`memory/facts/global/<slug>.md` and `memory/facts/<project-dir>/<slug>.md`.
There is no intermediate `project/` directory.

`memory/export.ndjson`, `memory/index.sqlite` and `memory/manifest.json` are
derived siblings outside the git root. They can be rebuilt from facts, so they
are not the canonical memory source.

## Target Commands

In the Phase 7 target contract, `secretary doctor` validates config, data and the
live host without changing it. Host inventory is on by default; `--offline`
skips only that inventory for fixtures and isolated config/data checks. There is
no `--dry-run`, because doctor never writes. It inventories names only, never
opens env files or secrets.

Exit `0` means no findings (warnings are allowed), `1` means findings, and `2`
means the check could not complete. `--strict` turns warnings into exit `1`.
Undeclared host surface is a warning rather than permission for reconcile to
adopt it.

`secretary reconcile` will render desired process state from instance config,
heads, policies, projects and runtime components. `host.units` and
`host.orca_repos` are not a second desired-state list: the instance supplies
ownership boundaries and inputs, while reconcile records its applied result in a
managed manifest. It may create, update or delete a resource only when that
manifest or a verifiable managed marker proves ownership. A name prefix only
detects a conflict; it never grants mutation rights. Planned managed resources
include systemd units, Orca bindings, generated env files, memory indexes and
board schema. In Phase 1 it is an explicit stub.

Project `id` is a stable logical identifier. A repository path and Orca binding
name are explicit binding fields, not transformations of the id; in particular,
the CLI does not infer a dash/underscore conversion.

`secretary backup create` will create a consistent archive of operational data:
board export, memory facts and export, run state, transcripts, artifacts and
debug snapshots where allowed. In Phase 1 `backup` is an explicit stub.

Before full backup exists, Phase 3 exposes two narrow data commands:

```bash
secretary data init --instance ~/secretary-instance
secretary data raw-kanboard-dump --instance ~/secretary-instance
secretary data export --instance ~/secretary-instance
```

`data init` creates the target `secretary-data` directories, initializes
`memory/facts` as a local git repository without a remote, and writes the
schema-validated `data-manifest.json` under `data_dir`. `data raw-kanboard-dump`
copies the Kanboard container data directory into a fresh timestamped directory
under `secretary-data/board/`; repeated runs keep earlier dumps intact. This is a
raw layer for later backup/export work, not a normalized board export.

`data export` writes the Phase 3 normalized snapshot: `board/cards.json` and
`cards.ndjson` from the pipeline board CLI, `memory/facts` plus
`memory/export.ndjson` from a local import of `panelmem-kb`,
`runs/runs.ndjson`, `runs/watermarks.json` and `runs/cards.json` from
triggered-agents state, and `transcripts/inventory.*` from head transcript
directories. The exporters are read-only against their external sources and
publish deterministic files. A repeat memory import without fact changes leaves
the journal commit history unchanged.

`secretary memory import --instance ~/secretary-instance --from ~/panelmem-kb`
is the explicit pre-cutover sync. It copies supported markdown facts from
`panelmem-kb/memory` into `memory/facts`, commits the local journal with
`Op: import`, records the source HEAD in `memory/manifest.json`, and keeps
`panelmem-kb` untouched as the readable fallback. Re-running it after curator
freeze can pull facts added since the first seed. It refuses to run after the
first non-import commit appears in the journal, so protocol writes cannot be
overwritten by another source sync.

`secretary backup verify` will check backup structure and version compatibility
before restore or offsite retention decisions. It is planned after Phase 1.

`secretary restore` validates an archive and publishes its data plane into a new
target from the private instance repo. Board import, index rebuild and host
reconcile remain explicit handoffs; see `docs/restore-contract.md`.

`secretary bootstrap --empty` creates a fresh installation target for restore or
a new empty deployment. It refuses to overwrite an existing target.

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
