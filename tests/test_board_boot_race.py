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
import sys
import tempfile
import unittest
import urllib.error
import venv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from triggered_agents.agents.retro import cli as retro_cli
from triggered_agents.agents.steward import cli as steward_cli
from triggered_agents.runtime import health, kanboard
from triggered_agents.runtime.state import PRECHECK_BOARD_UNREACHABLE, PRECHECK_DEFERRED, AgentState

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
        patch = mock.patch.object(
            kanboard,
            "_creds",
            return_value=mock.Mock(
                url="http://board.invalid/jsonrpc.php", authorization_header=lambda: "Basic x"
            ),
        )
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

        clock = mock.patch.object(kanboard, "time", mock.Mock(monotonic=lambda: self.now, sleep=sleep))
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

        with (
            mock.patch.object(
                kanboard.urllib.request, "urlopen", lambda *a, **k: Response(b'{"error": {"code": -32601}}')
            ),
            self.assertRaises(kanboard.KanboardError) as rpc,
        ):
            kanboard.call("nope")
        self.assertNotIsInstance(rpc.exception, kanboard.KanboardUnreachable)

    def test_a_timeout_is_not_treated_as_a_refused_connection(self):
        """A timeout may mean the request was already delivered; retrying it is not free."""
        with (
            mock.patch.object(
                kanboard.urllib.request,
                "urlopen",
                mock.Mock(side_effect=urllib.error.URLError(TimeoutError("timed out"))),
            ),
            self.assertRaises(kanboard.KanboardError) as caught,
        ):
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
        retention = mock.Mock()
        retention.close_old_done.side_effect = unreachable
        with (
            mock.patch.object(retro_cli, "STATE", self.state),
        ):
            self.assert_deferred(retro_cli.cmd_precheck(retention))

    def test_retro_precheck_uses_its_injected_retention_port(self):
        class Retention:
            calls = 0

            def close_old_done(self):
                self.calls += 1
                return {"closed": [], "closed_count": 0}

        retention = Retention()
        with (
            mock.patch.object(retro_cli, "STATE", self.state),
            mock.patch.object(retro_cli.harvest, "harvest", return_value={"sessions": []}),
        ):
            self.assertEqual(retro_cli.cmd_precheck(retention), 100)
        self.assertEqual(retention.calls, 1)

    def test_steward_defers_its_tick_instead_of_failing_the_unit(self):
        unreachable = kanboard.KanboardUnreachable("getAllTasks: board unreachable after 90s")
        with (
            mock.patch.object(steward_cli, "STATE", self.state),
            mock.patch.object(steward_cli.signals, "scan", side_effect=unreachable),
        ):
            self.assert_deferred(steward_cli.cmd_precheck())

    def test_a_precheck_that_really_broke_still_fails_the_unit(self):
        """The deferral branch is for the board being absent, not for the agent being broken."""
        with (
            mock.patch.object(steward_cli, "STATE", self.state),
            mock.patch.object(steward_cli.signals, "scan", side_effect=kanboard.KanboardError("rpc error")),
        ):
            self.assertEqual(steward_cli.cmd_precheck(), 2)
        self.assertEqual([r["result"] for r in self.runs() if r["event"] == "precheck"], ["error"])


class HealthTests(unittest.TestCase):
    """A deferred run is not an answered tick, so it must not set the freshness clock."""

    def runs_status(self, results: list[str]) -> tuple[list[str], str]:
        now = datetime.now(UTC)
        records = [
            {"ts": (now - timedelta(minutes=len(results) - i)).isoformat(), "event": "precheck", "result": r}
            for i, r in enumerate(results)
        ]
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
    """The shipped gate, run for real against isolated product roots and venvs.

    The waiting has to live in this script: systemd refuses `RestartForceExitStatus=` on a
    `Type=oneshot` service, and these units are oneshot (`secretary/host.py` also reads that Type to
    decide what a healthy inactive unit looks like), so there is no unit-level retry to lean on. A
    oneshot has no start timeout by default, so the gate is free to wait.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.product = self.make_product(self.root / "runtime")
        self.ambient_bin = self.root / "ambient-bin"
        self.ambient_bin.mkdir()
        self.ambient_python = self.ambient_bin / "python3"
        self.ambient_python.write_text(
            '#!/bin/sh\nprintf "%s\\n" "used ambient python" > "$AMBIENT_SENTINEL"\nexit 86\n',
            encoding="utf-8",
        )
        self.ambient_python.chmod(0o755)
        self.run_number = 0

    def make_product(self, root: Path) -> Path:
        """Build a fake checkout whose required dependency exists only in its venv."""
        source = root / "src"
        for package in (
            source / "triggered_agents" / "runtime",
            source / "secretary" / "dispatch",
        ):
            package.mkdir(parents=True)
            current = package
            while current != source:
                (current / "__init__.py").touch()
                current = current.parent

        (source / "secretary" / "config.py").write_text(
            "import referencing\nDEPENDENCY = referencing.MARKER\n",
            encoding="utf-8",
        )
        (source / "triggered_agents" / "runtime" / "role_env.py").write_text(
            "from secretary.config import DEPENDENCY\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "\n"
            "def main():\n"
            "    with open(os.environ['STUB_RECORDS'], 'a', encoding='utf-8') as handle:\n"
            "        handle.write(json.dumps({'kind': 'role_env', 'executable': sys.executable, "
            "'prefix': sys.prefix}) + '\\n')\n"
            "    marker = sys.argv.index('--')\n"
            "    command = sys.argv[marker + 1:]\n"
            "    os.execvpe(command[0], command, os.environ)\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    main()\n",
            encoding="utf-8",
        )
        target = (
            "from secretary.config import DEPENDENCY\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "\n"
            "def main(module):\n"
            "    agent, command, *rest = sys.argv[1:]\n"
            "    with open(os.environ['STUB_RECORDS'], 'a', encoding='utf-8') as handle:\n"
            "        handle.write(json.dumps({'kind': 'role', 'executable': sys.executable, "
            "'prefix': sys.prefix, 'module': module, 'agent': agent, 'command': command, "
            "'rest': rest}) + '\\n')\n"
            "    if command == 'precheck':\n"
            "        codes = os.environ['STUB_CODES']\n"
            "        with open(codes, encoding='utf-8') as handle:\n"
            "            remaining = handle.read().splitlines()\n"
            "        code = int(remaining.pop(0)) if remaining else 0\n"
            "        with open(codes, 'w', encoding='utf-8') as handle:\n"
            "            handle.write('\\n'.join(remaining) + ('\\n' if remaining else ''))\n"
            "        with open(codes + '.log', 'a', encoding='utf-8') as handle:\n"
            "            handle.write('precheck\\n')\n"
            "        raise SystemExit(code)\n"
            "    print('ran: -m ' + module + ' ' + ' '.join([agent, command, *rest]))\n"
            "\n"
        )
        (source / "triggered_agents" / "__main__.py").write_text(
            target + "if __name__ == '__main__':\n    main('triggered_agents')\n", encoding="utf-8"
        )
        (source / "secretary" / "dispatch" / "standing_agent.py").write_text(
            target + "if __name__ == '__main__':\n    main('secretary.dispatch.standing_agent')\n",
            encoding="utf-8",
        )

        venv.EnvBuilder(with_pip=False).create(root / ".venv")
        python = root / ".venv" / "bin" / "python3"
        site_packages = Path(
            subprocess.run(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        (site_packages / "referencing.py").write_text("MARKER = 'venv-only'\n", encoding="utf-8")
        return root

    def run_gate(
        self,
        codes: list[int],
        attempts: int = 3,
        *,
        agent: str = "retro",
        variant: str | None = None,
        env_overrides: dict[str, str | None] | None = None,
        home: Path | None = None,
    ) -> subprocess.CompletedProcess:
        self.run_number += 1
        run_dir = self.root / f"run-{self.run_number}"
        run_dir.mkdir()
        codes_file = run_dir / "codes"
        codes_file.write_text("\n".join(str(c) for c in codes) + "\n", encoding="utf-8")
        records = run_dir / "records.jsonl"
        ambient_sentinel = run_dir / "ambient-python-used"
        env = dict(
            os.environ,
            PATH=f"{self.ambient_bin}:{os.environ['PATH']}",
            HOME=str(home or self.root / "home"),
            STUB_CODES=str(codes_file),
            STUB_RECORDS=str(records),
            AMBIENT_SENTINEL=str(ambient_sentinel),
            TA_GATE_BOARD_ATTEMPTS=str(attempts),
            TA_GATE_BOARD_WAIT="0",
            TA_RUNTIME_PYTHONPATH=str(self.product),
            TA_SECRETARY_REPO=str(self.root / "configured-but-not-selected"),
            VIRTUAL_ENV=str(self.root / "activated-candidate-venv"),
        )
        for name, value in (env_overrides or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        command = [str(GATE), agent]
        if variant is not None:
            command.append(variant)
        result = subprocess.run(command, check=False, capture_output=True, text=True, env=env, timeout=120)
        log = Path(str(codes_file) + ".log")
        result.attempts = len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0
        result.records = records
        self.assertFalse(ambient_sentinel.exists(), result.stderr)
        return result

    def records(self, result: subprocess.CompletedProcess) -> list[dict]:
        if not result.records.is_file():
            return []
        return [json.loads(line) for line in result.records.read_text(encoding="utf-8").splitlines() if line]

    def assert_selected_venv(self, result: subprocess.CompletedProcess, root: Path) -> None:
        records = self.records(result)
        self.assertTrue(records)
        expected_python = str(root / ".venv" / "bin" / "python3")
        expected_prefix = str(root / ".venv")
        self.assertEqual({record["executable"] for record in records}, {expected_python})
        self.assertEqual({record["prefix"] for record in records}, {expected_prefix})

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
        self.assert_selected_venv(work, self.product)
        skip = self.run_gate([100])
        self.assertEqual(skip.returncode, 0)
        self.assertIn("--cleanup-only", skip.stdout)
        self.assert_selected_venv(skip, self.product)
        broke = self.run_gate([2])
        self.assertEqual(broke.returncode, 2)
        self.assertIn("ERROR (rc=2)", broke.stderr)
        self.assert_selected_venv(broke, self.product)
        # Only the board's own code is waited on; a broken precheck is not re-run.
        self.assertEqual(broke.attempts, 1)

    def test_settlement_defer_exits_successfully_without_dispatch_or_cleanup(self):
        result = self.run_gate([PRECHECK_DEFERRED])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.attempts, 1)
        self.assertIn("settlement busy, tick deferred", result.stderr)
        self.assertNotIn("ran:", result.stdout)

    def test_gate_routes_board_roles_through_secretary_and_leaves_curator_legacy(self):
        for agent in ("retro", "steward"):
            with self.subTest(agent):
                result = self.run_gate([0], agent=agent)
                self.assertEqual(result.returncode, 0)
                self.assertIn(f"-m secretary.dispatch.standing_agent {agent} dispatch", result.stdout)
                self.assert_selected_venv(result, self.product)

        curator = self.run_gate([0], agent="curator")
        self.assertEqual(curator.returncode, 0)
        self.assertIn("-m triggered_agents curator dispatch", curator.stdout)
        self.assertNotIn("secretary.dispatch.standing_agent", curator.stdout)
        self.assert_selected_venv(curator, self.product)

    def test_deep_sweep_keeps_its_ungated_variant_through_the_standing_root(self):
        result = self.run_gate([], agent="steward", variant="deep-sweep")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.attempts, 0)
        self.assertIn("-m secretary.dispatch.standing_agent steward dispatch deep-sweep", result.stdout)
        self.assert_selected_venv(result, self.product)

    def test_curator_enters_and_leaves_role_env_with_the_selected_venv_not_ambient_python(self):
        """Reproduce the live split without consulting the host's system site-packages."""
        ambient = subprocess.run(
            [
                sys.executable,
                "-S",
                "-P",
                "-m",
                "triggered_agents.runtime.role_env",
                "exec",
                "--role",
                "curator",
                "--",
                "true",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(self.product / "src")},
            timeout=120,
        )
        self.assertNotEqual(ambient.returncode, 0)
        self.assertIn("ModuleNotFoundError", ambient.stderr)
        self.assertIn("referencing", ambient.stderr)

        result = self.run_gate([100], agent="curator")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.attempts, 1)
        self.assertIn("--cleanup-only", result.stdout)
        self.assert_selected_venv(result, self.product)
        self.assertEqual(
            [record["kind"] for record in self.records(result)], ["role_env", "role", "role_env", "role"]
        )

    def test_every_supported_root_precedence_selects_its_own_venv(self):
        runtime = self.make_product(self.root / "runtime-wins")
        configured = self.make_product(self.root / "configured")
        home = self.root / "home-default"
        fallback = self.make_product(home / "secretary")

        cases = (
            (
                "runtime",
                {"TA_RUNTIME_PYTHONPATH": str(runtime), "TA_SECRETARY_REPO": str(configured)},
                runtime,
            ),
            ("configured", {"TA_RUNTIME_PYTHONPATH": None, "TA_SECRETARY_REPO": str(configured)}, configured),
            ("home", {"TA_RUNTIME_PYTHONPATH": None, "TA_SECRETARY_REPO": None}, fallback),
        )
        for name, overrides, expected in cases:
            with self.subTest(name):
                result = self.run_gate([100], agent="curator", env_overrides=overrides, home=home)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_selected_venv(result, expected)

    def test_missing_non_executable_and_wrong_venv_interpreters_fail_before_precheck(self):
        missing = self.root / "missing"
        non_executable = self.root / "non-executable"
        (non_executable / "src").mkdir(parents=True)
        interpreter = non_executable / ".venv" / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("not executable\n", encoding="utf-8")
        wrong = self.root / "wrong"
        (wrong / "src").mkdir(parents=True)
        wrong_interpreter = wrong / ".venv" / "bin" / "python3"
        wrong_interpreter.parent.mkdir(parents=True)
        wrong_interpreter.symlink_to(sys.executable)

        for name, root in (("missing", missing), ("non-executable", non_executable), ("wrong", wrong)):
            with self.subTest(name):
                result = self.run_gate(
                    [0], agent="curator", env_overrides={"TA_RUNTIME_PYTHONPATH": str(root)}
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.attempts, 0)
                self.assertEqual(self.records(result), [])
                self.assertIn(str(root), result.stderr)
                self.assertIn("secretary upgrade --no-pull --product-root", result.stderr)
                self.assertIn("Do not use system-wide pip", result.stderr)


class UnitSpecTests(unittest.TestCase):
    """What the shipped units must keep for the gate's own waiting to work."""

    BOARD_DEPENDENT = ("secretary-retro.service", "secretary-steward.service")
    MECHANICAL_ROLE_UNITS = (
        "secretary-curator.service",
        "secretary-retro.service",
        "secretary-steward.service",
        "secretary-steward-deep-sweep.service",
    )

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

    def test_a_tick_ending_does_not_kill_the_local_pty_head_it_started(self):
        """The local-pty supervisor is the durable owner after the oneshot exits.

        systemd's default ``KillMode=control-group`` sends SIGTERM to every process the service
        left behind when its main process exits. That made the production steward canary record
        ``run.started`` and ``signal:15`` almost back-to-back. Mechanical roles can all select the
        local-pty backend, so every unit that launches one must leave its supervisor to the
        runtime's explicit drain/stop protocol.
        """
        for name in self.MECHANICAL_ROLE_UNITS:
            with self.subTest(name):
                body = (UNITS / name).read_text(encoding="utf-8")
                self.assertIn("Type=oneshot\n", body)
                self.assertIn("KillMode=process\n", body)


if __name__ == "__main__":
    unittest.main()
