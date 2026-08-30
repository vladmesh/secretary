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

The test job remains the aggregate required result and succeeds only when every suite succeeds.
The manifest owns the taxonomy: every top-level tests/test_*.py file must occur once, under one
of those names. Unknown names, missing files, stale entries, duplicate entries and empty suites
make the manifest invalid before a selected suite starts.

Use a focused local check while changing the runner or its manifest:

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
uses focused checks only. Complete validation remains dispatcher-owned exact-SHA GitHub CI.

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
