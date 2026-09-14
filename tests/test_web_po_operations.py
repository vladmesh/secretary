"""`/po` end to end: the real PO layer and runner, fake `claude`/`codex`, PostgreSQL 16.

Every request goes through `WebApp.handle` with a valid PO cookie; the fakes are the ones
`tests.test_po_runner` drives the runner with (`SLEEP` keeps a turn running).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from secretary.po import store as po_store
from secretary.po import token as po_token
from secretary.po.models import DEFAULT_MODELS
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

    def test_the_default_list_opens_fable_and_gpt_6_astra_sessions_and_refuses_an_off_list_model(
        self,
    ) -> None:
        self.layer = PoLayer(self.root, data_dir=self.data, runner=self.runner, models=DEFAULT_MODELS)
        self.app = WebApp(
            *(Recording() for _ in range(8)),
            po_auth=PoTokenLayer(self.root, data_dir=self.data),
            po=self.layer,
        )
        form = self.get("/po").body.decode()
        self.assertIn('<option value="fable" data-cli="claude" selected>', form)
        self.assertEqual(form.count(" selected>"), 2, "only the CLI and its first model are preselected")

        created = []
        for cli, model in (("claude", "fable"), ("codex", "gpt-6-astra")):
            session_id = self.create(cli, model, request_id=f"create-{model}")
            session = self.store.session(session_id)
            self.assertEqual((session.cli, session.model), (cli, model))
            created.append(session_id)

        for cli, model in (("claude", "haiku"), ("codex", "gpt-5.6-astra")):
            with self.subTest(cli=cli, model=model):
                response = self.post(
                    "/po/sessions", [("request_id", f"bad-{cli}-{model}"), ("cli", cli), ("model", model)]
                )
                self.assertEqual(response.status, 400)
        self.assertEqual(sorted(item.session_id for item in self.store.sessions()), sorted(created))

    def test_the_new_session_form_preselects_the_first_model_of_the_chosen_cli(self) -> None:
        from secretary.web.pages import _PO_FORM_SCRIPT, _po_new_session_form

        models = {cli: list(values) for cli, values in DEFAULT_MODELS.items()}
        for submitted, expected in (
            ({}, ("claude", "fable")),
            ({"cli": "claude"}, ("claude", "fable")),
            ({"cli": "codex"}, ("codex", "gpt-6-astra")),
            ({"cli": "codex", "model": "fable"}, ("codex", "gpt-6-astra")),
            ({"cli": "codex", "model": "gpt-5.6-sol"}, ("codex", "gpt-5.6-sol")),
        ):
            with self.subTest(submitted=submitted):
                form = _po_new_session_form(models, request_id="r", submitted=submitted)
                cli, model = expected
                self.assertIn(f'<option value="{cli}" selected>', form)
                self.assertIn(f'<option value="{model}" data-cli="{cli}" selected>', form)
                self.assertEqual(form.count(" selected>"), 2)
        # Changing the CLI in the browser selects that CLI's first listed model.
        self.assertIn("if (owned && first === null) first = option;", _PO_FORM_SCRIPT)
        self.assertIn("if ((!current || current.disabled) && first) first.selected = true;", _PO_FORM_SCRIPT)

    def test_enter_sends_the_message_once_and_shift_enter_keeps_a_newline(self) -> None:
        from secretary.web.pages import _PO_SESSION_SCRIPT

        page = self.page(self.create())
        self.assertIn("Enter to send, Shift+Enter for a new line", page)
        # The key handler is installed outside the running-turn branch.
        self.assertLess(
            _PO_SESSION_SCRIPT.index("addEventListener('keydown'"),
            _PO_SESSION_SCRIPT.index("if (__RUNNING__)"),
        )
        self.assertIn("draft.addEventListener('keydown'", page)
        for line in (
            "if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;",
            "if (event.isComposing || event.keyCode === 229) return;",
            "if (submitted || !draft.value.trim()) return;",
            "form.requestSubmit();",
            "if (submitted) { event.preventDefault(); return; }",
            "if (button) button.disabled = true;",
        ):
            with self.subTest(line=line):
                self.assertIn(line, _PO_SESSION_SCRIPT)

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

    def test_a_form_sent_again_after_its_turn_failed_to_launch_is_that_turn_and_no_second_launch(
        self,
    ) -> None:
        session_id = self.create()
        self.runner.executables["claude"] = str(self.root / "no-such-claude")

        with mock.patch.object(self.runner, "_launch", wraps=self.runner._launch) as launch:
            first = self.send(session_id, "hello", "message-broken")
            second = self.send(session_id, "hello", "message-broken")
            replay = self.layer.po_send(request_id="message-broken", session_id=session_id, text="hello")

        self.assertEqual(first.status, 503)
        self.assertEqual(second.status, 303)
        self.assertEqual(second.headers["Location"], f"/po/sessions/{session_id}")
        self.assertEqual(launch.call_count, 1)
        [turn] = self.store.turns(session_id)
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("could not start", turn.reason)
        request = self.store.request("message-broken")
        self.assertEqual((request.operation, request.session_id, request.seq), (po_store.SEND, session_id, 1))
        self.assertEqual((replay["seq"], replay["state"], replay["repeated"]), (1, po_store.FAILED, True))
        self.assertEqual(self.feed(session_id), [(1, "owner", "hello")])
        page = self.page(session_id)
        self.assertIn('data-state="failed"', page)
        self.assertIn("could not start", page)

    # --- one request id, one operation with fixed inputs ---------------------------------------

    def assert_request_conflict(self, response) -> None:
        self.assertEqual(response.status, 409)
        self.assertIn("refused (request_conflict)", response.body.decode())

    def test_a_request_id_that_created_a_session_is_refused_for_a_message(self) -> None:
        session_id = self.create(request_id="shared-form")

        with mock.patch.object(self.runner, "_launch", wraps=self.runner._launch) as launch:
            refused = self.send(session_id, "hello", "shared-form")

        self.assert_request_conflict(refused)
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(self.store.turns(session_id), [])
        self.assertEqual(self.feed(session_id), [])
        request = self.store.request("shared-form")
        self.assertEqual(
            (request.operation, request.session_id, request.seq), (po_store.SESSION_CREATE, session_id, None)
        )

    def test_a_request_id_that_sent_in_one_session_is_refused_in_another(self) -> None:
        first = self.create(request_id="create-first")
        second = self.create(request_id="create-second")
        self.assertEqual(self.send(first, "hello", "one-form").status, 303)
        self.settle(first)

        self.assert_request_conflict(self.send(second, "hello", "one-form"))

        self.assertEqual(self.store.turns(second), [])
        self.assertEqual(self.feed(second), [])
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.store.request("one-form").session_id, first)

    def test_the_same_request_id_with_other_text_is_refused_and_the_feed_is_unchanged(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "hello", "text-form").status, 303)
        self.settle(session_id)
        before = self.feed(session_id)

        self.assert_request_conflict(self.send(session_id, "hello, again", "text-form"))

        self.assertEqual(self.feed(session_id), before)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual(self.calls(), 1)

    def test_an_id_refused_while_another_turn_runs_records_nothing_and_goes_through_after(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "SLEEP please", "running-form").status, 303)
        self.spawned(1)

        refused = self.send(session_id, "later", "waiting-form")

        self.assertEqual(refused.status, 409)
        self.assertIn("not sent: a turn is still running in this session", refused.body.decode())
        self.assertIsNone(self.store.request("waiting-form"))
        self.assertEqual(self.post(f"/po/sessions/{session_id}/stop", [("seq", "1")]).status, 303)

        self.assertEqual(self.send(session_id, "later", "waiting-form").status, 303)
        self.settle(session_id)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1, 2])
        self.assertEqual(self.store.request("waiting-form").seq, 2)

    def test_concurrent_claims_of_one_new_request_id_have_exactly_one_winner(self) -> None:
        workers = 8

        def claim_session(_index: int):
            barrier.wait()
            return self.store.claim_session(
                session_id=str(uuid.uuid4()),
                cli="claude",
                model="opus",
                cwd=str(self.data / "po"),
                cli_session_id=None,
                request_id="race-create",
            )

        barrier = threading.Barrier(workers)
        with ThreadPoolExecutor(workers) as pool:
            sessions = list(pool.map(claim_session, range(workers)))
        self.assertEqual(sum(created for _session, created in sessions), 1)
        self.assertEqual(len({session.session_id for session, _created in sessions}), 1)
        self.assertEqual(len(self.store.sessions()), 1)
        session_id = sessions[0][0].session_id

        def path(seq: int) -> Path:
            return self.root / f"{seq}.out"

        def claim_turn(index: int):
            barrier.wait()
            try:
                return self.store.claim_turn(
                    session_id, "same" if index % 2 else "other", path, request_id="race-send"
                )
            except po_store.RequestConflict:
                return None

        barrier = threading.Barrier(workers)
        with ThreadPoolExecutor(workers) as pool:
            turns = list(pool.map(claim_turn, range(workers)))
        winners = [turn for turn in turns if turn is not None and turn[1]]
        self.assertEqual(len(winners), 1)
        [entry] = self.store.feed(session_id)
        self.assertEqual(len(self.store.turns(session_id)), 1)
        self.assertEqual(turns.count(None), workers // 2)
        self.assertTrue(all(turn[0].seq == 1 for turn in turns if turn is not None))
        self.assertEqual(
            [index % 2 for index, turn in enumerate(turns) if turn is not None],
            [1 if entry.text == "same" else 0] * (workers // 2),
        )

    # --- the session list -------------------------------------------------------------------

    def seed(self, session_id: str, created: str, turns=(), feed=()) -> None:
        """A session at fixed times: `turns` are (seq, started, finished), `feed` is (seq, role, text, at)."""
        import psycopg

        self.store.create_session(
            session_id=session_id, cli="claude", model="opus", cwd="/", cli_session_id=None
        )
        with psycopg.connect(self.store.credentials.conninfo()) as connection:
            connection.execute(
                "UPDATE po_sessions SET created_at = %s WHERE session_id = %s", (created, session_id)
            )
            for seq, started, finished in turns:
                connection.execute(
                    "INSERT INTO po_turns (session_id, seq, started_at, finished_at, state, stdout_path) "
                    "VALUES (%s, %s, %s, %s, 'completed', '/dev/null')",
                    (session_id, seq, started, finished),
                )
            for seq, role, text, at in feed:
                connection.execute(
                    "INSERT INTO po_feed (session_id, turn_seq, role, text, created_at) VALUES (%s, %s, %s, %s, %s)",
                    (session_id, seq, role, text, at),
                )

    def test_the_session_list_carries_the_first_owner_message_and_is_newest_activity_first(self) -> None:
        # a: created first, but its turn finished last, after its newest feed entry.
        self.seed(
            "a",
            "2026-09-01T10:00:00Z",
            turns=[(1, "2026-09-01T10:01:00Z", "2026-09-05T10:00:00Z")],
            feed=[
                (1, "agent", "an agent spoke first", "2026-09-01T10:00:30Z"),
                (1, "owner", "the owner's first", "2026-09-01T12:00:00Z"),
                (1, "owner", "earlier time, later entry", "2026-09-01T10:00:10Z"),
            ],
        )
        # b: its feed entry is newer than its turn.
        self.seed(
            "b",
            "2026-09-02T10:00:00Z",
            turns=[(1, "2026-09-02T10:01:00Z", "2026-09-02T10:02:00Z")],
            feed=[(1, "owner", "hello b", "2026-09-03T10:00:00Z")],
        )
        # c: no turns, no feed, created after b's last activity.
        self.seed("c", "2026-09-04T10:00:00Z")
        # d: only an agent entry and the same last activity as c, created earlier; ties go to newer creation.
        self.seed(
            "d",
            "2026-09-01T09:00:00Z",
            turns=[(1, "2026-09-01T09:01:00Z", "2026-09-04T10:00:00Z")],
            feed=[(1, "agent", "only the agent", "2026-09-01T09:02:00Z")],
        )

        sessions = self.store.sessions()
        self.assertEqual([item.session_id for item in sessions], ["a", "c", "d", "b"])
        by_id = {item.session_id: item for item in sessions}
        self.assertEqual(by_id["a"].first_message, "the owner's first")
        self.assertEqual(by_id["b"].first_message, "hello b")
        self.assertIsNone(by_id["c"].first_message)
        self.assertIsNone(by_id["d"].first_message)
        self.assertEqual(
            {key: item.last_activity_at.isoformat() for key, item in by_id.items()},
            {
                "a": "2026-09-05T10:00:00+00:00",
                "b": "2026-09-03T10:00:00+00:00",
                "c": "2026-09-04T10:00:00+00:00",
                "d": "2026-09-04T10:00:00+00:00",
            },
        )

        document = self.layer.po_overview()
        self.assertEqual([item["session_id"] for item in document["sessions"]], ["a", "c", "d", "b"])
        first = document["sessions"][0]
        self.assertEqual(first["first_message"], "the owner's first")
        self.assertEqual(first["last_activity_at"], by_id["a"].last_activity_at.isoformat())
        self.assertEqual(first["created_at"], by_id["a"].created_at.isoformat())
        self.assertEqual(
            set(first),
            {
                "session_id",
                "cli",
                "model",
                "created_at",
                "state",
                "closed_at",
                "closed_by",
                "running",
                "first_message",
                "last_activity_at",
            },
        )

    def test_a_row_shows_the_escaped_collapsed_start_of_the_first_message_or_no_message_yet(self) -> None:
        cyrillic = "Глянь  по обоим\n\tспринтам что происходит и какие действия требуются, " + "я" * 40
        self.seed(
            "long",
            "2026-09-04T10:00:00Z",
            turns=[(1, "2026-09-04T10:00:00Z", "2026-09-04T10:00:00Z")],
            feed=[(1, "owner", cyrillic, "2026-09-04T10:00:00Z")],
        )
        self.seed(
            "html",
            "2026-09-03T10:00:00Z",
            turns=[(1, "2026-09-03T10:00:00Z", "2026-09-03T10:00:00Z")],
            feed=[(1, "owner", "<b>bold</b> & **not markdown**", "2026-09-03T10:00:00Z")],
        )
        self.seed(
            "short",
            "2026-09-02T10:00:00Z",
            turns=[(1, "2026-09-02T10:00:00Z", "2026-09-02T10:00:00Z")],
            feed=[(1, "owner", "exactly  fits", "2026-09-02T10:00:00Z")],
        )
        self.seed("none", "2026-09-01T10:00:00Z")

        response = self.get("/po")
        self.assertEqual(response.status, 200)
        page = response.body.decode()
        collapsed = " ".join(cyrillic.split())
        shown = collapsed[:79] + "…"
        self.assertEqual(len(shown), 80)
        self.assertIn(f'<a class="ref" href="/po/sessions/long">{shown}</a>', page)
        self.assertNotIn(collapsed[:80], page)
        self.assertIn(
            '<a class="ref" href="/po/sessions/html">&lt;b&gt;bold&lt;/b&gt; &amp; **not markdown**</a>', page
        )
        self.assertNotIn("<b>bold</b>", page)
        self.assertIn('<a class="ref" href="/po/sessions/short">exactly fits</a>', page)
        self.assertIn(
            '<a class="ref" href="/po/sessions/none"><span class="empty">no message yet</span></a>', page
        )
        self.assertIn("2026-09-04T10:00:00+00:00", page)
        self.assertIn('<span class="age">long</span>', page)
        order = [page.index(f"/po/sessions/{key}") for key in ("long", "html", "short", "none")]
        self.assertEqual(order, sorted(order))

    # --- closing a session ------------------------------------------------------------------

    def close(self, session_id: str):
        return self.post(f"/po/sessions/{session_id}/close", [])

    def requests_rows(self) -> int:
        import psycopg

        with psycopg.connect(self.store.credentials.conninfo()) as connection:
            return connection.execute("SELECT count(*) FROM po_requests").fetchone()[0]

    def test_closing_an_open_session_moves_it_from_the_default_list_to_the_closed_one(self) -> None:
        kept = self.create(request_id="create-kept")
        session_id = self.create(request_id="create-closed")
        self.assertEqual(self.send(session_id, "hello", "message-1").status, 303)
        self.settle(session_id)
        overview = self.get("/po").body.decode()
        self.assertIn(f'action="/po/sessions/{session_id}/close"', overview)
        self.assertIn(f'action="/po/sessions/{kept}/close"', overview)
        self.assertIn('href="/po?closed=1">closed sessions (0)</a>', overview)
        self.assertIn(f'action="/po/sessions/{session_id}/close"', self.page(session_id))

        response = self.close(session_id)

        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/po")
        session = self.store.session(session_id)
        self.assertEqual((session.state, session.closed_by), (po_store.SESSION_CLOSED, "owner"))
        self.assertIsNotNone(session.closed_at)
        self.assertEqual([item.session_id for item in self.store.sessions()], [kept])
        closed = self.store.sessions(po_store.SESSION_CLOSED)
        self.assertEqual([item.session_id for item in closed], [session_id])
        self.assertEqual(closed[0].first_message, "hello")
        self.assertEqual(closed[0].closed_at, session.closed_at)

        overview = self.get("/po").body.decode()
        self.assertNotIn(f"/po/sessions/{session_id}", overview)
        self.assertIn(f"/po/sessions/{kept}", overview)
        self.assertIn('href="/po?closed=1">closed sessions (1)</a>', overview)
        self.assertIn('id="po-new"', overview)
        listed = self.app.handle("GET", "/po", query="closed=1", headers=self.headers())
        self.assertEqual(listed.status, 200)
        listed_page = listed.body.decode()
        self.assertIn(f'<a class="ref" href="/po/sessions/{session_id}">hello</a>', listed_page)
        self.assertNotIn(f"/po/sessions/{kept}", listed_page)
        self.assertIn(session.closed_at.isoformat(), listed_page)
        self.assertIn('<a class="more" href="/po">open sessions</a>', listed_page)
        self.assertNotIn("/close", listed_page)
        self.assertEqual(self.layer.po_overview(closed=True)["closed_count"], 1)

        page = self.page(session_id)
        self.assertIn("hello", page)
        self.assertIn(f"closed {session.closed_at.isoformat()} by owner", page)
        self.assertNotIn('id="po-send"', page)
        self.assertNotIn("/close", page)
        self.assertEqual(self.document(session_id)["session"]["closed_by"], "owner")

    def test_closing_while_a_turn_runs_is_refused_and_writes_nothing(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "SLEEP please", "message-1").status, 303)
        self.spawned(1)
        self.assertNotIn("/close", self.page(session_id))

        refused = self.close(session_id)

        self.assertEqual(refused.status, 409)
        body = refused.body.decode()
        self.assertIn("not closed: a turn is still running in this session", body)
        self.assertIn('id="po-send"', body)
        session = self.store.session(session_id)
        self.assertEqual(
            (session.state, session.closed_at, session.closed_by), (po_store.SESSION_OPEN, None, None)
        )
        with self.assertRaises(po_store.TurnInProgress):
            self.store.close_session(session_id, "owner")
        self.assertEqual(self.store.session(session_id), session)
        self.assertEqual(self.store.turn(session_id, 1).state, po_store.RUNNING)
        self.assertEqual(self.layer.po_running_count()["running"], 1)
        self.assertIn("1 PO turn running", self.app.handle("GET", "/").body.decode())

    def test_closing_twice_keeps_the_first_close(self) -> None:
        session_id = self.create()
        first = self.store.close_session(session_id, "owner")

        self.assertEqual(self.close(session_id).status, 303)
        again = self.store.close_session(session_id, "someone-else")

        self.assertEqual(again, first)
        self.assertEqual(self.store.session(session_id), first)
        self.assertEqual(self.store.session_count(po_store.SESSION_CLOSED), 1)

    def test_closing_an_unknown_session_is_404(self) -> None:
        response = self.close("no-such-session")

        self.assertEqual(response.status, 404)
        with self.assertRaises(po_store.SessionNotFound):
            self.store.close_session("no-such-session", "owner")
        self.assertEqual(self.store.session_count(po_store.SESSION_CLOSED), 0)

    def test_a_close_form_with_a_field_is_refused(self) -> None:
        session_id = self.create()
        self.assertEqual(self.post(f"/po/sessions/{session_id}/close", [("seq", "1")]).status, 400)
        self.assertEqual(self.store.session(session_id).state, po_store.SESSION_OPEN)

    def test_a_message_into_a_closed_session_is_refused_with_nothing_written(self) -> None:
        session_id = self.create()
        self.assertEqual(self.send(session_id, "hello", "message-1").status, 303)
        self.settle(session_id)
        self.assertEqual(self.close(session_id).status, 303)
        feed, calls, requests = self.feed(session_id), self.calls(), self.requests_rows()

        refused = self.send(session_id, "again", "message-2")

        self.assertEqual(refused.status, 409)
        body = refused.body.decode()
        self.assertIn("not sent: this session is closed", body)
        self.assertNotIn('id="po-send"', body)
        with self.assertRaises(po_store.SessionClosed):
            self.store.claim_turn(session_id, "again", lambda seq: self.root / f"turn-{seq}")
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual(self.feed(session_id), feed)
        self.assertEqual(self.requests_rows(), requests)
        self.assertIsNone(self.store.request("message-2"))
        self.assertEqual(self.calls(), calls)

        # The send made before the close is still answered by its recorded turn, and launches nothing.
        replay = self.send(session_id, "hello", "message-1")
        self.assertEqual(replay.status, 303)
        self.assertEqual([turn.seq for turn in self.store.turns(session_id)], [1])
        self.assertEqual(self.calls(), calls)

    def test_the_audit_check_holds_closed_exactly_when_who_and_when_are_set(self) -> None:
        import psycopg

        session_id = self.create()
        statements = (
            "UPDATE po_sessions SET state = 'closed' WHERE session_id = %s",
            "UPDATE po_sessions SET state = 'closed', closed_at = now() WHERE session_id = %s",
            "UPDATE po_sessions SET state = 'closed', closed_by = 'owner' WHERE session_id = %s",
            "UPDATE po_sessions SET closed_at = now(), closed_by = 'owner' WHERE session_id = %s",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with (
                    psycopg.connect(self.store.credentials.conninfo()) as connection,
                    self.assertRaises(psycopg.errors.CheckViolation) as raised,
                ):
                    connection.execute(statement, (session_id,))
                self.assertEqual(raised.exception.diag.constraint_name, "po_session_closed_iff_audited")
        self.assertEqual(self.store.session(session_id).state, po_store.SESSION_OPEN)


if __name__ == "__main__":
    unittest.main()
