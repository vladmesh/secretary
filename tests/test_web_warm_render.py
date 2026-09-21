"""A warm Dashboard render starts no subprocess, opens no board file and issues a bounded SQL count.

The Definition-of-Done evidence for sprint:1449's item. The Dashboard handler is driven twice
through `WebApp.handle`, over the layers `web-serve` builds for its reads -- the read layer and the
doctor lamp wired by `health_layers`, the sprint and pause read layers, the provider usage layer --
against a real `postgres:16` store reached the production way: `SECRETARY_CARD_BACKEND=postgres`
and the instance's own `board-store.env`, behind the exclusion guard `web-serve` holds at start-up.
The layers that start or recover live work are recording fakes.

The first render is cold and may do anything. The second is counted at the process's own seams:
the `subprocess.Popen`/`os.*` audit events for a child process, the `open` audit event for a
`board/*.ndjson` path, and `psycopg.Cursor.execute` for a statement. The cold render is counted
too, so the test also says the counters see this path at all.

Like the other `*_sql_backend` suites this needs Docker and never skips.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from secretary.board import store
from secretary.board.backend import reset_card_backend
from secretary.board.sql_sprints import sprint_key
from secretary.web.app import WebApp
from secretary.web.commands import health_layers
from secretary.web.provider_usage import ProviderUsageLayer
from secretary.webproto.pause_reads import PauseReadLayer
from secretary.webproto.reads import hold_store_exclusion
from secretary.webproto.sprint_reads import SprintReadLayer
from tests.fakes.tasks import FakeKanboard
from tests.sql_backend_fixtures import PostgresBoard, seed_client
from tests.web_fakes import Recording

#: The Definition of Done's ceiling for one warm render.
MAX_WARM_STATEMENTS = 50

BOARD: PostgresBoard

#: What the audit hook records while a render is being counted; `None` while nothing is.
_COUNTING: dict[str, list[str]] | None = None

_CHILD_EVENTS = frozenset(
    {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.fork", "os.exec"}
)


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _COUNTING is None:
        return
    if event in _CHILD_EVENTS:
        _COUNTING["processes"].append(f"{event}: {args[:2]!r}")
    elif event == "open" and args and isinstance(args[0], (str, os.PathLike)):
        path = Path(os.fspath(args[0]))
        if path.suffix == ".ndjson" and path.parent.name == "board":
            _COUNTING["ndjson"].append(str(path))


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    # An audit hook cannot be removed, so it is installed once and records only inside `counted`.
    sys.addaudithook(_audit)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


class WarmDashboardRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            mock.patch.dict(os.environ, {"BOARD_ROLE": "", "SECRETARY_CARD_BACKEND": "postgres"})
        )
        reset_card_backend()
        self.addCleanup(reset_card_backend)
        self.enterContext(mock.patch.dict(store._HELD, clear=True))
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data_dir = self.tmp / "data"
        (self.data_dir / "board").mkdir(parents=True)
        (self.data_dir / "dispatcher").mkdir(parents=True)
        # The Kanboard-era card projection is still on disk: the cold render's health collection
        # counts it, which is what shows the counter below sees the path it is asserting about.
        (self.data_dir / "board" / "cards.ndjson").write_text(
            json.dumps({"ref": "secretary-468"}) + "\n", encoding="utf-8"
        )
        self.instance = self._instance()
        config = BOARD.fresh_database()
        path = store.store_path(self.instance)
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in config.as_environ().items()), encoding="utf-8"
        )
        path.chmod(0o600)
        client = seed_client(config, FakeKanboard(), self.instance)
        with client.transaction():
            client._execute(
                "INSERT INTO sprints (ref, board_key, sprint_number, goal, definition_of_done, status, "
                "created_at, updated_at) VALUES ('sprint:7', %s, 7, 'Goal', 'DoD', 'open', now(), now())",
                (sprint_key("sprint:7"),),
            )
        client.close()

    def _instance(self) -> Path:
        instance = self.tmp / "instance"
        (instance / "projects").mkdir(parents=True)
        (instance / "instance.yaml").write_text(
            "version: 1\nname: warm-render-test\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: https://example.invalid/instance.git\n",
            encoding="utf-8",
        )
        repo = self.tmp / "repos" / "secretary"
        repo.mkdir(parents=True)
        (instance / "projects" / "secretary.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "secretary",
                    "repo": str(repo),
                    "enabled": True,
                    "adapter": "secretary",
                    "default_branch": "main",
                }
            ),
            encoding="utf-8",
        )
        for arguments in (
            ("init", "-b", "main"),
            ("config", "user.email", "test@example.invalid"),
            ("config", "user.name", "Test"),
            ("add", "-A"),
            ("commit", "-m", "Initial"),
        ):
            subprocess.run(["git", "-C", str(instance), *arguments], check=True, capture_output=True)
        return instance

    def app(self) -> WebApp:
        """The dashboard's read layers, wired as `web-serve` wires them; the rest are fakes."""
        self.assertIsNone(hold_store_exclusion(str(self.instance)))
        reads, doctor = health_layers(str(self.instance), data_dir=str(self.data_dir), offline=True)
        return WebApp(
            reads,
            Recording(run_list={"items": []}),
            SprintReadLayer(self.instance, data_dir=self.data_dir),
            Recording(),
            PauseReadLayer(self.instance, data_dir=self.data_dir),
            Recording(),
            Recording(),
            Recording(),
            ProviderUsageLayer(home=self.tmp / "home", fetch_json=self._no_provider),
            doctor,
        )

    @staticmethod
    def _no_provider(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        raise AssertionError(f"a provider was asked: {url}")

    def counted(self, app: WebApp) -> tuple[str, dict[str, list[str]], int]:
        """One Dashboard render, and what it started, opened and executed."""
        global _COUNTING
        import psycopg

        statements = 0
        execute = psycopg.Cursor.execute

        def counting(cursor: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal statements
            statements += 1
            return execute(cursor, *args, **kwargs)

        _COUNTING = {"processes": [], "ndjson": []}
        try:
            with mock.patch.object(psycopg.Cursor, "execute", counting):
                response = app.handle("GET", "/")
            recorded = _COUNTING
        finally:
            _COUNTING = None
        self.assertEqual(response.status, 200)
        return response.body.decode("utf-8"), recorded, statements

    def test_a_warm_render_starts_no_subprocess_opens_no_board_file_and_stays_under_the_ceiling(
        self,
    ) -> None:
        app = self.app()
        gitignore = (self.instance / ".gitignore").read_text(encoding="utf-8")

        cold_page, cold, cold_statements = self.counted(app)
        warm_page, warm, warm_statements = self.counted(app)

        self.assertTrue(cold["processes"], "the cold render collects health, and the counter sees it")
        self.assertTrue(cold["ndjson"], "the cold render reads the card projection, and the counter sees it")
        self.assertGreater(cold_statements, 0)

        self.assertEqual(warm["processes"], [], "a warm render starts no subprocess")
        self.assertEqual(warm["ndjson"], [], "a warm render opens no board/*.ndjson")
        self.assertGreater(warm_statements, 0, "the warm render still reads the store")
        self.assertLessEqual(warm_statements, MAX_WARM_STATEMENTS)

        # The warm page is the dashboard, over the store: the open sprint and the health panel.
        for page in (cold_page, warm_page):
            self.assertIn("sprint:7", page)
            self.assertIn("cards on the board", page)
        # Neither request touched the exclusion `web-serve` established before serving.
        self.assertEqual((self.instance / ".gitignore").read_text(encoding="utf-8"), gitignore)


if __name__ == "__main__":
    unittest.main()
