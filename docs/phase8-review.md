# Phase 8 review

Date: 2026-07-17

Status: closed by vladmesh after the operator off-host run.

Phase 8 asks whether the appliance is portable, and answers it by restoring rather than by
argument. This review maps the design acceptance in `control-panel/docs/design-secretary-appliance.md`
onto the implementation, records what automated coverage proves, and hands the remaining operator
off-host run to a separate card without performing it.

The code part of the phase is complete. The full suite is green with 408 tests. One design
criterion is only partly met and is recorded as a finding below.

## Acceptance mapping

`restore из архива даёт рабочую пустую или восстановленную установку`. Met.
`test_fixture_backup_restores_to_green_doctor_without_the_source_data_root` runs both installs in
one test: `bootstrap --empty` on a clean target reaches `doctor --offline` exit 0 with no board
export, and a separate target restores a full archive and reaches exit 0 with no restore findings.

`restore не использует ничего со старого хоста, кроме забранного архива`. Met. The same test
copies the archive to an offsite directory, deletes the producer data root and the producer
instance, then runs the chain. The age key is generated outside both. If the restore reached back
to the producer, the chain would fail on a missing path rather than pass quietly.

`raw и normalized exports сходятся по числу карточек/facts`. Partly met, see findings. Every
restored count is compared against the archive manifest, and the memory export is compared against
the manifest and the journal. Nothing compares the raw Kanboard dump against the normalized export.

`board restore умеет подняться из normalized core-экспорта без raw-дампа`. Met.
`test_core_archive_restores_normalized_board_without_a_raw_dump` restores a core archive, asserts
the data root holds no `kanboard-raw-*` dump, and imports the board into an empty backend with a
passing parity check. Core archives exclude `Done` cards by design (`_filter_core_board_export`),
record `policy.done_cards: excluded` in `export.json`, and set the manifest count to the filtered
set. The test asserts that contract rather than a bare card count, so a core restore that silently
dropped a live card would fail.

`история git-журнала памяти присутствует после restore`. Met. The producer's `git rev-list HEAD`
is captured before its data root is deleted and compared with the restored journal in both the
full and the core test. Both fixtures commit twice, so a restore that kept only a flattened tip
would fail.

`Orca state не накатывается как канон`. Met.
`test_derived_host_state_is_never_restored_as_canon` asserts the plan classifies
`debug_orca_state` as `exclude` and `memory_index` as `rebuild`; that the Orca snapshot travels
only under `debug/` and never under `secretary-data/`; and that the vector index, worktrees,
generated units and the producer's own backups reach neither the archive nor the restored data
root. The index is rebuilt from restored canon through the real `memory reindex` argv contract.

`живые sessions не требуются для успешного restore`. Met. The whole chain runs headless in
`unittest` with no dispatcher, no session and no live board; the only injected boundary is the
Kanboard client.

## Negative coverage

`test_restore_e2e.py` covers a foreign age key, a corrupted archive, an unsupported archive
version, a non-empty target, a board import that finds a dirty backend, and a reconcile that does
not match desired state. The first four leave no target data root at all. The last two leave the
restore red: `restore_findings` still names the incomplete step and `doctor --offline` exits 1, so
a half-finished restore cannot read as success.

The negatives were checked against mutated product code rather than trusted because they pass.
Removing the `index.sqlite` exclusion from the backup policy fails five of the nine tests; removing
the existing-target guard from `restore` fails the non-empty-target test. Component-level negatives
(checksum mismatch, journal without a resolvable head, alternates, publish failure) stay in
`test_restore_archive.py`.

## Findings

Raw and normalized card counts are never compared with each other. Both are compared against the
manifest, which is written from the same export, so a defect in the raw dump path would not be
caught. Closing this needs a verifier that reads the Kanboard dump's own schema, which is backup
contract work rather than restore work, and its fixture must be a real dump rather than the
placeholder blob the current tests carry. Recommended as a separate card before Phase 9 retires
the old stack.

The age-dependent tests are skipped when `age`/`age-keygen` are absent. On a host without the age
tooling the suite reports success while the four archive-level e2e tests do not run. The tooling is
present on the VPS and is a hard product dependency that `doctor` already checks, so this is
documented rather than fixed; a runner that must not skip should assert the binaries first.

## Operator off-host result

The operator run was performed on a separate VPS using a real encrypted archive and age identity.
Archive transfer, checksum, decryption and isolated data/board restore were exercised without
moving production ownership from the source host.

The target had 1.9 GiB RAM. Loading the production embedding runtime for a live memory rebuild
exhausted its resources. Switching to a smaller model was rejected because it would change search
quality. On 2026-07-17 vladmesh accepted this as a test-host limitation and closed Phase 8 without
repeating the run on a larger server. The automated restore suite remains the evidence for the
rebuild contract; the operator run does not claim a green online doctor after a production-model
rebuild.

After the run, the archive and age identity, deploy credential, checkouts, restored data, target
board, projects, units and other secretary test resources were removed from the target. No test
dispatcher or parallel secretary installation remains active.

The original operator sequence was:

Sequence, using only a pulled archive and the key from the password manager:

```bash
git clone <secretary-remote> ~/secretary
git clone <secretary-instance-remote> ~/secretary-instance
secretary restore ~/backups/<archive>.tar.age --instance ~/secretary-instance \
  --age-identity ~/secret/secretary.agekey --dry-run
secretary restore ~/backups/<archive>.tar.age --instance ~/secretary-instance \
  --age-identity ~/secret/secretary.agekey
secretary restore-board --instance ~/secretary-instance
secretary memory reindex --instance ~/secretary-instance
secretary reconcile plan --instance ~/secretary-instance
secretary restore-reconcile --instance ~/secretary-instance
secretary doctor --instance ~/secretary-instance
```

The unexecuted production-model rebuild and its dependent online doctor check are recorded as an
accepted deviation, not carried into Phase 9 as a gate.

## Reproduction

The automated chain is in `tests/test_restore_e2e.py`; the commands are in
`docs/restore-contract.md`.
