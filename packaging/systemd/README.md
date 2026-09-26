# systemd assets

Templates for the secretary runtime units: production dispatcher ticks, the memory service, the PO
service, the web transport and its front, curator, steward (including the deep sweep) and retro. There is no
scheduled backup unit: the git checkpoint is the recovery contract ([Recovery](../../docs/RECOVERY.md)),
and `backup create` is a manual, optional cold archive.

These files are templates, not host-ready files or a starting point to copy by hand. `secretary
reconcile apply` compiles them with the installation user, home, product checkout, instance and
data layout, then installs those bytes and records ownership in `host-managed.json`. `secretary
upgrade` does the same on every release. The rendered bytes and their digest are the desired state,
so editing a template or changing the installation layout makes the next apply update the unit.

The manifest is private state for the installation account. A root-run reconcile publishes it as that
account with mode `0600`, using the instance checkout owner as the source of the installation user;
an unreadable manifest is reported as a host-state error, not treated as an empty managed set.

Every unit name here must fall under the instance's `host.unit_prefix`, and its component name (the
file name minus that prefix and the suffix) is what `host.components` opts out of. Paths in
committed templates contain placeholders for that layout. Run `systemd-analyze verify` on anything
you change.
A unit already on the host is never overwritten until it is adopted; apply refuses to write over a
name it cannot prove it owns.

`secretary-dispatcher-production.timer` launches a one-shot `production-tick` through the configured
venv's isolated `runtime_preflight.py`. The preflight runs by pathname before the `secretary` entry
point can import an editable package, refuses foreign or task-workspace provenance with the existing
production-state diagnostic, and execs the tick only after a valid observation. It is materialized
with the unit through `reconcile apply` or `secretary upgrade`; do not copy or edit it on the host.
Every one-shot unit that can launch a local-pty head (the production tick and the curator, retro,
steward and deep-sweep ticks) sets `KillMode=process`: the head's supervisor outlives the tick, and
systemd's default control-group kill would SIGTERM it the moment the tick exits. A new one-shot unit
either sets it too or is listed in `tests/test_board_boot_race.py` as one that launches no head.
`secretary-instance-maintenance.timer` fires daily (`Persistent=true`) a one-shot
`secretary instance-maintenance` at idle CPU and I/O priority: Git's own `gc --auto` heuristic run
outside any tick, because the lifecycle sets `gc.auto=0` in the instance repository so that no
checkpoint commit packs. The packing step takes no state-repo lock; only the short reflog expiry
after it does ([Recovery](../../docs/RECOVERY.md#local-git-packing-controls)).
`secretary-memory.service` serves MCP on the configured local endpoint and loads the instance
embedding model. `secretary-web.service` runs the web transport on `127.0.0.1:8787` — that host is
not a default the unit may relax — and `secretary-web-front.service` runs the distribution's Caddy that
terminates TLS, checks the owner's password and proxies to it. The front is `PartOf` the transport,
so the pair starts, stops and restarts together, and its configuration is not a template here: it
carries a bcrypt hash and is rendered from the secret store by `secretary web-front render` into
`<data-dir>/webfront/Caddyfile` with mode 0600. The distribution's own `caddy.service` is masked on
this installation so that installing the package can never start an unconfigured public listener;
see [Operations](../../docs/OPERATIONS.md#the-published-web-front).
`secretary-po.service` runs `secretary po-serve`, the one owner of PO head turns: every turn process is
a child in its control group, it takes messages from the durable queue `<data-dir>/po-queue/` and
listens on the Unix socket `<data-dir>/po-service/po.sock` (mode 0600) for the web and, later, the
dispatcher. It has no `PartOf=`/`BindsTo=` coupling to the web, so a web restart touches no turn.
`secretary upgrade` never restarts it while a turn runs: it asks, and the service exits by itself
(`Restart=always` brings it back on the new code) once no turn runs; see
[Operations](../../docs/OPERATIONS.md#the-po-service). Scheduler-backed roles must have exactly one
owner: the systemd timer here.
