# Testing

GitHub CI is the complete test contract. It validates tests/ci-shards.txt before it starts a
suite, then runs these seven named jobs in parallel:

| Suite | CI job | Scope |
| --- | --- | --- |
| unit | test / unit | Isolated product and protocol behaviour. |
| component | test / component | Individual Secretary components and their direct adapters. |
| runtime-component | test / runtime-component | Runtime and local-PTY component boundaries. |
| integration-recovery | test / integration-recovery | Backup, checkpoint, restore and recovery flows. |
| integration-memory | test / integration-memory | Memory and curator integration flows. |
| integration-board | test / integration-board | Board, dispatcher and Pipeline integration flows. |
| packaging | test / packaging | Bootstrap, installation, provisioning and upgrade flows. |

Each exact-SHA suite execution writes its GitHub step summary and uploads the
`ci-evidence-<suite>-<sha>` artifact. Its artifact root contains `report.json`, `junit.xml` and
`test-output.log`; the log contains all output up to 1,000,000 bytes and carries an explicit
truncation marker if it reaches that boundary. Artifacts, including the JUnit XML, are retained for
14 days. For pull requests, `<sha>` is the branch-head candidate SHA; for other events it is
`github.sha`. The summary names the same candidate SHA, outcome, counts, duration, slowest tests
and concise failure locations. Immediately before and after each selected suite, the runner records
Git's complete `status --porcelain=v1 --untracked-files=all` snapshot for the candidate checkout.
A green suite requires those snapshots to match exactly. Evidence retains snapshot entry counts and
digests, plus at most ten bounded changed-status entries, rather than publishing unbounded checkout
contents.

The test job remains the aggregate required result and succeeds only when every applicable suite
succeeds. Its own summary lists each suite as `success`, `product_failure`,
`infrastructure_failure`, `cancelled` or `not_applicable`. A failing test is a product failure;
missing, malformed or unwritable JSON/JUnit/log evidence is an infrastructure failure. Cancelled
matrix work is never treated as success, while routing that explicitly skips a suite is recorded as
not applicable rather than a test failure.
An unavailable Git status command or any test-generated tracked or untracked product artifact is
also an infrastructure failure. If a product test failed in the same contaminated suite, its concise
failure location remains in the evidence, but the suite is classified as infrastructure failure
because the execution boundary cannot be trusted.
The manifest owns the taxonomy: every top-level tests/test_*.py file must occur once, under one
of those names. Unknown names, missing files, stale entries, duplicate entries and empty suites
make the manifest invalid before a selected suite starts.

On a control host, use only focused local work while changing the runner or its manifest:

    python3 -m unittest -v tests.test_ci_shards
    python3 scripts/ci_test_shards.py --check

## Control-host fast profile

    python3 scripts/ci_test_shards.py --fast

This is the one canonical executable fast profile for worker feedback. It validates its fixed
module list before it starts, then runs only the existing hermetic Kanboard, Orca-discovery and
pipeline-state proofs. It is deliberately not a CI suite and does not read `tests/ci-shards.txt`,
expand to the seven-suite taxonomy, or use repository-wide discovery.

The runner gives the complete child process group a 120-second ceiling. On timeout it reports a
failure, terminates the group, and waits for the test child to stop. The child inherits only a
fixture-owned temporary home, XDG directories, Codex home, pipeline-state directory, temporary
directory and a restricted standard tool path, together with the candidate checkout source path. It does not
inherit board, API, cloud or other ambient credentials. A process-startup guard rejects network
connections and subprocesses other than Python and the read-only temporary-instance Git queries
used by the board seam. Live board/API use and Docker, VM, Ansible or provisioning commands
therefore fail loudly. Mutable state is limited to those temporary fixtures and the candidate
workspace.

The profile is intentionally narrow: it proves the existing isolation seams rather than testing
real host, systemd, Orca, credentials, Docker, VM, Ansible or provisioning behaviour. Those runtime
contours remain in their named CI suites or explicit operator checks. The control host intentionally
uses focused checks and `--fast` only. Complete validation remains dispatcher-owned exact-SHA GitHub
CI; do not run a local broad suite.

## Changed Python lint

The product checkout's `.venv` supplies the same pinned Ruff that worker and reviewer role commands
receive on `PATH`. Never lint the repository as a whole. Against the task base, build the non-deleted
changed and untracked Python path set, then pass only that set explicitly to both checks:

```bash
base=$(git merge-base main HEAD)
{
  git diff --name-only -z --diff-filter=d "$base" -- '*.py'
  git ls-files --others --exclude-standard -z -- '*.py'
} | sort -zu | xargs -0r ruff check

base=$(git merge-base main HEAD)
{
  git diff --name-only -z --diff-filter=d "$base" -- '*.py'
  git ls-files --others --exclude-standard -z -- '*.py'
} | sort -zu | xargs -0r ruff format --check
```

Use both commands whenever the set contains Python files. The `xargs -r` guard leaves an empty set
as a no-op, rather than making Ruff choose a repository-wide default.
