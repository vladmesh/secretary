from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.checkpoint import _credential_snapshot
from secretary.config import validate_instance
from secretary.head_health import HeadReadiness
from secretary.infra.recovery_inventory import collect_recovery_inventory
from secretary.secret_store import initialize_store, set_secret
from secretary.secret_words import RECOVERY_WORDS
from tests.head_registry import write_installed_pair

REGISTRY = """resources:
  never-used:
    account: spare
    probe: 'false'
  used:
    account: primary
    probe: 'true'
profiles:
  worker:
    resource: used
    adapter: codex
    fallback: []
role_defaults:
  new_card: worker
"""


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


class RecoveryInventoryTests(unittest.TestCase):
    def fixture(self, root: Path, recorded: dict | None = None) -> tuple[Path, object]:
        instance = root / "instance"
        data = root / "data"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            f"version: 1\nname: test\ndata_dir: {data}\noffsite:\n  instance_remote: https://github.com/example/instance.git\n",
            encoding="utf-8",
        )
        git(instance, "init", "--quiet", "--initial-branch", "main")
        git(instance, "config", "user.name", "operator")
        git(instance, "config", "user.email", "operator@example.invalid")
        git(instance, "add", "instance.yaml")
        git(instance, "commit", "--quiet", "-m", "fixture")
        git(instance, "remote", "add", "origin", "https://github.com/example/instance.git")
        write_installed_pair(instance, REGISTRY)
        if recorded is not None:
            health = data / "dispatcher" / "resource_health.json"
            health.parent.mkdir(parents=True)
            health.write_text(json.dumps(recorded), encoding="utf-8")
        return instance, validate_instance(instance)

    def test_every_installed_resource_is_visible_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp))
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        self.assertEqual([row["resource"] for row in snapshot["resources"]], ["never-used", "used"])
        never = snapshot["resources"][0]
        self.assertEqual(never["profiles"], [])
        self.assertEqual(never["state"], "unknown")
        self.assertEqual(never["source"], "unavailable")

    def test_fresh_cache_is_reused_and_stale_offline_is_explicit(self) -> None:
        now = time.time()
        recorded = {
            "used": {"status": "ready", "reason": "cached green", "checked_at": now},
            "never-used": {"status": "ready", "reason": "old green", "checked_at": now - 1000},
        }
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp), recorded)
            with mock.patch("secretary.infra.recovery_inventory.run_probe") as probe:
                snapshot = collect_recovery_inventory(report, inspect_live=False, now=now, checkpoint={})

        probe.assert_not_called()
        rows = {row["resource"]: row for row in snapshot["resources"]}
        self.assertEqual(rows["used"]["source"], "dispatcher-cache")
        self.assertEqual(rows["used"]["state"], "ready")
        self.assertEqual(rows["never-used"]["state"], "stale")
        self.assertEqual(rows["never-used"]["observed_state"], "ready")

    def test_stale_cache_is_reprobed_online_without_writing_cache(self) -> None:
        now = time.time()
        recorded = {"used": {"status": "ready", "reason": "old", "checked_at": now - 1000}}
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp), recorded)
            health = report.data_dir / "dispatcher" / "resource_health.json"
            before = health.read_bytes()
            with mock.patch(
                "secretary.infra.recovery_inventory.run_probe",
                side_effect=lambda resource, probe, stamp: HeadReadiness(
                    resource, "unauthenticated", "login required", stamp
                ),
            ):
                snapshot = collect_recovery_inventory(report, inspect_live=True, now=now, checkpoint={})
            self.assertEqual(before, health.read_bytes())
            self.assertFalse((instance / "state").exists())

        rows = {row["resource"]: row for row in snapshot["resources"]}
        self.assertEqual(rows["used"]["state"], "unauthenticated")
        self.assertEqual(rows["used"]["source"], "live-read-only-probe")

    def test_managed_readiness_and_old_push_failure_remain_independent(self) -> None:
        checkpoint = {
            "push_status": "failed",
            "push_reason": "earlier credential was missing",
            "last_push_at": "2026-09-04T20:00:00Z",
            "credential": {
                "state": "managed-ready",
                "source": "encrypted-store",
                "reason": "",
                "last_verified_at": "2026-09-04T23:30:54Z",
                "last_verified_age_minutes": 3,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp))
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint=checkpoint)

        consumer = snapshot["credential_consumers"][0]
        self.assertEqual(consumer["state"], "managed-ready")
        self.assertEqual(checkpoint["push_status"], "failed")
        self.assertNotIn("earlier credential was missing", consumer["reason"])

    def test_git_bypass_is_metadata_only_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            git(
                instance, "config", "url.https://credential@example.invalid/.insteadOf", "https://github.com/"
            )
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        row = next(row for row in snapshot["bypasses"] if row["kind"] == "insteadOf")
        self.assertEqual(row["configuration_key"], "url.*.insteadOf")
        self.assertNotIn("credential", json.dumps(row))
        self.assertFalse(row["supported"])
        self.assertTrue(row["supported_next_action"])

    def test_instead_of_bypass_does_not_replace_managed_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, _ = self.fixture(Path(tmp))
            initialize_store(instance, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
            set_secret(
                instance,
                secret_id="github.checkpoint-token",
                value=b"fixture-checkpoint-token",
                scope="installation",
                purpose="checkpoint",
                actor="tester",
            )
            git(instance, "config", "url.ssh://git@example.invalid/.insteadOf", "https://github.com/")
            credential = _credential_snapshot(instance, {}, time.time())

        self.assertEqual(credential["state"], "managed-ready")
        self.assertEqual(credential["source"], "encrypted-store")

    def test_matching_and_conflicting_runtime_overrides_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            with mock.patch.dict(
                os.environ,
                {
                    "SECRETARY_INSTANCE": str(instance),
                    "SECRETARY_RUNTIME_ENV_FILE": str(instance / "runtime.env"),
                },
                clear=False,
            ):
                matching = collect_recovery_inventory(report, inspect_live=False, checkpoint={})
            with mock.patch.dict(
                os.environ,
                {
                    "SECRETARY_INSTANCE": str(instance),
                    "SECRETARY_RUNTIME_ENV_FILE": str(instance / "elsewhere.env"),
                },
                clear=False,
            ):
                conflicting = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        match = next(row for row in matching["paths"] if row["capability"] == "runtime-env")
        conflict = next(row for row in conflicting["paths"] if row["capability"] == "runtime-env")
        self.assertEqual(match["state"], "supported")
        self.assertEqual(conflict["state"], "conflicting-override")

    def test_missing_materialization_and_locked_store_are_not_claimed_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp))
            initialize_store(instance, phrase=" ".join(RECOVERY_WORDS[:16]), actor="tester")
            set_secret(
                instance,
                secret_id="provider.token",
                value=b"fixture-provider-value",
                scope="installation",
                purpose="provider login",
                actor="tester",
                environment="PROVIDER_TOKEN",
                materialize={"target": "runtime-env"},
            )
            checkpoint = {
                "credential": {
                    "state": "locked/unverifiable",
                    "source": "none",
                    "reason": "installation key is unavailable",
                }
            }
            snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint=checkpoint)
            runtime = instance / "runtime.env"
            runtime.write_text("PROVIDER_TOKEN=fixture\n", encoding="utf-8")
            runtime.chmod(0o644)
            drifted = collect_recovery_inventory(report, inspect_live=False, checkpoint=checkpoint)

        self.assertEqual(snapshot["materializations"][0]["state"], "missing")
        self.assertEqual(drifted["materializations"][0]["state"], "drifted")
        self.assertEqual(snapshot["credential_consumers"][0]["state"], "locked/unverifiable")
        self.assertNotIn("equal", snapshot["credential_consumers"][0]["reason"])

    def test_legacy_board_catalog_entries_are_retired_bypasses_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.fixture(Path(tmp))
            with mock.patch(
                "secretary.infra.recovery_inventory.list_secrets",
                return_value=({"id": "kanboard_api_token"}, {"id": "current.provider"}),
            ):
                snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})

        legacy = [row for row in snapshot["bypasses"] if row["kind"] == "retired-secret-catalog-entry"]
        self.assertEqual([row["entry"] for row in legacy], ["kanboard_api_token"])
        self.assertIn("cannot overwrite board transport", legacy[0]["reason"])

    def test_repeated_inventory_does_not_change_repository_or_dispatcher_cache(self) -> None:
        now = time.time()
        recorded = {"used": {"status": "ready", "reason": "cached", "checked_at": now}}
        with tempfile.TemporaryDirectory() as tmp:
            instance, report = self.fixture(Path(tmp), recorded)
            cache = report.data_dir / "dispatcher" / "resource_health.json"
            before_cache = cache.read_bytes()
            before_status = subprocess.run(
                ["git", "-C", str(instance), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            first = collect_recovery_inventory(report, inspect_live=False, now=now, checkpoint={})
            second = collect_recovery_inventory(report, inspect_live=False, now=now, checkpoint={})
            after_cache = cache.read_bytes()
            after_status = subprocess.run(
                ["git", "-C", str(instance), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertEqual(first, second)
        self.assertEqual(before_cache, after_cache)
        self.assertEqual(before_status, after_status)


if __name__ == "__main__":
    unittest.main()
