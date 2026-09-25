# Head runtime

A head runs on one runtime, `local-pty`: a supervisor of this product owns the process group, PTY,
socket and journal (`LocalPtyHeadRuntime`). A head profile may say so with `runtime = "local-pty"`
or name no runtime. Heads used to run as Orca panes (`orca-legacy`); since A20 step 2 that name is
only a marker on old durable records, which stay readable and are never launched.

The lifecycle boundary is in [Architecture](ARCHITECTURE.md#head-runtime-ownership); liveness is in
[Head vitality](HEAD_VITALITY.md). This page records how `local-pty` replaced Orca: the parity it had
to reach, and the A20 exit checklist that removed Orca from the product, step by step.

## Supervisor progress journal

The supervisor feeds every PTY output chunk into a bounded text screen (rows and columns follow
the PTY size). It considers that screen in 0.5-second windows during an open turn. It writes
`provider.progressed` only when the screen contains a normalized line it has not seen in that turn.
The screen places printable text, tracks cursor moves, erasure, scrolling and the alternate screen,
and ignores colour and other non-text terminal controls. Normalization masks digit runs, removes
Dingbat, Braille, Geometric Shapes, `•` and `·` spinner glyphs and a lone `*`, and collapses
whitespace. Empty lines are ignored.
The supervisor keeps at most 4096 line hashes per turn and clears them at `turn.started`.
Windows without newly appearing lines write no progress record; their bytes accumulate in the next
progress record's `output_bytes`, which also carries `folded_windows`. The line hashes form a
bounded FIFO: a new line evicts the oldest hash when the cap is full. Lines still visible on the
screen do not progress again after eviction. `turn.finished.output_bytes` still counts all
output in the turn, including folded windows; `turn.finished.folded_windows` reports any folds
still pending at the end. Quiet turn timing is unchanged.

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
  it never falls back to `local-pty`. So a legacy record is never launched or delivered to.
- **Dispatcher records.** The dispatcher host places every card and observer in a git worktree and
  has no Orca path (step 3). A dispatcher record whose workspace is under the Orca workspaces root
  and is not a git worktree the host owns, or whose worker, reviewer or observer run is a legacy
  record, is refused by every verb — launch, delivery, stop, teardown — with `LegacyDispatcherRecord`
  before any child runs or any backend is asked. Its bring-up cause is the card's own contract, so
  the card goes Blocked with a reason naming the record; it is never torn down through Orca and never
  re-placed. `head-status` shows such a record as legacy (`runtime: orca-legacy`,
  `legacy_record: true`) through its pid heartbeat alone (step 5 removed the pane inventory).
- **Memory access grants.** A grant whose `head_run` is a legacy record loads, so it is never
  `runtime_identity_malformed` for its runtime alone. It is decided by liveness like any grant: the
  Orca pane its head ran in is gone, so it is denied `runtime_identity_unbound` (no pid file) or
  `runtime_identity_stale`. The PO memory bridge and the memory health probe build their spec by
  hand and now name `local-pty`; grants they wrote before that say `orca-legacy` and keep working
  while their process is alive.

A standing agent's tick with no usable `local-pty` profile fails closed (secretary-1720): it starts no
head; the pane fallback went in A20 step 4. The causes are: the registry would not load, no profile is
routed to the role, the profile will not make a `HeadSpec`, its command will not render, or it names a
runtime other than `local-pty` (so an `orca-legacy` pin fails closed too). The tick changes nothing
else: it creates no steward report card, stops no head, leaves `head_run.json` and `active_report.json`
as they are and closes no report, and exits 1. A head an earlier tick raised finishes its turn under its
own supervisor; the next tick with a usable profile finds it through `head_run.json` (busy-skip, or a
bring-up over a head that has ended). To see the reason, read the last entry of
`automation-state/<agent>/runs.jsonl` (under `TA_STATE`, by default
`~/secretary-data/automation-state`): `action="no-supervised-head"`, `result="error"`, the cause in
`error`. The unit's journal (`journalctl -u secretary-<agent>.service`) has the same reason on stderr. A
`terminal_handle.json` left in the agent's state by the pane backend before A20 step 4 is refused the
same way (`action="supervised-owner-conflict"`, exit 1) whenever the file exists, even empty, unreadable
or without a `handle`: the tick never deletes it and never raises a head beside it. Remove it once the
pane-era head it names is confirmed gone. Pinned by `tests/test_automations_dispatch_local_pty.py`
(`FailClosedTests`).

## `local-pty` parity criteria

Every capability Orca gave a head, and what gives it on `local-pty`. Status is one of:
**proven live**, **merged, live proof pending**, **accepted** (not given on `local-pty`, and not a
reason to keep Orca). No row is open. Refs are sprint:1459 and sprint:1461 cards and their merge
commits on `main`.

| Capability | Status | Evidence |
| --- | --- | --- |
| A head is launched and survives the end of the tick that started it | proven live | secretary-1698 (e0b9706); secretary-1699 (4e102c9): scheduler units `KillMode=process`, `_proc.run_isolated` cleans up its own group. Live: every local-pty head of this sprint, including observer run `327b521eaa6c474abda60078668ce850`. |
| A prompt is typed and submitted, and an event wakes the observer, through the runtime | proven live | PR #534 (fae497b). Live: that observer run was woken by a PO comment at 2026-09-23 20:10Z (journal `observer-wake`, then `observer-wake:submit`). |
| A card workspace is a plain `git worktree`, not an Orca worktree | proven live | secretary-1700 (24931d6). Live: secretary-1701 onward, workspaces under `/home/dev/secretary-data/workspaces/secretary/`. |
| Worker and reviewer share one workspace as two supervised processes | proven live | secretary-1700 (24931d6). Live: secretary-1701, worker run `1d15915f…`, reviewer run `f5d8d2a2…`. |
| A continuation reaches a retained (SIGSTOPped) worker | proven live | secretary-1702 (10392c6): the runtime runs the transport's `before_send` (SIGCONT) for a suspended head. Live: "retained worker resumed" on secretary-1703 at 2026-09-24 00:33Z. secretary-1719 runs the same hook for a running head too (row below). |
| `head-status` reads a local-pty head (pid, heartbeat, lease, supervisor, journal tail) | proven live | secretary-1701 (5a1cba2). Live: secretary-1702's worker at 2026-09-23 23:36Z. |
| The web shows a head's journal, read-only | merged, live proof pending | secretary-1703 (5b8336e). Live proof waits for the PO upgrade. |
| A project runs without Orca: optional `orca_binding`, no Orca kind in reconcile or doctor | merged, live proof pending | secretary-1704 (7f092ae). Live proof: a `project add` / `reconcile apply` after the final upgrade. Since secretary-1722 `orca_binding` has one reader: curator routing of any source whose derived cwd is under the Orca workspaces root, for example Claude and Codex sessions or Claude personal-memory files (`automations/agents/curator/discover.py`, `RouteResolver.resolve`), which does not depend on the runtime. |
| The observer workspace is a detached `git worktree` | merged, live proof pending | secretary-1705 (1cbf343). Live proof comes at the next local-pty observer launch. |
| Background agents run from the product's systemd units, with no Orca automations; the old top-level agents package is deleted | merged, live proof pending | secretary-1706 (6d866de), secretary-1707 (3240db2). Live proof: one tick per agent after the final upgrade. |
| Role heads get the product venv on `PATH` | merged, live proof pending | secretary-1708 (d83f9b5). Live proof: the next observer, steward, retro and curator heads. |
| The steward files proposals in Issues | merged, live proof pending | secretary-1709 (d148fa5). Live proof: the next steward tick that proposes. |
| Codex heads use a `CODEX_HOME` under the data dir; card and observer workspace roots are disjoint | proven live | secretary-1710 (ca96b09); secretary-1723 removed the legacy rung. Live on 2026-09-24: `secretary doctor` reports `codex home: /home/dev/secretary-data/codex-home (data-dir home)`, every live Codex process has `CODEX_HOME=/home/dev/secretary-data/codex-home`, no `*.jsonl` under the legacy home's `sessions/` was written after 12:00Z (newest 08:38Z), and `resolve_codex_home` resolves all six installed Codex profiles to `/home/dev/secretary-data/codex-home` (`data-dir`). |
| The Codex provider-ingress `before_send` (`bind_before_delivery`) runs for a running head | merged, live proof pending | secretary-1719: `LocalPtyHeadRuntime._before_send` runs the transport's `before_send` once per admitted delivery, after admission and before the first byte, whatever the head's stop state. The run the hook returns is merged into the receipt (`post_delivery_run`). secretary-1741: every Codex worker continuation and report prompt with a provider source installs the durable run's ingress if needed, then `_nudge_worker` runs SIGCONT when needed and binds through that hook; the bound run and source state reach the record and dispatcher comment. Live proof: a continued Codex worker run id after deploy. |
| Vitality does not read a working resumed worker as stalled | merged, live proof pending | secretary-1719. Cause: `command_terminal_status` read the provider cursor only for a head in Orca's pane inventory, so a `local-pty` head's episode aged on the pid alone. Its `reason: "pid"` status now carries the run's provider cursor. secretary-1703's worker read `suspected_stall` (01:16Z) and `confirmed_stall` (01:21Z) while its supervisor journal logged output every minute. Live proof: the next retained-then-continued `local-pty` worker. |
| The dashboard shows the steward's "Needs a human" | accepted | issue:57ddd3549f21eff1abda option (a) is merged: the steward files proposals in Issues (secretary-1709, d148fa5). The steward's own report stays readable on its Blocked report card and in the web's read-only head view of its journal (secretary-1703, 5b8336e). The dashboard showing it is a web feature, not something Orca gave a head, so it does not block removing Orca. The issue stays open for option (c). |

The live proof of every "merged, live proof pending" row is the sprint's final acceptance after
the last upgrade; the observer records it when sprint:1461 closes.

## A20 exit checklist

A20 removed from the product everything that was still Orca-only after sprint:1459. Every step
needed the runtime default to be `local-pty` first: from then on no profile reached Orca unless it
named `orca-legacy`. The checklist is finished: steps 1-7 and 9 are merged on `main`, step 10 is a
PO action outside the product, and steps 8 and 11 are the deferred remainder.

| Step | Status | Card | Merge commit on `main` |
| --- | --- | --- | --- |
| 1 Preconditions and parity rows | done | secretary-1719 | `dbd4ea5` (PR #552) |
| 2 The `orca-legacy` runtime | done, with step 3 | secretary-1722 | `de3d086` (PR #554) |
| 3 Orca branches of `dispatch/host.py` | done, with step 2 | secretary-1722 | `de3d086` (PR #554) |
| 4 Orca branches of `automations/runtime/` | done | secretary-1720 | `fbb4506` (PR #553) |
| 5 `head_status` pane inventory | done | secretary-1723 | `ab0941b` (PR #555) |
| 6 Orca head backend and pane host | done, in three parts | secretary-1720 (`finalizer.py`), secretary-1723 (`dispatch/`), secretary-1725 (`runtime/` and the whole-tree rule) | `fbb4506` (PR #553), `ab0941b` (PR #555), `7322695` (PR #556) |
| 7 Legacy `CODEX_HOME` rung | done | secretary-1723 | `ab0941b` (PR #555) |
| 8 `orca_binding` and the `orca` records in `host-managed.json` | deferred | — | — |
| 9 Host coupling | done | secretary-1726 | `78c6d1f` (PR #557) |
| 10 Leftover Orca automations and state on the host | PO action, outside the product | requested on sprint:1461 | — |
| 11 Role worktrees under `~/orca/workspaces` | deferred | — | — |

**Order.** The steps landed in this order on `main`: 1, 4, 2 with 3, then 5 and 7 with the
`dispatch/` half of 6, then the `runtime/` half of 6, then 9. Steps 2 and 3 needed each other
(secretary-1722's block report): step 2 took away the only backend the host's Orca branches ran on,
so every Orca launch, delivery, stop and teardown in `dispatch/host.py` failed with
`LegacyHeadRecordError`, and about 60 host tests still drove that path. Landing step 3 first would
have retargeted the same tests twice; landing step 2 alone would have left dead branches. So the
observer merged them into one card. Each later step deleted what the earlier ones had stopped
using: step 6 the backend and pane host that no tick, host path or `head_status` row called any
more, and step 9 the host coupling once no tick, head or command called Orca.

**Deferred remainder: steps 8 and 11** (the owner's decision of 2026-09-24: out of A20's scope).
Both stay for one reason: curator routing reads the `~/orca/workspaces` root, whatever the head
runtime. They go together, once curator routing no longer needs that root.

1. **Done (secretary-1719, `dbd4ea5`, PR #552).** Preconditions (no code). The runtime-default card
   was merged and upgraded. The instance had no profile on `orca-legacy` and no live record naming
   Orca: no dispatcher record with a workspace under `~/orca/workspaces`, no observer on an Orca
   worktree, no `HeadRun` on `orca-legacy`. The three open parity rows were closed: two are merged
   with live proof pending, and the steward's "Needs a human" is accepted.
2. **Done (secretary-1722 with step 3, `de3d086`, PR #554).** The `orca-legacy` runtime. It was
   dropped from `HEAD_RUNTIMES` and from `head_runtime_backends`, and the explicit
   `runtime = "orca-legacy"` profiles from the shipped `heads.toml`. Why: after step 1 nothing
   selected it, and the default no longer did. Old records stay readable as legacy records (see
   [The runtime default](#the-runtime-default)). The Orca backend module itself went in step 6.
3. **Done (secretary-1722 with step 2, `de3d086`, PR #554).** Orca branches in `dispatch/host.py`:
   the Orca worktree create, show and rm, `_orca_repo`, `_orca_binding_name`, `_runs_in_orca_pane`,
   `_split_anchor` / `_worktree_terminals`, the observer's Orca worktree,
   `_observer_workspace_registered` and `_register_observer_repo`. Every card is placed by
   `GitWorkspaceManager` and every observer in its detached git worktree, whatever the profiles; the
   host imports neither the pane host nor the Orca backend. A legacy dispatcher record is refused
   (see [The runtime default](#the-runtime-default)). The host still reads the Orca workspaces root,
   only to recognise such a record. The observer root repo needs no Orca registration.
4. **Done (secretary-1720, `fbb4506`, PR #553).** Orca branches in `automations/runtime/dispatch.py`
   and `automations/runtime/orca_rpc.py`. A standing agent's tick without a `local-pty` head fails
   closed instead of falling back to a pane. So the pane lifecycle (`PANE_FALLBACK_RUNTIME`, warm
   reuse, ghost reap, watchdog restart, finalizer trailer) and `orca_rpc.py` were deleted. Step 6
   extended the architecture rule to the whole tree.
5. **Done (secretary-1723, `ab0941b`, PR #555).** The pane inventory in `dispatch/head_status.py`.
   Every supervised row reads the supervisor as before; a legacy record is shown as legacy
   (`is_legacy_record`) through its pid heartbeat, with no pane inventory. The `pane_channel`,
   `runtime_pane_channel`, `runtime_pane` and `pane` fields are gone.
6. **Done (secretary-1720, `fbb4506`; secretary-1723, `ab0941b`; secretary-1725, `7322695`).** The
   Orca head backend and the pane host were deleted, and so were the pane halves of their importers:
   - `automations/runtime/finalizer.py` went with its `--spawn-finalizer` / `--finalize` flags
     (secretary-1720, PR #553);
   - `dispatch/`: `command_terminal_status` reads the pid heartbeat and the exact-run provider
     cursor, the `workspace_panes` seam is gone, and `dispatch/tui.py` keeps the delivery vocabulary
     and the provider-journal readers (secretary-1723, PR #555);
   - `runtime/`: `orca_legacy_head.py` and `pane_host.py` are deleted; `tui_delivery.py` keeps the
     delivery vocabulary (`DeliveryEvidence`, `DeliveryOutcome`, `TuiDeliveryError`, the stage,
     readiness and receipt names) and no pane read, send or screen probe;
     `agent_prompt_transport.py` keeps the prompt validation policy and no terminal send;
     `head/operations.py` keeps the typed refusals, `NudgePointer` and `post_delivery_run`, and no
     pane `spawn` / `nudge` / `stop` (secretary-1725, PR #556).

   One rule in `tests/test_architecture.py` (`NoOrcaInSourceTests`, secretary-1725) holds it over
   every module under `src/secretary`: no import of the pane host, the Orca backend or an `orca_rpc`
   module, and no string constant whose program is `orca` or `orca-cli`. Since step 9 its allowlist
   holds only the two owner decisions of steps 8 and 11 (a record kind and a path, neither a
   program), and each entry excuses exactly one finding on its line.
7. **Done (secretary-1723, `ab0941b`, PR #555).** The legacy `CODEX_HOME` rung
   (`~/.config/orca/.../home`) in `codex_preflight.resolve_codex_home`, and its readers in
   `upgrade.py` and `installation.py` (secretary-1710). With no profile `codex_home`, no
   `TA_CODEX_HOME` and no data-dir login the resolver raises `CodexHomeLoginMissing`, whose message
   names the fix; `secretary doctor` reports it as a red finding (`codex_home_login_missing`) when an
   installed profile runs Codex. Seeding and the Memory-client reconcile manage
   `<data_dir>/codex-home` only. One read-only reader is left: `runtime/codex_home.py`
   `session_roots` still scans the legacy home's `sessions/`, because the curator had not ingested
   468 of its 3217 rollouts on 2026-09-24; it goes once the curator's watermark names every one. The
   legacy home itself is not moved or deleted (a PO action, if ever).
8. **Deferred (the owner's decision of 2026-09-24).** `orca_binding` and the `orca` records in
   `host-managed.json`. `orca_binding` had two readers. Step 3 removed the first, orca-legacy
   workspace placement (secretary-1722). The second is curator routing of any source whose derived
   cwd is under the Orca workspaces root, for example Claude and Codex sessions or Claude
   personal-memory files: the route boundary adds `<workspaces root>/<orca_binding>` to the project,
   and without it such a source routes to `unknown`, whatever the head runtime. So "no live Orca
   head" is not enough.

   The condition: the binding may be dropped only once curator routing no longer needs the
   `~/orca/workspaces` root, that is, it maps every path under that root to its project without
   reading `orca_binding`, for example through a path-prefix map kept in the instance. Whether and
   how to build that is a separate product decision, outside A20. Until then `orca_binding` stays on
   existing bindings. That is harmless: it is optional, and new projects do not get it.

   Reconcile and doctor already ignore the leftover `orca` records. Durable formats stay loadable:
   the loader keeps accepting and ignoring the key and the record until the instance drops them (a
   PO edit).
9. **Done (secretary-1726, `78c6d1f`, PR #557).** Host coupling. No packaged unit orders after or
   requires `orca-server.service` or `xvfb.service`; doctor and status neither expect nor report
   either (`status --json` has no `host.external_runtime`, the doctor lamp no
   `external_runtime.inactive`); bootstrap installs Docker and Compose only, with no Orca AppImage,
   `xvfb` or Electron runtime packages; recovery's prerequisites are the PostgreSQL store alone, with
   no `orca` binary; `SystemdLayout` has no `orca_executable` and templates no
   `{{SECRETARY_ORCA_EXECUTABLE}}`; a new full backup writes no Orca state. An older full archive that
   carries the optional `debug/orca-state/inventory.json` (`debug_orca_state`) still verifies and
   restores: the entry is checksummed like any other, required by no policy, and never restored as
   data. The step-9 entries of the `NoOrcaInSourceTests` allowlist went with it. Why: after steps
   2–7 no tick, head or command called Orca, so ordering after it, requiring it or backing it up
   protected nothing. `orca-server.service` and `xvfb.service` are host-owned units Secretary never
   wrote; stopping and disabling them, and uninstalling Orca, are PO actions after the merge and
   upgrade (step 10).
10. **PO action, outside the product (requested on sprint:1461).** Leftover Orca automations and
    Orca's own state on the host, and the host-owned units of step 9. Why: sprint:1459 moved the
    background agents to the product's units (secretary-1706); the Orca copies no longer run
    anything. The product never writes Orca state, so it removes none either.
11. **Deferred, with step 8 (the owner's decision of 2026-09-24).** Role worktrees under
    `~/orca/workspaces/secretary/{curator,pipeline,retro,steward}` (`data.py`, `cli.py`, the
    automations' default `TA_WORKSPACE`). An Orca-flavoured path with no Orca dependency, so it did
    not block removing Orca. The curator also routes by the `~/orca/workspaces` root, whatever the
    runtime: it is the base of the step-8 boundaries, and sources under
    `~/orca/workspaces/observers/<token>` route by their sprint's reservations. Move or retire that
    root, and the role worktrees under it, only under step 8's condition: curator routing no longer
    needs it.
