"""`/po` end to end: the real PO layer and runner, fake `claude`/`codex`, PostgreSQL 16.

Every request goes through `WebApp.handle` with a valid PO cookie; the fakes are the ones
`tests.test_po_runner` drives the runner with (`SLEEP` keeps a turn running).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from secretary.po import store as po_store
from secretary.po import token as po_token
from secretary.po.runner import PoRunner
from secretary.po.store import PoStore
from secretary.web.app import WebApp
from secretary.webproto.errors import ValidationRefused
from secretary.webproto.po_auth import PoTokenLayer
from secretary.webproto.po_ops import PoLayer
from tests.po_cli_fakes import FAKE_CLAUDE, FAKE_CODEX, eventually
from tests.sql_backend_fixtures import PostgresBoard
from tests.web_fakes import Recording

BOARD: PostgresBoard
MODELS = {"claude": ("opus", "sonnet"), "codex": ("gpt-5.6-sol",)}


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


class PoWebOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executables = {}
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            executables[name] = str(path)
        self.log = self.root / "fake.log"
        config = BOARD.fresh_database()
        self.addCleanup(BOARD.drop_database, config.dbname)
        self.store = PoStore(config.for_role("app"))
        self.runner = PoRunner(
            self.store, self.data, executables=executables, env={**os.environ, "FAKE_LOG": str(self.log)}
        )
        self.addCleanup(self.stop_everything)
        self.layer = PoLayer(self.root, data_dir=self.data, runner=self.runner, models=MODELS)
        po_token.ensure_token(self.data)
        self.cookie = po_token.cookie_value(po_token.read_token(self.data))
        self.app = WebApp(
            *(Recording() for _ in range(8)),
            po_auth=PoTokenLayer(self.root, data_dir=self.data),
            po=self.layer,
        )

    def stop_everything(self) -> None:
        for turn in self.store.running_turns():
            self.runner.stop(turn.session_id)
        pids = self.log.with_name(self.log.name + ".pids")
        if pids.exists():
            for line in pids.read_text().splitlines():
                for pid in line.split():
                    try:
                        os.kill(int(pid), 9)
                    except (ProcessLookupError, PermissionError):
                        pass

    # --- requests ---------------------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        return {"Cookie": f"{po_token.COOKIE_NAME}={self.cookie}"}

    def post(self, path: str, fields: list[tuple[str, str]]):
        return self.app.handle("POST", path, body=urlencode(fields).encode(), headers=self.headers())

    def get(self, path: str):
        return self.app.handle("GET", path, headers=self.headers())

    def create(self, cli: str = "claude", model: str = "opus", request_id: str = "create-1") -> str:
        response = self.post("/po/sessions", [("request_id", request_id), ("cli", cli), ("model", model)])
        self.assertEqual(response.status, 303, response.body.decode())
        return response.headers["Location"].rsplit("/", 1)[1]

    def send(self, session_id: str, text: str, request_id: str):
        return self.post(f"/po/sessions/{session_id}/messages", [("request_id", request_id), ("text", text)])

    def document(self, session_id: str) -> dict:
        response = self.get(f"/po/api/sessions/{session_id}")
        self.assertEqual(response.status, 200)
        return json.loads(response.body)

    def page(self, session_id: str) -> str:
        response = self.get(f"/po/sessions/{session_id}")
        self.assertEqual(response.status, 200)
        return response.body.decode()

    def feed(self, session_id: str) -> list[tuple[int, str, str]]:
        return [
            (entry["turn_seq"], entry["role"], entry["text"]) for entry in self.document(session_id)["feed"]
        ]

    def settle(self, session_id: str) -> None:
        eventually(lambda: not self.document(session_id)["running"], "the turn never settled")

    def spawned(self, count: int) -> None:
        pids = self.log.with_name(self.log.name + ".pids")
        eventually(
            lambda: pids.exists() and len(pids.read_text().splitlines()) >= count,
            "the fake never reached its sleeping child",
        )

    def calls(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    # --- sessions ---------------------------------------------------------------------------

    def test_a_session_opens_with_a_listed_model_and_a_model_or_cli_outside_the_list_is_refused(self) -> None:
        session_id = self.create("claude", "sonnet")

        session = self.store.session(session_id)
        self.assertEqual((session.cli, session.model), ("claude", "sonnet"))
        self.assertIn(session_id[:8], self.get("/po").body.decode())

        for cli, model in (("claude", "gpt-5.6-sol"), ("codex", "opus"), ("gemini", "opus"), ("claude", "")):
            with self.subTest(cli=cli, model=model):
                response = self.post(
                    "/po/sessions", [("request_id", f"bad-{cli}-{model}"), ("cli", cli), ("model", model)]
                )
                self.assertEqual(response.status, 400)
                self.assertIn("refused (validation)", response.body.decode())
        with self.assertRaises(ValidationRefused):
            self.layer.po_create_session(request_id="direct", cli="codex", model="gpt-4")
        self.assertEqual([item.session_id for item in self.store.sessions()], [session_id])

    def test_the_same_session_form_submitted_twice_is_one_session(self) -> None:
        first = self.create(request_id="create-twice")
        second = self.create(request_id="create-twice")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.sessions()), 1)

    # --- turns ------------------------------------------------------------------------------

    def test_a_message_runs_a_turn_and_the_feed_holds_the_owner_message_then_the_answer(self) -> None:
        session_id = self.create()
        sid = self.store.session(session_id).cli_session_id

        response = self.send(session_id, "remember 42", "message-1")

        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], f"/po/sessions/{session_id}")
        self.settle(session_id)
        self.assertEqual(
            self.feed(session_id),
            [(1, "owner", "remember 42"), (1, "agent", f"claude --session-id {sid}: remember 42")],
        )
        self.assertEqual(self.document(session_id)["last_turn"]["state"], po_store.COMPLETED)
        page = self.page(session_id)
        self.assertIn("remember 42", page)
        self.assertIn('data-state="completed"', page)
        self.assertNotIn("stop turn", page)

    def test_while_a_turn_runs_the_page_says_so_and_a_second_message_is_refused_with_nothing_written(
        self,
    ) -> None:
        session_id = self.create("codex", "gpt-5.6-sol")
        self.assertEqual(self.send(session_id, "SLEEP please", "message-1").status, 303)
        self.spawned(1)

        document = self.document(session_id)
        self.assertTrue(document["running"])
        self.assertEqual(document["running_seq"], 1)
        self.assertEqual(self.feed(session_id), [(1, "owner", "SLEEP please")])
        self.assertEqual(self.layer.po_running_count()["running"], 1)
        page = self.page(session_id)
        self.assertIn('data-state="running"', page)
        self.assertIn("stop turn", page)

        refused = self.send(session_id, "hurry up", "message-2")

        self.assertEqual(refused.status, 409)
        body = refused.body.decode()
        self.assertIn("not sent: a turn is still running in this session", body)
        self.assertIn("hurry up", body)
        self.assertIn('value="message-2"', body)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual(self.feed(session_id), [(1, "owner", "SLEEP please")])
        self.assertEqual(self.calls(), 1)

    def test_stop_interrupts_the_running_turn_and_the_next_message_goes_through(self) -> None:
        session_id = self.create()
        sid = self.store.session(session_id).cli_session_id
        self.send(session_id, "SLEEP please", "message-1")
        self.spawned(1)

        stopped = self.post(f"/po/sessions/{session_id}/stop", [("seq", "1")])

        self.assertEqual(stopped.status, 303)
        turn = self.store.turn(session_id, 1)
        self.assertEqual((turn.state, turn.reason), (po_store.INTERRUPTED, "stopped by the owner"))
        page = self.page(session_id)
        self.assertIn('data-state="interrupted"', page)
        self.assertIn("stopped by the owner", page)

        self.assertEqual(self.send(session_id, "again", "message-2").status, 303)
        self.settle(session_id)
        self.assertEqual(self.store.turn(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.feed(session_id)[-1], (2, "agent", f"claude --resume {sid}: again"))

    def test_a_stop_form_for_a_turn_that_is_no_longer_running_stops_nothing(self) -> None:
        session_id = self.create()
        self.send(session_id, "hello", "message-1")
        self.settle(session_id)
        self.send(session_id, "SLEEP now", "message-2")
        self.spawned(1)

        self.assertEqual(self.post(f"/po/sessions/{session_id}/stop", [("seq", "1")]).status, 303)

        self.assertEqual(self.store.turn(session_id, 2).state, po_store.RUNNING)

    def test_the_same_message_form_submitted_twice_is_one_turn(self) -> None:
        running = self.create(request_id="create-running")
        for _ in range(2):
            self.assertEqual(self.send(running, "SLEEP once", "message-running").status, 303)
        self.spawned(1)
        self.assertEqual([turn.seq for turn in self.store.turns(running)], [1])

        finished = self.create("codex", "gpt-5.6-sol", request_id="create-finished")
        self.assertEqual(self.send(finished, "hello", "message-finished").status, 303)
        self.settle(finished)
        self.assertEqual(self.send(finished, "hello", "message-finished").status, 303)

        self.assertEqual([turn.seq for turn in self.store.turns(finished)], [1])
        self.assertEqual([role for _seq, role, _text in self.feed(finished)], ["owner", "agent"])
        self.assertEqual(self.calls(), 2)


if __name__ == "__main__":
    unittest.main()
