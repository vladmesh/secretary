# systemd assets

Templates for the current secretary runtime. The live installation uses units for production
dispatcher ticks, memory service, curator, steward and retro. Archive backup is no longer a
scheduled unit: git-checkpoint is the recovery contract (docs/RECOVERY.md), and `backup create`
remains only a manual, optional cold-archive tool.

These files are templates, not host-ready files or a starting point to copy by hand. `secretary
reconcile apply` compiles them with the installation user, home, product checkout, instance and
data layout, then installs those bytes and records ownership in `host-managed.json`. `secretary
upgrade` does the same on every release. The rendered bytes and their digest are the desired state,
so editing a template or changing the installation layout makes the next apply update the unit.

Every unit name here must fall under the instance's `host.unit_prefix`, and its component name (the
file name minus that prefix and the suffix) is what `host.components` opts out of. Paths in
committed templates contain placeholders for that layout. `secretary-orca.service.template` is
rendered by bootstrap but remains bootstrap-owned, not part of reconcile desired state. Run
`systemd-analyze verify` on anything you change. A unit already on the host is never overwritten until it is adopted; apply
refuses to write over a name it cannot prove it owns.

`secretary-dispatcher-production.timer` launches a one-shot `production-tick`.
`secretary-memory.service` serves MCP on the configured local endpoint and loads the instance
embedding model. Scheduler-backed roles must have exactly one owner; do not enable both systemd and
Orca Automations for the same role.
