# systemd assets

Templates for the current secretary runtime. The live installation uses units for production
dispatcher ticks, memory service, curator, steward and retro. Archive backup is no longer a
scheduled unit: git-checkpoint is the recovery contract (docs/RECOVERY.md), and `backup create`
remains only a manual, optional cold-archive tool.

These files are the desired state, not a starting point to copy by hand: `secretary reconcile apply`
installs them and records ownership in `host-managed.json`, and `secretary upgrade` runs that on
every release. A unit's file digest is part of its planned resource, so editing a file here makes
the next apply reinstall it.

Every unit name here must fall under the instance's `host.unit_prefix`, and its component name (the
file name minus that prefix and the suffix) is what `host.components` opts out of. Paths in
committed templates still reflect the current production layout; run `systemd-analyze verify` on
anything you change. A unit already on the host is never overwritten until it is adopted — apply
refuses to write over a name it cannot prove it owns.

`secretary-dispatcher-production.timer` launches a one-shot `production-tick`.
`secretary-memory.service` serves MCP on the configured local endpoint and loads the instance
embedding model. Scheduler-backed roles must have exactly one owner; do not enable both systemd and
Orca Automations for the same role.
