from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.agents.curator import discover


class ClaudeProjectDirectoryTests(unittest.TestCase):
    def test_exclude_directory_uses_claudes_non_alphanumeric_encoding(self) -> None:
        self.assertEqual(
            discover._dirname_for_cwd("/home/dev/codegen_orchestrator/v1.2"),
            "-home-dev-codegen-orchestrator-v1-2",
        )


class CuratorIdentityDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.curator = "/home/dev/orca/workspaces/secretary/curator"
        self.env = mock.patch.dict("os.environ", {"TA_CURATOR_WORKSPACE": self.curator, "TA_CURATOR_SESSION_ID": "self"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_only_the_exact_curator_workspace_or_session_is_excluded(self) -> None:
        included = [
            "/home/dev/secretary",
            "/home/dev/orca/workspaces/secretary/worker-123",
            "/home/dev/orca/workspaces/secretary/observer-456",
        ]
        self.assertFalse(discover._excluded("/home/dev/secretary", "po"))
        for cwd in included:
            self.assertFalse(discover._excluded(cwd, "other"))
        self.assertTrue(discover._excluded(self.curator, "other"))
        self.assertTrue(discover._excluded("/anywhere", "self"))

    def test_claude_codex_and_hermes_fixtures_share_the_same_matrix(self) -> None:
        projects, sessions = self.root / "claude", self.root / "codex"
        projects.mkdir()
        sessions.mkdir()
        included = "/home/dev/orca/workspaces/secretary/observer-456"
        for cwd, session_id in ((self.curator, "self"), (included, "other")):
            project = projects / discover._dirname_for_cwd(cwd)
            project.mkdir()
            (project / f"{session_id}.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n", encoding="utf-8")
            (sessions / f"{session_id}.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"session_id": session_id, "cwd": cwd}}) + "\n",
                encoding="utf-8",
            )
        with mock.patch.object(discover, "CLAUDE_PROJECTS", projects), mock.patch.object(discover, "CODEX_SESSIONS", sessions):
            self.assertEqual([s["cwd"] for s in discover.claude_sessions()], [included])
            self.assertEqual([s["cwd"] for s in discover.codex_sessions()], [included])
        with mock.patch.object(discover, "HERMES_STATE_DB", self.root / "state.db"), mock.patch.object(
            discover, "_hermes_query", return_value=[("self", self.curator), ("other", included)]
        ):
            (self.root / "state.db").touch()
            self.assertEqual([s["cwd"] for s in discover.hermes_sessions()], [included])
