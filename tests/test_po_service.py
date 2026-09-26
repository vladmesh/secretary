"""The PO service (`secretary.po.service`): the durable queue, one turn per session, restarts, the socket.

Unit-level: the fake `claude`/`codex` of `tests.po_cli_fakes` run as real processes, the board store is
the in-memory `tests.po_fake_store`, and a "restart" is a new `PoService` over the same board and data
directory after the old one's store connection was cut (`FakePoStore.crash`) and, where the scenario
says so, its turn processes killed. No PostgreSQL, no Docker, no systemd.
"""

from __future__ import annotations

import ast
import getpass
import inspect
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock
from urllib.parse import urlencode

from secretary import upgrade
from secretary.host import (
    SHIPPED_PACKAGING_ROOT,
    SystemdLayout,
    build_doctor_expectations,
    build_plan,
    load_packaged_units,
    render_systemd_unit,
)
from secretary.po import client as po_client
from secretary.po import runner as po_runner
from secretary.po import store as po_store
from secretary.po import token as po_token
from secretary.po.client import PoServiceClient, ServiceUnavailable
from secretary.po.queue import PoQueue, QueueError, queue_dir
from secretary.po.runner import (
    RERUN_INTERRUPTED_REASON,
    RERUN_REASON,
    STOPPED_REASON,
    PoRunner,
    RunnerError,
    still_running,
)
from secretary.po.service import PoService, ServiceStartError, listening
from secretary.po.sprints import SprintRecord, WhyDocument, find_why_documents, why_document_label
from secretary.web.app import WebApp
from secretary.webproto.errors import (
    NOTHING_WRITTEN,
    PoOutcomeUnknown,
    PoRequestConflict,
    PoSessionClosed,
    PoTurnInProgress,
    RuntimeUnavailable,
)
from secretary.webproto.po_auth import PoTokenLayer
from secretary.webproto.po_ops import PoLayer
from tests.fakes.upgrade import FakeUnitInstaller
from tests.po_cli_fakes import FAKE_CLAUDE, FAKE_CODEX, eventually
from tests.po_fake_store import FakeBoard, FakePoStore, FakeSprints
from tests.web_fakes import Recording

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"claude": ("opus",), "codex": ("gpt-5.6-sol",)}


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


class ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        # A short root: the service's Unix socket lives under it.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="po-")))
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.executables = {}
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            self.executables[name] = str(path)
        self.log = self.root / "fake.log"
        self.gate = self.log.with_name(self.log.name + ".gate")
        self.board = FakeBoard()
        self.services: list[PoService] = []
        self.addCleanup(self.kill_everything)

    # --- one "process" of the service ------------------------------------------------------

    def service(self, *, run: bool = True, **options) -> PoService:
        """A fresh service process over the shared board and data dir, started (and its loop running).

        `options` go to `PoService` (its sprints and models, for the sprint-session resolver).
        """
        codex_home = str(self.root / "codex-home")
        runner = PoRunner(
            FakePoStore(self.board),
            self.data,
            executables=self.executables,
            env={
                **os.environ,
                "FAKE_LOG": str(self.log),
                "CODEX_HOME": codex_home,
                "FAKE_CODEX_HOME": codex_home,
            },
        )
        service = PoService(runner, data_dir=self.data, **options)
        self.services.append(service)
        self.start_lines = service.start()
        if run:
            thread = threading.Thread(target=service.run, kwargs={"tick": 0.05, "say": lambda _line: None})
            thread.start()
            service.thread = thread  # type: ignore[attr-defined]
            self.addCleanup(thread.join, 10)
            self.addCleanup(service.stop)
        return service

    def crash(self, service: PoService, *, kill: bool = True) -> None:
        """The service process dies: its store connection is gone and, under systemd, its turns with it."""
        running = service.store.running_turns()
        service.store.crash()
        service.stop()
        if kill:
            # As systemd does before it starts the unit again: the control group is gone.
            for turn in running:
                if turn.pid:
                    self.kill_group(turn.pid)
                    eventually(
                        lambda turn=turn: not still_running(turn.pid, turn.process_identity),
                        "a killed turn process kept running",
                    )

    @staticmethod
    def kill_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_everything(self) -> None:
        self.gate.touch()
        for turn in self.store().running_turns():
            if turn.pid:
                self.kill_group(turn.pid)
        for service in self.services:
            service.stop()
            for live in list(service.runner._live.values()):
                self.kill_group(live.process.pid)

    def store(self) -> FakePoStore:
        return FakePoStore(self.board)

    # --- reading -----------------------------------------------------------------------------

    def session(
        self, service: PoService, cli: str = "claude", model: str = "opus", request_id: str | None = None
    ) -> str:
        created = service.create_session(
            cli=cli,
            model=model,
            effort="default",
            request_id=request_id or f"c-{cli}-{len(self.board.sessions)}",
        )
        return created["session_id"]

    def turns(self, session_id: str) -> list[po_store.Turn]:
        return self.store().turns(session_id)

    def feed(self, session_id: str) -> list[tuple[int, str, str]]:
        return [(entry.turn_seq, entry.role, entry.text) for entry in self.store().feed(session_id)]

    def reached_gate(self, session_id: str, seq: int, attempts: int = 1) -> None:
        """The fake of turn `seq` printed its stream up to the gate (on its `attempts`-th launch)."""
        stdout = self.data / "po-runs" / session_id / f"turn-{seq:04d}.stdout"
        eventually(
            lambda: stdout.exists() and stdout.read_text().count("TOOL-CALL-SECRET") >= attempts,
            f"turn {seq} never reached its gate",
        )

    def settled(self, session_id: str, seq: int) -> po_store.Turn:
        eventually(
            lambda: (
                len(self.turns(session_id)) >= seq
                and self.turns(session_id)[seq - 1].state != po_store.RUNNING
            ),
            f"turn {seq} never settled",
        )
        return self.turns(session_id)[seq - 1]

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def queued(self) -> list[str]:
        return [item.text for item in PoQueue(self.data).pending()]


class QueueOrderTests(ServiceFixture):
    def test_two_inputs_for_one_session_run_one_after_the_other_never_together(self) -> None:
        service = self.service()
        session_id = self.session(service)

        first = service.submit(session_id=session_id, text="GATE first", request_id="m-1")
        second = service.submit(session_id=session_id, text="second", request_id="m-2")

        self.assertEqual((first["queued"], first["seq"], first["state"]), (False, 1, po_store.RUNNING))
        self.assertEqual((second["queued"], second["seq"]), (True, None))
        self.reached_gate(session_id, 1)
        self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
        self.assertEqual(self.queued(), ["second"])
        self.assertEqual(len(self.calls()), 1)

        self.gate.touch()
        one, two = self.settled(session_id, 1), self.settled(session_id, 2)

        self.assertEqual((one.state, two.state), (po_store.COMPLETED, po_store.COMPLETED))
        self.assertLess(one.finished_at, two.started_at)
        self.assertEqual(self.queued(), [])
        self.assertEqual(
            [role for _seq, role, _text in self.feed(session_id)], ["owner", "agent", "owner", "agent"]
        )

    def test_inputs_for_two_sessions_run_at_the_same_time(self) -> None:
        service = self.service()
        claude = self.session(service, "claude", "opus")
        codex = self.session(service, "codex", "gpt-5.6-sol")

        service.submit(session_id=claude, text="GATE a", request_id="a")
        service.submit(session_id=codex, text="GATE b", request_id="b")

        self.reached_gate(claude, 1)
        self.reached_gate(codex, 1)
        self.assertEqual(
            sorted(turn.session_id for turn in self.store().running_turns()), sorted([claude, codex])
        )
        self.gate.touch()
        self.assertEqual(self.settled(claude, 1).state, po_store.COMPLETED)
        self.assertEqual(self.settled(codex, 1).state, po_store.COMPLETED)

    def test_a_request_id_answers_what_it_made_and_is_refused_for_anything_else(self) -> None:
        service = self.service()
        session_id = self.session(service)
        other = self.session(service, request_id="other-session")
        service.submit(session_id=session_id, text="GATE hold", request_id="m-1")
        service.submit(session_id=session_id, text="later", request_id="m-2")

        running = service.submit(session_id=session_id, text="GATE hold", request_id="m-1")
        waiting = service.submit(session_id=session_id, text="later", request_id="m-2")

        self.assertEqual((running["repeated"], running["seq"], running["queued"]), (True, 1, False))
        self.assertEqual((waiting["repeated"], waiting["queued"]), (True, True))
        for session, text, request_id in (
            (session_id, "other text", "m-1"),
            (other, "later", "m-2"),
            (session_id, "x", "other-session"),
        ):
            with self.subTest(request_id=request_id, text=text), self.assertRaises(po_store.RequestConflict):
                service.submit(session_id=session, text=text, request_id=request_id)
        with self.assertRaises(po_store.SessionNotFound):
            service.submit(session_id="no-such-session", text="x", request_id="m-9")
        self.assertEqual(self.queued(), ["later"])

        self.gate.touch()
        self.settled(session_id, 2)
        self.assertEqual(service.submit(session_id=session_id, text="later", request_id="m-2")["seq"], 2)

    def test_a_closed_session_takes_nothing_and_a_session_with_queued_messages_does_not_close(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="m-1")
        service.submit(session_id=session_id, text="waiting", request_id="m-2")

        with self.assertRaisesRegex(po_store.TurnInProgress, "1 message\\(s\\) queued"):
            service.close_session(session_id=session_id, actor="owner")
        self.assertEqual(self.store().session(session_id).state, po_store.SESSION_OPEN)

        self.gate.touch()
        self.settled(session_id, 2)
        service.close_session(session_id=session_id, actor="owner")
        with self.assertRaises(po_store.SessionClosed):
            service.submit(session_id=session_id, text="after", request_id="m-3")
        self.assertEqual(self.queued(), [])


class ServiceRestartTests(ServiceFixture):
    def test_a_turn_whose_process_died_with_the_service_is_rerun_once_over_the_same_conversation(
        self,
    ) -> None:
        for cli, model, resumed in (
            ("codex", "gpt-5.6-sol", "codex resume 019a-fake-thread"),
            ("claude", "opus", "--resume"),
        ):
            with self.subTest(cli=cli):
                self.gate.unlink(missing_ok=True)
                first = self.service()
                session_id = self.session(first, cli, model)
                first.submit(session_id=session_id, text="GATE remember 42", request_id=f"m-{cli}")
                self.reached_gate(session_id, 1)
                self.crash(first)

                second = self.service()

                turn = self.turns(session_id)[0]
                self.assertEqual((turn.state, turn.reason), (po_store.RUNNING, RERUN_REASON))
                self.gate.touch()
                done = self.settled(session_id, 1)
                self.assertEqual(done.state, po_store.COMPLETED)
                self.assertEqual(
                    done.reason, RERUN_REASON, "the completed turn keeps the record of its re-run"
                )
                self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
                [(_, owner, asked), (_, agent, answer)] = self.feed(session_id)
                self.assertEqual((owner, asked, agent), ("owner", "GATE remember 42", "agent"))
                self.assertIn(resumed, answer)
                second.stop()

    def test_a_rerun_interrupted_again_is_settled_and_the_queued_input_behind_it_runs(self) -> None:
        first = self.service()
        session_id = self.session(first, "codex", "gpt-5.6-sol")
        first.submit(session_id=session_id, text="GATE long", request_id="m-1")
        first.submit(session_id=session_id, text="behind it", request_id="m-2")
        self.reached_gate(session_id, 1)
        self.crash(first)

        second = self.service()
        self.reached_gate(session_id, 1, attempts=2)
        self.assertEqual(self.queued(), ["behind it"], "the queued input survives both restarts")
        self.crash(second)

        self.service()

        interrupted = self.turns(session_id)[0]
        self.assertEqual(interrupted.state, po_store.INTERRUPTED)
        self.assertTrue(interrupted.reason.startswith(RERUN_INTERRUPTED_REASON), interrupted.reason)
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.feed(session_id)[-2:][0], (2, "owner", "behind it"))
        launches = [call for call in self.calls() if call["prompt"] == "GATE long"]
        self.assertEqual(len(launches), 2, "one launch and exactly one re-run")
        self.assertEqual(self.queued(), [])

    def test_a_turn_process_still_alive_at_start_is_killed_then_rerun(self) -> None:
        first = self.service()
        session_id = self.session(first, "codex", "gpt-5.6-sol")
        first.submit(session_id=session_id, text="GATE alive", request_id="m-1")
        self.reached_gate(session_id, 1)
        old_pid = self.turns(session_id)[0].pid
        self.crash(first, kill=False)
        self.assertTrue(alive(old_pid))

        self.service()

        eventually(lambda: not alive(old_pid), "the previous run's process survived")
        turn = self.turns(session_id)[0]
        self.assertEqual(turn.reason, RERUN_REASON + "; its process was killed first")
        self.assertNotEqual(turn.pid, old_pid)
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)

    def test_a_turn_the_owner_stopped_is_never_rerun(self) -> None:
        first = self.service()
        session_id = self.session(first)
        first.submit(session_id=session_id, text="GATE stop me", request_id="m-1")
        self.reached_gate(session_id, 1)
        self.assertTrue(first.stop_turn(session_id=session_id, seq=1)["stopped"])
        self.crash(first)

        self.service()

        turn = self.turns(session_id)[0]
        self.assertEqual((turn.state, turn.reason), (po_store.INTERRUPTED, STOPPED_REASON))
        self.assertEqual(len(self.calls()), 1)

    def test_a_crash_between_the_claim_and_the_dequeue_makes_no_second_turn(self) -> None:
        first = self.service(run=False)
        session_id = self.session(first)
        real_remove = first.queue.remove

        class Crash(BaseException):
            pass

        def crash_before_removal(item) -> None:
            first.store.crash()
            raise Crash

        first.queue.remove = crash_before_removal  # type: ignore[method-assign]
        with self.assertRaises(Crash):
            first.submit(session_id=session_id, text="hello once", request_id="m-1")
        self.assertEqual(self.queued(), ["hello once"])
        self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
        first.queue.remove = real_remove  # type: ignore[method-assign]
        first.stop()

        self.service()

        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        eventually(lambda: self.queued() == [], "the handed-over input stayed queued")
        self.assertEqual([turn.seq for turn in self.turns(session_id)], [1])
        self.assertEqual([role for _seq, role, _text in self.feed(session_id)], ["owner", "agent"])


class RestartRuleTests(ServiceFixture):
    def test_an_idle_service_exits_now_for_a_restart(self) -> None:
        service = self.service()

        answer = po_client.request_restart(self.data, "code changed", client=_Direct(service))

        self.assertEqual((answer.outcome, answer.running), ("now", 0))
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the idle service did not exit")  # type: ignore[attr-defined]
        self.service(run=False)
        self.assertFalse(
            po_client.restart_marker_path(self.data).exists(), "the new process fulfils the request"
        )

    def test_a_busy_service_defers_the_restart_holds_the_queue_and_exits_once_idle(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE running upgrade", request_id="m-1")
        self.reached_gate(session_id, 1)

        answer = po_client.request_restart(self.data, "code changed", client=_Direct(service))

        self.assertEqual((answer.outcome, answer.running), ("deferred", 1))
        self.assertEqual(answer.detail, "PO service restart deferred: 1 turn(s) running")
        held = service.submit(
            session_id=self.session(service, request_id="c-2"), text="held", request_id="m-2"
        )
        self.assertTrue(held["queued"], "no new turn starts while a restart is pending")
        self.assertTrue(service.thread.is_alive())  # type: ignore[attr-defined]

        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the service did not exit at idle")  # type: ignore[attr-defined]
        self.assertEqual(self.queued(), ["held"])

        self.service()
        eventually(lambda: self.queued() == [], "the new process never took the held input")


class _Direct:
    """A client that calls the service in-process: the rule, without the socket."""

    def __init__(self, service: PoService) -> None:
        self.service = service

    def request_restart(self, *, reason: str) -> dict:
        return self.service.request_restart(reason=reason)


class EndpointTests(ServiceFixture):
    def layer(self, client: PoServiceClient | None = None) -> PoLayer:
        return PoLayer(self.root, data_dir=self.data, store=self.store(), client=client, models=MODELS)

    def app(self, layer: PoLayer) -> tuple[WebApp, dict[str, str]]:
        po_token.ensure_token(self.data)
        cookie = po_token.cookie_value(po_token.read_token(self.data))
        app = WebApp(
            *(Recording() for _ in range(8)), po_auth=PoTokenLayer(self.root, data_dir=self.data), po=layer
        )
        return app, {"Cookie": f"{po_token.COOKIE_NAME}={cookie}"}

    def test_the_socket_is_private_and_one_service_serves_an_installation(self) -> None:
        service = self.service()
        with listening(service) as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertTrue(stat.S_ISSOCK(path.stat().st_mode))
            with self.assertRaisesRegex(ServiceStartError, "another PO service"), listening(self.service()):
                pass
            self.assertEqual(PoServiceClient(self.data).status()["running"], 0)
        self.assertFalse(path.exists())

    def test_a_web_restart_leaves_the_running_turn_alone_and_its_answer_reaches_the_feed(self) -> None:
        service = self.service()
        with listening(service):
            first_web = self.layer()
            created = first_web.po_create_session(request_id="c-1", cli="claude", model="opus")
            session_id = created["session_id"]
            sent = first_web.po_send(
                request_id="m-1", session_id=session_id, text="GATE across a web restart"
            )
            self.assertEqual((sent["kind"], sent["seq"]), ("po_turn_started", 1))
            self.reached_gate(session_id, 1)
            del first_web

            second_web = self.layer()
            self.assertTrue(second_web.po_session(session_id)["running"])
            self.gate.touch()
            eventually(lambda: not second_web.po_session(session_id)["running"], "the turn never settled")

            document = second_web.po_session(session_id)
            self.assertEqual(document["last_turn"]["state"], po_store.COMPLETED)
            self.assertEqual([entry["role"] for entry in document["feed"]], ["owner", "agent"])
            self.assertIn("GATE across a web restart", document["feed"][1]["text"])

    def test_the_layer_keeps_the_outcomes_it_had_and_shows_queued_messages(self) -> None:
        service = self.service()
        with listening(service):
            layer = self.layer()
            session_id = layer.po_create_session(request_id="c-1", cli="codex", model="gpt-5.6-sol")[
                "session_id"
            ]
            layer.po_send(request_id="m-1", session_id=session_id, text="GATE first")
            queued = layer.po_send(request_id="m-2", session_id=session_id, text="second")
            self.assertEqual(
                (queued["kind"], queued["queued"], queued["seq"]), ("po_turn_queued", True, None)
            )
            self.assertEqual([item["text"] for item in layer.po_session(session_id)["queued"]], ["second"])
            self.assertTrue(layer.po_send(request_id="m-2", session_id=session_id, text="second")["repeated"])
            with self.assertRaises(PoRequestConflict):
                layer.po_send(request_id="m-2", session_id=session_id, text="changed")
            with self.assertRaises(PoTurnInProgress):
                layer.po_close(session_id=session_id)
            self.reached_gate(session_id, 1)
            self.assertFalse(layer.po_stop(session_id=session_id, seq=7)["stopped"])
            stopped = layer.po_stop(session_id=session_id, seq=1)
            self.assertEqual((stopped["stopped"], stopped["turn"]["reason"]), (True, STOPPED_REASON))
            eventually(
                lambda: (
                    self.store().turns(session_id)[-1].state == po_store.COMPLETED
                    and len(self.store().turns(session_id)) == 2
                ),
                "the queued message never ran",
            )
            self.assertEqual(layer.po_session(session_id)["queued"], [])
            self.assertEqual(
                layer.po_close(session_id=session_id)["session"]["state"], po_store.SESSION_CLOSED
            )
            with self.assertRaises(PoSessionClosed):
                layer.po_send(request_id="m-3", session_id=session_id, text="after close")

    def test_every_write_is_refused_with_nothing_written_when_the_service_is_not_running(self) -> None:
        store = self.store()
        session, _ = store.claim_session(
            session_id="s-1",
            cli="claude",
            model="opus",
            cwd=str(self.data / "po"),
            cli_session_id="u",
            request_id="c-0",
        )
        layer = self.layer()
        for call in (
            lambda: layer.po_create_session(request_id="c-1", cli="claude", model="opus"),
            lambda: layer.po_send(request_id="m-1", session_id="s-1", text="hello"),
            lambda: layer.po_stop(session_id="s-1", seq=1),
            lambda: layer.po_close(session_id="s-1"),
        ):
            with self.subTest(), self.assertRaisesRegex(RuntimeUnavailable, "PO service is not running"):
                call()
        with self.assertRaises(ServiceUnavailable):
            PoServiceClient(self.data).status()

        app, headers = self.app(layer)
        response = app.handle(
            "POST",
            "/po/sessions/s-1/messages",
            body=urlencode([("request_id", "m-2"), ("text", "hello")]).encode(),
            headers=headers,
        )
        self.assertEqual(response.status, 503)
        self.assertIn("PO service is not running", response.body.decode())
        self.assertIn("hello", response.body.decode(), "the draft is kept")

        self.assertEqual(store.turns("s-1"), [])
        self.assertEqual(store.feed("s-1"), [])
        self.assertEqual(len(store.sessions()), 1)
        self.assertFalse(queue_dir(self.data).exists() and any(queue_dir(self.data).glob("*.json")))
        self.assertEqual(layer.po_session("s-1")["session"]["session_id"], session.session_id)


class RequestIdReservationTests(ServiceFixture):
    """Review 5: one reservation check (`PoService._reserve`) owns every request id from its acknowledgement."""

    def test_a_queued_message_keeps_its_id_against_a_session_create_and_later_becomes_its_turn(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        queued = service.submit(session_id=session_id, text="accepted message", request_id="reserved")
        self.assertTrue(queued["queued"])

        with self.assertRaisesRegex(po_store.RequestConflict, "queued for PO session"):
            service.create_session(cli="claude", model="opus", effort="default", request_id="reserved")

        self.assertEqual(len(self.board.sessions), 1)
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.store().request("reserved").seq, 2)
        self.assertEqual(self.feed(session_id)[2], (2, "owner", "accepted message"))
        self.assertEqual(list((queue_dir(self.data) / "refused").glob("*.json")), [])

    def test_a_set_aside_message_keeps_its_id_too(self) -> None:
        service = self.service()
        session_id = self.session(service)
        item = service.queue.put(session_id="gone", text="lost", request_id="aside", source="web")
        service.queue.refuse(item, "there is no PO session gone")

        for call in (
            lambda: service.create_session(cli="claude", model="opus", effort="default", request_id="aside"),
            lambda: service.submit(session_id=session_id, text="lost", request_id="aside"),
        ):
            with self.subTest(), self.assertRaisesRegex(po_store.RequestConflict, "set aside"):
                call()
        self.assertIsNone(self.store().request("aside"))

    def test_every_operation_that_takes_a_request_id_goes_through_the_one_reservation(self) -> None:
        from secretary.po import service as service_module

        takers = sorted(
            name
            for name in service_module._OPERATIONS.values()
            if "request_id" in inspect.signature(getattr(PoService, name)).parameters
        )
        self.assertEqual(takers, ["create_session", "sprint_session", "submit"])
        self.assertEqual(set(takers), service_module.ID_OPERATIONS)
        service = self.service(sprints=FakeSprints({"sprint:1": None}), models=MODELS)
        with mock.patch.object(service, "_reserve", wraps=service._reserve) as reserve:
            session_id = self.session(service, request_id="c-9")
            service.submit(session_id=session_id, text="hello", request_id="m-9")
            service.sprint_session(sprint_ref="sprint:1", request_id="s-9")
        self.assertEqual(
            [call.args[:2] for call in reserve.call_args_list],
            [("c-9", po_store.SESSION_CREATE), ("m-9", po_store.SEND), ("s-9", po_store.SPRINT_SESSION)],
        )


class OutcomeUnknownTests(EndpointTests):
    """Review 5: a request written to the service whose answer is lost is not "nothing was written"."""

    def lose_the_reply(self):
        """The service does the operation, then the connection drops before any answer."""
        from secretary.po import service as service_module

        def no_reply(handler) -> None:
            request = json.loads(handler.rfile.readline())
            self.assertTrue(handler.server.service.handle(request)["ok"])
            handler.connection.shutdown(socket.SHUT_RDWR)

        return mock.patch.object(service_module._Handler, "handle", no_reply)

    def test_a_lost_reply_keeps_the_request_id_and_the_resend_is_the_same_message(self) -> None:
        service = self.service(run=False)
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        app, headers = self.app(self.layer())
        route = f"/po/sessions/{session_id}/messages"
        with listening(service):
            with self.lose_the_reply():
                response = app.handle(
                    "POST",
                    route,
                    body=urlencode({"request_id": "original", "text": "do this once"}).encode(),
                    headers=headers,
                )
            body = response.body.decode()
            self.assertEqual(response.status, 503)
            self.assertIn(
                "the PO service may have accepted this message; sending again with the same form is safe",
                body,
            )
            self.assertNotIn("nothing was sent or written", body)
            form = re.search(
                r'<form[^>]+action="' + re.escape(route) + r'"[^>]*>(.*?)</form>', body, re.DOTALL
            ).group(1)
            self.assertEqual(re.findall(r'name="request_id" value="([^"]+)"', form), ["original"])
            self.assertIn("do this once", form)

            retry = app.handle(
                "POST",
                route,
                body=urlencode({"request_id": "original", "text": "do this once"}).encode(),
                headers=headers,
            )

        self.assertEqual(retry.status, 303)
        self.assertEqual(self.queued(), ["do this once"])

    def test_create_stop_and_close_say_the_same_and_repeating_them_is_safe(self) -> None:
        service = self.service()
        layer = self.layer()
        with listening(service):
            with (
                self.lose_the_reply(),
                self.assertRaisesRegex(PoOutcomeUnknown, "may have opened this session"),
            ):
                layer.po_create_session(request_id="c-1", cli="claude", model="opus")
            again = layer.po_create_session(request_id="c-1", cli="claude", model="opus")
            self.assertTrue(again["repeated"])
            self.assertEqual(len(self.board.sessions), 1)
            session_id = again["session_id"]
            layer.po_send(request_id="m-1", session_id=session_id, text="GATE hold")
            self.reached_gate(session_id, 1)
            with (
                self.lose_the_reply(),
                self.assertRaisesRegex(PoOutcomeUnknown, "stopping it again is safe") as stop,
            ):
                layer.po_stop(session_id=session_id, seq=1)
            self.assertEqual(stop.exception.data["action"], "repeat_same_request")
            self.assertFalse(layer.po_stop(session_id=session_id, seq=1)["stopped"])
            with self.lose_the_reply(), self.assertRaisesRegex(PoOutcomeUnknown, "closing it again is safe"):
                layer.po_close(session_id=session_id)
            self.assertEqual(
                layer.po_close(session_id=session_id)["session"]["state"], po_store.SESSION_CLOSED
            )

    def test_the_service_runs_no_request_whose_line_never_ended(self) -> None:
        service = self.service()
        session_id = self.session(service)
        with listening(service) as path, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(path))
            line = {"op": "submit", "session_id": session_id, "text": "half", "request_id": "half"}
            connection.sendall(json.dumps(line).encode())
            connection.shutdown(socket.SHUT_WR)
            answer = json.loads(connection.makefile("rb").readline())
        self.assertEqual(answer["error"]["code"], "validation")
        self.assertEqual(self.queued(), [])
        self.assertIsNone(self.store().request("half"))


def form_request_ids(body: str, action: str) -> list[str]:
    form = re.search(r'<form[^>]+action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', body, re.DOTALL)
    assert form is not None, f"no form posting to {action}"
    return re.findall(r'name="request_id" value="([^"]+)"', form.group(1))


def fail_once(target, name: str, when):
    """Patch `target.name` so the first call for which `when(*args)` holds raises `PoStoreError`."""
    real = getattr(target, name)
    failed: list[tuple] = []

    def wrapper(*args, **kwargs):
        if not failed and when(*args, **kwargs):
            failed.append(args)
            raise po_store.PoStoreError(f"injected failure in {name}")
        return real(*args, **kwargs)

    return mock.patch.object(target, name, wrapper), failed


class AcceptanceAnswerTests(EndpointTests):
    """Review 17: after acceptance a request is answered as accepted, and a refused form keeps its id."""

    def post(self, app, headers, route: str, fields: dict[str, str]):
        return app.handle("POST", route, body=urlencode(fields).encode(), headers=headers)

    def test_a_failed_lookup_after_the_queue_write_answers_accepted_and_a_resend_is_the_same_message(
        self,
    ) -> None:
        service = self.service(run=False)
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        app, headers = self.app(self.layer())
        route = f"/po/sessions/{session_id}/messages"
        lookups: list[str] = []

        def second_lookup(_store, request_id):
            lookups.append(request_id)
            return request_id == "original" and lookups.count("original") == 2

        patch, failed = fail_once(FakePoStore, "request", second_lookup)
        with listening(service):
            with patch:
                first = self.post(app, headers, route, {"request_id": "original", "text": "do this once"})
            self.assertEqual(failed, [(mock.ANY, "original")], "the post-enqueue lookup failed once")
            self.assertEqual(first.status, 303, first.body.decode())
            again = self.post(app, headers, route, {"request_id": "original", "text": "do this once"})

        self.assertEqual(again.status, 303)
        self.assertEqual(self.queued(), ["do this once"])

    def test_a_queue_write_that_failed_after_landing_is_outcome_unknown_and_keeps_the_id(self) -> None:
        service = self.service(run=False)
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        app, headers = self.app(self.layer())
        route = f"/po/sessions/{session_id}/messages"
        real_put = service.queue.put

        def put_then_fail(**kwargs):
            real_put(**kwargs)
            raise QueueError("the directory fsync failed after the rename")

        with listening(service):
            with mock.patch.object(service.queue, "put", side_effect=put_then_fail):
                first = self.post(app, headers, route, {"request_id": "original", "text": "do this once"})
            body = first.body.decode()
            self.assertEqual(first.status, 503)
            self.assertIn("may have accepted this message", body)
            self.assertEqual(form_request_ids(body, route), ["original"])
            self.assertEqual(
                self.post(app, headers, route, {"request_id": "original", "text": "do this once"}).status, 303
            )

        self.assertEqual(self.queued(), ["do this once"])

    def test_every_post_acceptance_failure_of_a_session_create_ends_with_one_session(self) -> None:
        service = self.service(run=False)
        app, headers = self.app(self.layer())
        form = {"request_id": "create-once", "cli": "claude", "model": "opus"}
        real_claim = FakePoStore.claim_session

        def commit_then_fail(store, **kwargs):
            real_claim(store, **kwargs)
            raise po_store.PoStoreError("the connection dropped after the commit")

        with listening(service):
            with mock.patch.object(FakePoStore, "claim_session", commit_then_fail):
                first = self.post(app, headers, "/po/sessions", form)
            body = first.body.decode()
            self.assertEqual(first.status, 503)
            self.assertIn("may have opened this session", body)
            self.assertEqual(form_request_ids(body, "/po/sessions"), ["create-once"])
            self.assertEqual(len(self.board.sessions), 1)
            [session_id] = self.board.sessions

            # The resend is the replay; its one enrichment read fails, and it is still the session.
            patch, failed = fail_once(FakePoStore, "session", lambda _store, sid: sid == session_id)
            with patch:
                again = self.post(app, headers, "/po/sessions", form)
            self.assertEqual(len(failed), 1)
            self.assertEqual((again.status, again.headers["Location"]), (303, f"/po/sessions/{session_id}"))
            third = self.post(app, headers, "/po/sessions", form)

        self.assertEqual(third.headers["Location"], f"/po/sessions/{session_id}")
        self.assertEqual(len(self.board.sessions), 1)

    def test_each_exit_of_submit_and_create_says_whether_it_wrote_nothing(self) -> None:
        service = self.service(run=False)
        session_id = self.session(service, request_id="made")
        closed = self.session(service, request_id="made-closed")
        service.close_session(session_id=closed, actor="owner")
        service.submit(session_id=session_id, text="GATE hold", request_id="hold")
        service.submit(session_id=session_id, text="waiting", request_id="waiting")

        def error(**request):
            answer = service.handle(request)
            self.assertFalse(answer["ok"], answer)
            return answer["error"]["code"], answer["error"].get("nothing_written", False)

        definite = [
            ({"op": "submit", "session_id": session_id, "text": " ", "request_id": "e"}, "validation"),
            ({"op": "submit", "session_id": session_id, "text": "x"}, "validation"),
            (
                {"op": "submit", "session_id": session_id, "text": "x", "request_id": "made"},
                "request_conflict",
            ),
            (
                {"op": "submit", "session_id": session_id, "text": "other", "request_id": "waiting"},
                "request_conflict",
            ),
            ({"op": "submit", "session_id": "nope", "text": "x", "request_id": "n"}, "session_not_found"),
            ({"op": "submit", "session_id": closed, "text": "x", "request_id": "c"}, "session_closed"),
            ({"op": "create_session", "cli": "gemini", "model": "m", "request_id": "g"}, "validation"),
            ({"op": "create_session", "cli": "claude", "model": " ", "request_id": "g"}, "validation"),
            (
                {"op": "create_session", "cli": "claude", "model": "opus", "request_id": "waiting"},
                "request_conflict",
            ),
            (
                {"op": "create_session", "cli": "codex", "model": "m", "request_id": "made"},
                "request_conflict",
            ),
        ]
        for request, code in definite:
            with self.subTest(request=request):
                self.assertEqual(error(**request), (code, True))

        # A store that fails before acceptance wrote nothing either, but nobody marked it: the id is kept.
        with mock.patch.object(FakePoStore, "request", side_effect=po_store.PoStoreError("away")):
            self.assertEqual(
                error(op="submit", session_id=session_id, text="x", request_id="k"), ("unavailable", False)
            )
            self.assertEqual(
                error(op="create_session", cli="claude", model="opus", request_id="k"), ("unavailable", False)
            )
        self.assertEqual(self.queued(), ["waiting"])


class KeptRequestIdTests(ServiceFixture):
    """The web keeps a refused form's request id unless the refusal is marked as having written nothing."""

    SESSION: ClassVar[dict] = {
        "kind": "po_session",
        "session": {"session_id": "s-1", "state": "open", "cli": "claude", "model": "opus"},
        "turns": [],
        "feed": [],
        "running": False,
        "queued": [],
    }
    OVERVIEW: ClassVar[dict] = {
        "kind": "po_overview",
        "closed": False,
        "closed_count": 0,
        "sessions": [],
        "running": 0,
        "models": {"claude": ["opus"]},
        "efforts": {"claude": ["default"]},
    }

    def forms(self, failure: Exception) -> tuple[list[str], list[str]]:
        po_token.ensure_token(self.data)
        cookie = po_token.cookie_value(po_token.read_token(self.data))
        po = Recording(
            po_send=failure, po_create_session=failure, po_session=self.SESSION, po_overview=self.OVERVIEW
        )
        app = WebApp(
            *(Recording() for _ in range(8)), po_auth=PoTokenLayer(self.root, data_dir=self.data), po=po
        )
        headers = {"Cookie": f"{po_token.COOKIE_NAME}={cookie}"}
        sent = app.handle(
            "POST",
            "/po/sessions/s-1/messages",
            body=urlencode({"request_id": "form-1", "text": "hello"}).encode(),
            headers=headers,
        )
        created = app.handle(
            "POST",
            "/po/sessions",
            body=urlencode({"request_id": "form-2", "cli": "claude", "model": "opus"}).encode(),
            headers=headers,
        )
        return (
            form_request_ids(sent.body.decode(), "/po/sessions/s-1/messages"),
            form_request_ids(created.body.decode(), "/po/sessions"),
        )

    def test_an_unexpected_exception_from_the_layer_keeps_the_id(self) -> None:
        self.assertEqual(self.forms(RuntimeError("boom")), (["form-1"], ["form-2"]))

    def test_an_unmarked_refusal_keeps_the_id_and_only_a_marked_one_gets_a_fresh_one(self) -> None:
        for failure in (
            RuntimeUnavailable("the store answered badly"),
            PoOutcomeUnknown("no answer"),
            PoRequestConflict("taken"),
            PoTurnInProgress("busy"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(self.forms(failure), (["form-1"], ["form-2"]))
        for failure in (
            RuntimeUnavailable("the PO service is not running", data=NOTHING_WRITTEN),
            PoRequestConflict("taken", data=NOTHING_WRITTEN),
            PoSessionClosed("closed", data=NOTHING_WRITTEN),
        ):
            with self.subTest(failure=type(failure).__name__, marked=True):
                sent, created = self.forms(failure)
                self.assertNotEqual(sent, ["form-1"])
                self.assertNotEqual(created, ["form-2"])
                self.assertTrue(sent[0].startswith("web-po-") and created[0].startswith("web-po-"))


class RecoveryProgressTests(ServiceFixture):
    """Review 5: recovery is complete only when no `running` row is left without a process."""

    def interrupted(self, *, queued: str = "waiting") -> str:
        first = self.service(run=False)
        session_id = self.session(first)
        first.submit(session_id=session_id, text="GATE interrupted", request_id="original")
        first.submit(session_id=session_id, text=queued, request_id="waiting")
        self.reached_gate(session_id, 1)
        self.crash(first)
        return session_id

    def test_one_failed_feed_read_spends_no_rerun_and_recovery_retries_until_the_queue_moves(self) -> None:
        session_id = self.interrupted()
        real_feed = FakePoStore.feed
        failures = []

        def feed(store, sid):
            if not failures:
                failures.append(sid)
                raise po_store.PoStoreError("temporary feed read failure")
            return real_feed(store, sid)

        with mock.patch.object(FakePoStore, "feed", feed):
            second = self.service(run=False)
            turn = self.turns(session_id)[0]
            self.assertFalse(second._recovered)
            self.assertEqual(
                (turn.state, turn.reason), (po_store.RUNNING, None), "the re-run allowance is unspent"
            )
            second.pump()
            self.assertEqual(self.queued(), ["waiting"], "the session of an unrecovered row takes nothing")
            thread = threading.Thread(target=second.run, kwargs={"tick": 0.05, "say": lambda _line: None})
            thread.start()
            self.addCleanup(thread.join, 10)
            self.addCleanup(second.stop)
            eventually(lambda: second._recovered, "recovery never completed")

        self.assertEqual(failures, [session_id])
        self.assertEqual(self.turns(session_id)[0].reason, RERUN_REASON)
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertEqual(self.queued(), [])

    def test_a_rerun_that_cannot_launch_is_settled_failed_and_the_queue_goes_on(self) -> None:
        session_id = self.interrupted(queued="GATE waiting")
        self.executables = {**self.executables, "claude": str(self.root / "no-such-claude")}

        second = self.service()

        turn = self.turns(session_id)[0]
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("could not start", turn.reason)
        self.assertTrue(second._recovered)
        self.assertEqual(self.settled(session_id, 2).state, po_store.FAILED, "the queued input was taken")

    def test_a_rerun_that_can_never_be_prepared_is_settled_failed_without_spending_it(self) -> None:
        session_id = self.interrupted()
        with mock.patch.object(PoRunner, "_owner_text", side_effect=RunnerError("no owner message")):
            second = self.service()

        turn = self.turns(session_id)[0]
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("could not be prepared", turn.reason)
        self.assertTrue(second._recovered)
        self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)

    def test_a_launch_failure_the_store_could_not_record_is_settled_by_the_next_pass(self) -> None:
        session_id = self.interrupted()
        runner = PoRunner(
            FakePoStore(self.board), self.data, executables={"claude": str(self.root / "missing")}
        )
        real_finish = FakePoStore.finish_turn
        calls = []

        def finish(store, *args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise po_store.PoStoreError("the store is away")
            return real_finish(store, *args, **kwargs)

        with mock.patch.object(FakePoStore, "finish_turn", finish):
            runner.recover(rerun=True)
            self.assertEqual(
                [t.seq for t in runner.orphaned_turns()], [1], "left running, reported as orphaned"
            )
            runner.recover(rerun=True)

        turn = self.turns(session_id)[0]
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("the re-run did not start", turn.reason)
        self.assertEqual(runner.orphaned_turns(), [])


PO_UNIT = "secretary-po.service"


class UpgradeStepTests(ServiceFixture):
    """`step_po`: an upgrade restarts the PO service now only while it is idle, else defers it."""

    def context(self, units: FakeUnitInstaller, **flags) -> upgrade.UpgradeContext:
        report = SimpleNamespace(
            host={"unit_prefix": "secretary-"},
            instance={"host": {"unit_prefix": "secretary-"}},
            data_dir=self.data,
            bindings=[],
        )
        return upgrade.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.root / "product",
            base_branch="main",
            dry_run=flags.pop("dry_run", False),
            units=units,
            pull=False,
            report=report,
            **{"code_changed": True, **flags},
        )

    def units(self) -> FakeUnitInstaller:
        return FakeUnitInstaller(present={PO_UNIT: b"[Service]\n"}, active={PO_UNIT})

    def test_the_step_sits_before_the_web_restart(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]
        self.assertEqual(names.index("step_po"), names.index("step_web") - 1)

    def test_idle_the_service_is_restarted_now_and_the_new_process_is_waited_for(self) -> None:
        service = self.service()
        units = self.units()
        before = units.identities[PO_UNIT]

        def systemd_brings_it_back(_seconds: float) -> None:
            units.identities[PO_UNIT] = units._new_identity()

        with listening(service), mock.patch.object(upgrade, "_sleep", side_effect=systemd_brings_it_back):
            result = upgrade.step_po(self.context(units))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn(f"restarted {PO_UNIT} while idle", result.detail)
        self.assertIn("product code or dependencies changed", result.detail)
        self.assertNotEqual(units.identities[PO_UNIT], before)
        self.assertNotIn(
            ("restart", PO_UNIT), units.calls, "the service exits by itself; systemd restarts it"
        )
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive())  # type: ignore[attr-defined]

    def test_busy_the_restart_is_deferred_and_applied_by_the_service_at_idle(self) -> None:
        service = self.service()
        session_id = self.session(service)
        service.submit(session_id=session_id, text="GATE secretary upgrade", request_id="m-1")
        self.reached_gate(session_id, 1)
        units = self.units()

        with listening(service), mock.patch.object(upgrade, "_sleep") as sleep:
            result = upgrade.step_po(self.context(units))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertTrue(
            result.detail.startswith("PO service restart deferred: 1 turn(s) running"), result.detail
        )
        sleep.assert_not_called()
        self.assertEqual(units.calls, [])
        self.assertTrue(po_client.restart_marker_path(self.data).exists())
        self.assertEqual(self.turns(session_id)[0].state, po_store.RUNNING, "the caller's turn is untouched")

        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).state, po_store.COMPLETED)
        service.thread.join(5)  # type: ignore[attr-defined]
        self.assertFalse(service.thread.is_alive(), "the deferred restart was not applied at idle")  # type: ignore[attr-defined]

    def test_a_service_that_does_not_answer_keeps_the_request_on_disk(self) -> None:
        result = upgrade.step_po(self.context(self.units()))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("not acknowledged", result.detail)
        self.assertTrue(po_client.restart_marker_path(self.data).exists())

    def test_uninstalled_stopped_unchanged_and_dry_run(self) -> None:
        missing = upgrade.step_po(self.context(FakeUnitInstaller()))
        self.assertEqual(missing.status, "skipped", missing.detail)

        stopped = FakeUnitInstaller(present={PO_UNIT: b"[Service]\n"})
        started = upgrade.step_po(self.context(stopped))
        self.assertEqual((started.status, stopped.calls), ("changed", [("restart", PO_UNIT)]))

        current = upgrade.step_po(self.context(self.units(), code_changed=False))
        self.assertEqual(current.status, "unchanged", current.detail)

        dry = upgrade.step_po(self.context(self.units(), dry_run=True, po_unit_changed=True))
        self.assertEqual(dry.status, "changed")
        self.assertIn("would ask", dry.detail)
        self.assertIn("the PO unit file changed", dry.detail)
        self.assertFalse(po_client.restart_marker_path(self.data).exists())


class TurnEnvironmentTests(ServiceFixture):
    """Every turn names its own PO session, so `sprint create` inside it records that session."""

    def test_a_new_turn_and_its_rerun_both_carry_their_session(self) -> None:
        first = self.service()
        session_id = self.session(first)
        other = self.session(first, "codex", "gpt-5.6-sol")
        first.submit(session_id=other, text="elsewhere", request_id="m-other")
        first.submit(session_id=session_id, text="GATE remember 42", request_id="m-1")
        self.reached_gate(session_id, 1)
        self.settled(other, 1)
        self.crash(first)

        second = self.service()
        self.gate.touch()
        self.assertEqual(self.settled(session_id, 1).reason, RERUN_REASON)

        launches = [call for call in self.calls() if call["prompt"] == "GATE remember 42"]
        # The first launch, its re-run, and the re-run's relaunch over `--resume`.
        self.assertGreaterEqual(len(launches), 2)
        self.assertEqual({call["po_session"] for call in launches}, {session_id})
        [elsewhere] = [call for call in self.calls() if call["prompt"] == "elsewhere"]
        self.assertEqual(elsewhere["po_session"], other)
        self.assertNotIn(po_runner.PO_SESSION_ENV, second.runner.env)


def why(path: str, text: str) -> WhyDocument:
    return WhyDocument(path, text)


class SprintSessionTests(ServiceFixture):
    """`PoService.sprint_session`: the sprint's live PO session, re-seeded once when it is gone."""

    DOC = "state/knowledge/decisions/2026-09-26-sprint-1-why.md"

    def resolver(self, sprints: FakeSprints, **options) -> PoService:
        return self.service(sprints=sprints, models=MODELS, **options)

    def first_prompt(self, session_id: str) -> str:
        return self.feed(session_id)[0][2]

    def test_an_open_recorded_session_is_the_answer_and_nothing_is_written(self) -> None:
        sprints = FakeSprints({})
        service = self.resolver(sprints)
        session_id = self.session(service)
        sprints.records["sprint:1"] = SprintRecord("sprint:1", "open", session_id)

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        self.assertEqual(answer, {"session_id": session_id, "created": False, "repeated": False})
        self.assertEqual(list(self.board.sessions), [session_id])
        self.assertIsNone(self.store().request("r-1"))
        self.assertEqual((sprints.comments, sprints.recorded, self.queued()), ({}, {}, []))

    def test_a_sprint_with_no_session_gets_one_seeded_with_its_why_document_and_notes(self) -> None:
        sprints = FakeSprints(
            {"sprint:1": None},
            documents={"sprint:1": [why(self.DOC, "# Why\n\nBecause the owner said so.\n")]},
        )
        service = self.resolver(sprints)

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        self.assertEqual((answer["created"], answer["repeated"]), (True, False))
        session = self.store().session(answer["session_id"])
        # No recorded session: the new-session form's preselection, first CLI and first model.
        self.assertEqual((session.cli, session.model, session.effort), ("claude", "opus", "default"))
        self.assertEqual(sprints.records["sprint:1"].po_session, session.session_id)
        self.assertEqual(
            list(sprints.comments.values()),
            [
                (
                    "sprint:1",
                    (
                        f"the PO session none no longer exists; opened {session.session_id} seeded with "
                        f"{self.DOC} and NOTES.md"
                    ),
                )
            ],
        )
        self.assertEqual(self.settled(session.session_id, 1).state, po_store.COMPLETED)
        seed = self.first_prompt(session.session_id)
        for expected in (
            "sprint:1",
            "recorded no PO session",
            "NOTES.md",
            self.DOC,
            "Because the owner said so.",
        ):
            self.assertIn(expected, seed)
        [launch] = self.calls()
        self.assertEqual((launch["prompt"], launch["po_session"]), (seed, session.session_id))

    def test_a_session_missing_from_the_store_is_replaced_and_no_why_document_is_said(self) -> None:
        sprints = FakeSprints({"sprint:1": "gone-session"})
        service = self.resolver(sprints)

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        new = answer["session_id"]
        self.assertTrue(answer["created"])
        self.assertEqual(self.store().session(new).cli, "claude")
        self.assertEqual(
            list(sprints.comments.values()),
            [
                (
                    "sprint:1",
                    (
                        f"the PO session gone-session no longer exists; opened {new} seeded with "
                        "no why-document found and NOTES.md"
                    ),
                )
            ],
        )
        self.settled(new, 1)
        seed = self.first_prompt(new)
        self.assertIn("gone-session no longer exists", seed)
        self.assertIn("No why-document under state/knowledge/decisions/ names sprint:1", seed)

    def test_a_closed_session_is_replaced_with_its_own_cli_model_and_effort(self) -> None:
        sprints = FakeSprints({})
        service = self.resolver(sprints)
        old = service.create_session(cli="codex", model="gpt-5.6-sol", effort="high", request_id="c-old")
        service.close_session(session_id=old["session_id"], actor="owner")
        sprints.records["sprint:1"] = SprintRecord("sprint:1", "open", old["session_id"])

        answer = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")

        session = self.store().session(answer["session_id"])
        self.assertNotEqual(session.session_id, old["session_id"])
        self.assertEqual((session.cli, session.model, session.effort), ("codex", "gpt-5.6-sol", "high"))
        self.settled(session.session_id, 1)
        self.assertIn(f"{old['session_id']} is closed", self.first_prompt(session.session_id))
        self.assertEqual(sprints.records["sprint:1"].po_session, session.session_id)

    def test_several_why_documents_are_listed_and_none_is_quoted(self) -> None:
        documents = [
            why("state/knowledge/decisions/a.md", "sprint:1 first TEXT-A"),
            why("state/knowledge/decisions/b.md", "sprint:1 second TEXT-B"),
        ]
        sprints = FakeSprints({"sprint:1": None}, documents={"sprint:1": documents})
        service = self.resolver(sprints)

        new = service.sprint_session(sprint_ref="sprint:1", request_id="r-1")["session_id"]

        self.settled(new, 1)
        seed = self.first_prompt(new)
        self.assertIn("- state/knowledge/decisions/a.md", seed)
        self.assertIn("- state/knowledge/decisions/b.md", seed)
        self.assertNotIn("TEXT-A", seed)
        [(_, comment)] = sprints.comments.values()
        self.assertIn(
            "no single why-document (state/knowledge/decisions/a.md, state/knowledge/decisions/b.md)", comment
        )

    def test_a_repeat_replays_and_finishes_a_resolve_that_failed_part_way(self) -> None:
        sprints = FakeSprints({"sprint:1": None})
        service = self.resolver(sprints)
        sprints.fail["record_po_session"] = 1
        request = {"op": "sprint_session", "sprint_ref": "sprint:1", "request_id": "r-1"}

        failed = service.handle(request)

        self.assertEqual(failed["error"]["code"], "outcome_unknown")
        [opened] = self.board.sessions
        self.assertIsNone(sprints.records["sprint:1"].po_session)

        again = service.handle(request)
        once_more = service.handle(request)

        self.assertEqual(again["result"], {"session_id": opened, "created": True, "repeated": True})
        self.assertEqual(once_more["result"], again["result"])
        self.assertEqual(list(self.board.sessions), [opened])
        self.assertEqual(sprints.records["sprint:1"].po_session, opened)
        self.assertEqual(len(sprints.comments), 1)
        self.settled(opened, 1)
        self.assertEqual([role for _seq, role, _text in self.feed(opened)], ["owner", "agent"])
        self.assertEqual(self.queued(), [])
        # A later resolve under another id finds the recorded session open.
        later = service.sprint_session(sprint_ref="sprint:1", request_id="r-2")
        self.assertEqual((later["session_id"], later["created"]), (opened, False))
        with self.assertRaises(po_store.RequestConflict):
            service.sprint_session(sprint_ref="sprint:2", request_id="r-1")

    def test_two_resolves_of_one_sprint_open_one_session(self) -> None:
        sprints = FakeSprints({"sprint:1": "gone-session"})
        service = self.resolver(sprints)
        start = threading.Barrier(4)
        answers: list[dict] = []

        def resolve(request_id: str) -> None:
            start.wait(5)
            answers.append(service.sprint_session(sprint_ref="sprint:1", request_id=request_id))

        threads = [threading.Thread(target=resolve, args=(f"r-{index}",)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual(len(answers), 4)
        self.assertEqual(len({answer["session_id"] for answer in answers}), 1)
        self.assertEqual(sorted(answer["created"] for answer in answers), [False, False, False, True])
        self.assertEqual(len(self.board.sessions), 1)
        self.assertEqual(len(sprints.comments), 1)

    def test_an_unknown_or_closed_sprint_is_refused_with_nothing_written(self) -> None:
        sprints = FakeSprints({"sprint:done": None}, status={"sprint:done": "closed"})
        service = self.resolver(sprints)
        for ref in ("sprint:none", "sprint:done"):
            with self.subTest(ref=ref):
                answer = service.handle({"op": "sprint_session", "sprint_ref": ref, "request_id": f"r-{ref}"})
                self.assertEqual(answer["error"]["code"], "validation")
                self.assertTrue(answer["error"]["nothing_written"])
        self.assertEqual((self.board.sessions, sprints.comments), ({}, {}))

    def test_the_client_resolves_through_the_socket(self) -> None:
        sprints = FakeSprints({"sprint:1": None})
        service = self.resolver(sprints, run=False)
        with listening(service):
            client = PoServiceClient(self.data)
            first = client.sprint_session(sprint_ref="sprint:1", request_id="r-1")
            second = client.sprint_session(sprint_ref="sprint:1", request_id="r-1")
        self.assertTrue(first["created"])
        self.assertEqual((second["session_id"], second["repeated"]), (first["session_id"], True))


class FakeStoreVocabularyTests(unittest.TestCase):
    def test_an_operation_outside_the_check_writes_nothing_as_postgresql_would(self) -> None:
        store = FakePoStore()
        with self.assertRaisesRegex(po_store.PoStoreError, "po_request_operation_in_vocabulary"):
            store.claim_session(
                session_id="s-1",
                cli="claude",
                model="opus",
                cwd="/po",
                cli_session_id=None,
                request_id="r-1",
                operation="po_not_in_the_check",
                fingerprint="f",
            )
        self.assertEqual((store.board.sessions, store.board.requests), ({}, {}))
        for operation in po_store.REQUEST_OPERATIONS:
            if operation != po_store.SEND:
                store.claim_session(
                    session_id=f"s-{operation}",
                    cli="claude",
                    model="opus",
                    cwd="/po",
                    cli_session_id=None,
                    request_id=f"r-{operation}",
                    operation=operation,
                    fingerprint="f",
                )
        self.assertEqual(len(store.board.sessions), 2)


class WhyDocumentTests(unittest.TestCase):
    def test_only_a_decision_that_names_the_whole_reference_is_found(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertEqual(find_why_documents(root, "sprint:14"), [])
        decisions = root / "state" / "knowledge" / "decisions"
        decisions.mkdir(parents=True)
        (decisions / "a.md").write_text("Why sprint:14 exists.\n", encoding="utf-8")
        (decisions / "b.md").write_text("About sprint:1465 and xsprint:14.\n", encoding="utf-8")
        (decisions / "c.md").write_text("(sprint:14)\n", encoding="utf-8")
        (decisions / "notes.txt").write_text("sprint:14\n", encoding="utf-8")

        found = find_why_documents(root, "sprint:14")

        self.assertEqual(
            [document.path for document in found],
            ["state/knowledge/decisions/a.md", "state/knowledge/decisions/c.md"],
        )
        self.assertEqual(found[0].text, "Why sprint:14 exists.\n")
        self.assertEqual(why_document_label(found[:1]), "state/knowledge/decisions/a.md")
        self.assertEqual(why_document_label([]), "no why-document found")


class UnitTemplateTests(unittest.TestCase):
    LAYOUT = SystemdLayout(
        product_root=Path("/opt/product"),
        instance_path=Path("/opt/instance"),
        data_dir=Path("/opt/data"),
        runtime_user="runner",
        runtime_home=Path("/opt/home"),
    )

    def unit(self, name: str, layout: SystemdLayout | None = None) -> str:
        return render_systemd_unit(
            (SHIPPED_PACKAGING_ROOT / name).read_bytes(), layout or self.LAYOUT
        ).decode()

    def test_the_unit_is_a_simple_always_restarted_service_with_the_web_units_templating(self) -> None:
        text = self.unit(PO_UNIT)
        self.assertIn("Type=simple", text)
        self.assertIn("Restart=always", text)
        self.assertIn("[Install]", text)
        self.assertIn("ExecStart=/opt/product/.venv/bin/secretary po-serve --instance /opt/instance", text)
        directives = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
        for coupling in ("PartOf=", "BindsTo=", "Requires=", "secretary-web"):
            self.assertNotIn(coupling, directives)

        def shared(unit: str) -> list[str]:
            keys = ("User=", "Group=", "WorkingDirectory=", "EnvironmentFile=", "Environment=")
            return [line for line in self.unit(unit).splitlines() if line.startswith(keys)]

        self.assertEqual(shared(PO_UNIT), shared("secretary-web.service"))

    def test_it_is_planned_listed_by_doctor_and_can_be_opted_out(self) -> None:
        packaged = load_packaged_units(SHIPPED_PACKAGING_ROOT, "secretary-", self.LAYOUT)
        instance = {"host": {"unit_prefix": "secretary-"}}
        planned = {r.name: r for r in build_plan(instance, [], packaged=packaged) if r.kind == "unit"}
        self.assertIn(PO_UNIT, planned)
        self.assertEqual(json.loads(planned[PO_UNIT].spec)["installable"], "yes")
        expected = build_doctor_expectations(instance, [], packaged=packaged)
        self.assertIn(PO_UNIT, expected.units)
        self.assertEqual(expected.unit_runtime[PO_UNIT], (True, True))

        opted_out = {"host": {"unit_prefix": "secretary-", "components": {"po": {"enabled": False}}}}
        names = {r.name for r in build_plan(opted_out, [], packaged=packaged)}
        self.assertNotIn(PO_UNIT, names)
        self.assertIn("secretary-web.service", names)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_the_rendered_unit_passes_systemd_analyze_verify(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        executable = root / "product" / ".venv" / "bin" / "secretary"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        layout = SystemdLayout(
            product_root=root / "product",
            instance_path=root / "instance",
            data_dir=root / "data",
            runtime_user=getpass.getuser(),
            runtime_home=Path.home(),
        )
        path = root / "units" / PO_UNIT
        path.parent.mkdir()
        path.write_text(self.unit(PO_UNIT, layout), encoding="utf-8")

        result = subprocess.run(
            ["systemd-analyze", "verify", "--man=no", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(PO_UNIT, result.stderr, "systemd warned about the unit")


class ThinWebTests(unittest.TestCase):
    def test_no_web_module_imports_the_runner_or_the_service(self) -> None:
        offenders = []
        for package in ("web", "webproto"):
            for path in sorted((ROOT / "src" / "secretary" / package).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
                    for name in names:
                        if name in ("secretary.po.runner", "secretary.po.service"):
                            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
        self.assertEqual(offenders, [])

    def test_the_web_recovery_module_is_gone(self) -> None:
        self.assertFalse((ROOT / "src" / "secretary" / "webproto" / "po_recovery.py").exists())


if __name__ == "__main__":
    unittest.main()
