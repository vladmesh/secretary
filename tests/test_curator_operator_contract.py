"""The curator route is released separately from any live installation activation.

The implementation owns the registry and fail-closed baseline transition.  These checks keep the
operator text coupled to that contract: a later installation-canon edit is manual, auditable and
cannot smuggle in a curator start or fact mutation.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HEADS = REPO_ROOT / "src" / "triggered_agents" / "agents" / "pipeline" / "heads.toml"
AUTOMATION = REPO_ROOT / "src" / "triggered_agents" / "agents" / "curator" / "automation.toml"
PROTOCOLS = REPO_ROOT / "docs" / "PROTOCOLS.md"
OPERATIONS = REPO_ROOT / "docs" / "OPERATIONS.md"


def _normalized(document: str) -> str:
    """Compare prose as prose, rather than making Markdown line wrapping contractual."""
    return " ".join(document.replace("`", "").split())


class CuratorRouteContractTests(unittest.TestCase):
    def test_portable_default_and_last_resort_use_the_dedicated_profile(self) -> None:
        registry = tomllib.loads(HEADS.read_text(encoding="utf-8"))
        automation = tomllib.loads(AUTOMATION.read_text(encoding="utf-8"))

        self.assertEqual(registry["role_defaults"]["curator"], "codex-curator")
        self.assertEqual(automation["head"], "codex-curator")
        self.assertEqual(
            registry["profiles"]["codex-curator"],
            {
                "resource": "openai-sub",
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "extra",
                "codex_mode": "tui",
                "fallback": ["claude-default"],
            },
        )


class CuratorOperatorDocumentationTests(unittest.TestCase):
    def assert_document_includes(self, document: str, required: str) -> None:
        self.assertTrue(required in document, f"documentation is missing: {required!r}")

    def test_protocol_keeps_baseline_project_scoped_and_metadata_only(self) -> None:
        protocols = _normalized(PROTOCOLS.read_text(encoding="utf-8"))

        for required in (
            "one registered canonical project id",
            "explicit actor",
            "non-empty one-line reason",
            "exactly one opaque evidence identity",
            "versioned pending record bound to the current curator workspace/run/session identity",
            "unversioned, stale, foreign, corrupt, or cursor-only pending data is not guessed, rewritten or advanced",
            "no transcript text, personal-memory text, fact content, raw source payloads or credentials",
        ):
            self.assert_document_includes(protocols, required)

    def test_operations_describes_the_manual_disabled_route_rollout_and_rollback(self) -> None:
        operations = _normalized(OPERATIONS.read_text(encoding="utf-8"))

        for required in (
            "### Curator installation-canon rollout",
            "/home/dev/secretary-instance/heads/heads.toml",
            'curator = "claude-opus-medium"',
            'curator = "codex-curator"',
            "actor-and-reason-audited per-project operation",
            "not a startup action",
            "curator.enabled remains false",
            "secretary upgrade --instance \"$INSTANCE\" --no-pull",
            "Rollback",
            "without fact mutation",
            "transcript text, fact content, secrets or raw source payloads",
        ):
            self.assert_document_includes(operations, required)


if __name__ == "__main__":
    unittest.main()
