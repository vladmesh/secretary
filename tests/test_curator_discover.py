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


class CuratorProjectRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = self.root / "instance"
        (self.instance / "projects").mkdir(parents=True)
        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _binding(self, project: str, repo: Path, binding: str | None = None) -> None:
        (self.instance / "projects" / f"{project}.yaml").write_text(
            f"id: {project}\nrepo: {repo}\norca_binding: {binding or project}\n",
            encoding="utf-8",
        )

    def test_checkout_and_binding_workspace_routes_are_boundary_safe(self) -> None:
        repo_a, repo_ab = self.root / "repo-a", self.root / "repo-ab"
        repo_a.mkdir()
        repo_ab.mkdir()
        (repo_a / "child").mkdir()
        alias = self.root / "repo-alias"
        alias.symlink_to(repo_a, target_is_directory=True)
        (self.workspaces / "a" / "worker").mkdir(parents=True)
        self._binding("a", repo_a)
        self._binding("ab", repo_ab)
        with mock.patch.dict("os.environ", {"TA_WORKSPACES_ROOT": str(self.workspaces)}):
            self.assertEqual(discover.resolve_route(str(repo_a / "child"), instance=self.instance), "a")
            self.assertEqual(discover.resolve_route(str(alias / "child"), instance=self.instance), "a")
            self.assertEqual(discover.resolve_route(str(self.workspaces / "a" / "worker"), instance=self.instance), "a")
            self.assertEqual(discover.resolve_route(str(self.root / "repo-a-not-a-route"), instance=self.instance), "unknown")

    def test_ambiguous_or_missing_binding_never_guesses_a_project(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self._binding("a", repo, "a")
        self._binding("b", repo, "b")
        self._binding("gone", self.root / "missing", "gone")
        self.assertEqual(discover.resolve_route(str(repo), instance=self.instance), "unknown")
        self.assertEqual(discover.resolve_route(str(self.root / "missing"), instance=self.instance), "unknown")

    def test_observer_uses_exactly_one_structured_sprint_reservation(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self._binding("alpha", repo)
        observer = self.workspaces / "observers" / "sprint-sprint:one" / "head"
        observer.mkdir(parents=True)
        with mock.patch.dict("os.environ", {"TA_WORKSPACES_ROOT": str(self.workspaces)}), mock.patch.object(
            discover, "SprintReader"
        ) as reader, mock.patch.object(discover.KanboardClient, "for_instance"):
            reader.return_value.show.return_value = {"reservations": ["alpha"]}
            self.assertEqual(discover.resolve_route(str(observer), instance=self.instance), "alpha")
            reader.return_value.show.return_value = {"reservations": ["alpha", "other"]}
            self.assertEqual(discover.resolve_route(str(observer), instance=self.instance), "unknown")
