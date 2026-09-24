# Head runtime

A head runs on one of two runtimes, named per head profile by its `runtime` key:

- `orca-legacy`: the head is an Orca pane and Orca owns its process (`OrcaLegacyHeadRuntime`);
- `local-pty`: a supervisor of this product owns the process group, PTY, socket and journal
  (`LocalPtyHeadRuntime`).

The lifecycle boundary is in [Architecture](ARCHITECTURE.md#head-runtime-ownership); liveness is in
[Head vitality](HEAD_VITALITY.md). This page records what `local-pty` must do to replace Orca and
what is left to delete once it has (A20).

## The runtime default

`secretary.runtime.head_runtimes.DEFAULT_HEAD_RUNTIME` is the only owner of what an absent `runtime`
key means, and `secretary.runtime.head_runtime_backends` is the only place a name becomes a backend.
Until the runtime-default card of sprint:1459 (its last card), an absent key means `orca-legacy`,
pinned by `tests/test_dispatcher_contracts.py`. That card makes it `local-pty` and writes
`runtime = "orca-legacy"` explicitly into the shipped `heads.toml` and the installed registry for
the profiles that stay on Orca until A20. Update this section when it lands.

## `local-pty` parity criteria

Every capability Orca gave a head, and what gives it on `local-pty`. Status is one of:
**proven live**, **merged, live proof pending**, **open**. Refs are sprint:1459 cards and their
merge commits on `main`.

| Capability | Status | Evidence |
| --- | --- | --- |
| A head is launched and survives the end of the tick that started it | proven live | secretary-1698 (e0b9706); secretary-1699 (4e102c9): scheduler units `KillMode=process`, `_proc.run_isolated` cleans up its own group. Live: every local-pty head of this sprint, including observer run `327b521eaa6c474abda60078668ce850`. |
| A prompt is typed and submitted, and an event wakes the observer, through the runtime | proven live | PR #534 (fae497b). Live: that observer run was woken by a PO comment at 2026-09-23 20:10Z (journal `observer-wake`, then `observer-wake:submit`). |
| A card workspace is a plain `git worktree`, not an Orca worktree | proven live | secretary-1700 (24931d6). Live: secretary-1701 onward, workspaces under `/home/dev/secretary-data/workspaces/secretary/`. |
| Worker and reviewer share one workspace as two supervised processes | proven live | secretary-1700 (24931d6). Live: secretary-1701, worker run `1d15915f…`, reviewer run `f5d8d2a2…`. |
| A continuation reaches a retained (SIGSTOPped) worker | proven live | secretary-1702 (10392c6): the runtime runs the transport's `before_send` (SIGCONT) for a suspended head. Live: "retained worker resumed" on secretary-1703 at 2026-09-24 00:33Z. |
| `head-status` reads a local-pty head (pid, heartbeat, lease, supervisor, journal tail) | proven live | secretary-1701 (5a1cba2). Live: secretary-1702's worker at 2026-09-23 23:36Z. |
| The web shows a head's transcript tail and journal, read-only | merged, live proof pending | secretary-1703 (5b8336e). Live proof waits for the PO upgrade. |
| A project runs without Orca: optional `orca_binding`, no Orca kind in reconcile or doctor | merged, live proof pending | secretary-1704 (7f092ae). Live proof: a `project add` / `reconcile apply` after the final upgrade. `orca_binding` still has two readers: orca-legacy workspace placement (`dispatch/host.py`) and the curator's routing of historical sessions (`automations/agents/curator/discover.py`, `RouteResolver.resolve`), which does not depend on the runtime. |
| The observer workspace is a detached `git worktree` | merged, live proof pending | secretary-1705 (1cbf343). Live proof comes at the next local-pty observer launch. |
| Background agents run from the product's systemd units, with no Orca automations; the old top-level agents package is deleted | merged, live proof pending | secretary-1706 (6d866de), secretary-1707 (3240db2). Live proof: one tick per agent after the final upgrade. |
| Role heads get the product venv on `PATH` | merged, live proof pending | secretary-1708 (d83f9b5). Live proof: the next observer, steward, retro and curator heads. |
| The steward files proposals in Issues | merged, live proof pending | secretary-1709 (d148fa5). Live proof: the next steward tick that proposes. |
| Codex heads use a `CODEX_HOME` under the data dir; card and observer workspace roots are disjoint | merged, live proof pending | secretary-1710 (ca96b09). Live proof waits for the Codex login under the data dir (a PO action). |
| The Codex provider-ingress `before_send` (`bind_before_delivery`) runs for a running head | open | Never performed on local-pty; secretary-1702 wakes only suspended heads and names this as its own card (1702 report). |
| Vitality does not read a working resumed worker as stalled | open | secretary-1703: `suspected_stall` at 2026-09-24 01:16Z and `confirmed_stall` at 01:21Z while the worker was working (it reported done at 01:22Z). The watchdog correctly refused the destructive step. |
| The dashboard shows the steward's "Needs a human" | open | issue:57ddd3549f21eff1abda, option c. |

## A20 exit checklist

What stays Orca-only in the product after sprint:1459. Every item becomes deletable only once the
runtime default is `local-pty`: from then on no profile reaches Orca unless it names
`orca-legacy`, and A20 removes those profiles first. Delete in this order; each step needs the one
before it.

1. **Preconditions (no code).** The runtime-default card is merged and upgraded. The instance has no
   profile on `orca-legacy`, and no live record names Orca: no dispatcher record with a workspace under
   `~/orca/workspaces`, no observer on an Orca worktree, no `HeadRun` on `orca-legacy`. The open
   parity items above are fixed or explicitly accepted.
2. **The `orca-legacy` runtime.** Drop it from `HEAD_RUNTIMES` and from
   `head_runtime_backends`, and the explicit `runtime = "orca-legacy"` profiles from the shipped
   `heads.toml`. Why: after step 1 nothing selects it, and the default no longer does.
3. **Orca branches in `dispatch/host.py`**: the Orca worktree create, show and rm, `_orca_repo`,
   `_split_anchor` / `_worktree_terminals`, the observer's Orca worktree and
   `_register_observer_repo`. Why: the git manager is chosen for every card whose heads are
   supervised, and later operations route by path, so with no Orca path left on a live record these
   branches are unreachable. The observer root repo then needs no Orca registration.
4. **Orca branches in `automations/runtime/dispatch.py`** and `automations/runtime/orca_rpc.py`.
   Why: a background agent reaches `orca_rpc` only on an `orca-legacy` profile.
5. **The pane inventory in `dispatch/head_status.py`.** Why: it is read only for a run on
   `orca-legacy` or an Orca workspace; every other row already reads the supervisor and sets
   `pane_channel: not_consulted`.
6. **`runtime/orca_legacy_head.py`, then `runtime/pane_host.py`.** Why: only the `orca-legacy`
   backend constructs them. Remove the pane-host importers first (`runtime/tui_delivery.py`,
   `runtime/agent_prompt_transport.py`, `runtime/head/operations.py`, `dispatch/tui.py`,
   `dispatch/review.py`, `automations/runtime/finalizer.py`): each keeps a pane path only for Orca.
7. **The legacy `CODEX_HOME` rung** (`~/.config/orca/codex-runtime-home/home`) in
   `codex_preflight.resolve_codex_home`, and its readers in `upgrade.py` and `installation.py`
   (secretary-1710). Why: once the login under the data dir is proven live, no Codex head reads the
   legacy home.
8. **`orca_binding` and the `orca` records in `host-managed.json`.** `orca_binding` has two
   readers. Step 3 removes the first, orca-legacy workspace placement. The second is the curator:
   its route boundary adds `<workspaces root>/<orca_binding>` to the project, so a historical Claude
   or Codex session whose cwd is under `~/orca/workspaces/<orca_binding>` routes to that project.
   Without the binding it routes to `unknown`, whatever the head runtime. So "no live Orca head" is
   not enough. The binding may be dropped only when the curator no longer needs it, that is, either:
   - (a) the curator's watermark is past every session whose cwd is under `~/orca/workspaces/`, and
     no such session source is retained; or
   - (b) curator routing has another way to map those historical paths, for example a path-prefix
     map kept in the instance. Choosing (b) is a product decision, taken as its own change outside
     A20's deletions.

   Take this step only after (a) or (b) holds. Reconcile and doctor already ignore the leftover
   `orca` records. Durable formats stay loadable: the loader keeps accepting and ignoring the key and
   the record until the instance drops them (a PO edit).
9. **Host coupling.** The units' `After=orca-server.service` (`packaging/systemd/*.service`),
   doctor's `orca-server.service` expectation (`host.py`), bootstrap's Orca AppImage and `xvfb`
   install (`bootstrap.py`), and the Orca state dirs in `backup.py`. Why: after steps 2–8 no tick,
   head or command calls Orca, so ordering after it, requiring it or backing it up protects nothing.
   `orca-server` itself is a host-owned unit; stopping it and uninstalling Orca are PO actions after
   this step.
10. **Leftover Orca automations on the host** (Orca's own state). Why: sprint:1459 moved the
    background agents to the product's units (secretary-1706); the Orca copies no longer run anything.
    A PO action, since the product never writes Orca state.
11. **Role worktrees under `~/orca/workspaces/secretary/{curator,pipeline,retro,steward}`**
    (`data.py`, `cli.py`, the automations' default `TA_WORKSPACE`). An Orca-flavoured path with no
    Orca dependency, so it does not block removing Orca; move them under the data dir in A20 or later.
    The curator also reads `~/orca/workspaces` by path, whatever the runtime: it is the base of the
    step-8 boundaries, and historical observer sessions under `~/orca/workspaces/observers/<token>`
    route by their sprint's reservations. Move or retire that root only under step 8's condition, (a)
    or (b).
