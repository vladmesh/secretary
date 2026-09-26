"""The PO service (`secretary.po.service`): the durable queue, one turn per session, restarts, the socket.

Unit-level: the fake `claude`/`codex` of `tests.po_cli_fakes` run as real processes, the board store is
the in-memory `tests.po_fake_store`, and a "restart" is a new `PoService` over the same board and data
directory after the old one's store connection was cut (`FakePoStore.crash`) and, where the scenario
says so, its turn processes killed. No PostgreSQL, no Docker, no systemd.
"""

from __future__ import annotations

import ast
import getpass
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
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
from secretary.po import store as po_store
from secretary.po import token as po_token
from secretary.po.client import PoServiceClient, ServiceUnavailable
from secretary.po.queue import PoQueue, queue_dir
from secretary.po.runner import (
    RERUN_INTERRUPTED_REASON,
    RERUN_REASON,
    STOPPED_REASON,
    PoRunner,
    still_running,
)
from secretary.po.service import PoService, ServiceStartError, listening
from secretary.web.app import WebApp
from secretary.webproto.errors import PoRequestConflict, PoSessionClosed, PoTurnInProgress, RuntimeUnavailable
from secretary.webproto.po_auth import PoTokenLayer
from secretary.webproto.po_ops import PoLayer
from tests.fakes.upgrade import FakeUnitInstaller
from tests.po_cli_fakes import FAKE_CLAUDE, FAKE_CODEX, eventually
from tests.po_fake_store import FakeBoard, FakePoStore
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

    def service(self, *, run: bool = True) -> PoService:
        """A fresh service process over the shared board and data dir, started (and its loop running)."""
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
        service = PoService(runner, data_dir=self.data)
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
