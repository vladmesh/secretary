# Security

## Current status

The project is pre-1.0 and has no release branches. Development and fixes happen on `main`.

## Reporting a security problem

Reports are welcome in whatever form is convenient: a public issue, a pull request or GitHub's
private vulnerability-reporting flow. Rough notes and incomplete reports are welcome too.

Please do not include working credentials, private keys, recovery phrases, personal data or other
secrets in a public report.

## Current security boundaries

The appliance assumes **one trusted owner on one host**. Agent heads run as the installation user
with that user's filesystem and network access. They are not sandboxed and are not treated as
untrusted tenants. Anything an agent can reach, a compromised agent can reach.

- Board and memory endpoints listen on loopback. Host access control is the perimeter.
- Kanboard JSON-RPC transport, including its application-token Basic-Auth value, is deterministic
  local configuration in `board-transport.env`, not a recovery secret or secret-store value.
- Installation secrets live in an encrypted store in the private instance repository. The raw
  installation key stays on the host, mode `0600`, outside Git; the recovery phrase is printed once
  and is never stored by the product.
- The store gives observability, versioning and recoverability. It does not isolate workers: the
  installation key opens every secret at once, with the same rights that previously read
  `runtime.env`. There is no broker and no per-role grants.
- Provider logins for agent runtimes are deliberately not stored by the product.
- State pushed to the private remote passes a secret scan before every commit. A detected secret
  fails the commit closed rather than publishing it.

Examples of security problems include a way to read secrets without the installation key or the
recovery phrase; a path that publishes secrets into the checkpoint past the scan; privilege
escalation beyond the installation user; or a remote path into board, memory or dispatcher state.

A process already running as the installation user can read that user's files. Workers are not
sandboxed, and at-rest encryption does not protect a compromised host. These are current design
boundaries, but discussion and proposals to change them are welcome.
