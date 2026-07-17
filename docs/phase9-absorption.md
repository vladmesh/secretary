# Phase 9: code absorption handoff

Date: 2026-07-17

Status: code copied and tested side-by-side; no production cutover performed.

## Boundary

This increment moves product code into `secretary` and installation-specific persona/config
into `secretary-instance`. It deliberately does not stop, restart, enable, disable or rewrite
any live service, timer, Orca workspace, dispatcher or board resource. The old board remains the
only live board until an explicit operator command.

## Absorbed into `secretary`

- memory MCP server, sqlite-vec schema, full rebuild and CLI entry points;
- incremental add/update/delete indexing keyed by stable fact id and content hash;
- the deterministic curator, retro, steward and legacy pipeline compatibility runtime;
- role automation specs and head profiles;
- generic role skills and the role-skill sync helper;
- a side-by-side memory systemd unit asset and agent gate script;
- restore's default memory rebuild path, which now imports the product package rather than
  executing `~/memory-mcp/reindex.py`.

The compatibility package keeps the Python import and CLI name `triggered_agents` during the
migration, but it is built from the `secretary` repository. It does not require the old checkout
for imports. The product dispatcher remains the canonical future pipeline owner; the copied legacy
pipeline modules are rollback/CLI compatibility, not a second future dispatcher.

## Moved into `secretary-instance`

- the canonical persona now describes `secretary`, `secretary-instance` and `secretary-data`;
- memory model and dimension are explicit instance settings;
- runtime env ownership is documented as instance-local. No secret file was created or committed.

## Verification

- `python3 -m unittest discover -s tests`: 410 passed, 2 skipped;
- original triggered-agents suite against `PYTHONPATH=/home/dev/secretary`: 975 passed;
- memory incremental tests under the existing memory dependency environment: 2 passed;
- in-repo restore + memory MCP smoke: green without importing the external checkout;
- wheel build: green and contains `secretary.memory_service`, the compatibility runtime and its
  automation TOML files;
- no systemd/Orca mutation command was run.

## Live state intentionally unchanged

- `memory-mcp.service` still executes `/home/dev/memory-mcp/run.sh` and owns port 8077;
- `ta-*` services still execute `/usr/local/bin/ta-gate.sh` from old workspaces;
- the production board, dispatcher and timers continue on the old contour;
- `secretary-memory.service` is only a source asset, not installed.

## Cutover handoff

The later operator step must create a fresh encrypted backup, install `secretary[memory]` in its
own venv, render/adopt new units, stop old owners, start the new owners, run memory/doctor/backup
smokes and one pilot card, then hold a rollback window before deleting old checkouts. It must not
run implicitly from install, tests or `reconcile plan`.
