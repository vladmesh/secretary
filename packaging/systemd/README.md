# systemd assets

Templates for the current secretary runtime. The live installation uses units for production
dispatcher ticks, memory service, backup, curator, steward and retro.

Paths in committed templates still reflect the current production layout. Until the installer and
renderer milestone is complete, review rendered commands for the target instance and run
`systemd-analyze verify` before installation. Copying a unit does not grant product ownership of an
existing service.

`secretary-dispatcher-production.timer` launches a one-shot `production-tick`.
`secretary-memory.service` serves MCP on the configured local endpoint and loads the instance
embedding model. Scheduler-backed roles must have exactly one owner; do not enable both systemd and
Orca Automations for the same role.
