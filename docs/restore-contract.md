# Restore contract

`bootstrap --empty` and `restore` only prepare the secretary data plane. They do
not import Kanboard cards, rebuild the memory index, or apply host changes.

## Empty target

Clone the product and the private instance repository first. The instance config
is the target identity and names its data root.

```bash
secretary bootstrap --empty --instance /srv/secretary-instance --dry-run
secretary bootstrap --empty --instance /srv/secretary-instance
```

The dry run prints a JSON plan and creates nothing. The non-dry-run command
creates the empty data layout, memory journal, and empty board/runs directories.
Its plan contains only initialized components: board import, memory-index rebuild,
and host reconcile have no work to perform on an empty target.
The configured `data_dir` must be absolute. It refuses any existing target data
root. Removing or replacing an installation is an operator action outside this
command.

## Restore

Keep the age identity outside both repositories and archives. Restore validates
the identity file, archive kind and version, archive structure, archive instance
identity, and the configured target data root before it creates target state.

```bash
secretary restore /mnt/backups/secretary-backup-core.tar.age \
  --instance /srv/secretary-instance \
  --age-identity /run/secrets/secretary.agekey \
  --dry-run

secretary restore /mnt/backups/secretary-backup-core.tar.age \
  --instance /srv/secretary-instance \
  --age-identity /run/secrets/secretary.agekey
```

Both commands emit JSON. Exit code `0` means the plan completed. Exit code `2`
means invalid arguments or a rejected preflight; it leaves the target untouched.
The restore extracts into a sibling staging directory and publishes the data root
only after extraction succeeds. A retry therefore either sees no target and can
run again, or sees a published installation and refuses to overwrite it.

The plan lists every component with an action. `restore` components are copied
into the data root, `rebuild` components are derived after restore, and `exclude`
components are deliberately not restored as canonical state. Core restores copy
normalized board data, the memory journal/export, and runs
watermarks/cards/claims. Full restores also copy raw board data, transcripts,
and artifacts. Its Orca debug inventory is classified as `exclude` and stays
outside the restored data root. The memory journal's Git history metadata is
required; an older archive without it is rejected. Restore rebuilds its Git
index locally and discards Git runtime state. `memory/index.sqlite` is
classified as `rebuild` and is not archived.

## Handoffs

After a successful data-plane restore, hand the remaining work to the following
operations in order:

1. Board restore imports the normalized export, including the core-only path. It
   restores card text, metadata, column, swimlane, position, and comment bodies
   in their exported order. Kanboard assigns new comment timestamps and
   `date_moved`, so neither timestamp is restored.
2. Memory index rebuild derives the index from the restored journal.
3. `secretary reconcile plan` and then its approved apply path restore managed
   host resources. Orca debug state is not applied as canonical state.

Run `secretary doctor` after those handoffs.

```bash
secretary restore-board --instance /srv/secretary-instance
secretary memory reindex --instance /srv/secretary-instance
secretary reconcile plan --instance /srv/secretary-instance
secretary restore-reconcile --instance /srv/secretary-instance
secretary doctor --instance /srv/secretary-instance
```

`restore-board` needs the live Kanboard runtime environment. `memory reindex`
reads `host.memory_reindex_python`, `host.memory_reindex_script`,
`host.memory_model` and `host.memory_dim` from the instance config. Until every
handoff completes, `doctor` reports the restore findings and exits 1.

## Reproducing the restore chain

The whole chain runs offline against fixtures. It produces a real age-encrypted
archive, deletes the producer data root, and restores from the archive alone:

```bash
python3 -m unittest tests.test_restore_e2e
python3 -m unittest
```

The archive-level tests need `age` and `age-keygen` on PATH and skip without
them. `scripts/check_memory_mcp_restore_e2e.py` is a separate cross-repository
gate for the memory-mcp side of the rebuild:

```bash
MEMORY_MCP_REPO=/home/dev/memory-mcp \
  MEMORY_MCP_TEST_PYTHON=/home/dev/memory-mcp/.venv/bin/python \
  python3 scripts/check_memory_mcp_restore_e2e.py
```

`docs/phase8-review.md` maps this coverage onto the Phase 8 acceptance and
records the operator off-host run that automation cannot make.
