# Head runtime

A head runs on one runtime, `local-pty`: a supervisor of this product owns the process group, PTY,
socket and journal (`LocalPtyHeadRuntime`). A head profile may say so with `runtime = "local-pty"`
or name no runtime. Heads used to run as Orca panes (`orca-legacy`); since A20 step 2 that name is
only a marker on old durable records, which stay readable and are never launched.

The lifecycle boundary is in [Architecture](ARCHITECTURE.md#head-runtime-ownership); liveness is in
[Head vitality](HEAD_VITALITY.md). This page records what `local-pty` must do to replace Orca and
what is left to delete once it has (A20).

## The runtime default

`secretary.runtime.head_runtimes` owns the vocabulary and what an absent `runtime` means, and
`secretary.runtime.head_runtime_backends` is the only place a name becomes a backend.

- **Profile.** `HEAD_RUNTIMES` is `("local-pty",)` and `DEFAULT_HEAD_RUNTIME` is `local-pty`: a head
  profile with no `runtime` key is a `local-pty` head. A profile naming any other runtime, including
  `orca-legacy`, is refused by `validate_launch_shape` when the registry loads, by profile id, with
  the fix: set `runtime = "local-pty"` or drop the key. That is the fail-closed upgrade boundary: no
  profile is rewritten silently. The shipped `heads.toml` names no runtime (secretary-1722).
- **Record.** `RECORD_RUNTIME_WHEN_ABSENT` is `orca-legacy`: a durable `HeadRun` with no
  `head_runtime`, or with `"orca-legacy"`, and a spec rebuilt by hand from a record that never named
  one, is a legacy Orca record. `HeadRun.from_json` loads it unchanged and `to_json` writes the same
  runtime back. `head_runtime_backends.is_legacy_record` is the one predicate for it; `head-status`
  (`legacy_record: true`) and the web head view ("legacy runtime") show it as legacy.
- **No backend for a legacy record.** `build_head_runtime` builds only `LocalPtyHeadRuntime`. Asked
  for a legacy record's runtime it raises `LegacyHeadRecordError` (an `UnknownHeadRuntimeError`), and
  it never falls back to `local-pty`. So a legacy record is never launched or delivered to. The
  dispatcher's remaining Orca branches (steps 3 and 5) still compare against the legacy name.
- **Memory access grants.** A grant whose `head_run` is a legacy record loads, so it is never
  `runtime_identity_malformed` for its runtime alone. It is decided by liveness like any grant: an
  Orca pane is never alive, so it is denied `runtime_identity_unbound` (no pid file) or
  `runtime_identity_stale`. The PO memory bridge and the memory health probe build their spec by
  hand and now name `local-pty`; grants they wrote before that say `orca-legacy` and keep working
  while their process is alive.

A standing agent's tick with no usable `local-pty` profile fails closed (secretary-1720): it starts
no head and never falls back to a pane. The causes are: the registry would not load, no profile is
routed to the role, the profile will not make a `HeadSpec`, its command will not render, or it names
a runtime other than `local-pty` (so an `orca-legacy` pin fails closed too). The tick changes
nothing else: it creates no steward report card, stops no head, leaves `head_run.json` and
`active_report.json` as they are and closes no report, and exits 1. A head an earlier tick raised
finishes its turn under its own supervisor; the next tick with a usable profile finds it through
`head_run.json` (busy-skip, or a bring-up over a head that has ended).
To see the reason, read the last entry of `automation-state/<agent>/runs.jsonl` (under
`TA_STATE`, by default `~/secretary-data/automation-state`): `action="no-supervised-head"`,
`result="error"`, the cause in `error`. The unit's journal (`journalctl -u secretary-<agent>.service`)
has the same reason on stderr. A `terminal_handle.json` left in the agent's state by the pane
backend is refused the same way (`action="supervised-owner-conflict"`, exit 1) whenever the file
exists, even empty, unreadable or without a `handle`: the tick never deletes it and never raises a
head beside it. Remove it once that pane is confirmed gone. Pinned by
`tests/test_automations_dispatch_local_pty.py` (`FailClosedTests`).

## `local-pty` parity criteria

Every capability Orca gave a head, and what gives it on `local-pty`. Status is one of:
**proven live**, **merged, live proof pending**, **accepted** (not given on `local-pty`, and not a
reason to keep Orca), **open**. Refs are sprint:1459 and sprint:1461 cards and their merge commits on
`main`.

| Capability | Status | Evidence |
| --- | --- | --- |
| A head is launched and survives the end of the tick that started it | proven live | secretary-1698 (e0b9706); secretary-1699 (4e102c9): scheduler units `KillMode=process`, `_proc.run_isolated` cleans up its own group. Live: every local-pty head of this sprint, including observer run `327b521eaa6c474abda60078668ce850`. |
| A prompt is typed and submitted, and an event wakes the observer, through the runtime | proven live | PR #534 (fae497b). Live: that observer run was woken by a PO comment at 2026-09-23 20:10Z (journal `observer-wake`, then `observer-wake:submit`). |
| A card workspace is a plain `git worktree`, not an Orca worktree | proven live | secretary-1700 (24931d6). Live: secretary-1701 onward, workspaces under `/home/dev/secretary-data/workspaces/secretary/`. |
| Worker and reviewer share one workspace as two supervised processes | proven live | secretary-1700 (24931d6). Live: secretary-1701, worker run `1d15915f…`, reviewer run `f5d8d2a2…`. |
| A continuation reaches a retained (SIGSTOPped) worker | proven live | secretary-1702 (10392c6): the runtime runs the transport's `before_send` (SIGCONT) for a suspended head. Live: "retained worker resumed" on secretary-1703 at 2026-09-24 00:33Z. secretary-1719 runs the same hook for a running head too (row below). |
| `head-status` reads a local-pty head (pid, heartbeat, lease, supervisor, journal tail) | proven live | secretary-1701 (5a1cba2). Live: secretary-1702's worker at 2026-09-23 23:36Z. |
| The web shows a head's transcript tail and journal, read-only | merged, live proof pending | secretary-1703 (5b8336e). Live proof waits for the PO upgrade. |
| A project runs without Orca: optional `orca_binding`, no Orca kind in reconcile or doctor | merged, live proof pending | secretary-1704 (7f092ae). Live proof: a `project add` / `reconcile apply` after the final upgrade. `orca_binding` still has two readers: orca-legacy workspace placement (`dispatch/host.py`) and curator routing of any source whose derived cwd is under the Orca workspaces root, for example Claude and Codex sessions or Claude personal-memory files (`automations/agents/curator/discover.py`, `RouteResolver.resolve`), which does not depend on the runtime. |
| The observer workspace is a detached `git worktree` | merged, live proof pending | secretary-1705 (1cbf343). Live proof comes at the next local-pty observer launch. |
| Background agents run from the product's systemd units, with no Orca automations; the old top-level agents package is deleted | merged, live proof pending | secretary-1706 (6d866de), secretary-1707 (3240db2). Live proof: one tick per agent after the final upgrade. |
| Role heads get the product venv on `PATH` | merged, live proof pending | secretary-1708 (d83f9b5). Live proof: the next observer, steward, retro and curator heads. |
| The steward files proposals in Issues | merged, live proof pending | secretary-1709 (d148fa5). Live proof: the next steward tick that proposes. |
| Codex heads use a `CODEX_HOME` under the data dir; card and observer workspace roots are disjoint | merged, live proof pending | secretary-1710 (ca96b09). Live proof waits for the Codex login under the data dir (a PO action). |
| The Codex provider-ingress `before_send` (`bind_before_delivery`) runs for a running head | merged, live proof pending | secretary-1719: `LocalPtyHeadRuntime._before_send` runs the transport's `before_send` once per admitted delivery, after admission and before the first byte, whatever the head's stop state (it ran for a suspended head only). The run the hook returns is merged into the receipt as the transport's handoff merges it (`post_delivery_run`). Live proof: the next Codex head on `local-pty` with a provider source. |
| Vitality does not read a working resumed worker as stalled | merged, live proof pending | secretary-1719. Cause: `command_terminal_status` read the provider cursor only for a head in Orca's pane inventory, so a `local-pty` head's episode aged on the pid alone. Its `reason: "pid"` status now carries the run's provider cursor. secretary-1703's worker read `suspected_stall` (01:16Z) and `confirmed_stall` (01:21Z) while its supervisor journal logged output every minute. Live proof: the next retained-then-continued `local-pty` worker. |
| The dashboard shows the steward's "Needs a human" | accepted | issue:57ddd3549f21eff1abda option (a) is merged: the steward files proposals in Issues (secretary-1709, d148fa5). The steward's own report stays readable on its Blocked report card and in the web's read-only head view of its transcript and journal (secretary-1703, 5b8336e). The dashboard showing it is a web feature, not something Orca gave a head, so it does not block removing Orca. The issue stays open for option (c). |

## A20 exit checklist

What stays Orca-only in the product after sprint:1459. Every item becomes deletable only once the
runtime default is `local-pty`: from then on no profile reaches Orca unless it names
`orca-legacy`, and A20 removes those profiles first. Delete in this order; each step needs the one
before it.

1. **Preconditions (no code).** The runtime-default card is merged and upgraded. The instance has no
   profile on `orca-legacy`, and no live record names Orca: no dispatcher record with a workspace under
   `~/orca/workspaces`, no observer on an Orca worktree, no `HeadRun` on `orca-legacy`. The open
   parity items above are fixed or explicitly accepted.
2. **Done (secretary-1722; the merge commit is filled in by a later docs card).** The `orca-legacy`
   runtime. It is dropped from `HEAD_RUNTIMES` and from `head_runtime_backends`, and the explicit
   `runtime = "orca-legacy"` profiles from the shipped `heads.toml`. Why: after step 1 nothing
   selects it, and the default no longer does. Old records stay readable as legacy records (see
   [The runtime default](#the-runtime-default)); `runtime/orca_legacy_head.py` is unreferenced by
   the backend builder until step 6 deletes it.
3. **Orca branches in `dispatch/host.py`**: the Orca worktree create, show and rm, `_orca_repo`,
   `_split_anchor` / `_worktree_terminals`, the observer's Orca worktree and
   `_register_observer_repo`. Why: the git manager is chosen for every card whose heads are
   supervised, and later operations route by path, so with no Orca path left on a live record these
   branches are unreachable. The observer root repo then needs no Orca registration.
4. **Done (secretary-1720; the merge commit is filled in by a later docs card).** Orca branches in
   `automations/runtime/dispatch.py` and `automations/runtime/orca_rpc.py`. A standing agent's
   tick without a `local-pty` head fails closed instead of falling back to a pane. So the pane
   lifecycle (`PANE_FALLBACK_RUNTIME`, warm reuse, ghost reap, watchdog restart, finalizer
   trailer) and `orca_rpc.py` are deleted. `tests/test_architecture.py` keeps every module under
   `secretary.automations` free of `pane_host`, `orca_rpc` and the `orca` / `orca-cli` binary.
5. **The pane inventory in `dispatch/head_status.py`.** Why: it is read only for a run on
   `orca-legacy` or an Orca workspace; every other row already reads the supervisor and sets
   `pane_channel: not_consulted`.
6. **`runtime/orca_legacy_head.py`, then `runtime/pane_host.py`.** Why: only the `orca-legacy`
   backend constructs them. Remove the pane-host importers first (`runtime/tui_delivery.py`,
   `runtime/agent_prompt_transport.py`, `runtime/head/operations.py`, `dispatch/tui.py`,
   `dispatch/review.py`): each keeps a pane path only for Orca. `automations/runtime/finalizer.py`
   is already deleted, with its `--spawn-finalizer` / `--finalize` flags (done: secretary-1720).
7. **The legacy `CODEX_HOME` rung** (`~/.config/orca/codex-runtime-home/home`) in
   `codex_preflight.resolve_codex_home`, and its readers in `upgrade.py` and `installation.py`
   (secretary-1710). Why: once the login under the data dir is proven live, no Codex head reads the
   legacy home.
8. **`orca_binding` and the `orca` records in `host-managed.json`.** `orca_binding` has two
   readers. Step 3 removes the first, orca-legacy workspace placement. The second is curator
   routing of any source whose derived cwd is under the Orca workspaces root, for example Claude and
   Codex sessions or Claude personal-memory files: the route boundary adds
   `<workspaces root>/<orca_binding>` to the project, and without it such a source routes to
   `unknown`, whatever the head runtime. So "no live Orca head" is not enough.

   The binding may be dropped only after curator routing no longer needs it: curator routing maps
   every path under the Orca workspaces root to its project without reading `orca_binding`, for
   example through a path-prefix map kept in the instance. Whether and how to build that is a
   separate product decision, outside A20's deletions. Until then `orca_binding` stays on existing
   bindings. That is harmless: it is optional, and new projects do not get it.

   Reconcile and doctor already ignore the leftover `orca` records. Durable formats stay loadable:
   the loader keeps accepting and ignoring the key and the record until the instance drops them (a
   PO edit).
9. **Host coupling.** The units' `After=orca-server.service` (`packaging/systemd/*.service`),
   doctor's `orca-server.service` expectation (`host.py`), bootstrap's Orca AppImage and `xvfb`
   install (`bootstrap.py`), and the Orca state dirs in `backup.py`. Why: after steps 2–7 no tick,
   head or command calls Orca, so ordering after it, requiring it or backing it up protects nothing.
   `orca-server` itself is a host-owned unit; stopping it and uninstalling Orca are PO actions after
   this step.
10. **Leftover Orca automations on the host** (Orca's own state). Why: sprint:1459 moved the
    background agents to the product's units (secretary-1706); the Orca copies no longer run anything.
    A PO action, since the product never writes Orca state.
11. **Role worktrees under `~/orca/workspaces/secretary/{curator,pipeline,retro,steward}`**
    (`data.py`, `cli.py`, the automations' default `TA_WORKSPACE`). An Orca-flavoured path with no
    Orca dependency, so it does not block removing Orca; move them under the data dir in A20 or later.
    The curator also routes by the `~/orca/workspaces` root, whatever the runtime: it is the base of
    the step-8 boundaries, and sources under `~/orca/workspaces/observers/<token>` route by their
    sprint's reservations. Move or retire that root only under step 8's condition: curator routing
    no longer needs it.
