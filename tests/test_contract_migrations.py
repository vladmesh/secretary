"""Contract drafts left in an obsolete shape are repaired by the read that validates them.

secretary-845: a merge that dropped `compatibility_manifest` from the contract schema
stopped every dispatcher tick, because the migration lived on the gate path while the
validation ran on the load path.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary import contract_migrations
from secretary.cli import main
from secretary.config import validate_instance
from secretary.contract_migrations import migrate_contract_dir, normalize_contract


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INSTANCE = REPO_ROOT / "examples" / "instance"
HAPPY_PATH = REPO_ROOT / "tests" / "fixtures" / "onboarding" / "happy-path.json"

LEGACY_BLOCK = {
    "consumer": "legacy-dispatcher",
    "role": "derived-transition-consumer",
    "canonical_source": "onboarding-contract-v1",
}


class ContractMigrationTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.instance = Path(tmpdir.name) / "instance"
        shutil.copytree(EXAMPLE_INSTANCE, self.instance)
        self.drafts = self.instance / "adapter-drafts"
        self.drafts.mkdir()
        self.draft_path = self.drafts / "example-project.yaml"
        self.draft = json.loads(HAPPY_PATH.read_text(encoding="utf-8"))

    def write_draft(self, draft: dict) -> None:
        self.draft_path.write_text(yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")

    def load_draft(self) -> dict:
        return yaml.safe_load(self.draft_path.read_text(encoding="utf-8"))

    def test_clean_draft_validates_and_is_not_rewritten(self):
        self.write_draft(self.draft)
        before = self.draft_path.read_bytes()

        report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_legacy_block_is_migrated_before_validation(self):
        draft = dict(self.draft, compatibility_manifest=LEGACY_BLOCK)
        self.write_draft(draft)

        report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        self.assertNotIn("compatibility_manifest", self.load_draft())

    def test_migration_is_idempotent(self):
        self.write_draft(dict(self.draft, compatibility_manifest=LEGACY_BLOCK))
        self.assertTrue(validate_instance(self.instance).ok)
        migrated = self.draft_path.read_bytes()

        with mock.patch("secretary.contract_migrations.publish_state_atomic") as publish:
            report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        publish.assert_not_called()
        self.assertEqual(self.draft_path.read_bytes(), migrated)

    def test_mutable_binding_fields_in_identity_are_migrated(self):
        draft = json.loads(json.dumps(self.draft))
        draft["identity"]["plane"] = "product"
        draft["identity"]["policy"] = {"autonomy": "full"}
        self.write_draft(draft)

        report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        stored = self.load_draft()
        self.assertNotIn("plane", stored["identity"])
        self.assertNotIn("policy", stored["identity"])

    def test_unknown_field_still_fails_and_file_is_untouched(self):
        """A field nobody removed from the schema is a corrupt contract, not an old one."""
        draft = dict(self.draft, mystery_block={"consumer": "nobody"})
        self.write_draft(draft)
        before = self.draft_path.read_bytes()

        report = validate_instance(self.instance)

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.source == self.draft_path.name for error in report.errors), report.errors
        )
        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_unparsable_draft_still_fails_and_file_is_untouched(self):
        self.draft_path.write_text("identity: [unterminated\n", encoding="utf-8")
        before = self.draft_path.read_bytes()

        report = validate_instance(self.instance)

        self.assertFalse(report.ok)
        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_legacy_block_removal_survives_an_unrelated_broken_draft(self):
        """One corrupt file does not stop the others from being repaired."""
        self.write_draft(dict(self.draft, compatibility_manifest=LEGACY_BLOCK))
        broken = self.drafts / "broken-project.yaml"
        broken.write_text("identity: [unterminated\n", encoding="utf-8")

        report = validate_instance(self.instance)

        self.assertFalse(report.ok)
        self.assertNotIn("compatibility_manifest", self.load_draft())

    def test_suspended_keeps_the_verdict_without_writing(self):
        self.write_draft(dict(self.draft, compatibility_manifest=LEGACY_BLOCK))
        before = self.draft_path.read_bytes()

        with contract_migrations.suspended():
            report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(self.draft_path.read_bytes(), before)
        # The switch is scoped: the next read repairs the file.
        self.assertTrue(validate_instance(self.instance).ok)
        self.assertNotIn("compatibility_manifest", self.load_draft())

    def test_doctor_dry_run_writes_nothing_through_nested_validations(self):
        """`--dry-run` is set once at the CLI boundary, so helpers that revalidate obey it."""
        self.write_draft(dict(self.draft, compatibility_manifest=LEGACY_BLOCK))
        before = self.draft_path.read_bytes()

        with contextlib.redirect_stdout(io.StringIO()):
            main(["doctor", "--instance", str(self.instance), "--dry-run", "--offline"])

        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_unwritable_draft_is_reported_as_a_config_problem(self):
        self.write_draft(dict(self.draft, compatibility_manifest=LEGACY_BLOCK))

        with mock.patch(
            "secretary.contract_migrations.publish_state_atomic",
            side_effect=OSError(5, "injected"),
        ):
            report = validate_instance(self.instance)

        self.assertFalse(report.ok)
        self.assertTrue(
            any("cannot rewrite migrated contract" in error.message for error in report.errors),
            report.errors,
        )

    def test_a_held_project_lock_defers_the_rewrite(self):
        """The writer holding the lock normalizes the draft itself, so nothing is forced."""
        self.write_draft(dict(self.draft, compatibility_manifest=LEGACY_BLOCK))
        before = self.draft_path.read_bytes()

        with mock.patch("secretary.contract_migrations.try_file_lock") as lock:
            lock.return_value.__enter__.return_value = False
            report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_normalize_contract_leaves_unknown_keys_in_place(self):
        document = {"mystery_block": 1, "compatibility_manifest": LEGACY_BLOCK}
        normalize_contract(document)
        self.assertEqual(document, {"mystery_block": 1})

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(migrate_contract_dir(self.instance / "absent"), ({}, []))


if __name__ == "__main__":
    unittest.main()
