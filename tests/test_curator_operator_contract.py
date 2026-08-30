"""The manual curator-routing boundary is an operator contract, not product policy."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CuratorOperatorContractTests(unittest.TestCase):
    def test_the_instance_only_curator_routing_procedure_is_documented(self) -> None:
        operations = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

        for required in (
            "### Manual curator routing in an instance canon",
            "`INSTANCE/heads/heads.toml`",
            "`role_defaults.curator` as `PREVIOUS_PROFILE`",
            '`model = "gpt-5.6-terra"` and `effort = "extra"`',
            "declared `fallback` sequence",
            'curator = "codex-curator"',
            "`secretary-curator.timer` is the sole scheduler owner",
            "Orca curator\nautomation must remain disabled",
            "`DISABLED curator`",
            "Do not change `host.components.curator`",
            "run a production\nbaseline/backfill",
            "write or delete a fact, reindex, or run a canary",
            "no automatic rollout, shim, migration, or dependency step",
            '`role_defaults.curator = "PREVIOUS_PROFILE"`',
            "curator workspace, run and\nsession identity",
            "explicit actor, a non-empty reason, and exactly one current opaque cutoff or pending-batch identity",
            "redacted reason",
            "unversioned, stale, foreign, corrupt or cursor-only pending state",
        ):
            with self.subTest(required=required):
                self.assertIn(required, operations)

    def test_the_shipped_registry_keeps_installation_policy_out(self) -> None:
        shipped = ROOT / "src" / "triggered_agents" / "agents" / "pipeline" / "heads.toml"
        canon = tomllib.loads(shipped.read_text(encoding="utf-8"))

        self.assertNotIn("codex-curator", canon["profiles"])
        self.assertEqual(
            {
                resource.get("account")
                for resource in canon["resources"].values()
            },
            {"claude-subscription", "openai-subscription"},
        )
        self.assertFalse(
            any(
                any(char.isdigit() for char in str(profile.get("model", "")))
                for profile in canon["profiles"].values()
            )
        )
