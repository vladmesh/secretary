"""The board is not listening yet: a precheck defers its run instead of spending it.

A timer with `Persistent=true` catches its missed run up seconds after boot, which is exactly when
a docker-hosted Kanboard is least likely to answer.  Before secretary-964 that refused connection
travelled as a plain KanboardError all the way out of `precheck`, left the unit `failed`, and — for
the daily retro — consumed the only scheduled run of the day.

Three seams carry the fix and are covered here: the client retries a refused connection for a
bounded window and then raises the distinct KanboardUnreachable; the board-dependent prechecks turn
that one error into exit code 101; the gate waits and re-runs a precheck that answers 101, a
bounded number of times, and dispatches nothing while the board is out of reach.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from triggered_agents.agents.retro import cli as retro_cli
from triggered_agents.agents.steward import cli as steward_cli
from triggered_agents.runtime import health, kanboard
from triggered_agents.runtime.state import PRECHECK_BOARD_UNREACHABLE, AgentState

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "secretary-agent-gate.sh"
UNITS = REPO_ROOT / "packaging" / "systemd"


def refused(*_args, **_kwargs):
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


class Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class ClientRetryTests(unittest.TestCase):
    """`_post` rides out a refused connection, and only a refused connection."""

    def setUp(self) -> None:
        patch = mock.patch.object(kanboard, "_creds", return_value=mock.Mock(
            url="http://board.invalid/jsonrpc.php", authorization_header=lambda: "Basic x"))
        patch.start()
        self.addCleanup(patch.stop)
        for name, value in (("_CONNECT_RETRY_WINDOW_S", 9.0), ("_CONNECT_RETRY_SLEEP_S", 3.0)):
            p = mock.patch.object(kanboard, name, value)
            p.start()
            self.addCleanup(p.stop)
        # A fake clock, so the retry window is exercised at full length without waiting for it and
        # without touching the real `time` module every other test shares.
        self.slept: list[float] = []
        self.now = 0.0

        def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds

        clock = mock.patch.object(kanboard, "time",
                                  mock.Mock(monotonic=lambda: self.now, sleep=sleep))
        clock.start()
        self.addCleanup(clock.stop)

    def test_a_board_that_comes_up_late_is_waited_for_not_crashed_on(self):
        answers = [refused, refused, lambda *a, **k: Response(b'{"result": 7}')]

        def urlopen(*args, **kwargs):
            return answers.pop(0)(*args, **kwargs)

        with mock.patch.object(kanboard.urllib.request, "urlopen", urlopen):
            self.assertEqual(kanboard.call("getAllProjects"), 7)
        self.assertEqual(self.slept, [3.0, 3.0])

    def test_a_board_that_never_comes_up_raises_the_distinct_unreachable_error(self):
        with mock.patch.object(kanboard.urllib.request, "urlopen", refused):
            with self.assertRaises(kanboard.KanboardUnreachable) as caught:
                kanboard.call("getAllProjects")
        self.assertIn("getAllProjects", str(caught.exception))
        # Bounded: it gives up inside the window instead of retrying forever.
        self.assertLessEqual(sum(self.slept), 9.0)
        self.assertTrue(self.slept)

    def test_unreachable_is_still_a_kanboard_error_for_every_existing_handler(self):
        self.assertTrue(issubclass(kanboard.KanboardUnreachable, kanboard.KanboardError))

    def test_an_answering_board_is_never_retried(self):
        """An HTTP error and an RPC error come from something that is listening: no second call."""
        calls = []

        def http_error(*_args, **_kwargs):
            calls.append("http")
            raise urllib.error.HTTPError("http://board.invalid", 500, "boom", {}, None)

        with mock.patch.object(kanboard.urllib.request, "urlopen", http_error):
            with self.assertRaises(kanboard.KanboardError) as caught:
                kanboard.call("getAllProjects")
        self.assertNotIsInstance(caught.exception, kanboard.KanboardUnreachable)
        self.assertEqual(calls, ["http"])
        self.assertEqual(self.slept, [])

        with mock.patch.object(kanboard.urllib.request, "urlopen",
                               lambda *a, **k: Response(b'{"error": {"code": -32601}}')):
            with self.assertRaises(kanboard.KanboardError) as rpc:
                kanboard.call("nope")
        self.assertNotIsInstance(rpc.exception, kanboard.KanboardUnreachable)

    def test_a_timeout_is_not_treated_as_a_refused_connection(self):
        """A timeout may mean the request was already delivered; retrying it is not free."""
        with mock.patch.object(kanboard.urllib.request, "urlopen",
                               mock.Mock(side_effect=urllib.error.URLError(TimeoutError("timed out")))):
            with self.assertRaises(kanboard.KanboardError) as caught:
                kanboard.call("getAllProjects")
        self.assertNotIsInstance(caught.exception, kanboard.KanboardUnreachable)
        self.assertEqual(self.slept, [])


class PrecheckDeferralTests(unittest.TestCase):
    """Both board-dependent prechecks report 101, and say so in runs.jsonl."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = AgentState("agent", Path(self.temp.name))

    def runs(self) -> list[dict]:
        path = self.state.dir / "runs.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def assert_deferred(self, code: int) -> None:
        self.assertEqual(code, PRECHECK_BOARD_UNREACHABLE)
        self.assertNotEqual(PRECHECK_BOARD_UNREACHABLE, 0)
        events = [r for r in self.runs() if r["event"] == "precheck"]
        self.assertEqual([r["result"] for r in events], ["board-unreachable"])
        # The loss must be visible in the agent's own telemetry, not only as a stale healthy tick.
        self.assertIn("unreachable", events[0]["error"])

    def test_retro_defers_the_daily_run_instead_of_crashing_on_it(self):
        unreachable = kanboard.KanboardUnreachable("getAllProjects: board unreachable after 90s")
        with mock.patch.object(retro_cli, "STATE", self.state), \
             mock.patch.object(retro_cli.pipeline_ops, "close_old_done_cards", side_effect=unreachable):
            self.assert_deferred(retro_cli.cmd_precheck())

    def test_steward_defers_its_tick_instead_of_failing_the_unit(self):
        unreachable = kanboard.KanboardUnreachable("getAllTasks: board unreachable after 90s")
        with mock.patch.object(steward_cli, "STATE", self.state), \
             mock.patch.object(steward_cli.signals, "scan", side_effect=unreachable):
            self.assert_deferred(steward_cli.cmd_precheck())

    def test_a_precheck_that_really_broke_still_fails_the_unit(self):
        """The deferral branch is for the board being absent, not for the agent being broken."""
        with mock.patch.object(steward_cli, "STATE", self.state), \
             mock.patch.object(steward_cli.signals, "scan", side_effect=kanboard.KanboardError("rpc error")):
            self.assertEqual(steward_cli.cmd_precheck(), 2)
        self.assertEqual([r["result"] for r in self.runs() if r["event"] == "precheck"], ["error"])


class HealthTests(unittest.TestCase):
    """A deferred run is not an answered tick, so it must not set the freshness clock."""

    def runs_status(self, results: list[str]) -> tuple[list[str], str]:
        now = datetime.now(timezone.utc)
        records = [{"ts": (now - timedelta(minutes=len(results) - i)).isoformat(),
                    "event": "precheck", "result": r} for i, r in enumerate(results)]
        with mock.patch.object(health, "_runs", return_value=records):
            return health._runs_status("retro")

    def test_a_board_that_never_comes_back_still_goes_red(self):
        problems, _ = self.runs_status(["board-unreachable"] * 5)
        self.assertEqual(problems, ["no answered tick yet — board/env never came up"])

    def test_an_answered_tick_is_still_what_freshness_is_measured_from(self):
        problems, detail = self.runs_status(["no-change", "board-unreachable"])
        self.assertEqual(problems, [])
        self.assertIn("last tick", detail)


class GateTests(unittest.TestCase):
    """The shipped gate, run for real against a stub interpreter.

    The waiting has to live in this script: systemd refuses `RestartForceExitStatus=` on a
    `Type=oneshot` service, and these units are oneshot (`secretary/host.py` also reads that Type to
    decide what a healthy inactive unit looks like), so there is no unit-level retry to lean on. A
    oneshot has no start timeout by default, so the gate is free to wait.
    """

    def run_gate(self, codes: list[int], attempts: int = 3) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            codes_file = bin_dir / "codes"
            codes_file.write_text("\n".join(str(c) for c in codes) + "\n", encoding="utf-8")
            shim = bin_dir / "python3"
            # Stands in for `python3 -P -m ... role_env exec --role X -- python3 -P -m ... <cmd>`:
            # drops the role_env wrapper, answers each successive precheck with the next queued
            # code, and records anything else the gate decided to run.
            shim.write_text(
                "#!/usr/bin/env bash\n"
                "args=(\"$@\")\n"
                "for i in \"${!args[@]}\"; do\n"
                "  if [ \"${args[$i]}\" = \"--\" ]; then args=(\"${args[@]:$((i+1))}\"); break; fi\n"
                "done\n"
                "if [ \"${args[-1]}\" = precheck ]; then\n"
                "  code=$(head -n1 \"$STUB_CODES\")\n"
                "  tail -n +2 \"$STUB_CODES\" > \"$STUB_CODES.rest\"\n"
                "  mv \"$STUB_CODES.rest\" \"$STUB_CODES\"\n"
                "  echo precheck >> \"$STUB_CODES.log\"\n"
                "  exit \"${code:-0}\"\n"
                "fi\n"
                "echo \"ran: ${args[*]}\"\n",
                encoding="utf-8")
            shim.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=tmp,
                       STUB_CODES=str(codes_file),
                       TA_GATE_BOARD_ATTEMPTS=str(attempts), TA_GATE_BOARD_WAIT="0")
            result = subprocess.run([str(GATE), "retro"], capture_output=True, text=True,
                                    env=env, timeout=120)
            log = Path(str(codes_file) + ".log")
            result.attempts = len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0
            return result

    def test_a_board_that_comes_up_during_the_wait_still_gets_its_run(self):
        """The whole point: the run happens instead of being spent on the boot race."""
        result = self.run_gate([PRECHECK_BOARD_UNREACHABLE, PRECHECK_BOARD_UNREACHABLE, 0])
        self.assertEqual(result.returncode, 0)
        self.assertIn("dispatch", result.stdout)
        self.assertEqual(result.attempts, 3)

    def test_the_re_attempts_are_bounded_and_dispatch_nothing_when_they_run_out(self):
        result = self.run_gate([PRECHECK_BOARD_UNREACHABLE] * 9, attempts=3)
        self.assertEqual(result.returncode, PRECHECK_BOARD_UNREACHABLE)
        self.assertEqual(result.attempts, 3)
        self.assertIn("board unreachable after 3 attempts", result.stderr)
        # Dispatch and cleanup both talk to the same board none of the attempts could reach.
        self.assertNotIn("ran:", result.stdout)

    def test_work_and_skip_and_breakage_keep_their_existing_outcomes(self):
        work = self.run_gate([0])
        self.assertEqual(work.returncode, 0)
        self.assertIn("dispatch", work.stdout)
        skip = self.run_gate([100])
        self.assertEqual(skip.returncode, 0)
        self.assertIn("--cleanup-only", skip.stdout)
        broke = self.run_gate([2])
        self.assertEqual(broke.returncode, 2)
        self.assertIn("ERROR (rc=2)", broke.stderr)
        # Only the board's own code is waited on; a broken precheck is not re-run.
        self.assertEqual(broke.attempts, 1)


class UnitSpecTests(unittest.TestCase):
    """What the shipped units must keep for the gate's own waiting to work."""

    BOARD_DEPENDENT = ("secretary-retro.service", "secretary-steward.service")

    def test_the_board_dependent_units_stay_oneshot_with_no_start_timeout(self):
        """A start timeout would kill the gate mid-wait; a non-oneshot Type would change what
        `secretary/host.py` considers a healthy inactive unit."""
        for name in self.BOARD_DEPENDENT:
            with self.subTest(name):
                body = (UNITS / name).read_text(encoding="utf-8")
                self.assertIn("Type=oneshot\n", body)
                self.assertNotIn("TimeoutStartSec=", body)
                self.assertNotIn("TimeoutSec=", body)

    def test_the_daily_unit_still_catches_its_missed_run_up(self):
        timer = (UNITS / "secretary-retro.timer").read_text(encoding="utf-8")
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main()
