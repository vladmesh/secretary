"""Portable end-user Memory scope acceptance, with no live installation."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

try:
    from secretary import memory_service
except ImportError:
    memory_service = None

from secretary.memory import access
from tests.integration_setup import require_integration_setup
from triggered_agents.runtime.head import HeadRun, HeadSpec, TaskRef
from triggered_agents.runtime.head.identity import publish_heartbeat


class MemoryScopeAcceptanceTests(unittest.TestCase):
    """A safe isolated matrix over the production token verifier and read tools."""

    @classmethod
    def setUpClass(cls) -> None:
        require_integration_setup(memory_service, "secretary[memory] is not installed")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.db = self.root / "index.sqlite"
        self.audit = self.root / "search-log.jsonl"
        assert memory_service is not None
        conn = memory_service.db(self.db)
        try:
            memory_service.create_schema(conn, 4)
            conn.executemany(
                "INSERT INTO memories(text, scope, tags, source, created_at) VALUES (?,?,?,?,?)",
                [
                    ("acceptance sentinel global", "global", None, "fixture", None),
                    ("acceptance sentinel alpha", "project:alpha", None, "fixture", None),
                    ("acceptance sentinel beta", "project:beta", None, "fixture", None),
                    ("acceptance sentinel secretary", "project:secretary", None, "fixture", None),
                    ("acceptance sentinel product", "product:secretary", None, "fixture", None),
                ],
            )
            conn.commit()
            self.ids = {scope: row_id for row_id, scope in conn.execute("SELECT id, scope FROM memories")}
        finally:
            conn.close()
        self.environment = mock.patch.dict(
            os.environ, {access.MEMORY_ACCESS_BINDINGS_ENV: str(access.bindings_dir(self.data_dir))}
        )
        self.environment.start()
        self.paths = mock.patch.multiple(memory_service, DB_PATH=str(self.db), SEARCH_LOG=str(self.audit))
        self.paths.start()
        self.runs: list[HeadRun] = []

    def tearDown(self) -> None:
        self.paths.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def grant(self, role: str, task: TaskRef, subject: dict) -> access.MemoryAccessGrant:
        run = HeadRun(
            run_id=f"acceptance-{role}-{len(self.runs)}",
            spec=HeadSpec(profile_id="acceptance", adapter="fixture"),
            workspace=str(self.root),
            task_ref=task,
            role=role,
            pid_file=str(self.data_dir / "heartbeats" / f"{len(self.runs)}.pid"),
        )
        publish_heartbeat(run.pid_file, {"run_id": run.run_id, "role": role, "task": f"{task.kind}:{task.ref}"})
        self.runs.append(run)
        return access.issue_grant(run, subject, data_dir=self.data_dir)

    @contextmanager
    def authenticated(self, grant: access.MemoryAccessGrant):
        assert memory_service is not None
        token = asyncio.run(memory_service.MemoryTokenVerifier().verify_token(grant.token))
        self.assertIsNotNone(token)
        with mock.patch.object(memory_service, "get_access_token", return_value=token):
            yield

    def scopes_from_list(self) -> set[str]:
        assert memory_service is not None
        rows = memory_service.memory_list(limit=20)
        self.assertTrue(all(isinstance(row, dict) and "error" not in row for row in rows))
        return {str(row["scope"]) for row in rows}

    def assert_get(self, scope: str, allowed: bool) -> None:
        assert memory_service is not None
        result = memory_service.memory_get(self.ids[scope])
        self.assertEqual("id" in result and "text" in result, allowed)

    def test_end_user_scope_matrix_and_no_expansion_paths(self) -> None:
        po = self.grant("po", TaskRef.standing("interactive"), access.interactive_po_subject())
        foreign_worker = self.grant("worker", TaskRef.card("alpha-card"), access.card_subject("alpha-card", "alpha"))
        secretary_worker = self.grant(
            "worker", TaskRef.card("secretary-card"), access.card_subject("secretary-card", "secretary")
        )
        observer = self.grant(
            "observer", TaskRef.sprint("sprint-acceptance"), access.sprint_subject("sprint-acceptance", ["alpha", "beta"])
        )

        with self.authenticated(po):
            self.assertEqual(
                self.scopes_from_list(),
                {"global", "project:alpha", "project:beta", "project:secretary", "product:secretary"},
            )
            self.assert_get("global", True)

        with self.authenticated(foreign_worker):
            self.assertEqual(self.scopes_from_list(), {"project:alpha", "product:secretary"})
            self.assert_get("project:alpha", True)
            self.assert_get("project:secretary", False)
            self.assert_get("project:beta", False)
            self.assert_get("global", False)
            # `caller` and a manually wider scope are inputs to the real tool, not authority.
            denied = memory_service.memory_search("acceptance query", caller="po", scope="project:beta")
            self.assertEqual(denied, {"status": "denied", "error": "scope_not_permitted"})
            with mock.patch.object(memory_service, "search_ready", return_value=True), mock.patch.object(
                memory_service, "search_memory", return_value=[]
            ) as search:
                self.assertEqual(memory_service.memory_search("acceptance query", caller="po"), [])
            self.assertEqual(search.call_args.kwargs["allowed_scopes"], frozenset({"project:alpha", "product:secretary"}))

        with self.authenticated(secretary_worker):
            self.assertEqual(self.scopes_from_list(), {"project:secretary", "product:secretary"})
            self.assert_get("project:secretary", True)
            self.assert_get("project:alpha", False)

        with self.authenticated(observer):
            self.assertEqual(self.scopes_from_list(), {"project:alpha", "project:beta", "product:secretary"})
            self.assert_get("project:alpha", True)
            self.assert_get("project:beta", True)
            self.assert_get("project:secretary", False)
            self.assert_get("global", False)

        entries = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual({"action", "outcome", "role", "subject", "scopes"} <= set(entry), True)
            self.assertFalse({"text", "query", "token", "capability", "grant", "token_digest"} & set(entry))


if __name__ == "__main__":
    unittest.main()
