from __future__ import annotations

import unittest

from triggered_agents.agents.curator import discover


class ClaudeProjectDirectoryTests(unittest.TestCase):
    def test_exclude_directory_uses_claudes_non_alphanumeric_encoding(self) -> None:
        self.assertEqual(
            discover._dirname_for_cwd("/home/dev/codegen_orchestrator/v1.2"),
            "-home-dev-codegen-orchestrator-v1-2",
        )
