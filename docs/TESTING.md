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

The control host intentionally uses focused checks only. Complete validation belongs to GitHub CI
for the exact candidate SHA. Fast profiles, artifacts, coverage, timings and integration fixtures
are later-card work.
