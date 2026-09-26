# Operations

Operator runbooks for a running installation. Install and restore are in [Recovery](RECOVERY.md);
command and route contracts are in [Protocols](PROTOCOLS.md). The state of a particular installation
comes from `secretary status` and `secretary doctor`, not from this file.

- [installation and host requirements](#install-and-check-the-code);
- [data, status and checkpoint operation](#data-plane);
- [connecting a project](#connecting-a-project-gate-and-stale-input-recovery);
- [sprints and observer heads](#starting-a-sprint);
- [recovery and the optional cold archive](#recovery);
- [dispatcher operation and watchdogs](#auto-merging-green-cards);
- [background roles, web service and units](#background-role-telemetry);
- [upgrade and runtime health](#upgrade).

## Install and check the code

```bash
python3 -m pip install .
python3 -m pip install '.[memory]'
python3 -m pip install '.[dev]'
python3 -m tests.broad
```

The first form installs the CLI, the second adds the memory runtime, the third the pinned linter. `ruff`
is pinned in `pyproject.toml` and any other version refuses to run; run it only on changed Python paths
with the command in [Testing](TESTING.md#changed-python-lint). Host bootstrap supports Ubuntu 24.04,
installs Docker and Compose from the distribution and provisions the board store; `secretary install` or
`secretary recover` then applies the instance ([Recovery](RECOVERY.md)).

`secretary status --instance <dir>` summarizes an installation; `--json` is a structured snapshot and
writes no state. `doctor` reports broken invariants (`--json` for structured findings). Changing the
host requires `reconcile plan` and a separate confirmed apply.

## Runtime secrets

### Installation secrets

Installation secrets live in the recoverable store (`secretary secret init/set/import`, the `secrets/`
directory of the private repository) and are materialised into env files. The store contract is in
[Recovery](RECOVERY.md#secrets). `runtime.env` next to `instance.yaml` can be a materialisation target;
whether it is shows under `secret_store.materialize` in `secretary status --json`. The product does
not migrate it on its own. Either way the file is `0600`, gitignored and in no checkpoint or archive.
`secretary shell` receives the whole file; dispatcher-launched workers and reviewers receive
non-secret runtime switches through the role-environment wrapper.

Migrate an existing `<instance>/runtime.env` with the CLI, never by copying values through a shell or
an argument list:

```bash
python3 -P -m secretary secret init --instance INSTANCE
python3 -P -m secretary secret import --instance INSTANCE --file INSTANCE/runtime.env \
  --scope installation --purpose runtime --materialize runtime-env
python3 -P -m secretary secret materialize --instance INSTANCE --target runtime-env
```

`secret init` is interactive and shows the recovery phrase once. `runtime-env` is a named target
resolving to the installation's canonical runtime-env path (including the supported override); do
not pass `--materialize-path` with it — that flag is only for the `file` target. `reconcile` never
decrypts the store.

### PostgreSQL board store

A fresh `secretary bootstrap` creates `/opt/secretary/postgres-compose.yml` (or checks the one root
installed), the `secretary-board-store_board-db` volume and `<instance>/board-store.env`, runs Alembic to the
shipped head and verifies the owner/app/read logins and privilege boundary. PostgreSQL is the only
container on this path and publishes only `127.0.0.1:5432`. Schema and roles are in
[Board store](BOARD_STORE.md).

`board-store.env` holds nine keys and three independent passwords, mode 0600, gitignored. Do not
print it, put its values on an argument list or commit it. An upgrade with no file reports PostgreSQL
as not provisioned and continues. Once the file exists, an invalid file, unreachable server,
unexpected image/volume/port, role drift or migration failure stops the upgrade before consumers
restart: preserve the file, Compose definition and volume, repair the named cause and rerun the same
command. A rerun keeps the volume and credentials and applies only owed migrations.

No credential rotation command is shipped. Editing `POSTGRES_PASSWORD` does not rotate an owner in a
non-empty volume. Rotation is one explicit manual operation: `ALTER ROLE`, an atomic whole-file rewrite
of `board-store.env`, and a restart of the web and dispatcher consumers together.

### Checkpoint and project GitHub access

One managed token, `github.checkpoint-token`, serves the checkpoint push and every dispatcher Git
operation on registered GitHub HTTPS projects. Set or rotate it with `secret checkpoint-github set
--instance INSTANCE --stdin` (or a caller-owned mode-0600 `--file`); it needs read and write access to
the instance remote and every such project. Transport classification, recovery bootstrap credentials
and readiness rows are in [Recovery](RECOVERY.md#github-checkpoint-credential). A manual
`~/.git-credentials` entry is not checkpoint readiness; read `checkpoint.credential` in `status --json`.

Before claiming a Ready card the dispatcher runs a bounded `ls-remote` preflight. A missing, locked or
rejected credential blocks the card with `step: git-access-preflight` and a refusal code
(`credential-missing`, `credential-locked`, `credential-rejected`, `unsupported-https`,
`unsupported-transport`, `unsafe-remote`, `remote-unresolved`); no workspace or head is created and the
block is not retried. A preflight with no answer leaves the card in Ready. A credential refused later,
at the gate, blocks with `git_access_refusal`. To recover: read the `project-git:<project>` rows of
`secretary doctor --instance INSTANCE`, rotate or unlock the token, return the card to Ready.

## Codex provider-internal fan-out policy

The policy and the telemetry it records are in
[Protocols](PROTOCOLS.md#codex-provider-internal-fan-out-policy). Fan-out observations are telemetry
only: do not stop or replace a head, block work or refuse delivery because of one.

Rerun the capability matrix only when an approved disposable-auth probe is warranted (a Codex
binary/model change or a candidate provider control). Use `scripts/codex_capability_matrix.py` with a
freshly isolated empty git worktree and `CODEX_HOME`; never point it at a production home. The
harness copies an explicitly approved auth source only into the temporary home, deletes the copy
before the next row, and outputs only raw-stream digests and typed event summaries.

## System requirements

The memory runtime loads a local embedding model and is the dominant memory consumer; an index
rebuild is its peak. No supported minimum is declared: size the host from `secretary status --json`
resource figures.

The model cache is `DATA_DIR/memory/fastembed-cache`, never `/tmp`. `host.memory_threads` sets the
ONNX Runtime inference limit (default `1`). `secretary doctor` prints the cache path and warns when
`data_dir` puts it under a temporary directory.

Heads run on `local-pty`, which ships with the product: no host-owned head runtime is installed,
ordered after or reported (A20 step 9, [Head runtime](HEAD_RUNTIME.md#a20-exit-checklist)).

## Data plane

```bash
python3 -P -m secretary data init --instance INSTANCE
python3 -P -m secretary data export --instance INSTANCE [--copy-transcripts]
```

`data init` creates the local layout and manifest. The memory-fact canon is
`INSTANCE/state/memory/facts`; the data directory keeps its export and index. `data export` writes
normalised board, memory, run and transcript exports; without `--copy-transcripts` only a transcript
inventory is kept.

## Checkpoint writer

The tick writer, its cadence, pathspecs and validation gate are in [Recovery](RECOVERY.md#writers).
Operationally: card reconciliation runs every one-minute tick, while checkpoint export and Git work
run at most once per five-minute window (a due push forces a fresh preparation). The gate is
fail-closed: pending task audit, an `export.json` counter mismatch or a detected secret blocks the
commit, the reason goes into the dispatcher's checkpoint state, and the next tick retries.

The shipped memory pack `packaging/memory/product-secretary` is materialized into the memory canon
during install and upgrade, with its ownership and digest record in
`INSTANCE/state/memory/packs/product-secretary.json`. It publishes facts under `product:secretary`. A
local fact at a shipped id stops the upgrade. `secretary upgrade --no-pull` still compares the
checkout's pack digest to the ledger and restarts the memory service when reconciliation changed it.
The ledger stays `pending` until the export is published and readable by the service user; the next
upgrade retries.

### Memory access from Claude and Codex

Install and upgrade reconcile an installation-owned `po_memory` stdio MCP entry in the installation
user's `~/.claude.json`, `~/.codex/config.toml` and `DATA_DIR/codex-home`, preserving login state
and unrelated entries. The command is `PRODUCT_ROOT/.venv/bin/secretary-memory-po-bridge`; its
environment names only the grant directory and loopback Memory URL, and no bearer is stored.

The entry applies to Claude or Codex processes started afterwards; restart existing sessions to get
`po_memory`. `secretary shell` and dispatcher-launched heads use the direct HTTP Memory endpoint with
a role-bound capability instead; a worker or reviewer gets scope `project:<card-project> +
product:secretary`.

Inspect without exposing credentials:

```bash
secretary upgrade --dry-run --no-pull --instance INSTANCE
rg -n 'po_memory|secretary-memory-po-bridge' \
  ~/.claude.json ~/.codex/config.toml \
  DATA_DIR/codex-home/config.toml
```

### Codex home (`CODEX_HOME`)

Every Codex head launched by the dispatcher, and the `openai-sub` resource probe, runs with one
`CODEX_HOME`, resolved at each launch in this order:

1. the profile's `codex_home`;
2. `TA_CODEX_HOME`;
3. `DATA_DIR/codex-home`, when it holds a login (a non-empty `auth.json`).

With none of them the launch fails closed: no Codex head starts, and the refusal names the fix, "log
in under `DATA_DIR/codex-home` (`CODEX_HOME=DATA_DIR/codex-home codex login`), or copy an `auth.json`
there". There is no fallback to the legacy Orca home (`~/.config/orca/...`); A20 step 7 removed it
(secretary-1723) once every live Codex head was proven to run on the data-dir login.

Rung 3 needs to know the data dir. It comes from `SECRETARY_DATA_DIR`. The production dispatcher
tick, the background agents and `secretary shell` set that variable for their own run from the
selected instance, and the web unit sets it in its unit file. A process with no data dir, no profile
`codex_home` and no `TA_CODEX_HOME` is refused.

Install and upgrade (the `codex-home` step) copy `AGENTS.md` and `config.toml` into
`DATA_DIR/codex-home` if they are missing. It is the only CODEX_HOME the installation manages: the
legacy Orca home is neither seeded nor reconciled, and is not moved or deleted either (that is a PO
action, if ever). Neither step copies or writes `auth.json`.

The managed home must hold the full `[mcp_servers.po_memory]` bridge entry (`command`, `args`,
`env`). Each head is launched with `-c mcp_servers.po_memory.enabled=false`, and on a home without
the entry that override creates a table with no `command` or `url`: Codex then refuses every command
with `invalid transport in mcp_servers.po_memory`. The packaged `config.toml` therefore carries no
such table. Seeding writes the entry into each `config.toml` it creates, in the same step. The
upgrade's `memory-clients` step reconciles it in `DATA_DIR/codex-home` whenever that directory
exists, since a login alone is enough for heads to select it: a file the home lacks is seeded first
through the same copy-once path, so the packaged defaults are never skipped, whichever of the
`memory-clients` and `codex-home` steps reaches the home first.

Check the login as the installation user:

```bash
secretary doctor --offline --instance INSTANCE | grep 'codex home'
#   error: codex home: no Codex login for this installation: log in under DATA_DIR/codex-home ...
CODEX_HOME=DATA_DIR/codex-home codex login
secretary doctor --offline --instance INSTANCE | grep 'codex home'
#   codex home: DATA_DIR/codex-home (data-dir home)
```

`doctor --json` returns the same answer in `codex_home` (`path`, `kind`, `data_dir_home`,
`login_missing`, `codex_required`). A missing login is a red finding (`codex_home_login_missing`,
with the fix text) whenever an installed head profile runs on the `codex` adapter; an installation
with no Codex profile only prints it.

Every session reader scans the `sessions/` of the current home and of the data-dir home, whichever
exist, and, read-only, the legacy Orca home's `sessions/`: on 2026-09-24 the curator had not yet
ingested 468 of the 3217 rollouts there (`runtime/codex_home.py`, `_LEGACY_SESSIONS`, which goes once
the curator's watermark names every one). No head is launched there. This covers the watchdog's
activity signal, the delivery confirmation for service heads, the dispatcher's continuation recovery
proof and the curator. Each reader counts a session only once. An explicit sessions override
(`TA_CODEX_SESSIONS`, `SECRETARY_CODEX_SESSIONS`, `TA_CODEX_SESSIONS_DIR`) is still the only root
while it is set.

### The PO workspace

The product owner head runs with `DATA_DIR/po` as its working directory (`DATA_DIR` is `data_dir`
of `instance.yaml`). Install and upgrade materialize it in the `po-workspace` step; after
`role-skills` has delivered into it, `po-workspace-owner` hands the whole tree to the runtime user on
every root-invoked upgrade (symlinks and hardlinked files are not followed), so a skill root left
root-owned by an earlier run is repaired:

| Path | Content | On upgrade |
| --- | --- | --- |
| `AGENTS.md` | copy of `packaging/po-workspace/AGENTS.md` from the product checkout | rewritten |
| `CLAUDE.md` | the single line `@AGENTS.md` | rewritten |
| `NOTES.md` | the PO's local notes | created if absent, never rewritten |
| `.mcp.json` | Claude project MCP entry `po_memory` | only that entry reconciled |
| `.codex/config.toml` | Codex `[mcp_servers.po_memory]` | only that section reconciled |
| `.claude/skills/` | PO skills for Claude | `role-skills sync` |
| `.agents/skills/` | PO skills for Codex 0.154, which reads this root in its cwd | `role-skills sync` |

The MCP entries are the same stdio bridge as the user-scoped ones above, written by the same
writers. The skills are the `po` role of `skills/manifest.toml`: `open-sprint`, `open-issue`,
`grilling`, `knowledge-doc`, delivered through targets whose root starts with `@po/`. A skill entry
`secretary/open-sprint` reads the skill from the secretary role's tree, so shared skills have one
source. `role-skills audit|sync` resolves `@po/` from the instance's `data_dir`, or `--data-dir`;
with no `instance.yaml` those targets are listed as unresolved and skipped.

`role-skills sync` removes a skill copy from a target root once no manifest declares that skill for
that root, but only a copy it can prove it delivered: one carrying the `.secretary-role-skill`
marker it writes into every copy, or, for copies delivered before the marker existed, one whose
`SKILL.md` is byte for byte a version the manifest's repository shipped under that name. Other
directories in a shell root are never touched. `audit` lists pending removals under `retired`.

Check:

```bash
ls -la DATA_DIR/po DATA_DIR/po/.claude/skills DATA_DIR/po/.agents/skills
secretary role-skills audit --instance INSTANCE
```

### PO head sessions and turns

`secretary.po.runner` runs the PO head headless, without a head runtime. A **session** is one
conversation with one CLI (`claude` or `codex`), one model and one reasoning effort, with `DATA_DIR/po` as cwd. A **turn**
is one CLI process with full permissions (`--dangerously-skip-permissions`,
`--dangerously-bypass-approvals-and-sandbox`), its own process group, and the owner's message on stdin:

| CLI | turn 1 | later turns | final answer |
| --- | --- | --- | --- |
| Claude | `claude -p --output-format json --session-id UUID` (UUID chosen at session creation) | `--resume UUID` once a turn completed; before that `--session-id UUID` again | `result` of the JSON result object |
| Codex | `codex exec --json -C DATA_DIR/po -` | `codex exec resume THREAD_ID -` (`thread_id` from turn 1's event stream) | the `-o` file |

**Effort.** Every turn of a session opened with an effort other than `default` carries it: Claude
`--effort LEVEL`, Codex `-c model_reasoning_effort=LEVEL` (both checked on Claude Code 2.1.280 and
Codex 0.155.1). `default` passes no flag, so the CLI runs with its own configured effort; every session
opened before efforts existed is `default`.

**Resolved model.** Each settled turn records the model the CLI actually ran (`po_turns.resolved_model`),
e.g. `claude-opus-5-5` for `opus`. Claude: the first key of `modelUsage` in its JSON result — the
session's own model; subagent models follow it (`claude-opus-5-5[1m]` is the long-context variant, kept
as reported). Codex: its `--json` event stream names no model, so it is the `model` of the last
`turn_context` in the thread's rollout, `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-THREAD_ID.jsonl`
(`CODEX_HOME` of the turn environment, else `~/.codex`). A turn that reported nothing (no result object,
no rollout) keeps `null`; the session reads show the latest turn's non-null value.
**Environment.** A turn gets the `po-serve` environment (HOME, auth, `SECRETARY_*`, `TA_*` kept) with
the directory of the interpreter running `po-serve` (the product runtime, `/home/dev/secretary/.venv/bin`
on prod) first on `PATH` and the product source it imports first on `PYTHONPATH`. So `python3 -P -m
secretary ...` and `secretary ...` from the PO workspace run the product, not the system Python.
`PoRunner.turn_environment()` computes it for every turn and re-run; an explicit `env=` replaces it.
Check, as the runtime user from `DATA_DIR/po` (prints the service interpreter, then `ok`):

```bash
BIN=$(dirname "$(tr '\0' '\n' </proc/$(systemctl show -p MainPID --value secretary-po.service)/cmdline | head -1)"); echo "$BIN"; env PATH="$BIN:$PATH" sh -c 'python3 -P -m secretary --help >/dev/null && secretary --help >/dev/null && echo ok'
```

Board store tables (revisions `0008_po_sessions`, `0009_po_requests`, `0010_po_session_close`,
`0015_po_effort_resolved_model`):

| Table | Holds |
| --- | --- |
| `po_sessions` | id, cli, model, cwd, created_at, state (`open`/`closed`), the CLI's session id, `closed_at` and `closed_by` (set exactly when closed), `effort` (`default` unless chosen) |
| `po_turns` | session, seq, started/finished, `running`/`completed`/`failed`/`interrupted`, stdout path, pid, process identity, reason (why it failed or was interrupted; on a re-run turn, why it was re-run), `resolved_model` |
| `po_feed` | the owner's messages and the agent's final answers only; no tool calls, no reasoning |
| `po_requests` | each /po form request id: operation (`po_session_create`, `po_send`), fingerprint of its inputs, the session and, for a send, the turn it made |

A partial unique index allows at most one `running` turn per session; the PO service keeps a second
message queued until the running turn ends ([The PO service](#the-po-service)). Different sessions run
turns in parallel.

**Close.** `PoStore.close_session(session, actor)` locks the session row and, in one transaction, sets
`state = 'closed'`, `closed_at = now()` and `closed_by`; the CHECK `po_session_closed_iff_audited`
refuses a closed row without both. A running turn refuses the close (`TurnInProgress`) and writes nothing;
closing a closed session answers it unchanged with its first `closed_at`/`closed_by`. A send into a closed
session is refused in the transaction that would create the turn (`SessionClosed`): no turn, feed entry,
request row or process. A replay of a send made before the close still answers its turn. Nothing is
deleted and a closed session is not reopened; its feed and raw output stay. Check:

```bash
psql "$SECRETARY_DB_READ_URL" -c "SELECT session_id, state, closed_at, closed_by FROM po_sessions ORDER BY created_at DESC LIMIT 10"
```

Raw output of a turn is in `DATA_DIR/po-runs/SESSION/turn-NNNN.{prompt,stdout,stderr,last-message}`,
outside the workspace. A non-zero exit, or no final answer, makes the turn `failed`; `reason` names the
exit status and quotes the stderr tail.

**Stop** kills the turn's process group and marks it `interrupted`. The session continues with
resume. A Codex turn stopped before its event stream named a `thread_id` leaves no id, and the next
turn starts a new Codex thread. A Claude session whose turns were all stopped or failed may or may not
have a saved conversation: the next turn uses `--session-id`, and if Claude answers
`Session ID UUID is already in use` the same turn is relaunched with `--resume`. Both attempts' output
is appended to the turn's files.

If the process of a turn starts but cannot be recorded in the store, its process group is killed and
reaped and the turn is `failed`. If the store does not answer at all, the row stays `running` with no
pid until the PO service's next start re-runs it.

### The PO service

`secretary-po.service` (`secretary po-serve --instance INSTANCE`, `Type=simple`, `Restart=always`, the
runtime user, the web unit's environment) is the one owner of PO turns: it holds the installation's only
`PoRunner`, and every turn process is a child in its control group. It has no `PartOf=`/`BindsTo=` tie to
`secretary-web.service`, so **a web restart touches no turn**: a running turn finishes and its answer
reaches the feed. `reconcile apply` and `upgrade` install and enable it with the other catalogue units,
doctor lists it, and `host.components.po.enabled: false` opts out (then no PO turn runs anywhere). The
service takes an exclusive lock, `DATA_DIR/po-service/service.lock`; a second `po-serve` on the same data
dir refuses to start.

**Queue.** A message is one file in `DATA_DIR/po-queue/` (`<time_ns>-<pid>-<n>.json`: `session_id`,
`text`, `request_id`, `source` — `web` or `po-service` — and `queued_at`), written to a temporary name, fsynced and
renamed before the submitter gets an answer. The service takes inputs oldest first per session and runs
one turn per session at a time; a message for a busy session waits in the queue, neither refused nor lost,
and sessions run in parallel. An input leaves the queue only after its turn row exists (`claim_turn`
under the input's request id), so a crash between the claim and the removal is answered by the same turn
on the next hand-over and creates nothing. An input that can never become a turn (session gone or closed,
request id taken by something else) moves to `DATA_DIR/po-queue/refused/` with its `reason`. A session
with queued messages refuses a close (409). Queued messages survive any restart of either service.

**Request ids.** One check, `PoService._reserve`, decides every request id, for `submit` and
`create_session` alike (and any later operation that takes one), under the lock that serializes them. An
id belongs to one operation with fixed inputs from the moment it is acknowledged, wherever it is recorded:
a `po_requests` row, a message pending in `po-queue/`, or a message set aside in `po-queue/refused/`. The
same operation with the same inputs is a replay; anything else is 409 `request_conflict`. So a session
create can never take the id of a message still waiting in the queue.

**Endpoint.** The Unix socket `DATA_DIR/po-service/po.sock` (mode 0600 in a 0700 directory): one JSON
request line, one JSON answer line, ops `submit`, `create_session`, `sprint_session`, `stop_turn`,
`close_session`, `status` and `restart` (`secretary.po.client`). The web is only a client: it reads the
store and the queue directory and sends every write here. The service runs a request only once its
whole line arrived.

**Accepted is accepted.** A message is accepted when its queue file is written, a session when its
`claim_session` commits. From then on the service answers "accepted" from what it knew at that moment —
the message queued under its id, or the session — even if a later step (the hand-over to a turn, the
lookup of the turn it became) fails; that lookup only adds detail. A failure *during* the accepting write,
which may have landed, is `outcome_unknown`. One wrapper in the service (`PoService._answer_id_operation`)
applies this to every operation that takes a request id.

**A refused form keeps its request id**, so sending it again is a replay of whatever the first attempt
did, never a second message or session. Only refusals marked, where they are raised, as having written
nothing (`data.nothing_written`) get a fresh id:

- nothing reached the service (no socket, connection refused, the send failed before the line ended):
  503, `the PO service is not running ... nothing was sent or written`;
- the input was refused before its id was reserved (empty text, a model or effort not offered, an unknown
  CLI): 400;
- the id already belongs to another request: 409 `request_conflict`;
- the session is unknown or closed: 404, 409 `session_closed`.

Everything else keeps the id: a lost or late answer (`the PO service may have accepted this message;
sending again with the same form is safe`, `data.reason = outcome_unknown`), a store that failed, any
unexpected error. A lost answer to a stop or a close says repeating it is safe (both are idempotent).
The web never starts a turn itself.

**Turn environment.** Every turn process gets `SECRETARY_PO_SESSION=<session_id>` beside the product
runtime's `PATH`/`PYTHONPATH`, on its first launch, a re-run and a relaunch alike. `sprint create` inside
a turn takes it as the default of `--po-session`, so the sprint records the session that opened it.

**A sprint's session (the resolver).** `sprint_session(sprint_ref, request_id)` answers the live PO
session of a sprint ([Protocols](PROTOCOLS.md#the-sprints-po-session-and-productions)). The session the
sprint recorded, while it exists and is open, is the answer and nothing is written. Otherwise the service
**re-seeds**, once: it opens a fresh session (the old one's CLI, model and effort, or the new-session
form's defaults when the old row is gone), queues as its first input a seeding message (the sprint, why
the session was opened, the text of the sprint's why-document from `state/knowledge/decisions/`, and the
instruction to read `NOTES.md` in the workspace), comments on the sprint as `po-service` (`the PO session
<old or none> no longer exists; opened <new> seeded with ... and NOTES.md`) and records the new id as the
sprint's `po_session`, in that order. The seed input is queued with `source: po-service`. Resolves run
under the service lock, so concurrent resolves of one sprint open one session; a repeat of the same
request id opens nothing and finishes whatever a failed attempt left. Check:

```bash
secretary sprint show --ref sprint:ID | jq '{po_session, allowed_productions}'
secretary sprint show --ref sprint:ID | jq '.comments[] | select(.body | contains("no longer exists"))'
```

**Service start.** Every turn left `running` is looked at once:

- its recorded process still alive (same PID and identity, not a zombie): killed first, then as below;
- no re-run recorded yet: **re-run once**. The reason `re-run: the PO service restarted while this turn was
  running` is written on the running row (`PoStore.mark_rerun`) and stays on it when it completes; the
  same prompt goes to the same conversation: Claude `--resume` once a turn of the session completed, else
  `--session-id` and, when Claude says the id is in use, `--resume`; Codex `exec resume THREAD_ID` when the
  interrupted attempt named its thread, else a new `exec`. Its output is appended to the same turn files;
- already a re-run (the reason is set): settled `interrupted` with `the PO service restarted again during
  this turn's re-run; not re-run a second time (it was re-run because: ...)`.

A turn the owner stopped was settled `interrupted`/`stopped by the owner` by the stop and is never re-run.
The re-run allowance is spent (`mark_rerun`) only after everything the re-run needs is loaded — the
owner's message from the feed, the session, the prompt file; a store that fails before that leaves the row
untouched for the next pass. A re-run that cannot be prepared for good (no owner message) is settled
`failed` with the reason; one whose launch fails after the allowance is settled `failed` (`the re-run did
not start: ...`), by the next pass if the store did not answer the first time. Recovery counts as done
only when every `running` row has a process under this service or is settled; until then it is retried,
first after a second and then doubling up to 30 s (journal: `still running without a process; recovery
retries in Ns`), and the sessions of those rows take no queued input while every other session goes on.
So a restart of the PO service costs at most the turn it interrupted, re-run.

**Upgrades never kill a running turn.** The PO itself runs `secretary upgrade` inside a turn. The `po`
step of `secretary upgrade` asks for a restart when the service's process inputs moved (product code or
dependencies, bundled schemas, the unit file) and the service decides by one rule
(`PoService.request_restart`): idle, it exits at once and `Restart=always` starts the new code, which the
step waits for (`restarted secretary-po.service while idle`); busy, it starts no queued
turn (new messages keep queueing) and exits by itself as soon as its running turns settle — the step reports `PO service restart
deferred: N turn(s) running` and does not fail. The request is the marker `DATA_DIR/po-service/restart-pending`;
the next process removes it at start. A stopped service is started. Check:

```bash
systemctl status secretary-po.service
journalctl -u secretary-po.service | grep 'secretary po'   # recovery, re-runs, set-aside inputs, deferred restarts
ls -l DATA_DIR/po-queue/ DATA_DIR/po-queue/refused/ DATA_DIR/po-service/
cat DATA_DIR/po-queue/*.json                               # what waits, oldest first by name
psql "$SECRETARY_DB_READ_URL" -c "SELECT session_id, seq, state, reason FROM po_turns WHERE state <> 'completed' OR reason IS NOT NULL ORDER BY started_at DESC LIMIT 10"
```

### The PO head in the dashboard

`/po` is a client of the PO service ([The PO service](#the-po-service)): its pages read the board store
and the queue, and every create, send, stop and close goes over the service's socket. `secretary
web-serve` builds no runner and recovers no turn. A PO head is a shell on this host, so `/po` has its own token on top of the
front's password.

**The token** is `DATA_DIR/po-web-token`: one line, mode 0600, owned by the runtime user, outside
`DATA_DIR/po`. The `po-token` step of install and upgrade creates it from `secrets` (256 bits) when it
is absent and never rewrites an existing one. Read it on the host:

```bash
sudo -u RUNTIME_USER cat DATA_DIR/po-web-token
```

**Rotate** by deleting the file and running the step again (`secretary upgrade`, or install). Every
browser cookie issued under the old token stops working on the next request; nothing needs a restart.

**Logging in.** Open `/po`, enter the token. The form posts to `/po/login`, which compares it with
`hmac.compare_digest` and sets cookie `secretary_po`: `HttpOnly; SameSite=Strict; Path=/po`, 30 days,
plus `Secure` when the request came through the TLS front (the front sets `X-Forwarded-Proto: https`).
The cookie value is an HMAC keyed by the token, never the token. Without a valid cookie every `/po`
route answers 401 (a page with the login form, or JSON `po_token_required`) before the PO service or the
board store is touched; a missing token file answers 503. Routes: [Protocols](PROTOCOLS.md#routes).

**The page.** `/po` lists open sessions, newest activity first, and opens a new one with a CLI, a model
from the list below and a reasoning effort (`CLI default`, preselected, passes no effort flag). A row's link is the start of the session's first owner message (whitespace
collapsed, at most 80 characters with `…` when cut, plain text; `no message yet` before the first
message), then its last activity (the latest of creation, any turn's start or finish, and any feed
entry), the model — the one its last turn reported (`claude-opus-5-5`, said `Opus 5.5`), else the one it
was opened with, the CLI and the exact id under it — its effort, state, whether a turn runs, the short session id and a `close` button. The panel
links to `closed sessions (N)`, `/po?closed=1`, which lists closed sessions the same way with their
`closed_at` instead of state and turn, and links back to the open ones. A session page names its model
and effort beside its title and shows the owner's messages, the PO
head's final answers and each turn's state (`running`, `completed`, `failed` or `interrupted` with its
reason), newest first, under a message box (Enter sends, Shift+Enter inserts a newline; the form goes
out once until the page reloads). **Every control is one row under that box, never at the end of the
feed**, which the newest-first order puts behind the whole scroll: `send` opens the row, and at its far
end, set apart from `send`, come `stop turn` while a turn runs, `close` while none does, and `new
session` always. `close` and `new session` are forms of their own and HTML has no nested form, so they
stand beside the message form and `send` reaches it by `form="po-send"`. `close` posts to
`/po/sessions/ID/close` as actor `owner` and returns to `/po`; pressed while a turn runs (from a list row)
it renders the session refused (409) and closes nothing. `new session` posts the `/po` form's own route,
`POST /po/sessions`, with the CLI, the model and the effort of the session being read and the page's request id
suffixed `-new-session` (one id belongs to one operation, so the create never takes the id the message
box carries); it opens a second session and leaves this one exactly as it was — sessions run side by
side, and nothing here closes one. A pair the installation no longer offers is refused the way the `/po`
form's create is, on `/po`, with the list of what it does offer. A closed session's page stays readable, says
`closed AT by owner`, and has no message box, no `send` and no `close` — only `new session`; a message
posted to it anyway is refused (409 `session_closed`). The running-turn count counts turns, whatever their session's state. The PO head's answers are rendered
server-side as a safe Markdown subset (headings, emphasis, code, lists, quotes, rules, `http(s)`/`mailto`
links; the text is escaped first, so raw HTML shows as text); the owner's messages are shown as typed.
While a turn runs or a message is queued the page
polls `/po/api/sessions/ID` every 3 seconds and reloads when the turn ends or the queued message starts. A
message sent while a turn runs is queued: it shows at the top of the feed marked `queued` and becomes the
next turn when the running one ends. Each form carries a request id minted when the page was served. It belongs to one
operation with fixed inputs, installation-wide: `po_requests` binds it to a session create (CLI,
model and effort; a `default` effort is left out of the fingerprint, so an id recorded before efforts
existed still binds the same inputs) or a send (session and the exact text), and is written in the transaction that creates the
session or the turn. Sending the same form again answers with what it made — the session, the message still
queued, or the turn running, completed, or failed with its reason even when its CLI never started — and
writes and launches nothing. The same id reused for anything else is refused (409 `request_conflict`). A queued message
has no request row until its turn is claimed; its id is bound by the queue file meanwhile. The dashboard's pipeline strip shows only the number of running PO turns
with a link to `/po`; it needs no token, and a PO store that does not answer hides the number.

**Models and efforts.** `instance.yaml`:

```yaml
po:
  models:
    claude: [fable, opus, sonnet]
    codex: [gpt-6-astra, gpt-5.6-terra, gpt-5.6-sol, gpt-5.6-luna]
  efforts:
    claude: [default, low, medium, high, xhigh, max]
    codex: [default, low, medium, high, xhigh]
```

Without `po.models` or `po.efforts` the product default is exactly that list. The first entry per CLI is preselected in
the new-session form: `fable` when Claude is chosen, `gpt-6-astra` when Codex is. A CLI left out keeps its default; an
empty list offers that CLI nothing. Creating a session with a CLI or model outside the list is refused
(400). The same holds for efforts, except that `default` (no effort flag) is always accepted, listed or
not; a create that names no effort is `default`. The lists are read on every request, so an edit needs no
restart.

### Read-only checkpoint and quiet-tick check

Observe an ordinary, already-authorized board transition; do not create a card change, invoke
`production-tick`, change a timer or force a push for this check. Before and after the next scheduled
tick:

```bash
secretary status --json --instance INSTANCE
secretary dispatcher production-observe --instance INSTANCE
secretary doctor --instance INSTANCE
journalctl --user -u secretary-dispatcher-production.service --since "TIME"
```

A changed normalized board or run export gives one local checkpoint commit at the end of that tick
(not an immediate push), visible under `checkpoint` in `status` and `production-observe`; `doctor`
reports a blocked gate, push failure, lag or divergence.

A quiet tick shows no card transition, `unchanged` checkpoint evidence, and for every
`observer-reconcile` result only `observer-live`, `observer-waiting` or `observer-idle` (or none). Any
other observer action — delivery, launch, relaunch or adoption, or an unrecognized one — fails the
observation.

## Status and doctor

`secretary status --json --instance INSTANCE` is the read-only operational snapshot and safe to poll.
It reports managed services and timers, projects and heads, active dispatcher attempts (workspace,
watchdog liveness, progress, respawn state), sprint observers, pause state, checkpoint freshness,
memory index state, and host disk, memory and load. A live run reads each head's liveness the way the
dispatcher's watchdog does (`command_terminal_status`: the pid heartbeat and the exact-run provider
cursor) into the watchdog's `panel` field; `--offline` reports it as `not-probed`.

`secretary doctor --json --instance INSTANCE` evaluates invariants over the same snapshot and exits
non-zero for a broken or unavailable host. Use `status` for what is running and `doctor` for what
needs repair.

The `recovery` object is shared with doctor:

- `resources` lists every resource in the installed head registry. `source` separates a fresh
  `dispatcher-cache` verdict, a `live-read-only-probe` and an unavailable observation; `freshness`,
  `observed_at`, `age_seconds` and `observed_state` expose staleness. Status never probes; doctor runs
  bounded read-only probes unless `--offline`. Neither writes the dispatcher cache.
- `credential_consumers` lists the managed checkpoint consumer (provider CLI logins stay unmanaged)
  and one `project-git:<project>` row per registered project with a checkout: `transport`,
  `managed_readiness`, `state`, `source`, `supported_next_action`. GitHub HTTPS rows take the managed
  state; local rows are `not-applicable`, SSH and other non-HTTPS `ambient/manual-bypass`, other HTTPS
  hosts `refused`. Consumer readiness does not borrow the last push outcome, and a locked store is
  `locked/unverifiable`. While any project uses an unmanaged HTTPS origin, doctor says to keep an
  ambient credential helper.
- `paths`, `materializations`, `catalog_envelope_divergences` and `bypasses` are metadata-only; every
  unsupported row carries `supported_next_action`.

`doctor` raises secret-store findings when catalog and values diverge, when the key is missing or
unusable with a non-empty catalog, or when the key is wider than `0600`.

With the production dispatcher enabled, the observer-root repository under the data directory belongs
to the installation. It is created on the first observer launch, so its absence on a fresh
installation is no finding. `reconcile` neither creates nor deletes it.

Timer-started oneshot units are neither required enabled nor active; their state is still
reported. No unit outside the installation's own is expected or probed; before A20 step 9 the
host-owned Orca unit was ([Head runtime](HEAD_RUNTIME.md)).

## How long things take

Three durations are recorded on every running installation, and one command measures the dashboard
against the thresholds a sprint is judged on.

### Where the durations are

| what | where it lands | how to read it |
| --- | --- | --- |
| one web request | `journalctl -u secretary-web.service` | `127.0.0.1 GET /sprints 200 4612.3ms` — client, verb, request target, HTTP status, and the milliseconds the application spent on it. One line per answered request, including a HEAD, a refusal and a contained 500. |
| one dispatcher tick | the dispatcher journal, and `secretary status` | `dispatcher.last_tick` carries `duration_ms` beside the outcome already recorded for that tick (`seq`, `at`, `status`, `healthy`, `actions`). The human `secretary status` prints it as `last tick: #12 ok at ... in 4322 ms`. |
| one checkpoint run | `secretary status` | `checkpoint.checkpoint_duration_ms`, printed by `secretary status` and by `secretary doctor` as `checkpoint: committed in 2100 ms`. Every outcome carries its own number, including an unchanged run and a blocked one — a no-change checkpoint still regenerated the whole projection. |

The web duration is the application's part of the answer — reading the body, handling the request,
and writing the headers and body back — not the whole socket lifetime. The tick duration is the
wall clock of `production_tick` up to the moment its outcome became durable.

### Measuring the dashboard

```bash
python3 scripts/measure_dashboard.py            # against http://127.0.0.1:8787
python3 scripts/measure_dashboard.py --json     # the same facts, as one document
```

Run it from a checkout, on the host the installation runs on, as the runtime user. It reads only,
and only from the installation named by `--base-url`: three dashboard pages and, to reproduce what
an operator's browser is doing, the `/po` overview and one session's JSON. It makes no POST and
starts no head. It also cannot be sent anywhere else: its one opener follows no redirect — a 3xx
from any route ends the run instead — and reads no `http_proxy`/`https_proxy` variable, because
either would time a different installation under this route's name and hand it this installation's
PO cookie.

It prints, with the Definition of Done threshold beside each number and whether that number meets it:

- **warm sequential** `GET /`, `GET /sprints` and `GET /projects` — one discarded warm-up request,
  then twenty timed ones per route. The judged number is the **p95 by nearest rank**, which over
  twenty samples is the second-slowest of the twenty: one request that actually happened, so two
  runs compare the same way every time. Min, median and max are printed beside it, and the warm-up
  is discarded because the first request after a deploy pays for import and cache warming that no
  later request pays again. Threshold: 1.0 s.
- **four concurrent** `GET /`, all four released together, while a `/po/api/sessions/{session}`
  poll runs on the three-second cadence the `/po` page itself uses. The scenario is repeated
  **three rounds**; every round's four durations are printed, and the threshold is judged on the
  **worst round** — the one holding the slowest single request. One round is not a measurement:
  the same unchanged installation produced 17 s, 27 s and 38 s on three runs of the first version
  of this script, and the DoD says *each* of the four answers within 2.0 s, so a scenario that
  breaches that in one round of three has not met it. Threshold: 2.0 s for each request of the
  worst round.

The session it polls is not just any open one. The `/po` session page installs its three-second
poll only while a turn is running and clears it when the turn ends, so the command picks the first
session the overview lists **whose own JSON reports `running: true`**, read at the route that would
be polled. An installation where nothing is running is one where no page is polling, and the
concurrent half cannot be reproduced on it at all — see the exit statuses below.

### The cadence, and how a round is scheduled against it

The concurrent number is only the DoD's number if the four requests ran *while* a session was being
polled every three seconds. Two things in the output say whether that happened, and the command
refuses to judge anything if either of them says no.

- **The cadence is an independent schedule, and it is observed.** The page polls with
  `setInterval(async () => { await fetch(...) }, 3000)`, which starts a read every three seconds
  whether or not the previous one has answered. The command does the same: poll *k* starts at
  `anchor + 3k` s on a thread of its own, so a slow session read neither delays the next start nor
  stretches an interval, and slow reads overlap each other as they would under the page. Every
  poll's actual start is compared with its scheduled one, both ways, and the run is refused if any
  is more than 100 ms off. The output prints the spacing and the furthest offset
  (`start against schedule: furthest +1.2 ms …`) and says `the poll started every 3 s` only where
  those numbers show it.
- **Every round is launched by a poll that fell due, while it is in flight.** The four requests
  wait for the next poll on the cadence; when that poll's clock starts they are released, together
  with the poll's own request, so all five go out at once. A round therefore costs up to three
  seconds of waiting before its first clock starts — that wait is in no number — and the ordering
  holds at any installation speed, because it is the order one thread does two things in. A round
  that no poll was issued for is refused.
- **Every request starts under a poll.** For each of the four requests the command records how many
  polls were in flight when it started. A round counts only when every request had at least one; a
  round that did not is printed as `not counted` and taken again, up to nine attempts for the three
  rounds, and a run that cannot collect three is refused.

Each round's line reports both facts:

```
round 2: 2102, 1976, 2034, 2004 ms [launched by a due poll; polls in flight at each start: 1, 1, 1, 1; 1 during the round]
```

`--no-poll` takes the same three rounds of four concurrent `GET /` with no poll beside them, and
labels every number `no poll`: the baseline an under-the-poll run is compared against, so the
difference the poll makes can be read off two runs of one command.

### The load is never lighter than the page's

The concurrent poll is held to a one-sided standard: **it is a load no lighter than an open `/po`
page on a running turn, so a number that meets its threshold under it is sound, and one that
exceeds it may be conservative.** The command prints that sentence with the poll it chose. It
need not be an exact copy of the page — a heavier load can at worst turn a pass into a
conservative fail, never a fail into a false pass.

That standard is why a turn that **ends during the run** is reported and not refused. The page
clears its timer when a poll answers `running: false`, which is zero load from then on; the command
keeps polling on schedule, which is more. It reads `running` from every poll and prints one line
naming where it happened — `the turn ended during round 2 (a poll answered running: false);
polling continued on schedule, at least as heavy as the page, which stops polling there` — and the
run stands. The session still has to be running when it is selected: that is what makes it a
session a real page is polling at the start.

### What it refuses to report

The rule the whole script is built around: **no number is judged MEETS unless it came from exactly
the scenario the DoD names.** So the exit statuses are

- `0` — every request answered 2xx, the whole specified scenario ran, and every judged number is at
  or under its threshold;
- `1` — the same, except that a judged number is over its threshold. A red number is still a real
  number, and the full table is printed;
- `2` — everything else.

Everything else includes: the installation is unreachable; any request on any route answers
non-2xx, including a 3xx (a missing route, a refusal, a redirect, or the transport's contained 500
under the load being measured); the data directory or the PO token cannot be resolved or read; a
session's JSON cannot be read for whether it is running, at selection or in any later poll; a
poll started more than 100 ms off its three-second schedule, either way; a round ran with no poll
issued for it; the installation has **no open PO session**; and
the installation has open sessions but **no turn running in any of them**.

Those last two are not faults of the installation — `/po` answered, and simply lists nothing, or
lists only sessions no page is polling. The concurrent half of the DoD cannot be reproduced without
a running session, and polling an idle one instead would put a request no page makes beside the
four and report it under the same heading. In every case the command prints the numbers it did
take, marks every one of them `NOT JUDGED`, says in one line what could not be measured, and exits
2. For the two PO cases, run it again while a PO turn is running.

When no turn is running and a number is still wanted, `--poll-idle-session` measures beside a
stand-in: of the open sessions, the one whose JSON is largest — the heaviest read the page's poll
makes on this installation — polled on the same cadence. The output names it as a `SUBSTITUTE`
and every concurrent label carries `substitute poll`, so such a run is never read as the DoD
scenario itself. An installation with no open session at all still exits 2.

The PO poll needs this installation's PO token (`DATA_DIR/po-web-token`, mode 0600), so run the
command as the runtime user. The data directory is resolved the way the product resolves it:
`--data-dir`, then `SECRETARY_DATA_DIR`, then `SECRETARY_INSTANCE`, then the instance the CLI
itself defaults to (`secretary.onboarding.DEFAULT_INSTANCE`, read through
`secretary.config.instance_data_dir`). That last step is what makes the one command above work in
an ordinary checkout shell, which does not inherit the service unit's environment. The output names
the directory it resolved and which of those four rules chose it.

## Record reconciliation and controlled divergences

Before advancing cards, every production tick walks the dispatcher records whose card is not among
the active (In progress / Validate) cards, re-reads that card from the board immediately before
acting, and drops the record if the card really is out of the cycle (`record-removed` with the
reference and state). It touches bookkeeping only: the workspace and terminal stay, and dealing with
them is the PO's decision. If the board is unavailable the record is left for the next tick.

A controlled divergence records a board answer the dispatcher did not expect (a claim mismatch and
similar) with expected and actual values. While its card stays active it is open: `status --json` and
`doctor --json` list it and `doctor` raises an unresolved-controlled-divergence finding, including
under `--offline`. The same reconciliation pass closes it, with time and reason, once the card leaves
the active cycle.

`dispatcher.divergences` carries the open count, total and open items (reference, reason, open time).
`dispatcher.reconciliation` carries the tracked record count, the last tick finish time and the last
reconciliation pass time (`null` until a pass has run).

## Connecting a project: gate and stale-input recovery

Stage contract, identity fields and re-onboarding semantics are in
[Protocols](PROTOCOLS.md#connecting-a-project). This is the order of work.

### Identity and mutable binding fields

Identity is `id`, `repo`, `adapter`, `default_branch`, repeated verbatim by the draft, the provision
task and the gate result; the provision result carries only `id` and `adapter`, and provisioning
rejects a mismatch as foreign. `plane`, `policy.code_concurrency` and the other mutable fields carry
over on a repeat `project add`, so refreshing a draft does not reset routing.

`project add` writes no `orca_binding`: a new project runs on local-pty heads in git workspaces.
`orca_binding` is optional legacy (A20 step 8). An existing one is kept; only curator routing reads
it, and card placement never does (secretary-1722).

### Stale input or an invalid schema

Validity is checked first, freshness second.

A schema-invalid input never mentions HEAD: `project add` answers `draft.invalid`, and
`provision-*` and `gate` answer `draft_invalid` and publish nothing. Errors name a schema path. A
failed `project add` prints diagnostics but writes nothing; fix the source the errors name.

Stale input means the default branch gained a commit after the draft was written.
`provision-start` and `provision-apply` answer stale with the expected and actual scanner heads; the
gate publishes a stale result with a `stale.input` finding. The gate reports a conflict for other
desyncs: provisioning not drafted, an unreadable or invalid canonical adapter, or an enabled binding
with no matching passed result.

To tell them apart, compare the scanner head in `adapter-drafts/<id>.yaml` with the tip of the
default branch. Different: stale, recover below. Equal and still refused: the named artifact is at
fault; do not loosen the guard, schema or policy.

Five `not-run` checks appear only when the gate on a fresh disabled draft saw HEAD move before building
its worktree; a stale result published mid-run keeps completed checks. A stale result on an enabled
binding exists only in command output.

### Refreshing a disabled draft

Do not edit instance files by hand; each stage rewrites its own artifacts.

```bash
python3 -P -m secretary project add PROJECT_PATH --instance "$INSTANCE"
python3 -P -m secretary project provision-start PROJECT_ID --instance "$INSTANCE"
# the provision agent writes result.yaml next to task.yaml, taking run_id and scanner head from the task
python3 -P -m secretary project provision-apply PROJECT_ID --instance "$INSTANCE"
python3 -P -m secretary project gate PROJECT_ID --instance "$INSTANCE"
```

A clean run: `project add` prints an ok scanner status and pending provision; `provision-start`
answers `task_ready` with the `task.yaml` path; `provision-apply` answers `drafted` with the binding
disabled; `gate` answers `passed`. Exit 0 only for success; any refusal exits 1.

- `project add` rescans. If HEAD changed, provision and gate state reset to pending and the stale
  canonical adapter is deleted in the same transition. Uncommitted project changes are not read.
- `provision-start` is idempotent per run id (a digest of identity, scanner head and onboarding
  cycle).
- `provision-apply` reads `--result PATH` or the default path, rejects a foreign run id or scanner
  head, publishes the canonical adapter and keeps the binding disabled.
- `project gate` builds a temporary worktree at the recorded head, runs setup, smoke and validation,
  and is the only stage that enables the binding.

`project add` on an enabled binding refuses ("existing binding is enabled"). Run `project gate` on it
first: a stale input clears the enable and returns the project to the disabled state recovery starts
from.

A disabled binding on another adapter (typically `adapter: inventory-only`) is moved onto the
project's adapter by `project add` and stays disabled; its provision and gate state reset, a stale
`adapters/<id>.yaml` is deleted. An enabled binding on another adapter refuses, with `--re-onboard`
too ("existing binding has conflicting adapter").

### Re-onboarding an enabled legacy project

A project with `enabled: true`, a canonical adapter and no draft, provision run or gate result makes
the gate refuse with "enabled binding has no matching passed gate result". `--re-onboard` is the only
supported way out; do not edit the binding or adapter by hand:

```bash
python3 -P -m secretary project add PROJECT_PATH --re-onboard --instance "$INSTANCE"
python3 -P -m secretary project provision-start PROJECT_ID --instance "$INSTANCE"
python3 -P -m secretary project provision-apply PROJECT_ID --instance "$INSTANCE"
python3 -P -m secretary project gate PROJECT_ID --instance "$INSTANCE"
```

Identity must still match and the binding must validate; otherwise the command writes nothing and the
enable stays. `plane`, `policy`, `remote` and `orca_binding` carry over. On an already disabled
binding the flag does nothing.

A refusal or I/O error restores every touched file. After a crash or kill, rerun:

- killed after the draft, before the binding: the enable is still in place; rerun `project add
  --re-onboard`;
- killed after the binding, before the adapter delete: the binding is disabled; a plain `project add`
  deletes the stale adapter.

### Verifying the result

Read the three artifacts:

```bash
cat "$INSTANCE/gate-runs/<project>/<run-id>/result.json"
cat "$INSTANCE/projects/<project>.yaml"
cat "$INSTANCE/adapter-drafts/<project>.yaml"
```

A passed result has empty findings, the four identity fields, scanner head and provision run id, the
adapter digest, and five passed checks: `clean_worktree`, `setup`, `smoke`, `validation`,
`artifact_policy`. The binding holds the identity and `enabled: true`; the draft's gate block is
passed with the same checks.

Verify by reading, not by running `project gate`: on an enabled binding it can change state.

- HEAD and adapter digest unchanged: returns the published passed result, exit 0, no change.
- HEAD moved, or the adapter was rewritten: clears the enable, prints a stale result, exit 1.

A clearing rewrites only the binding (disabled) and the draft (gate block failed, `stale.input`, five
`not-run`); the older passed result file stays on disk and the printed stale object may still show
passed checks. A result file describes its run, not current freshness.

### What this lifecycle does not prove

The gate does not check forge configuration. Without an explicit validation command it runs `git diff
--check HEAD`; a passed validation does not mean branch protection exists.

The mechanical gate reads `validation.required_checks` from the adapter:

- set: only those names (Actions check-run name or legacy status context) colour the card; missing
  or unfinished leaves it pending for the pending watchdog, a failed one makes it red, all successful
  make it green. Other checks do not matter;
- unset: every check on the SHA counts and any failure makes it red.

## Starting a sprint

A person starts a sprint through an interactive secretary session using the secretary role skill
`open-sprint` (delivered by `secretary role-skills sync`, in both Claude and Codex targets). The skill
gathers live context, checks that no other open sprint holds the needed repositories, interviews on
unresolved product forks and fixes a checkable Definition of Done. The goal is the person's choice.
A sprint needs its Product, at least one open Issue and at least one reserved registered project; an
installation holds one open sprint unless [two open sprints](#the-two-sprint-pilot) are enabled, and a
project another open sprint reserves is a resource conflict.

```bash
python3 -P -m secretary sprint create --role po --actor <actor> \
  --goal "<one sentence>" --dod-file DOD.md \
  --product <product-id> --issue issue:<ID> --project <project-id> \
  --observer <head-profile|none> \
  --repository <repo> [--repository <repo>]
python3 -P -m secretary sprint show --ref sprint:<ID>
python3 -P -m secretary sprint status --ref sprint:<ID>
```

After that the sprint is not driven by hand: the production tick launches the observer head,
intervention goes through [comments on the entity](#a-po-comment-on-a-running-sprint), and status is
read with `sprint status`, `sprint list` and `task list --sprint`. The sprint entity is checkpointed
and restored with the cards ([Recovery](RECOVERY.md#what-the-checkpoint-contains)).

Goal, Definition of Done, repositories, status, budget, current card and resume are entity fields; a
knowledge document holds only the "why" and a pointer to the sprint reference.

## What is running right now

```bash
python3 -P -m secretary sprint list                      # every sprint, with what each is doing
python3 -P -m secretary sprint list --status open        # only the ones that are open
python3 -P -m secretary sprint status --ref sprint:1431  # one sprint, plus its own fields
```

Both are reads over the same protocol operations and print one JSON document; the listing's
`sprints.items` entry and the watched sprint's `work` are the same object. Read it in this order:

1. **`status` and `current_task`.** Read `current_task.live`: a closed or stopped sprint keeps the
   card it ended on with `live: false`.
2. **`waiting.state`** (`working`, `waiting`, `blocked`, `ended`, `unknown`) with `waiting.reason` and
   `waiting.source`. `unknown` means the card is in an active column and whether a head is behind it
   could not be established; the reason still names the column.
3. **`checks`** — the recorded mechanical gate for the current card: `green` (with
   `gate.attested_sha`), `not_green` with reason, `unknown`, or `not_applicable`. Nothing is re-run.
4. **`decision`** — the observer's last resume `entry` and its `freshness`.

### Reading an answer that is only partly available

Every section carries a `source` (`available` with read time, or `unavailable` with reason and
evidence age) and `source.name`. `unavailable` means "nobody could say", never "nothing". The top-level
`cards.source`, `journal.source`, `liveness.source` and `installation.source` say which sources
answered. Which section each source can take away is in
[Protocols](PROTOCOLS.md#one-place-says-which-source-answered) and
[Protocols](PROTOCOLS.md#what-a-sprint-is-doing).

- `checks.state: unknown` with an `unavailable` source: production state unreadable; with an
  `available` source: no dispatcher record for the card yet. It never means the gate failed.
- With an invalid config, an explicit `--data-dir` and a reachable board still answer; without
  `--data-dir` the command exits `1` with `backend_unavailable`.

Exit statuses: unknown sprint or malformed filter `2` (`not_found` / `validation`), a refusing source
`1` (`backend_unavailable`). `secretary sprint show --ref` reads the entity record, comments included.

## What was commanded, and what became of a request

Both are reads: they write nothing and never re-send, retry or repair. Contract in
[Protocols](PROTOCOLS.md#what-has-been-commanded-and-what-became-of-a-request).

### The last commands, across everything

```bash
python3 -P -m secretary web-read commands --instance ~/secretary-instance
python3 -P -m secretary web-read commands --instance ~/secretary-instance --limit 20
python3 -P -m secretary web-read commands --instance ~/secretary-instance --json
```

One line per command, newest first, across cards, sprints, products and issues: who, action, entity,
result. Pass a page's `next_cursor` as `--cursor` for older commands; `(more)` (`has_more`) appears only
when the limit cut the page. `commands: unavailable (...)` with `items: null` means the audit could not
be read; an empty history is `items: []`.

### What happened to a request id

```bash
python3 -P -m secretary web-read request --instance ~/secretary-instance --request-id ID
```

Use the `web-read request` form when a command failed, timed out or was interrupted, instead of
running it again to find out:

| answer | what to do |
| --- | --- |
| `committed` | nothing. If `staged` is true beside it, run `secretary task reconcile-audit`; the operation itself is done |
| `pending` | repeat the operation **with the same request id**; a new id starts a second operation |
| `not_found` | the installation never saw it; safe to send |
| `unknown` | the audit could not be read; repair the journal and ask again. Not `not_found` |

`--json` carries the operation-identity table ([Protocols](PROTOCOLS.md#operation-identity-in-one-place)).
Both commands exit `2` with `validation` for an invalid config, a foreign cursor or a missing request
id, `1` with `backend_unavailable` if the layer could not run.

## A PO comment on a running sprint

A PO intervenes in a running sprint with a comment on the entity, not by editing its cards.

```bash
python3 -P -m secretary sprint comment --ref sprint:1431 --role po --actor <actor> \
  --request-id po-2026-09-06-slow-down --body-file NOTE.md
```

Keep `comment_id` from the output: the read below takes it. `saved: false` means this request id's
comment already existed and nothing new was written or woken. Keep `--request-id` for retries; without
one each run is a new comment. Reusing an id with a different body, sprint, role or actor is refused
with `validation`, exit `2`. A closed or stopped sprint accepts a comment too
([Commenting after the close](#commenting-after-the-close)).

### Reading what happened to that comment

```bash
python3 -P -m secretary sprint comment-delivery --ref sprint:1431 --comment-id evt_<...>
```

It only reads; redelivery belongs to the production tick. States are defined in
[Protocols](PROTOCOLS.md#what-happened-to-a-comment).

| `delivery.state` | what to do |
| --- | --- |
| `saved` | wait for the tick |
| `waiting` | wait; `batch.stage` says which stage |
| `handed_over` | nothing |
| `error` | the dispatcher retries; investigate the head if `batch` failure counts keep climbing |
| `not_deliverable` | nothing; the sprint has ended |
| `unknown` | read `delivery.source` and `delivery.reason` before concluding anything |

`handed_over` means the batch covering the comment was acknowledged, not that the observer read or
acted on it; `acceptance.established` is always `false`. To judge that, read the next resume entry
(`secretary sprint status --ref sprint:ID`, `decision.entry`). Only a `po` comment wakes the observer;
other roles' comments ride along with a later significant event.

## Closing a sprint

Write two files first.

**The decisions file** states what became of every declared issue and every card outside Done; a
close missing one is refused before anything is written. Shape and vocabulary:
[Protocols](PROTOCOLS.md#the-decisions-a-close-carries).

```yaml
issues:
  - ref: issue:1445748c5a2508769ef5
    verdict: resolved
    reason: the sprint's own cards landed it
  - ref: issue:9eee1d8ee505bc4ecdc2
    verdict: open
    reason: not reached before the sprint ended
cards:
  - ref: secretary-1573
    verdict: drop
    reason: superseded by secretary-1577
```

**The closeout file** is your prose account of what was achieved, what is unfinished and what you
decided about the remainder. The close writes it into `state/knowledge` and links it to the sprint.

```bash
python3 -P -m secretary sprint close --role po --actor <actor> --ref sprint:1431 \
  --request-id close-2026-09-07-1431 \
  --reason "the goal is reached far enough to cut the next sprint; the rest is deferred" \
  --decisions-file DECISIONS.yaml --closeout-file CLOSEOUT.md
```

`result.close` carries issue verdicts, card dispositions and the closeout path and commit;
`result.reservations` shows `released` projects (`held` should be empty); `result.sprint` the new
status; keep `event_id`.

**A close is not a completed Definition of Done.** Whether the goal was reached is what your decisions
and closeout say.

**`--request-id` is the retry handle.** A part-done close exits `4` telling you to repeat this request
id: run the identical command. It resumes without repeating committed steps or writing a second
closeout. A new id would start a second close. A repeat with other decisions, reason or closeout is
refused with `validation`, exit `2`.

| exit | code | what to do |
| --- | --- | --- |
| `2` | `validation` | fix the file the message names and rerun |
| `3` | `owner_conflict` (`live_work`) | settle the head still running on the disposed card, then repeat |
| `3` | `owner_conflict` (`close_conflict`) | amend exactly those entries to `already_closed` / `already_moved` with `actual`, repeat the same request id |
| `4` | pending | repeat the same request id |

The close stops no head. The next production tick stops the observer of a sprint that is no longer
open and drops its record; after one tick confirm with `secretary sprint status --ref sprint:ID` that
the observer reads `ended`.

```bash
python3 -P -m secretary sprint close-result --ref sprint:1431 --event-id evt_<...>
```

### Commenting after the close

`secretary sprint comment` on a closed or stopped sprint saves and audits the comment and does nothing
else: no reopen, no reservation, no head. `sprint comment-delivery` answers `not_deliverable`.

## The two-sprint pilot

The default is one open sprint per installation. A second is enabled by an instance setting. Admission
rules are in [Protocols](PROTOCOLS.md#the-open-sprint-limit): in practice the second sprint must touch
nothing the first touches. Each sprint declares its own observer.

Role `po` operations `close`, `reopen` and `record_budget` take the sprint reference as an argument and
do not check the observer binding, so with two open sprints an observer head declaring `--role po` can
close the other sprint; the audit records `role=po` with the observer's actor id.

### Enabling it

Add to `instance.yaml` and commit like any config change:

```yaml
open_sprint_limit: 2
```

Only `1` and `2` are accepted. Anything else keeps the limit at one and `secretary doctor` reports an
`open_sprint_limit` finding. The value is read at each admission; nothing restarts.

### Verifying it took effect

A clean `doctor` does not distinguish `2` from absent. Read the effective limit (config read only):

```bash
python3 -c 'import sys; from pathlib import Path; from secretary.sprints import instance_open_sprint_limit; print(instance_open_sprint_limit(Path(sys.argv[1])))' <instance>
```

`1` after writing `2` means a different file was read or the value was refused. The count refusal reads
`installation already has an open sprint` at limit one and `installation already holds its limit of 2
open sprints` at two.

### Reading a refusal

A refused `sprint create` writes nothing; fix the argument and repeat.

| refusal | what it says | what to do |
| --- | --- | --- |
| `resource_conflict` | `project(s) already reserved by an open sprint: <project> held by sprint:ID` | give the new sprint different projects, or close the holder |
| `resource_conflict` | `product <id> is already the product of open sprint sprint:ID; ...` | sequence the sprints, or use another Product |
| `resource_conflict` | `... declares no product, so it cannot be proven disjoint ...` | close the product-less sprint and open one that declares its Product |
| `resource_conflict` | `repository root <a> overlaps <b>, held by open sprint sprint:ID` | narrow the roots, or sequence the sprints |
| `resource_conflict` | `declares repository root '<value>', which is not an absolute path` | close that row or correct its `sprint_repositories` metadata |
| `sprint_conflict` | `installation already holds its limit of 2 open sprints: ...` | close one of the named sprints |

### What the pilot does not isolate

- **`pause drain` and `pause freeze` stop both sprints.** There is no per-sprint pause.
- **One production tick serves both sprints.** A bad tick or a stopped dispatcher is an outage of
  both, and the health line does not say which sprint caused it.
- **A tick that cannot read the sprint store fences the sprint-held work of both sprints.** It moves
  nothing identified as sprint work: projects the last successful pass recorded as reserved (a
  snapshot in production state) and cards whose metadata names a sprint. It reports
  `sprint_board_unavailable` as critical, naming the fenced sprints and projects, and clears when the
  store answers. Cards of no sprint keep running. Gap: a sprint admitted after the last successful pass
  is not in the snapshot, so an unlinked card already in a project it newly reserved can move during
  the outage. If the sprint store fails right after opening a sprint, `pause freeze` covers it; a
  `drain` covers only claims.

Per sprint: the declared observer and its bound writes, the observer fence when the store is readable,
the budget counter and hard stop, and the claim suppression a blocked card causes.

A sprint opened with `--observer none` has no observer at all: no resume entries, no parking; its
cards are bounded by the [no-observer ceiling](PROTOCOLS.md#the-no-observer-ceiling). Plan it as work a
person checks on.

### Rolling back to one open sprint

Lowering the limit closes nothing: an installation over its limit keeps both sprints ticking and
refuses every new `create` and `reopen`. A checkpoint taken with two open sprints cannot be restored
onto a limit-one installation (`restored open sprints are not admissible on this installation`).

1. Close the second sprint ([Closing a sprint](#closing-a-sprint)).
2. Confirm `python3 -P -m secretary sprint list --status open` shows exactly one.
3. Set `open_sprint_limit: 1` in `instance.yaml` (or delete the key) and commit.
4. Verify the effective limit is `1` with the read-back command.
5. Let one production tick write and push the checkpoint.

If the limit must drop before the close, lower it first and close afterwards; until then new admissions
and a restore of that window's archive are refused. Keep that window short.

## Dispatcher task Python isolation

Every new card workspace gets the dispatcher-owned `.secretary-task-env/venv`, separate from the
adapter-owned `.venv`. Before creating it the dispatcher appends any missing lines to the repository's
`info/exclude`: `.secretary-task-env/`, `/TASK.md` and `/state/checks/`. Projects need no `.gitignore`
entries; linked worktrees share the file, and the entries stay after cleanup.

When the adapter declares `broad_check` without `broad_check.interpreter`, the candidate's `.[dev]` is
installed into this environment, so worker and reviewer tools and the inner broad suite resolve there.
That install may need package-index access; an unavailable index is a bring-up failure, never
permission to install into the production virtualenv. An adapter with no `broad_check` gets a bare
environment. Adapter setup runs with neither virtualenv active and the production venv off `PATH`. A
retained older workspace gets the environment on its next rework or review launch. Never run candidate
installs against `PRODUCT_ROOT/.venv`, and never use `PYTHONPATH` to hide or repair an editable install
that points elsewhere.

Gate, release and cleanup accept an absent namespace and fail closed on an existing unowned one. The
owner record is written atomically before population; a valid owner without a `ready` marker is
resumed by the next prepare. A namespace with no valid owner is not adopted: if it holds no operator or
adapter data, remove only that worktree's `.secretary-task-env/` and retry bring-up; if uncertain, keep
the worktree and escalate. Never fabricate an owner record.

At prepare, launch, gate, release and before removal the dispatcher probes the production interpreter.
A failure (`interpreter_unavailable`, `missing_import`, `wrong_root`, `workspace_targeted_editable`)
blocks the card and retains its workspace. Inspect the reported interpreter, root, import origin and
metadata target, then repair from the registered production checkout only:

    PRODUCT_ROOT/.venv/bin/python3 -m pip install --no-deps -e PRODUCT_ROOT

Substitute the exact registered root for both occurrences. Do not restart or kill heads, rewrite task
metadata or delete the retained checkout as part of this repair.

`workspace_targeted_editable` covers both workspaces roots: the Orca workspaces root of A20 steps 8
and 11 (`SECRETARY_DISPATCHER_WORKSPACES_ROOT`, default `~/orca/workspaces`) and `DATA_DIR/workspaces`,
where git-managed card and observer worktrees live. A workspace's owner is read from its path, so the
real dispatcher refuses to start (`workspace_roots_overlap`, naming both paths) when the two roots
are equal or one is inside the other. Point `SECRETARY_DISPATCHER_WORKSPACES_ROOT` or the instance
`data_dir` elsewhere so the two are disjoint.

## Sprint observer heads

The production tick keeps one observer head per open sprint. Observers claim no cards and use no
project slot. Observer fence and declared-observer contracts are in
[Protocols](PROTOCOLS.md#the-observer-fence) and
[Protocols](PROTOCOLS.md#the-declared-observer); vitality policy is in [Head vitality](HEAD_VITALITY.md).

While a sprint is open its observer is the only writer of the sprint's cards on its reserved projects. To
intervene on such a card, the PO passes `--sprint-override` and a non-empty `--sprint-override-reason-file`
to `secretary task create`, `move` or `edit`; the reason goes to the audit. A PO card linked to no sprint
needs no override; on a reserved project the dispatcher admits it only as `research` or `infra`, and
blocks a `code` card at admission with `sprint-reservation-blocked` naming the reserving sprint
([Protocols](PROTOCOLS.md#cards-outside-a-sprint)). Refusals: `sprint_write_forbidden` (names the
sprint), `sprint_guard_unavailable` (the sprint store could not be checked), `observer_sprint_mismatch`,
`observer_identity_unbound`. A running observer without a sprint binding (`bound: false` in
`status --json`) is stopped by the tick with `observer head predates the sprint binding` and relaunched
bound on the next tick; no operator step.

At the budget signal threshold the observer prompt carries a note to reconsider the plan. At the hard
threshold the sprint becomes `stopped`: the head is stopped, newly linked Ready cards are skipped, active
cards finish their cycle. `secretary status --json` shows each sprint under `installation.sprints.items`
(status, hard-stop reason, budget, resume freshness, observer state) and an unreadable board under
`installation.sprints.error`. Only `secretary sprint reopen --role po` continues a stopped sprint.

The observer profile comes only from the sprint's `sprint_observer` field (or `none`); a profile the
registry lacks is fenced, never launched on a default. The [head readiness](#head-readiness) gate runs
first. The head is launched through the role-environment wrapper in its own registered worktree cut
from a separate observer repository the dispatcher creates under the data directory; do not delete it.
An unknown directory at the workspace path is removed and recreated. Stopping a head kills the
workspace's terminals and removes the worktree registration; an already unregistered worktree counts as
stopped.

### Tick actions

Each sprint's decision appears under the `observer-reconcile` step:

- `observer-launched` — an open sprint without a record got a head;
- `observer-live` — alive, nothing done;
- `observer-waiting` — working, no durable event needs a turn;
- `observer-idle` — ready for input, nothing owed;
- `observer-nudged` — a committed linked-card event woke an idle observer (after a release that merged,
  the post-merge CI result is that event, not the Done);
- `observer-wake-pending` — a sent batch awaits acknowledgement;
- `observer-wake-waiting` — an event arrived while working; the next tick with a ready observer nudges
  unless exact provider progress shows the run advancing. `admission` says what the provider source
  answered; an unadmitted source is held to the unproven turn ceiling;
- `observer-wake-progressing` — the admitted provider cursor advanced; nothing is sent or stopped;
- `observer-wake-no-progress` — the admitted cursor is unchanged while busy; the three-observation
  ladder advances, no raw input is sent;
- `observer-redelivered` — a batch was sent again (observer ready without acknowledgement, or the
  acknowledgement deadline ran out); the original batch is kept;
- `observer-wake-deferred` — the wake failed; after `SECRETARY_OBSERVER_WAKE_MAX_ATTEMPTS` (3) failures
  the head is replaced (`observer-relaunched`);
- `observer-relaunched` — the head was replaced (dead pid, exhausted wake retries, or the no-progress
  ladder). A replacement over a quiet queue sets a launch cooldown;
- `observer-stopped` — the sprint closed or vanished; head stopped, record dropped;
- `observer-stop-failed` — the host rejected the stop or returned no terminal list; the record stays
  `stop-pending` and the next tick retries;
- `observer-launch-deferred` — resource not ready, role skill not delivered, bring-up failed, or an old
  terminal could not be closed; the reason is on the record and the next tick retries;
- `observer-adopted` — a launch intent from a dead tick names a live pid; that head is accepted;
- `observer-launch-pending` — a launch intent is still inside its pid-wait window;
- `observer-launch-skipped` — a drain is in progress; a deferred record is created so the sprint is
  visible, and the head launches after `resume`;
- `sprint-board-unavailable` — the sprint store could not be read; no live head is stopped.

Timers:

- `SECRETARY_OBSERVER_ACK_DEADLINE_SECONDS` (30 minutes) — how long one sent batch may stay
  unacknowledged before redelivery, measured from the send.
- `SECRETARY_OBSERVER_UNPROVEN_TURN_CEILING_SECONDS` (15 minutes) — for a record whose provider source
  never got admitted (unbound, foreign, unreadable). Past it the delivery takes the wake retries and then
  replacement, carrying the batch into the replacement's launch.
- `SECRETARY_OBSERVER_TURN_CEILING_SECONDS` (3 hours) — for records with no provider-progress source.
- A head with an admitted cursor has no ceiling; its no-progress ladder decides. An unbound Codex source
  is retried for binding on every poll under the launch-time rules.

An observer is ready only when its supervisor reports no turn open and no delivery in flight, and idle
only when it is ready and its last-output time (the supervisor journal's last event) is readable. A
delivery left in `delivery-intent` by a dispatcher that died mid-send waits for the deadline rather than
being sent twice. A card in Ready, In progress or Validate is never by itself an idle observer. Never
clear a composer with Ctrl-C, Escape, a key chord or raw terminal input.

### The observer role skill

The `observer` role's `observe-sprint` skill is delivered by `secretary role-skills sync` (the
`role-skills` step of `secretary upgrade`) and checked by `secretary role-skills audit --check`. If the
skill is not in the head's shell, the launch is deferred with a reason like:

```
observer role skill is not available to this head: observer/observe-sprint is not in the codex
skill directory (<root>/observe-sprint/SKILL.md); run `secretary role-skills sync`
```

The same reason appears when `skills/manifest.toml` has no `observer` target for that shell or is
unreadable. It shows in `secretary status --json`, `secretary sprint status` and `secretary dispatcher
production-observe`. Fix:

```bash
secretary role-skills audit --check
secretary role-skills sync
```

Both commands read the product manifest plus the optional `<instance>/skills/manifest.toml` of the
installation named by `--instance` (default `SECRETARY_INSTANCE`). A skill may ship one executable
`<skill>.sh`, linked into the operator's bin directory as `<skill>` (see `skills/README.md`).

Liveness uses the versioned launch-identity heartbeat. A missing file counts as alive during the
initial-output window. A live file whose identity does not match the record is
`heartbeat-identity-mismatch`: not adopted, not stopped, no replacement beside it.

### Audit and launch intent

Lifecycle events are staged before the host call and committed after it, keyed by sprint reference,
record generation and launch counter.

- `observer-launch-deferred` with a staging reason, or `observer-stop-failed` mentioning staging —
  storage failed first; nothing happened; the next tick retries.
- An outcome with a pending audit field (degraded) — the action happened but its event is pending:

```bash
secretary task verify-audit --instance INSTANCE     # .pending, .backend
secretary task reconcile-audit --instance INSTANCE  # repaired/unresolved
```

Both read the card audit, the `requests` table ([Board store](BOARD_STORE.md) §7.3). `reconcile-audit`
answers `0/0`; a staged row is resolved by repeating its own request id.

The launch intent is written to production state before the host call:

- `observer-launch-deferred` with an intent-not-persisted reason — state is not writable; fix the disk
  or permissions;
- a record in a launching state with a pending launch — the tick died mid-launch. The next tick resolves
  it from the pid file (`observer-adopted`, `observer-launch-pending`, or close terminals and relaunch).
  Nothing to do by hand.

### Worker and reviewer launch intent

Card heads use the same intent on every launch path (claim, rework, watchdog respawn, relaunch on
resume). Delivery contracts are in
[Protocols](PROTOCOLS.md#a-settled-head-is-not-a-delivered-prompt) and
[Protocols](PROTOCOLS.md#a-live-head-is-not-a-delivered-pointer).

- Launch-intent-unwritable (degraded) — no head was launched; fix disk or permissions.
- A non-empty intent after a dead tick — the next tick resolves it from the heartbeat: launch-adopted,
  launch-pending, or drop and relaunch into the reserved round. A live identity mismatch stays degraded
  and untouched.
- Launch-aborted (degraded) — the terminal exists but the launch failed; the card is not blocked and the
  intent with its handle is resolved next tick.
- `worker-launch-undelivered` / `review-launch-undelivered` (degraded) — the pointer was not accepted
  (`busy`, `blocked`, `update-modal`, `starting`, `unknown-dialog`, or `refused` when found in the
  composer). The launch is not adopted as a claim, whatever the pid. After
  `SECRETARY_LAUNCH_DELIVERY_MAX_ATTEMPTS` (5) the head is stopped and relaunched
  (`*-launch-undeliverable`). A stop the host will not confirm reports `*-stop-unconfirmed` and keeps the
  intent; nothing is opened beside an unstopped head. A report of `pre-delivery-starting` after bytes were
  written is the normal path for a head still starting.
- Codex update prompt: preflight sets `dismissed_version` in the runtime `CODEX_HOME` `version.json`,
  best effort, before the head starts. No delivery ever upgrades Codex.
- The reviewer starts as a second supervised process in the worker's git worktree.
- A record written while heads were Orca panes (its workspace an Orca worktree, or a head run on
  `orca-legacy`) is refused by every launch, delivery, stop and teardown with a
  `legacy dispatcher record` reason, and the card goes Blocked naming the record. Nothing is torn down;
  clear that checkout by hand once its heads are confirmed gone.
- A stop the host did not confirm is not a stop: no replacement, no Blocked move, no freeze listing until
  confirmed. Check the head with [head-status](#head-status-in-a-live-workspace): the stop is refused
  or the process ignores the signal.

State without reading a transcript:

```bash
secretary status --json --instance INSTANCE                    # .dispatcher.observers
secretary dispatcher production-observe --instance INSTANCE    # .observers
secretary pause-status --instance INSTANCE                     # .heads.observers, .state.stopped_observer
```

An observer row carries sprint, profile, state (`running`, `waiting`, `idle-grace`, `wake-deferred`,
`launching`, `deferred`, `stop-pending`, `pause-stop-pending`, `stopped-by-pause`, `pending`), pid
liveness, launch count, workspace, handle flags, last action, deferred reason, a delivery object, and
`wake_liveness`.

### An infrastructure bring-up outcome

A card blocked because a head never came up says so. Vocabulary:
[Bring-up outcomes](PROTOCOLS.md#bring-up-outcomes).

- On the card: the Blocked reason ends in `[bring-up outcome: class=infrastructure,
  cause=host_unavailable, stage=claim, head=worker, attempt=ATTEMPT_ID]`. Infrastructure causes:
  `launch_aborted`, `host_unavailable`; task causes: `workspace_contract`,
  `base_branch_contract`.
- In the tick: `failure_class`, `failure_cause`, `failure_reason`, `bring_up`, and `contract_refusal`
  for a broad-check contract preflight refusal.
- In the audit: the request id ends in `-infrastructure-blocked`.
- In the sprint: `budget.uncharged.infrastructure_blocked`; infrastructure outcomes charge no threshold.

`cause=workspace_contract` means the requeued checkout is gone or not the claimed worktree and branch;
`cause=base_branch_contract` means an integration base the project cannot integrate into or a seed the
remote lacks. Neither is fixed by relaunching.

Repair what the cause names (head, resource, adapter, checkout), then move the card out of Blocked with a
reason. The dispatcher schedules no retry; the observer decides, and a returned card is claimed under a
fresh attempt id. Before concluding a head is missing, ask [head-status](#head-status-in-a-live-workspace).

## Checkpoint push

The push runs every 30 minutes, fast-forward only, never forced. Contract in
[Recovery](RECOVERY.md#failure-and-divergence). A push failure does not stop work; the next window
retries.

`remote diverged` stops the push and raises the alarm. Divergence from a green publish interleaving with
the checkpoint clears on its own on the next tick. When the remote holds history in neither the reviewed
branch nor the local checkout, merge by hand:

```bash
git -C INSTANCE fetch origin
git -C INSTANCE merge --no-edit FETCH_HEAD   # or rebase, as appropriate
```

Once the remote is an ancestor of local HEAD, the next tick pushes and the alarm clears.

`dispatcher production-observe` (`checkpoint`) and `doctor` show last commit, last push, lag in commits
and minutes (age of the oldest unpushed commit), blocked-gate reason and divergence. `doctor` raises a
finding on divergence, a blocked gate, or lag above 60 minutes.

### A checkpoint blocked by a Product/Issue transaction

The checkpoint gate and board export refuse while a Product or Issue write is staged. Find and repair it
through the CLI; never move files under `board/product-issue-transactions/` or `board/pending-audit/`:

```bash
secretary product transaction list --data-dir DATA_DIR
secretary product transaction retry --request-id REQUEST_ID --data-dir DATA_DIR
secretary product transaction discard --request-id REQUEST_ID --data-dir DATA_DIR
```

`retry` first: it resumes the operation and commits its event. `discard` is for a released transaction
the backend never accepted; it refuses with `live_write` if the row or comment exists, and always refuses
typed pending events. A document already outside the released journal comes back with `secretary product
transaction adopt --path FILE`.

### A checkpoint blocked by duplicate card references

`board export is not restorable: ... duplicate references` stopped publication before touching the prior
pair or the Git index. Use the preview and exact-ID apply commands in
[Recovery](RECOVERY.md#repairing-historical-duplicate-card-references). Do not pick a row with `task
show`, and do not edit normalized files or board storage. After apply, retry the checkpoint and verify
its remote SHA before any recovery drill.

## An export whose sprint rows carry no observer

Restore validates the whole exported sprint set before its first write and refuses, by name, a row
without an observer value (damaged or taken before the field existed). Add the value to each named row
in the export's `state/board/sprints.json` and restore again:

```json
"observer": {"kind": "head", "profile": "<profile>"}
"observer": {"kind": "none"}
```

Use `none` for a row that ran without an observer. A closed row whose head is unknown takes
`{"kind": "historical", "profile": null, "source": "migration_unknown"}`; an open row may not. An open row
naming a profile the registry lacks is refused and repaired the same way. Forms:
[Protocols](PROTOCOLS.md#the-declared-observer).

## Recovery

The checkpoint and the full sequence are in [Recovery](RECOVERY.md#fresh-install-and-recovery). On a
clean replacement host:

```bash
sudo secretary bootstrap --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER
sudo secretary recover --instance-remote REMOTE --instance-dir INSTANCE --installation-user INSTALL_USER \
  --recovery-phrase-file PHRASE_FILE
```

The recovery command is `recover`, not `install`. Operator rules for a recovery that does not finish
cleanly:

- Rerun the identical `secretary recover` after fixing the reported external cause. Completed board and
  memory phases are skipped, existing repositories are untouched, and only missing projects and their
  host state are retried. Do not edit `recovery-progress.json`, project registry files or Git credential
  files.
- A non-empty partial target from an older release: inspect it, then remove it or choose a fresh
  `--instance-dir`. Existing repositories are never reset or replaced.
- `checkpoint-publication` degraded: the local commit is retained. Repair only the destination or
  credential and rerun. Never reset, rebase, force-push, delete progress, create an empty commit or use an
  ambient credential helper.
- Unsupported local divergence, no trustworthy merge base, contract mismatch or conflict cleanup: preserve
  the checkout and stop. Do not deepen, resolve with `ours`/`theirs`, reset, rebase, delete or publish from
  it.
- `restore-board` reporting an uncertain card batch: rerun the same command without deleting backend
  rows, pending audit, restore state or the request namespace. An oversized `create`, `metadata/state` or
  `closure` payload is a pre-write refusal: fix the named record first. A duplicate reference or
  conflicting content is evidence to preserve and investigate.
- `failed` project rows make the result `degraded` and non-zero while everything else completes; dispatch
  refuses those bindings before any head or worktree.

`bootstrap --empty`, `restore-board`, `memory reindex`, `reconcile apply` and `restore-reconcile` are
diagnostic primitives, not the runbook. `restore-reconcile` exits non-zero `degraded` while a project
checkout is unavailable; repair it through `recover`.

## Optional cold archive

`backup create` and `backup verify` are a manual tool with no timer, offsite transfer or `doctor` gate;
the archive contract is in [Recovery](RECOVERY.md#backend-aware-cold-archives).

```bash
python3 -P -m secretary backup create --instance INSTANCE --kind both
python3 -P -m secretary backup verify ARCHIVE.tar [--strict]
```

`create` writes an unencrypted tar into `backups/` (`core`, `full` or `both`). `board-store.env` and the
memory model cache are never included; staging files are `0600` and no password reaches argv or logs.
`verify` returns `0` on success, `1` for findings or strict warnings, `2` for an unreadable archive.

Legacy extraction is `secretary restore ARCHIVE.tar`. A `full` archive restores into a separately
provisioned, migrated, empty target of the same instance with a different database endpoint:

```bash
python3 -P -m secretary restore-postgres ARCHIVE.tar --instance TARGET
```

Neither command reconciles or starts processes.

## Auto-merging green cards

A green verdict on a card whose sprint declares an observer parks the card in Assessment
([Tasks](PROTOCOLS.md#tasks)) once the mechanical gate is green; the merge runs on the tick that performs
a recorded `release`. Red or pending gates resolve in Validate. A card with no observer merges on the
verdict's tick. Gate receipt rules are in [Receipt names](PROTOCOLS.md#receipt-names).

A release the dispatcher cannot carry out takes the card to Blocked with the failure; it never sends the
card back for rework.

On release the dispatcher:

1. Pushes the worker branch to the default branch, fast-forward only; a diverged default branch is
   rejected, never forced or resolved.
2. Fast-forwards the project's local checkout onto the new tip (for the product repository this deploys
   the checkout it runs from). A card based on another card's branch lands on that base; the checkout is
   refreshed only from the default branch, and a failed refresh there does not send the card back.
3. Stops the worktree's terminals and removes the worktree.

For the private instance repository, publishing uses the checkpoint writer lock and publishes only the
reviewed branch and locally known checkpoint history; foreign remote history stays a manual case. After
publishing, the remote default branch is merged into the local instance checkout. A tick that died
between publish and merge is repeated idempotently.

Teardown happens only on this path; parked and rework cards keep their workspace and branch.

A merge that landed opens a post-merge CI watch before the card reaches Done, and the observer is woken
on its result (`green`, `red`, `absent` or `timeout`), not on the Done
([Protocols](PROTOCOLS.md#post-merge-ci)). `secretary dispatcher production-observe` lists open watches
under `post_merge_watches`; each resolution is a `post-merge-ci` tick action, and the result is a
dispatcher comment on the card and on its sprint. `SECRETARY_POST_MERGE_CI_CEILING_SECONDS` (3600)
bounds the wait.

Kill switch: `SECRETARY_DISPATCHER_AUTOMERGE=off` disables push and fast-forward. The card still reaches
done and needs a manual merge. Default on.

## Pausing the pipeline

Pause contract: [Protocols](PROTOCOLS.md#pause).

```bash
python3 -P -m secretary pause-scope  --instance INSTANCE                  # what a pause would reach
python3 -P -m secretary pause drain  --instance INSTANCE --reason "why"
python3 -P -m secretary pause freeze --instance INSTANCE --reason "why"
python3 -P -m secretary resume       --instance INSTANCE
python3 -P -m secretary pause-status --instance INSTANCE
```

`drain` stops claiming Ready cards, dispatching background roles and launching observers for new sprints;
in-flight work, running heads and live observers continue. Use it to stop inflow.

`freeze` also stops live worker, reviewer and observer heads and the tick advances nothing. Workspaces and
uncommitted work are untouched. Use it when the host must be free now (backup, reboot, incident).

`resume` lifts the pause, relaunches frozen worker and reviewer heads in their workspaces with fresh
watchdog windows, and leaves a card that reported during the freeze to the next tick. Observers come back
through the next tick's reconciliation.

If the host refused to stop an observer during a freeze, the `pause` response warns with the sprints and
the record stays `pause-stop-pending`; the frozen tick retries and goes degraded if the host refuses
again.

Switching mode requires `resume` first; a repeat in the same mode is a `noop`. The flag is
`<data_dir>/dispatcher/pause.json`; background roles read a legacy mirror that `resume` removes only if
the pause wrote it.

### Read the scope first, then decide

```bash
python3 -P -m secretary pause-scope --instance INSTANCE
```

It writes nothing and reports: `extent` (pipeline-wide, no per-sprint pause), `target` (the flag,
production state and legacy mirror), `sprints`, `cards` (every board card with its holding sprint or
`null`), `heads`, and `modes` (what drain and freeze each stop).

Then decide and read the result:

```bash
python3 -P -m secretary pause drain --instance INSTANCE --reason "why"
python3 -P -m secretary pause-status --instance INSTANCE
```

The response carries `action` (`paused`, `noop`, `resumed`), `changed` and the pause state. A drain while
frozen is refused with exit 3. After a drain, `pause-status` shows every `stopped_*` list empty and live
heads `running`; `resume` after a drain restores nothing and says so.

If production state is corrupt, a pause that took still answers with its `action`, the complaint under
`warnings` and the unreadable section as `unavailable`; read `<data_dir>/dispatcher/pause.json` directly
and repair production state. A command that really failed exits non-zero and leaves the flag untouched.
For a resume of a freeze in that state, `restored` lists are `null`, not empty.

`action` is decided under the tick lock, so trust it over comparing `pause-status` before and after.

### Pause or breakage

`pause-status` carries `state` (mode, actor, time), `target`, and per-head lines in `heads.cards` and
`heads.observers`, each with its source:

- `running` — alive;
- `stopped-by-pause` — stopped by the pause, workspace intact, `resume` brings it back;
- `not-running` — no head and not stopped by the pause: not reached yet, or a break
  ([Waiting watchdogs](#waiting-watchdogs)).

A frozen tick answers `skipped` with the reason and a pause snapshot; the health probe answers ok. It
keeps the checkpoint cadence and due-push coordination.

### A freeze that lifts itself

A freeze by an allowlisted automation actor expires after the configured TTL (45 minutes by default) and
the next tick resumes it. A freeze by any other actor never expires. TTL zero disables auto-resume.
`state.auto_resume` in `pause-status` is `fresh`, `manual-or-unknown-actor` or `disabled`; a tick that
lifted a pause reports its age and the heads it brought back.

## Waiting watchdogs

The dispatcher waits for a worker report (In progress) and a review verdict (Validate). Vitality
observation and the recovery ladder are in [Head vitality](HEAD_VITALITY.md); the headless-card contract
is in [Protocols](PROTOCOLS.md#a-card-in-an-active-state-with-no-worker).

Each waiting tick reads the head as `head-status` does (`command_terminal_status`): its launch-identity
pid heartbeat, the exact-run provider cursor and its child processes, reduced to one vitality verdict
([Head vitality](HEAD_VITALITY.md#decision-path)). A heartbeat naming a gone process takes the stall
path: one respawn in the same workspace, then Blocked. A runtime that answers unavailable is not a dead
head; the waiting ceiling still runs as a fallback.

The launch-identity heartbeat, written by the launcher before `exec`, lives under
`SECRETARY_DISPATCHER_BODY_DIR` (default `/tmp`) with its leaf handoff; respawn deletes both first. A
matching live heartbeat is positive liveness; a dead one takes the stall path; missing or unreadable
keeps the output fallback; a live mismatch is degraded and never authorizes a close, stop, signal,
adoption or replacement. The raw command override has no heartbeat and stays on output checks.

Every fresh progress signal restarts the waiting window, so a ceiling measures silence, not task age. A
head that printed nothing since launch gets the short first-output window; TUI heads on an alternate
screen also count the session rollout file's modification time. First breach: one respawn; second:
Blocked.

A worker whose episode suspects or confirms a stall, with nothing landed for the round, first gets one
reminder per report round, delivered through its supervisor, to run the report command from its
`TASK.md` (`worker-report-prompted`, degraded). A suspicion never destroys: once the reminder is spent
the tick reports `worker-stall-suspected` (degraded) and waits. Under a confirmed stall, a spent
reminder, a head that cannot take one (exited, suspended or not addressable) or an unconfirmed send
takes respawn then Blocked. A head nobody could observe is never replaced on the clock: past the long
ceiling the tick escalates to the operator. The respawned worker gets the same `TASK.md`, commands and
generation.

This idle bounce is a degraded tick and turns `secretary automations health` red until a healthy tick follows.
The Blocked move after it is not degraded; the steward reports it as `new_blocked`. Every respawn writes a
board comment.

### A card in an active column with no worker at all

Before waiting, the tick checks there is a head to wait for. A card moved into an active column with
nothing running (typically a raw move out of Blocked) is settled in that tick:

- `headless-worker-replacement-launched` (ok) — a replacement runs on the retained checkout; the outcome
  and a card comment name workspace, branch, candidate SHA and dirty flag. Nothing was reset.
- `headless-worker-recovery-refused` (blocked) — back to Blocked with `recovery_error`:
  `workspace_missing`, `workspace_unbindable`, `workspace_unreadable`, `candidate_unknown` (repair on
  disk), or `round_already_answered` (the round already has an accepted report; moving the card back was
  the wrong move).
- `orphan-worker-heartbeat-unbound` (degraded) — a live heartbeat at this card's worker pid path cannot be
  bound. Nothing is launched or signalled; find out whose process it is first.

Returning the same card again gets a fresh answer. While unresolved, `secretary status` marks the attempt
`degraded` with `headless` details, and `secretary sprint status` lists it under
`work.degraded_cards.items`. A card sitting in In progress is not on its own evidence that anything is running.

### Watchdog settings

- `SECRETARY_INITIAL_OUTPUT_STALL_SECONDS` — first-output window, default 180.
- `SECRETARY_REVIEW_VERDICT_STALL_SECONDS` — verdict ceiling after first output, default 5400.
- `SECRETARY_WORKER_REPORT_STALL_SECONDS` — report ceiling after first output, default 21600.
- `SECRETARY_HEAD_IDLE_STALL_SECONDS` — no production effect since the wait tick moved onto the vitality
  verdict; the vitality thresholds do not read it (see `docs/HEAD_VITALITY.md`, Thresholds).
- `SECRETARY_LAUNCH_DELIVERY_MAX_ATTEMPTS` — ticks a head may hold an unaccepted pointer before relaunch,
  default 5.

The stall settings are read at check time; garbage or zero falls back to the default.

### Reports and verdicts

Heads write bodies to `/tmp/secretary-report-<ref>-<round>.md` and `/tmp/secretary-verdict-<ref>-<round>.md`
(directory from `SECRETARY_DISPATCHER_BODY_DIR`); files are left in place. The round is part of the
verdict request id.

A worker round ends only with a report under the request id the dispatcher issued, taken from the hidden
`<!-- report-round generation=N ids=... -->` line at the end of `TASK.md`. Do not edit that line. To report
for a worker by hand, copy the command from its `TASK.md`, ids included; a report under any other id is
written to the card and moves nothing.

`secretary task report` answering `audit_pending` means the comment landed but the audit did not: rerun
the same command unchanged (answers `replayed`) or run `secretary task reconcile-audit`.

## Background-role telemetry

```bash
python3 -P -m secretary automations health
```

One line per role: timer state and freshness of the last healthy tick. Expected state comes from
`host.components` of the bound `instance.yaml`; an explicit `enabled: false` prints `DISABLED` and is
neutral. An unreadable config prints an error. Non-zero exit: an enabled role is red or the config is
unavailable.

- `scripts/secretary-agent-gate.sh` runs every role through one environment and exit-code protocol
  (every role through `python3 -P -m secretary automations`, which injects the board ports steward and retro need). It
  resolves the checkout as `TA_RUNTIME_PYTHONPATH`, then `TA_SECRETARY_REPO`, then `$HOME/secretary`, and
  uses only that checkout's `.venv/bin/python3`. A `configuration error` naming the checkout means its
  source tree or interpreter is missing, non-executable or another venv's; it fails before precheck.
  Inspect the rendered units or `secretary doctor`, then repair as the owner from a healthy installed
  command:

  ```bash
  secretary upgrade --no-pull --product-root /absolute/path/to/selected/checkout
  ```

  Do not use system-wide `pip`, copy site-packages or point `PYTHONPATH` at another checkout.

- curator, steward and retro run logs live under `$TA_STATE/<agent>/`, or the data directory when unset.
  Healthy is the last event whose result is neither `error` nor `board-unreachable`.
- Curator harvest limits (`TA_CURATOR_MAX_TURNS`, `TA_CURATOR_MAX_INPUT_BYTES`, `TA_CURATOR_MAX_SOURCES`),
  routing and pending-batch rules are in [Protocols](PROTOCOLS.md#memory). Precheck exit 102 is a
  successful deferred tick. A lock file never needs stale-PID repair. An unversioned, stale, foreign,
  corrupt or cursor-only pending file is refused and left untouched.
- the `pipeline` line comes from the production dispatcher's tick telemetry: time, healthy or degraded,
  diagnostics. A degraded tick colours the line immediately; a Blocked card does not. A tick that died with
  an exception writes a failed record. A tick that never reached its state (lock taken, guard refused)
  writes nothing and shows up as missing freshness. A freeze is healthy unless the frozen tick failed again
  to stop an observer.

Readers resolve dispatcher state like the dispatcher: `--data-dir`, else `SECRETARY_DATA_DIR`, else
`data_dir` from the instance (a relative value resolves from `instance.yaml`, `~` is expanded). Setting
`SECRETARY_DATA_DIR` in `runtime.env` moves both writer and readers.

A continuous run of unhealthy ticks is one incident; the steward reports one unhealthy event (opening
reason, failed tick count, `retained_window` grouped by step/action and error code) and one recovery.
Deduplication uses monotonic incident/recovery counters and a telemetry `generation`; a changed
generation or a counter that moved backwards gives a telemetry-reset hit. The resource-flip signal reads
the dispatcher's readiness cache; an unreadable cache keeps the previous baseline.

## The local web transport

`secretary web-serve` serves the dashboard and card pages over the `web-read` and `web-run` operations.
It answers on loopback only. Routes and codes: [Protocols](PROTOCOLS.md#serving-the-pipeline-locally).

> **It is never published directly.** A non-loopback bind is refused in code with: "this service has no
> password, no TLS and no authorisation, and its routes start real heads on this installation, so it
> binds a loopback address only. External access is published by the guarded front instead (`secretary
> web-front`, DoD 5), which terminates TLS, checks a password and proxies here; this refusal is what makes
> that front the only way in". Do not weaken it and do not forward the port. Outside access is
> [the published web front](#the-published-web-front).

The packaged `secretary-web.service` runs it on `127.0.0.1:8787`. To run another by hand:

```bash
# start it in the foreground; Ctrl-C stops it
python3 -P -m secretary web-serve --instance INSTANCE

# a second one beside the first, or a different data plane
python3 -P -m secretary web-serve --instance INSTANCE --port 8788 --data-dir DIR

# head profiles from a registry other than the installation's own
python3 -P -m secretary web-serve --instance INSTANCE --heads-registry REGISTRY
```

| flag | default | what it is |
| --- | --- | --- |
| `--instance` | required | instance directory or `instance.yaml` |
| `--data-dir` | the instance's own | override the data plane, or `SECRETARY_DATA_DIR` |
| `--host` | `127.0.0.1` | bind address; refused unless every resolved address is loopback |
| `--port` | `8787` | bind port |
| `--heads-registry` | the installation's own | where `--profile` values resolve, or `TA_HEADS_REGISTRY` |
| `--offline` | off | collect installation health without inspecting the live host |

**Stopping it** loses nothing: cursors belong to browsers. It does not stop heads its runs raised; a run
is ended by `secretary web-run state --run-id RUN` when its result arrives or its deadline passes.

**Diagnosing it.** It logs one line per request on stderr. Direct reads:

```bash
curl -s localhost:8787/api/system | python3 -m json.tool | head -40      # the dashboard's document
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/api/tasks/REF   # 200, 404, 503 …
python3 -P -m secretary web-read system --instance INSTANCE              # the same read, no HTTP
```

An unavailable source renders as a marked block with reason and age, never an empty list; an unreadable
record under `<data-dir>/webproto/runs/` marks that section and the JSON route answers 503
`backend_unavailable` naming the file. If the document is right and the page wrong, the transport is at
fault; if both agree, the source is. A port in use fails the bind naming it; a non-loopback `--host` exits
2 with `validation`.

### The operator's screen

Every fact is drawn once. What the bottom bar carries on every page — the provider windows and the
doctor lamp — has no panel of its own anywhere, and a page repeats nothing its header already says.
Long text (a goal, a decision, a report body) is in the page once, in the reading face: held to two
lines and grown in place by `show more`, never a one-line preview followed by the whole text again.

The dashboard (`/`) has three parts that fail apart:

1. **pipeline** — running, drained or frozen (since when, by whom), the dispatcher phase, the number of
   running PO turns, and the opposite control (`Drain…` opens a reason and the button; `resume`).
2. **attention** — while installation health reports a problem, a banner names the first one and links
   to `/doctor`; health that could not be read is the marked block every unreadable source gets. The
   facts behind health (checkpoint, disk, memory, load, cards, attempts, last tick, failed units) are one
   collapsed `Installation` panel.
3. **open sprints** — one card per sprint: goal (two lines), the card in hand with its state, age, gate
   and title, the heads (observer, worker, reviewer) with model and effort, and the card budget as a thin
   secondary line — a spend, not progress.

A sprint page (`/sprints/{ref}`) has a `Now` panel (the card in hand, the gate, what the observer is
doing, the heads, the budget line), the observer's call (decision in prose, its reasons and rejected
alternatives behind a disclosure, the next step), and tabs for the cards by state, the Definition of Done
rendered from its Markdown, the rest of the last resume, and the issues. The side panel says what no chip
does: projects, repositories, the declared observer and whether it is up (the held head is named only when
it is not the declared one), and the worker and reviewer pins. Closing an open sprint is under `More
actions`.

A card page (`/tasks/{ref}`) is headed by the card's title and opens on its **Task** tab: the card's full
description, rendered from its Markdown. Beside it are the work (report, verdict, decision, result), the
timeline of transitions with the records that made them, and the raw event tail. The side panel's `Heads`
lists every run the card recorded, by role: the latest run of each role is a chip with the model that ran
it — the exact id the run reported (`claude-opus-5-5`, shown as `Opus 5.5`), else the configured one
marked as configured — its effort (five bars and a word; hollow bars when no effort flag was passed and
the CLI default applies) and whether its process is alive; the runs before it are one line each, with
their own model, effort and attempt. A run a local-pty supervisor held links to its read-only view: the
tail of its terminal and its journal.

Card pages carry a comment and a move with reason (a second reason past the sprint's reservation); sprint
pages carry a comment and, when open, the close. All post to the routes in
[Protocols](PROTOCOLS.md#routes) as role `po`, actor `web`. There is no browser `decide`. Every page reloads
every 30 seconds unless a field has focus or holds typed text — a half-written `/po` message is never
discarded by it, and a password field counts, so the `/po` token being typed in is never cleared by a
tick; the checkbox on the bottom bar turns the reload off. A page rendered as the answer to a POST — a refusal, normally — carries no reload at
all, because reloading one is the browser offering to send the submission again.

### The bottom status bar

Every HTML page the transport serves — the dashboard, sprints, projects, cards, `/history`, the sprint
form, `/po` and its sessions, and refusal pages too — ends in one bar fixed to the bottom of the viewport.
It shows what each provider subscription has left: for Claude and for Codex, every usage window the
provider reported, its remaining percentage and **how long is left until it resets** — `2d 12h left`,
`6h 45m left`, `42m left`, `less than a minute left`, or, for a reading whose moment has gone by,
`reset already passed`; a window whose reading carries no moment says `no reset time recorded` rather
than showing a dash. One provider is one group and reads as one: its name is the group's heading, a
rule separates it from the next provider, each window is a chip of its own (`5-hour 73% · 1h 6m left`),
and the percentage follows the window's name at the chip's own gap. A percentage is drawn as the layer
rounded it, with a trailing `.0` dropped: `73%`, and `95.4%` when the reading really is fractional.
The exact moment is not lost: it is the hover title of that element, as the ISO
UTC string the reading carried. The time left is counted from when the page was drawn, not from when
the reading was taken, so a reading served from the cache does not overstate what is left. The bar is
the one place the windows are drawn. A provider whose reading is stale or
unavailable is shown with that word, the reason, and no percentage at all: on a bar a number is read as
what is left *now*, so no reading is drawn as words rather than as a figure. An available reading taken a
while ago says how old it is. The bar's height is reserved under the page rather than overlaid, so it
covers nothing, the `/po` composer included.

Its data is the cached provider layer (`secretary.web.provider_usage`, a five-minute in-process cache).
**Rendering a page never adds a provider
call**: the transport hands the bar the cached read, so a hundred page loads inside one cache window ask
each provider once, and JSON routes, which render no page, ask nothing. A process built without the
provider layer, and a read that refuses, both still serve every page; the bar then carries the reason
where the numbers would be.

#### The doctor lamp, and the page behind it

The bar also carries a doctor lamp, at its left, on every page. It has exactly three colours and no
fourth, and each one is decided by a rule rather than by a reading of the sentences:

- **red** — the installation cannot be trusted to run work, or its health is unknown. Any of
  `unit.failed`, `unit.missing`, `checkpoint.blocked`, `checkpoint.last_failed`,
  `secret_store.key_unusable`, or
  `health.unreadable`.
- **yellow** — it runs, but somebody should look. Any of `pipeline.paused`,
  `dispatcher.divergences_open`, `host.inventory_unreadable`,
  `memory.index_missing`. A problem whose code nobody has classified is yellow too — never green.
- **green** — health was read, and it reports no problem at all.

Each problem carries that stable code beside the sentence a person reads, and the **code**, not the
wording, is what the colour is decided from (`secretary.webproto.reads.PROBLEM_SEVERITY`). Red wins
over yellow, and yellow over green: one red problem is a red lamp however many yellow ones there are.

**Health that could not be read is red and never green.** A reading that did not happen — an instance
that does not validate, a collector that refused — is reported as the problem `health.unreadable`,
with the reason, and the lamp goes red. An empty list is never drawn as a clean installation.

The lamp is a link to `/doctor` from every page. That page lists the current problems, each with its
code and its message, grouped by the severity that decides the colour, with the red group first; when
there are none it says so plainly; when health could not be read it says that, with the reason.

**Refreshing the lamp reads recorded state only.** It is one cached read of the same collector the
dashboard's attention banner and `Installation` panel use (`secretary.web.doctor`, a one-minute in-process cache over
`reads.health_snapshot` → `collect_status` with no sprints and no panel probes): this host's own
systemd inventory, production state, checkpoint snapshot, store findings and memory index. It runs no
live `secretary doctor`, opens no SSH to any host, and touches no provider credential or provider
endpoint. As with the provider half of the bar, rendering a page adds no collection: a walk over every
page inside one cache window collects once, and JSON routes, which render no page, collect nothing. A
process built without the doctor layer still serves every page, and its lamp is red, because health
that nothing read is unknown health.

The dashboard's banner and `Installation` panel read that same cached reading, not a collection of their
own: one cache, one window, so they and the lamp cannot disagree, and they are up to one minute stale
exactly as the lamp is. A cache refresh is one collection however many requests arrive during it (they wait
for it and share it), and one response draws its panel and its lamp from one reading even when the
window expires mid-request. A warm dashboard render starts no subprocess and opens no `board/*.ndjson`. For the same
reason `web-serve` runs the board store's git-exclusion guard (`board-store.env` untracked and ignored)
once at start-up instead of on every request; a refusal it finds there holds for the life of the
process, and a store repaired or created later is picked up by restarting `secretary-web.service`.

### Running a card through the installed service

The two POST routes, through the front, as `curl`. `~/.secretary-owner.curlrc` is a mode-0600 file with
`user = "owner:..."` and `cacert = "DATA_DIR/webfront/caddy/pki/authorities/local/root.crt"`, so the password
never reaches a command line or history.

```bash
F=https://HOST
K=~/.secretary-owner.curlrc

# 1. a card this installation may run: a registered project, no open sprint reserving it, Issues
curl -sS -K $K "$F/api/tasks/REF" | python3 -m json.tool | head -30

# 2. raise the worker. `request_id` is the client's, and it is what makes a retry safe
curl -sS -K $K -H 'Content-Type: application/json' "$F/api/runs/start" \
  -d '{"ref":"REF","request_id":"ID","profile":"WORKER_PROFILE","instruction":"..."}'

# 3. watch it. A reload of /tasks/REF resumes; so does the same GET from a kept cursor
curl -sS -K $K "$F/api/runs/RUN" | python3 -m json.tool

# 4. review it, by its result, once it has ended
curl -sS -K $K -H 'Content-Type: application/json' "$F/api/runs/review" \
  -d '{"request_id":"ID2","profile":"REVIEWER_PROFILE","worker_run_id":"RUN"}'
```

Profiles come from the installation's head registry and must declare `runtime = "local-pty"`; others are
refused by name. After a registry change, see [Updating the service](#updating-the-service).

Repeating a request with the same `request_id` and inputs returns the existing run (same run id, pid,
workspace); different inputs are refused. That is the recovery for a reload, reconnect or retry.

On a card page the state column is what the process did (`running`, `finished`, `process_failed`,
`source_unavailable`, `unknown`, with `(open)` or `(over)`); the outcome column is what it produced
(verdict, result summary, exit status).

### Opening a sprint from the browser

Routes `GET /sprints/new`, `POST /sprints` and `GET /sprints/{ref}`; contract in
[Protocols](PROTOCOLS.md#opening-a-sprint-from-a-browser). The form offers this installation's products,
open issues, registered projects and head profiles. Fill in goal and Definition of Done, tick at least one
issue and one project, choose the observer, and leave worker and reviewer on "the observer chooses" unless
a role must be pinned. It calls the same `sprint_create` operation as the CLI, as role `po`, actor `web`.
The browser does not offer `none`; use `secretary sprint create --observer none` for that.

"Start this sprint" is the create; the tick raises the observer. The sprint page says:

| what the page says | what to do |
| --- | --- |
| saved — no observer is up for it yet | wait for the next tick; `secretary sprint status --ref REF` agrees |
| running — an observer head is up | nothing |
| stopped — an observer was raised for it and is not alive | look at the dispatcher; do not resubmit |
| no observer — this sprint declared none | nothing |
| not established — this could not be read at all | production state unreadable; the sprint fields are still true |

`saved` long after a tick is a dispatcher question, not a create problem.

The form keeps one request id while open, so double submits reach one sprint. If it says the sprint exists
and its request did not finish, submit **the same form again** without reloading; a reloaded form is a new
request id and can create a second sprint. A refusal returns the form with your values, the reason in the
board's words, and a fresh request id (except the part-done case, which keeps id and values); the block at
the top says which.

A POST from another site is refused with 403 based on `Origin`. Clients sending none (`curl`, `secretary
web-run`, the diagnostics above) are unaffected; to imitate a browser send `-H "Origin: https://HOST"`.

### Updating the service

Code: `secretary upgrade` moves the checkout and its `web` step restarts and probes the transport
([Updating the published application to `main`](#updating-the-published-application-to-main)).

Head profiles: edit the canonical registry, then materialize:

```bash
$EDITOR ~/secretary-instance/heads/heads.toml
cd ~/secretary && python3 -P -m secretary upgrade --instance ~/secretary-instance --no-pull
```

Never edit `heads/heads.yaml`: it is a generated snapshot pinned by `heads/source.yaml`, and an edited one
is rejected by the tick. The web process caches the registry, so a regenerated snapshot is a `web` step
restart reason (`the head registry snapshot changed`). If the upgrade stopped before its `web` step, restart
by hand:

```bash
sudo systemctl restart secretary-web.service            # the front is PartOf= and comes with it
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/api/system   # 200
```

A `validation` refusal saying a profile "is not launchable" right after a registry change usually means
the process was not restarted.

## The published web front

`secretary-web-front.service` is Caddy (Ubuntu archive) terminating TLS and checking a password with
`basicauth`, proxying to the loopback transport. The bcrypt hash comes from the secret store. Commands and
guard contract: [Protocols](PROTOCOLS.md#publishing-the-pipeline-the-guarded-front).

**The address** is `https://HOST/` for each rendered `--site`; the account is `owner`. Plain `http://`
redirects.

**First visit.** The certificate comes from Caddy's internal CA, so browsers show a full-page warning
(Firefox *Warning: Potential Security Risk Ahead*; Chrome *Your connection is not private*,
`NET::ERR_CERT_AUTHORITY_INVALID`). The connection is TLS either way; accept the warning (*Advanced* →
*Accept the risk* / *Proceed*) and the password prompt follows.

**Trusting the root.** The root is `DATA_DIR/webfront/caddy/pki/authorities/local/root.crt` on the host.
The front never installs it anywhere. Copy it to the browser's machine:

```bash
# copy it to the machine the browser runs on
scp USER@HOST:secretary-data/webfront/caddy/pki/authorities/local/root.crt secretary-root.crt
```

Import it as a trusted **certificate authority**: Firefox *Settings → Privacy & Security → Certificates →
View Certificates → Authorities → Import* with *Trust this CA to identify websites*; macOS Keychain Access
(*System* keychain, *Always Trust*); Chrome on Linux *Settings → Privacy and security → Security → Manage
certificates → Authorities*. Skipping this is fine: only the warning changes.

### Setting or reading the password

Values never travel through argv:

```bash
# the owner types their own, and it is read from stdin
python3 -P -m secretary web-front set-password --instance ~/secretary-instance --stdin

# or the product generates one from `secrets` and stores it
python3 -P -m secretary web-front set-password --instance ~/secretary-instance --generate
```

Read the current one back (writes a mode-0600 env file outside the repository):

```bash
python3 -P -m secretary secret materialize --instance ~/secretary-instance --target file
cat ~/secretary-data/webfront/owner-password.env      # SECRETARY_WEB_FRONT_PASSWORD=...
```

A new password takes effect after render and restart:

```bash
python3 -P -m secretary web-front render --instance ~/secretary-instance \
  --site https://HOST [--site https://ADDRESS ...]
sudo systemctl restart secretary-web-front.service
```

### Starting, updating and stopping

```bash
sudo systemctl status secretary-web.service secretary-web-front.service
sudo systemctl restart secretary-web-front.service       # after a render
sudo systemctl stop secretary-web-front.service          # off the public interfaces, now
python3 -P -m secretary status --instance ~/secretary-instance   # both units, enabled and active
```

The front is `PartOf=secretary-web.service`: restarting or stopping the transport does the same to the
front. Both are `Restart=always` with a three-second delay. Units roll out through `secretary reconcile
apply`; the Caddyfile does not, because it holds the hash — `web-front render` writes it under
`~/secretary-data/webfront/`, mode 0600, untracked. `ExecStartPre` runs `caddy validate`, so a broken
render fails the start instead of taking down a running front.

### Updating the published application to `main`

`secretary-web.service` runs the product from the configured editable checkout. A running process keeps
the code it imported at start but reads bundled schemas and other lazy files from the checkout as it is
now, so a checkout that moves under a running process can stop it answering. The supported update is one
command:

```bash
secretary upgrade --instance ~/secretary-instance      # `pull` fast-forwards ~/secretary onto main
```

Its `web` step runs after `pull`, `dependencies`, `head-registry` and `host` succeed, restarts
`secretary-web.service` (the front follows) and probes it. A failure in an earlier step stops the run before
the restart. Run the upgrade as the installation owner from the installed checkout
(`/home/dev/secretary/.venv/bin/secretary`), never from a task workspace: without `--product-root` it
materializes the configured checkout.

Restart reasons, from repository-relative changed paths:

| reason | what moved |
| --- | --- |
| `product code or dependencies changed` | `src/` (`secretary`, the background agents' `secretary.automations` included), `pyproject.toml`/`uv.lock`/`requirements.txt`, or a reinstall by `dependencies` |
| `bundled schemas changed` | `src/secretary/schemas/` |
| `a web unit file changed` | `secretary-web.service` or the front unit |
| `the head registry snapshot changed` | `heads/heads.yaml` regenerated |

| line | meaning |
| --- | --- |
| `changed   web: restarted secretary-web.service and probed http://127.0.0.1:8787/api/system -> 200; wrote web process receipt: ...` | replaced, answered, receipt written |
| `unchanged web: web process receipt verified: ...` | the active process generation matches the receipt for this revision and inputs |
| `skipped   web: secretary-web.service is not installed …` / `… is installed but not active; an upgrade does not start it` | optional unit; never started by upgrade |
| `failed    web: …` | see below |

An empty pull does not prove the process is current: a checkout the dispatcher advanced, or a missing
receipt, makes `--no-pull` restart once. `DATA_DIR/web/process-receipt.json` (mode 0600) is runtime
evidence of the process generation, revision and input hashes, excluded from backups. A missing or
mismatched receipt is stale, never `unchanged`; do not copy or edit it.

`--dry-run` compares `HEAD` with `origin/<branch>`, names the actions the target revision would cause
(`would restart secretary-web.service and probe ...`), and writes nothing.

`upgrade` restarts onto the dependency set `dependencies` left. That step reinstalls when the venv does
not match the checkout by its receipt ([Upgrade](#upgrade)); it does not inspect a venv edited by hand
behind a matching receipt.

#### When the restart or the probe fails

*The restart failed* (`restarting secretary-web.service failed: …`): nothing was probed; the old process may
or may not run.

```bash
systemctl is-active secretary-web.service
sudo journalctl -u secretary-web.service -n 50 --no-pager
```

*The probe failed* (`secretary-web.service restarted but the loopback probe failed:
http://127.0.0.1:8787/api/system did not answer 200 within 20s …`): the new process cannot serve. The
message gives the last answer (`HTTP 500` or a socket error). An unexpected failure is a `500` carrying a
reference; the journal line under it names the exception and call site:

```bash
sudo journalctl -u secretary-web.service -n 50 --no-pager     # the reference, the class, the frames
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/api/system
```

**Rollback** is the checkout plus `upgrade --no-pull` (`upgrade` is `--ff-only`):

```bash
git -C ~/secretary log --oneline -3                  # the revision to go back to
git -C ~/secretary switch --detach <previous-sha>
secretary upgrade --instance ~/secretary-instance --no-pull
```

`git switch` alone leaves the head-registry pin, units, dependencies and the memory and web processes as
the upgrade put them. `upgrade --no-pull` realigns them against the moved checkout: the venv and the memory
service through their receipts, the web transport through its own (the front is `PartOf=` and comes with
it). See [Taking the slice down, and rolling the application back a
revision](#taking-the-slice-down-and-rolling-the-application-back-a-revision).
For a target revision older than `bb43b5f` (secretary-1743), that revision's `upgrade` has no
dependency or memory receipts. After `secretary upgrade --no-pull`, also run
`"$HOME/secretary/.venv/bin/pip" install -e "$HOME/secretary[dev,memory]"` and restart
`secretary-memory.service`.

When an upgrade did not finish, or a service was restarted by hand, check whether the process is newer than
the checkout:

```bash
git -C ~/secretary rev-parse --short HEAD                        # which revision is checked out
git -C ~/secretary reflog show --date=iso -1 HEAD                # when that checkout last moved
systemctl show -p ExecMainStartTimestamp secretary-web.service   # when the process started
```

The process start (UTC) must be later than the reflog time (local time with offset). If the reflog has no
entry, ask the running process for a route only the expected code has, for example
`curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/sprints/new`. The head-registry pin in
`heads/source.yaml` (printed by `secretary status`) is written early in the upgrade and never proves the
upgrade finished or the process was replaced; a pin ahead of the checkout indicates a `git switch` rollback
without `upgrade`.

A request in flight during the restart fails; a reload a moment later reaches the new code.

### Taking the slice down, and rolling the application back a revision

**Taking the slice down** is `sudo systemctl stop secretary-web-front.service`; the transport and pipeline
keep running ([Rolling back to before this front existed](#rolling-back-to-before-this-front-existed)).

**Rolling the application back a revision** while staying published: move the tree, then run
`upgrade --no-pull` against it (the upgrade last):

```bash
git -C ~/secretary log --oneline -10     # `git -C ~/secretary reflog` says what was installed when
git -C ~/secretary switch --detach <revision>
secretary upgrade --instance ~/secretary-instance --no-pull
```

- `upgrade --no-pull` compares the venv and the memory service with the moved checkout through the
  dependency and memory process receipts ([Upgrade](#upgrade)), and reconciles both: it reinstalls the
  product with every extra the installation uses when the dependency manifests differ, and restarts the
  memory service when its revision, code, dependencies, model or pack differ. The `web` step restarts the
  transport through its own receipt. Do not install `[dev]` or restart units by hand.
- An `upgrade` that ends `status: failed` did only the steps printed before the failure; it rolls nothing
  back.

For a target revision older than `bb43b5f` (secretary-1743), that revision's `upgrade` has no
dependency or memory receipts. After `secretary upgrade --no-pull`, also run
`"$HOME/secretary/.venv/bin/pip" install -e "$HOME/secretary[dev,memory]"` and restart
`secretary-memory.service`.

A detached checkout makes the next upgrade's `pull` refuse by name. Return explicitly, the same way:

```bash
git -C ~/secretary switch main
secretary upgrade --instance ~/secretary-instance --no-pull
```

### A snapshot of the whole thing, in one go

No password, nothing written to the installation:

```bash
{
  date -Is
  git -C ~/secretary rev-parse HEAD
  git -C ~/secretary status --porcelain
  systemctl show -p ActiveState -p SubState -p ExecMainStartTimestamp \
    secretary-web.service secretary-web-front.service
  ss -ltnp '( sport = :8787 or sport = :443 )'
  secretary status --instance ~/secretary-instance
  secretary web-front check --instance ~/secretary-instance
  for path in / /api/system /api/tasks/secretary-1/events; do
    printf '%s ' "$path"
    curl -sk -o /dev/null -w '%{http_code} %{size_download}\n' "https://HOST$path"
  done
} 2>&1 | tee ~/secretary-data/webfront/snapshot-$(date -u +%Y%m%dT%H%M%SZ).txt
```

It shows the served revision and tree cleanliness, both units, that the transport is on `127.0.0.1:8787`
and only Caddy on `443`, the installation view, unguarded routes, and what an unauthorised client gets.

### Auditing what is exposed

```bash
python3 -P -m secretary web-front check --instance ~/secretary-instance
```

It parses the running configuration against every published route and prints `"unguarded": []`, or exits
3 naming the routes.

Over the wire, an unauthorised client must get 401 and no body:

```bash
for path in / /tasks/secretary-1 /api/system /api/tasks/secretary-1 \
            /api/tasks/secretary-1/events /api/runs/x; do
  printf '%s ' "$path"
  curl -sk -o /dev/null -w '%{http_code} %{size_download}\n' "https://HOST$path"
done
curl -sk -o /dev/null -w '%{http_code}\n' -X POST -d '{}' https://HOST/api/runs/start
```

Every line must read `401 0`. A `200` is an incident: `sudo systemctl stop secretary-web-front.service`
removes the public listener at once and leaves the pipeline running; then investigate.

### Rolling back to before this front existed

Nothing in the pipeline depends on either unit. In increasing permanence:

```bash
# 1. off the public interfaces, this second; the transport and the pipeline keep running
sudo systemctl stop secretary-web-front.service

# 2. rehearse or run guarded on loopback only — the same file, one line different
python3 -P -m secretary web-front render --instance ~/secretary-instance \
  --site https://HOST --bind 127.0.0.1
sudo systemctl restart secretary-web-front.service

# 3. permanently: disable both halves, then let reconcile remove the units
sudo systemctl disable --now secretary-web-front.service secretary-web.service
```

For 3, set `host.components.web.enabled: false` and `host.components.web-front.enabled: false` in
`instance.yaml` and run `secretary reconcile apply`. Delete `~/secretary-data/webfront/` if wanted; drop the
password and hash with `secretary secret remove --id web-front-password` and `--id
web-front-password-hash`. Remove Caddy with `sudo apt-get remove caddy`; `caddy.service` is masked so the
package never starts an unconfigured listener (`sudo systemctl unmask caddy.service` to undo).

### When it is unreachable

| symptom | what it means | what to do |
| --- | --- | --- |
| connection refused / times out from outside | the front is not listening, or the network is in the way | `ss -ltn '( sport = :443 )'`; `sudo systemctl status secretary-web-front.service` |
| the unit is `activating (auto-restart)` | `caddy validate` refused the configuration | `journalctl -u secretary-web-front.service -n 50`; re-render |
| `permission denied` binding 443 | the capability is not in effect | `systemctl cat secretary-web-front.service` must show `AmbientCapabilities=CAP_NET_BIND_SERVICE` |
| a certificate warning that will not go away | no trusted root | see *Trusting the root*; not a failure |
| 401 with the right password | the running configuration is older than the store | re-render and restart; `web-front check` prints the file the unit reads |
| 502 after the password | the loopback transport is down | `sudo systemctl status secretary-web.service`, then `curl -s localhost:8787/api/system` |
| a section is marked unavailable | a source below the transport refused | see *Diagnosing it* |
| every route answers `500` with `reference: <id>` | an unexpected failure escaped; a stale process against a moved checkout is the known cause | grep the reference in `journalctl -u secretary-web.service`, then *Updating the published application to `main`* |

SSH is unaffected by the front; if it is wedged, SSH in and stop it.

## Units

Templates are documented in [packaging/systemd/README.md](../packaging/systemd/README.md). Units are rolled
out by `secretary reconcile apply`; manual installation is neither needed nor a source of ownership. The
production dispatcher timer runs a one-shot tick. Memory, curator, steward and retro each have exactly one
scheduler owner.

### Production interpreter provenance

The dispatcher unit runs an isolated preflight before the `secretary` entry point, catching an editable
install that points at a task workspace (even a vanished one). A refusal exits non-zero before importing
candidate code, records its classification and metadata target in tick telemetry, turns `triggered-agents
health` red, and gives the steward one incident.

`secretary doctor --instance INSTANCE` reports `production_runtime_provenance` with the interpreter, product
root and offending target. The only supported repair is the command it prints:

```bash
PRODUCT_ROOT/.venv/bin/python3 -m pip install --no-deps -e PRODUCT_ROOT
```

Use Doctor's product root. Do not run `uv sync`, delete a task workspace, rewrite editable metadata, add
`PYTHONPATH`, attempt an automatic repair, or patch the unit by hand. The next valid tick closes the
incident.

## Upgrade

`secretary upgrade --instance <dir>` pulls a new product version and re-materialises the installation. It is
idempotent once materialized state, including an active web process receipt, is current.

```bash
secretary upgrade --instance INSTANCE --dry-run   # decide everything, write nothing
secretary upgrade --instance INSTANCE
```

Each step prints `changed`, `unchanged`, `skipped` or `failed`; the first failure stops the run:

| step | what it does |
| --- | --- |
| `pull` | `git fetch` plus `merge --ff-only`; a dirty checkout is refused |
| `registries` | read the skill manifest, instance overlay, head canon and memory pack; an unreadable or undeliverable registry stops the run before any write |
| `memory-pack` | materialize the shipped memory pack into the memory canon |
| `dependencies` | compare the venv with the checkout through the dependency receipt (tracked-manifest digest, extras, venv path); on a mismatch, a snapshot install or a wrong Ruff pin, `pip install -e <root>[dev,…]` with every extra this installation uses, then write the receipt |
| `dependency-provenance` | import `secretary`, psycopg, SQLAlchemy and Alembic with `-P` from the selected root and venv |
| `board-store-provision` | no-op before provisioning; otherwise verify/start the pinned `postgres:16` service and volume without rotating credentials |
| `board-store` | connect as owner and apply Alembic to the shipped head |
| `board-store-roles` | verify owner/app/read credentials, attributes and privilege boundaries |
| `memory-clients` | reconcile the `po_memory` MCP entries (Claude, `~/.codex`, the legacy Codex home and an existing `DATA_DIR/codex-home`, seeding what it lacks) without touching provider login state |
| `codex-home` | seed `AGENTS.md` and `config.toml` copy-once into `DATA_DIR/codex-home`; never `auth.json`, never the legacy Orca home ([Codex home](#codex-home-codex_home)) |
| `head-registry` | generate `heads/heads.yaml` and `heads/source.yaml` from the canon |
| `instance-packing` | keep the instance repository's local Git packing controls bounded, with implicit `gc --auto` off (`gc.auto=0`, `maintenance.auto=false`); packing runs from `secretary-instance-maintenance.timer` ([Recovery](RECOVERY.md#local-git-packing-controls)) |
| `head-registry-checkpoint` | commit only the generated pair under the writer lock and publish it fast-forward; an unavailable or diverged remote stops the upgrade naming the retained commit |
| `role-worktrees` | fast-forward role worktrees onto the base branch |
| `role-skills` | `role_skills sync` into shell skill directories |
| `host` | `reconcile apply`: units from `packaging/systemd` |
| `memory` | start a stopped memory service; restart an active one whose process receipt is missing, belongs to another process or binds another revision, source, dependency digest, `MEMORY_MODEL` or pack digest (or whose code, unit or pack this run changed); then a bounded `memory_list` read and a new receipt |
| `web` | for an active transport, verify its process receipt or restart, probe loopback, write the receipt after 200 |
| `verify` | repeat dry run; the second rollout must be a no-op |

Flags: `--no-pull`, `--base-branch`, `--product-root`, `--runtime-user`, `--json`.

`dependencies` and `memory` decide by installed state, not by what this run's `pull` moved, so a checkout
moved outside `upgrade` (a manual reset or pull, `--no-pull`, a recreated checkout) is caught by the next
run. Each line names what it compared, for example `unchanged dependencies: venv matches checkout (deps
sha256 1a2b3c4d5e6f, extras dev,memory)` or `changed dependencies: …: deps sha256 1a2b3c4d5e6f ->
4d5e6f7a8b9c`. The required extras are `dev`, plus `memory` when the memory unit is installed or active,
plus any declared extra whose distributions the venv already carries. The receipts are
`DATA_DIR/upgrade/dependency-receipt.json` and `DATA_DIR/upgrade/memory-process-receipt.json` (mode 0600,
excluded from backups). A missing, unreadable or malformed receipt means the work is done, never
`unchanged`; do not copy or edit them.

**First run after this change.** An installation has no receipts yet, so its first upgrade (the final
upgrade included) reinstalls the product into the venv once and restarts the memory service once, then
writes both receipts. The next run reports both steps `unchanged`.

The packaged systemd timers are the only schedule owner of the background roles (curator, retro,
steward). Before sprint:1459 they ran as Orca automations; upgrade no longer creates, repoints or
deletes any, and `doctor` does not report them.

When `pull` advances the checkout, the process re-executes `python -P -m secretary` from the pulled checkout
with the same arguments and changed paths, so steps new in that revision run in the same upgrade.
`--no-pull` runs the current schedule once; `--dry-run` fetches and reports without moving anything.

If `host` reports `unowned names in our namespace`, resolve it as in
[Ownership and fail-closed behaviour](#ownership-and-fail-closed-behaviour).

### Upgrading from another checkout

`--product-root` names the checkout to install; every step reads only it (skill manifest and roles,
`packaging/systemd`, agent specs, role worktrees, and its head canon when the installation owns none).
`secretary role-skills audit|sync --product-root <checkout>` delivers skills alone.

Without `--product-root`, install and upgrade materialize the configured checkout (`TA_SECRETARY_REPO`, else
`$HOME/secretary`), not the directory the command runs in. `install` and `recover` refuse a path with no
product. A first install from a checkout other than `~/secretary` names it with `--product-root`.

The selected checkout is written into the dispatcher unit as `TA_SECRETARY_REPO` and rendered into every
launched head's command line, so heads import the installed product.

### Path precedence

No absolute product path is shipped. First hit wins:

| what | order |
| --- | --- |
| the installation | `--instance` / `SECRETARY_INSTANCE`, else `~/secretary-instance` |
| the product checkout a head imports | `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the checkout an install or upgrade materializes | `--product-root`, else `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the product skill manifest | `--product-root`, else `SECRETARY_ROLE_SKILLS_MANIFEST`, else the configured checkout's |
| the checkout a launcher starts a role out of | `TA_RUNTIME_PYTHONPATH`, else `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the packaged units a plan or a doctor run compares against | the checkout named by the command, else the one `heads/source.yaml` recorded, else `TA_SECRETARY_REPO`, else `$HOME/secretary` |
| the account an upgrade materializes for | `--runtime-user`, else the owner of the instance directory |
| a skill's shell root | the manifest's `root`, expanded against the installation owner's home |
| a skill's command link | `SECRETARY_BIN_DIR`, else `<owner home>/bin` |
| a role worktree | `TA_WORKSPACES_ROOT`, else `<owner home>/orca/workspaces` |
| the role runtime env file | `SECRETARY_RUNTIME_ENV_FILE`, else `TA_RUNTIME_ENV_FILE`, else `<instance>/runtime.env` |
| the head registry a tick reads | `TA_HEADS_REGISTRY`, else `<instance>/heads/heads.yaml`, else the running checkout's default |

`~` in a shipped manifest and `$HOME` in a shipped entry point mean the installation owner's home, resolved
once per upgrade, so a repair run as root writes under the owner rather than `/root`. Skill sources resolve
beside their manifest. `secretary role-skills sync` run by hand uses the caller's home. Nothing falls back
to the checkout the running module was imported from; an offline `doctor` compares against the checkout
recorded in `heads/source.yaml`.

### The installation's head registry

A live tick reads only the installation's `heads/heads.yaml` and matching `heads/source.yaml` (canon,
checkout, revision, snapshot digest); a stale or incomplete pair fails before routing. Only `secretary
upgrade` moves and checkpoint-publishes that pair, so editing a product checkout's canon does not affect a
running installation.

An installation owns its registry by keeping `heads/heads.toml`; otherwise it materialises from the
product's small shipped default (a Claude and an OpenAI subscription, cross-family fallbacks, one default
per role, no installation policy). A present but unusable `heads/heads.toml` fails the upgrade by name.

`secretary status --json` returns `installation.head_registry` (`snapshot`, `canonical`, `canonical_owner`
`instance`/`product`, `product_root`, `revision`, `error`). `error` is set when the pin was never written on
this version or the snapshot is broken.

`[role_defaults]` routes worker and reviewer heads and the curator, retro and steward heads; it does not
route observers (`role_defaults.observer` only labels an observer record with no sprint to read). An
`automation.toml` `head` is a last resort. Packaged role units export `SECRETARY_INSTANCE` and their
`runtime.env` path; dispatcher-launched heads get both on their command line. `SECRETARY_INSTANCE` in a
`runtime.env` never overrides them.

### Manual curator routing in an instance canon

This is a deferred, manual operator procedure for an installation whose private
`INSTANCE/heads/heads.toml` already declares the Terra tier `profiles.codex-terra-high`. It changes only that instance
canon. Do not add the curator's profile choice, its model, or its account policy to
`src/secretary/runtime/heads.toml`: the product file remains the portable fallback for an installation
with no canon of its own.

Before changing the role default, record the current `role_defaults.curator` as `PREVIOUS_PROFILE`. Inspect the
existing `profiles.codex-terra-high` without editing it: it must remain a Codex profile with
`model = "gpt-5.6-terra"` and `effort = "high"`, and its declared `fallback` sequence must name existing profiles.
The fallback is instance policy. Record its current order and do not invent, delete, or reorder it as part of this
routing change. If the profile is missing, malformed, has a different model or effort, or has an invalid fallback,
stop. That is a separate canon-policy decision, not a reason to edit the portable registry or make a replacement
profile here.

Change the existing instance table only as follows:

```toml
[role_defaults]
curator = "codex-terra-high"
```

`secretary-curator.timer` is the sole scheduler owner when the curator component is enabled. The Orca curator
automation must remain disabled: it is a leftover of the schedule before sprint:1459, and removing it is a PO action
(A20 step 10). This installation's curator component must remain disabled for this deferred route change. Verify the latter read-only against the selected installation:

```bash
SECRETARY_INSTANCE=INSTANCE python3 -P -m secretary automations health
```

The output must retain the `DISABLED curator` line. Do not change `host.components.curator`, run `systemctl`, start or stop a service or timer, invoke the curator, run a production
baseline/backfill, write or delete a fact, reindex, or run a canary. Routing a role authorizes none of those actions.

After a separately approved instance-canon edit, materialize it manually with the normal instance rollout, for
example `secretary upgrade --no-pull --instance INSTANCE --product-root PRODUCT_ROOT`. Confirm with
`secretary status --json --instance INSTANCE` that the head-registry canonical owner is `instance` and that the new
snapshot was written. The routing assignment has no automatic rollout, shim, migration, or dependency step. The
routing change takes effect only for a later eligible scheduled run; it does not justify a
manual invocation. To roll back, restore `role_defaults.curator = "PREVIOUS_PROFILE"` in the same private canon,
leave `profiles.codex-terra-high` and its fallback untouched, repeat that same manual materialization, and confirm the
resulting instance snapshot. Do not delete the profile or alter scheduler ownership during rollback.

The role route does not widen the curator protocol. A fact-bearing pending batch remains bound to its curator workspace, run and
session identity, its selected-project or all-backlog selector, and its starting cursors. Replay or advance with a
different identity or selector fails closed.

A later intentional baseline is a separate manual operation. It requires one registered canonical project
or the reserved `review:po` selector,
an explicit actor, a non-empty reason, and exactly one current opaque cutoff or pending-batch identity. It cannot
bypass a pending record or use all-backlog mode. The baseline audit records the project, actor, redacted reason,
evidence identity, outcome, and hashed cursor identities/count only. Legacy line watermarks remain readable only through the released conversion
path; unversioned, stale, foreign, corrupt or cursor-only pending state, a changed source, an incomplete tail, or a
failed write is refused and left for manual resolution rather than guessed forward. The detailed protocol is in
[Memory](PROTOCOLS.md#memory) and [Project baseline settlement](PROTOCOLS.md#project-baseline-settlement).

A broken snapshot stops the tick and names the reason (missing table, wrong shape, unknown resource or
adapter, a role default naming a missing head). A process given `SECRETARY_INSTANCE` whose snapshot is
missing or unreadable fails on that path; the shipped registry is only for a checkout with no installation
selected. The dispatcher answers `invalid_heads`; the fix is `secretary upgrade`.

### Ownership and fail-closed behaviour

`reconcile apply` writes only what the managed manifest confirms. A name under the unit prefix that is in
neither the plan nor the manifest is a conflict, and any conflict aborts the run before the first write.
Resolve it:

- the unit is ours and matches the packaged file byte for byte:
  `secretary reconcile adopt --instance <dir> --logical-id systemd:unit:<name> --yes`;
- the name belongs to something else: list it in `host.foreign_units` in `instance.yaml`.

A differing unit is not adopted: remove it and let `apply` install the canonical one, or find out why the
host diverged. An `orca` record an older reconcile left in the managed manifest is kept, untouched
(A20 step 8).

Switch off a component in config, not by removing its unit:

```yaml
host:
  components:
    curator:
      enabled: false
      reason: "load shedding"
```

A disabled component whose unit is installed and owned is stopped and removed.

### Health suite

A deterministic gate before and after an upgrade:

```bash
secretary doctor --instance <dir>
secretary role-skills audit --check
secretary dispatcher production-tick --instance <dir> --probe
python3 -m tests.broad
```

The `secretary` commands check the installation; `python3 -m tests.broad` runs the `unit` and `component`
suites of the code it runs, not repository-wide discovery. This is an operator gate, not the test contract:
that is the dispatcher-owned exact-SHA CI run ([Testing](TESTING.md)). When an upgrade touches packaging,
recovery, memory, the local-PTY runtime or the board seam, run that suite directly, for example `python3
scripts/ci_test_shards.py packaging`.

`--probe` is a real dry tick: same lock, guards, card scan and decisions, but the first write aborts and is
reported as what the next tick would do.

### Worker-local broad receipt

Receipt ownership and travel are in [Receipt names](PROTOCOLS.md#receipt-names); broad-check handling in
[Protocols](PROTOCOLS.md#broad-check-handling).

```bash
python3 -m secretary check broad --module tests.broad
python3 -m secretary check show --module tests.broad
```

When the registered project's adapter declares a `broad_check` module, `secretary check broad --reuse` and
`secretary check show` run it with no flag; `--module` overrides. A project that declares none and passes
none is refused as `no_broad_check_module`. Task-packet commands use the registered production source and
interpreter with `-P`; when a contract omits `broad_check.interpreter`, the inner suite uses
`.secretary-task-env/venv/bin/python3`.

`check broad` streams output to stderr, exits with the check's status (`128+N` for a signal), and writes one
receipt under `state/checks/` in the workspace (ignored, never committed): check set and digest, working
directory, import provenance, timing, exit code, parsed verdict and counts, bounded output tail. A raw exit
code that disagrees with the runner's result is refused as `receipt_status_mismatch`.

Two shapes:

- `--module unittest` (with `--module-arg`) records working directory, interpreter and project package
  import. An adapter sets `broad_check.interpreter` (relative to the workspace unless absolute) and
  `broad_check.import_package`. Every registered project that gets cards must declare `broad_check`;
  otherwise `broad_check_not_declared`, here and at the dispatcher preflight. A checkout matching no
  registered project uses the CLI default (`module_contract.source: cli_default`, reason
  `no_project_binding` or `project_binding_disabled`). Adding `broad_check` changes the adapter digest, so
  run `project gate` again. An interpreter that cannot start gives `interpreter_start_failed`, exit 2, no
  receipt.
- `--command '<shell>'` records `origin: unobservable`, claims no import and is never reused.

The dispatcher's preflight refuses an unavailable or invalid adapter, a missing or incomplete `broad_check`,
and an absolute interpreter that cannot start, before any workspace or head, with the infrastructure class
([Bring-up outcomes](PROTOCOLS.md#bring-up-outcomes)). A relative interpreter is resolved later in the
workspace, which is why relative spelling is recommended.

A receipt may replace a run only when the check imported the configured package from this workspace. A
missing or unreadable record, an empty or unresolvable path, a path outside the candidate (for example via
`PYTHONPATH`), or an import from the interpreter's own environment such as `.venv/.../site-packages` is
refused for reuse.

A check is identified by its structured check set, not its rendering: `--module-arg 'one two'` and
`--module-arg one --module-arg two` are different checks.

`check show` runs nothing. It compares the recorded git tree id (tracked edits and untracked files included)
with the current one and exits non-zero when they differ. A truncated or edited receipt, a killed or
timed-out run, an unresolvable checkout, an import from outside the candidate, or a shape that attests no
import is "not usable". `load_receipt` also refuses result combinations no run could produce. `check broad
--reuse` skips the run exactly when `check show` would call the receipt usable.

### Head readiness

Before a worker, reviewer or observer launch the dispatcher probes the profile's resource from
`heads/heads.yaml`. Verdicts are cached in the data directory for 300 seconds:

```bash
secretary dispatcher resource-health --instance <dir>
```

- `ready` allows a launch.
- `unauthenticated`, `unavailable`, `exhausted` forbid it. A repeat worker launch on a taken card blocks it;
  an observer launch is deferred.
- `unknown` (unclassifiable or timed out) does not forbid a launch.
- `probe_broken` (command, interpreter or import missing) forbids a launch. Probes run with the
  dispatcher's interpreter directory first on `PATH`.

`secretary doctor` reports every resource's probe and names broken probes as findings, reusing a fresh
dispatcher verdict; `--offline` reports only what is recorded.

For a card in Ready, a forbidden verdict walks the registry's fallback chain to the first head whose resource
allows a launch; the tick, a card comment and the reviewer's document name the substitution. No launchable
head, or a fallback that would make worker and reviewer the same head, leaves the card in Ready with the
reason under `skipped_ready`, and the scan continues with the next card.

- `unauthenticated`: re-authenticate that runtime's CLI in the profile's runtime home, then wait for the TTL.
- `unavailable`: do not restart cards; check provider status and re-read after the TTL.
- `exhausted`: wait for the quota; cards with a fallback already moved.
- `probe_broken`: run the registry's probe string by hand under the dispatcher's environment and repair
  what it names; `doctor` prints the failing line.

### Head status in a live workspace

Whether a workspace that looks empty has a head:

```bash
secretary head-status --instance <dir> --workspace <path>
```

It prints one row per dispatcher-held head (worker and reviewer apart) with an actionable `summary`. Exit 0
for an answer, 3 for degraded (no workspace path, or a host in `noop` mode). No held head means no rows.

- `head` — `alive`, `absent` or `unproven`, from the vitality snapshot only and bound to the head's
  `run_id`. `alive`: heartbeat process running or suspended, or an advancing provider cursor bound to the
  run. `absent`: from the heartbeat alone. Anything else is `unproven`, with `unavailable_sources` and
  per-source `evidence`.
- `episode` — the persisted vitality conclusion: `quiet_seconds`, `dark_progress_sources`,
  `missing_progress_sources`, `last_progress`, and `next_recovery_deadline` (or `null` with
  `deadline_note`). Ladder semantics: [Head vitality](HEAD_VITALITY.md).

A head on the `local-pty` runtime (worker, reviewer or sprint observer; `kind` names which) is read from
its own supervisor (`runtime: local-pty`, the backend its recorded run names): `process` and `heartbeat`
from its launch identity (state, pid), `supervisor` from the supervisor's `status` (`alive`,
`turn_open`, `turn`, `draining`, `stopping`), `lease` from the kernel's lock table (`held` with
`holder_pid`, or `free`), and `journal.tail`, the last eight journal records; a journal with skipped,
torn or untimed lines is `degraded`, with the reason. A source that did not answer is listed in
`unavailable_sources`, never read as a gone head. A legacy record (a run on `orca-legacy`, or a head
identity with no durable run) says `runtime: orca-legacy` and `legacy_record: true`, and is read through
its pid heartbeat alone (secretary-1723 removed the pane inventory, A20 step 5).

Readings are advisory. No disconnected or unreadable source is evidence that a head is
absent; never drop the claim, kill the workspace or restart the card on that basis. The command only reads:
no lifecycle call, no rebinding, no harder probing.

The web card page lists the card's heads under **Heads** (role, run id, state); each local-pty one links to
`/tasks/<ref>/heads/<run_id>` (JSON: `/api/tasks/...`), a read-only view with no input or control and the
journal tail. The page does not render the head's PTY output.
