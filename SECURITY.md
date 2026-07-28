# Security policy

## Supported versions

The project is pre-1.0 and has no release branches. Only the current `main` receives fixes.

## Reporting a vulnerability

Report privately through GitHub's "Report a vulnerability" flow on the repository's Security tab.
Do not open a public issue and do not include working credentials in the report.

Include what an attacker can reach, the steps to reproduce it, and the commit you tested. Expect an
acknowledgement within seven days. Fixes land on `main`; the advisory is published once a fix exists.

## Security model

Know what the product does and does not promise before you file a report.

The appliance assumes **one trusted owner on one host**. Agent heads run as the installation user
with that user's filesystem and network access. They are not sandboxed and are not treated as
untrusted tenants. Anything an agent can reach, a compromised agent can reach.

- Board and memory endpoints listen on loopback. Host access control is the perimeter.
- Installation secrets live in an encrypted store in the private instance repository. The raw
  installation key stays on the host, mode `0600`, outside Git; the recovery phrase is printed once
  and is never stored by the product.
- The store gives observability, versioning and recoverability. It does not isolate workers: the
  installation key opens every secret at once, with the same rights that previously read
  `runtime.env`. There is no broker and no per-role grants.
- Provider logins for agent runtimes are deliberately not stored by the product.
- State pushed to the private remote passes a secret scan before every commit. A detected secret
  fails the commit closed rather than publishing it.

In scope for a report: a way to read secrets without the installation key or the recovery phrase; a
path that publishes secrets into the checkpoint past the scan; privilege escalation beyond the
installation user; a remote path into board, memory or dispatcher state.

Out of scope: a process already running as the installation user reading that user's files; the lack
of worker sandboxing; at-rest encryption not protecting against host compromise. These are stated
limits of the current model, not defects. If you think one of them should change, open an issue.
