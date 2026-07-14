# Phase 6 closure

Date: 2026-07-14

Phase 6 is accepted. Two isolated live repositories completed scanner, provision and gate. The
fixtures used local validation, so no GitHub check state is claimed. The full suite passed with
258 tests.

## Live runs

Both runs used this sequence through the public implementation paths:

```text
secretary project add <repo> --instance <instance>
secretary project provision-start <project> --instance <instance>
secretary project provision-apply <project> <result> --instance <instance>
secretary project gate <project> --instance <instance>
secretary project gate <project> --instance <instance>
secretary project add <repo> --instance <instance> --dry-run
```

`owned-live` recorded identity `owned-live`, scanner HEAD
`ebac4b756a16a24f1f8d8ddd2585761572b07ae0`, provision run
`provision-owned-live-3f2502913cd03348`, the same provision input revision, adapter digest
`sha256:e1f50dae9e04255029ebcd6c17fae5c16d6a7107b122df717d72bab1b8ec0767`, and gate run
`gate-ddb1ea49a6803bdf08d1`. The binding became enabled and the repeated gate selected that exact
run ID.

`external-live` recorded identity `external-live`, scanner HEAD
`9ecc578a37a61ba9aa69b87f81363c6e39127eec`, provision run
`provision-external-live-cb861fc060d04661`, the same provision input revision, adapter digest
`sha256:e1f50dae9e04255029ebcd6c17fae5c16d6a7107b122df717d72bab1b8ec0767`, and gate run
`gate-d5f98a0d43e0bad641fb`. The binding became enabled and the repeated gate selected that exact
run ID. A before/after comparison of HEAD, status and all non-git paths confirmed that onboarding
added no file to the external repository.

For each enabled run, the generated manifest was parsed with Python `tomllib`, the parser used by
the old dispatcher. It contained `[workspace] project` and `base_branch`, `[setup] commands`,
`[smoke] command`, and `[validate] ci` and `command`. Post-enable dry-run preserved the passed gate
and produced no derived-artifact drift.

## Negative and rollback runs

An adapter result without `validation.ci` returned exit 1 with `status: ci_undeclared`. Its binding
remained disabled and no compatibility manifest was published. Automated coverage also exercises
invalid adapters, failed setup/smoke/validation, a missing or changed default-branch HEAD, and
`ScannerError`. Stale enabled inputs return structured `stale`, disable the binding and remove the
manifest without a traceback.

A transient canonical-adapter read failure returns structured `conflict` and preserves the known
good enabled binding and manifest. A corrupt result for the current gate run now returns structured
`conflict`; corrupt historical results are ignored when selecting the exact current run.

Rollback was exercised for both successful runs by advancing the scanned branch and invoking the
gate again. Both returned exit 1 with `status: stale`, changed `enabled` to false and removed both
the instance copy and dispatcher copy of the compatibility manifest. The external repo received no
secretary-owned file during onboarding or rollback.

Failure-output tests cover PEM private keys, JWTs, AWS access-key IDs, GitHub token prefixes and
labelled token/password/secret/API-key values. The scrub is regex-based. It does not promise to
identify arbitrary opaque bearer strings or unlabelled base64/hex material. Full secret isolation
belongs to the Phase 7 secret-broker design.

## Compatibility and Phase 7 handoff

The current dispatcher lookup is
`triggered_agents.agents.pipeline.worker._load_manifest`: first `<project>/workspace.toml`, then
`control-panel/pipeline/manifests/<project>.toml`. External projects must use the second path.
`compatibility.dispatcher_manifest_dir` in `instance.yaml` now wires gate publication and stale
rollback to that actual consumer directory atomically with the canonical binding state. The copy
under `instance/compatibility-manifests` remains a derived artifact, not another source of truth.

`validation.ci: local` runs the declared command in the detached gate worktree. `ci: github` also
runs its declared/local fallback command in this Phase 6 gate; that alone is not proof that GitHub
checks ran. A live GitHub-policy onboarding must attach the actual check rollup before making that
claim. These closure fixtures deliberately used `local`.

Phase 7 must consume the host contract, replace the compatibility lookup during dispatcher and
reconcile migration, preserve the GitHub-check distinction, and design the secret broker around the
redaction limits above. This closure does not move dispatcher code, add reconcile ownership or
implement secret isolation.
