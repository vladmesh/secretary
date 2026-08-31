"""User client materialization for the operator-only Memory bridge."""

from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from secretary.memory.client_config import (
    ClientConfigError,
    reconcile_claude,
    reconcile_clients,
    reconcile_codex,
)


class MemoryClientConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.command = self.root / "product" / ".venv" / "bin" / "secretary-memory-po-bridge"
        self.command.parent.mkdir(parents=True)
        self.command.write_text("#!/bin/sh\n", encoding="utf-8")
        self.data_dir = self.root / "data"

    def test_codex_replaces_only_owned_memory_entries_and_is_idempotent(self) -> None:
        path = self.root / ".codex" / "config.toml"
        path.parent.mkdir()
        path.write_text(
            'model = "operator-choice"\n\n'
            '[mcp_servers.memory]\nurl = "http://127.0.0.1:8077/mcp"\n'
            'bearer_token_env_var = "SECRETARY_MEMORY_ACCESS_TOKEN"\n\n'
            '[mcp_servers.other]\ncommand = "keep-me"\n',
            encoding="utf-8",
        )

        self.assertTrue(reconcile_codex(path, self.command, self.data_dir))
        first = path.read_text(encoding="utf-8")
        payload = tomllib.loads(first)
        self.assertEqual(payload["model"], "operator-choice")
        self.assertEqual(payload["mcp_servers"]["other"]["command"], "keep-me")
        self.assertNotIn("memory", payload["mcp_servers"])
        bridge = payload["mcp_servers"]["po_memory"]
        self.assertEqual(bridge["command"], str(self.command))
        self.assertEqual(
            bridge["env"]["MEMORY_ACCESS_BINDINGS"],
            str(self.data_dir / "memory" / "access-grants"),
        )
        self.assertFalse(reconcile_codex(path, self.command, self.data_dir))
        self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_claude_preserves_login_state_and_unrelated_servers(self) -> None:
        path = self.root / ".claude.json"
        path.write_text(
            json.dumps(
                {
                    "oauthAccount": {"accountUuid": "keep-me"},
                    "theme": "dark",
                    "mcpServers": {
                        "memory": {"type": "http", "url": "http://127.0.0.1:8077/mcp"},
                        "other": {"command": "keep-me"},
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertTrue(reconcile_claude(path, self.command, self.data_dir))
        first = path.read_text(encoding="utf-8")
        payload = json.loads(first)
        self.assertEqual(payload["oauthAccount"], {"accountUuid": "keep-me"})
        self.assertEqual(payload["theme"], "dark")
        self.assertEqual(payload["mcpServers"]["other"], {"command": "keep-me"})
        self.assertNotIn("memory", payload["mcpServers"])
        self.assertEqual(payload["mcpServers"]["po_memory"]["command"], str(self.command))
        self.assertFalse(reconcile_claude(path, self.command, self.data_dir))
        self.assertEqual(path.read_text(encoding="utf-8"), first)

        payload["mcpServers"]["memory"] = {
            "type": "http",
            "url": "http://127.0.0.1:8077/mcp",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(reconcile_claude(path, self.command, self.data_dir))
        self.assertNotIn("memory", json.loads(path.read_text(encoding="utf-8"))["mcpServers"])

    def test_reconcile_clients_materializes_both_codex_homes_and_claude(self) -> None:
        runtime_home = self.root / "home"
        result = reconcile_clients(self.root / "product", runtime_home, self.data_dir)

        self.assertEqual(result.changed, 3)
        self.assertTrue((runtime_home / ".codex" / "config.toml").is_file())
        self.assertTrue((runtime_home / ".config/orca/codex-runtime-home/home/config.toml").is_file())
        self.assertTrue((runtime_home / ".claude.json").is_file())
        self.assertEqual(
            reconcile_clients(self.root / "product", runtime_home, self.data_dir).changed,
            0,
        )

    def test_dry_run_reports_without_writing(self) -> None:
        path = self.root / ".codex" / "config.toml"
        self.assertTrue(reconcile_codex(path, self.command, self.data_dir, dry_run=True))
        self.assertFalse(path.exists())

        self.command.unlink()
        runtime_home = self.root / "preview-home"
        self.assertEqual(
            reconcile_clients(self.root / "product", runtime_home, self.data_dir, dry_run=True).changed,
            3,
        )
        self.assertFalse(runtime_home.exists())

    def test_a_config_symlink_is_refused_without_touching_its_target(self) -> None:
        target = self.root / "operator-owned.toml"
        target.write_text('model = "keep-me"\n', encoding="utf-8")
        path = self.root / ".codex" / "config.toml"
        path.parent.mkdir()
        path.symlink_to(target)

        with self.assertRaisesRegex(ClientConfigError, "symlink"):
            reconcile_codex(path, self.command, self.data_dir)
        self.assertEqual(target.read_text(encoding="utf-8"), 'model = "keep-me"\n')


if __name__ == "__main__":
    unittest.main()
