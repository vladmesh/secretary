"""secretary-1474: a mechanical role whose head this product holds under a supervisor of its own.

The driver in `runtime/dispatch.py` has always driven every head of curator, steward and retro
through `SessionHost`. Which backend holds a head has been the profile's own answer since
secretary-1467, and the dispatcher's core has read it since; this driver did not. These tests are
about the choice it now makes and about the tick that choice leads to.

Nothing about the supervised half is faked, for the reason `test_local_pty_head_runtime` gives:
what the branch has to establish — a head that outlives the tick that raised it, a bring-up over a
live head that is refused before anything is spawned, a record from another boot that fences
nothing out — are facts about processes on this host, and a fake backend would settle none of them.
So every supervised tick here starts a real supervisor over a real pty, and every test gives back
what it started.

The pane half is asserted the other way round: on the supervised path the session manager is a host
that raises on contact, and the four pane readings the tick used to make are replaced by mocks that
fail the test if they are called at all.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatcher_watchdog import head_process_status
from triggered_agents.agents.pipeline import heads as pipeline_heads
from triggered_agents.agents.pipeline import health as pipeline_health
from triggered_agents.runtime import dispatch
from triggered_agents.runtime import state as runtime_state
from triggered_agents.runtime.head import HeadCommand
from triggered_agents.runtime.head.local_pty import protocol
from triggered_agents.runtime.head.local_pty.client import SupervisorClient
from triggered_agents.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from triggered_agents.runtime.pane_host import Pane

REPO = Path(__file__).resolve().parents[1]
#: A head that never exits and says what its own terminal handed it. Both properties are the point:
#: the first is what makes "the head outlived the tick" observable, and the second is the only
#: witness that can say a skill delivered across the boundary actually reached the head.
LINE_READER = REPO / "tests" / "fixtures" / "local_pty_line_reader.py"
HEAD_COMMAND = f"{sys.executable} -u {LINE_READER} --pause 0 --idle 0.2"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    close = stat.rfind(")")
    fields = stat[close + 2:].split()
    return bool(fields) and fields[0] != "Z"


def _kill(pid: int, number: int = signal.SIGKILL, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, number) if group else os.kill(pid, number)
    except OSError:
        pass


class ForbiddenSessionHost:
    """A session manager that fails the test on contact.

    The supervised path's claim is not "it uses Orca less"; it is that it uses Orca not at all. A
    host that answers plausibly could let a stray call pass unnoticed, so this one cannot answer.
    """

    def _refuse(self, verb: str):
        raise AssertionError(f"the supervised path reached the session manager: {verb}")

    def send(self, *args, **kwargs):
        self._refuse("send")

    def read(self, *args, **kwargs):
        self._refuse("read")

    def wait_idle(self, *args, **kwargs):
        self._refuse("wait_idle")

    def open_pane(self, *args, **kwargs):
        self._refuse("open_pane")

    def split_pane(self, *args, **kwargs):
        self._refuse("split_pane")

    def rename_pane(self, *args, **kwargs):
        self._refuse("rename_pane")

    def close_pane(self, *args, **kwargs):
        self._refuse("close_pane")

    def panes(self, *args, **kwargs):
        self._refuse("panes")

    def stop_workspace(self, *args, **kwargs):
        self._refuse("stop_workspace")


class RecordingSessionHost:
    """The pane backend as the ordinary tick uses it, recording what it was asked."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str, str]] = []

    def send(self, handle: str, text: str, *, enter: bool) -> dict:
        return {"send": {"accepted": True}}

    def read(self, handle: str, *, limit: int | None = None) -> dict:
        return {"terminal": {"tail": []}}

    def wait_idle(self, handle: str, *, timeout_ms: int) -> dict:
        return {"wait": {"satisfied": True}}

    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        self.opened.append((workspace, title, command))
        return Pane(handle=f"term-{len(self.opened)}", leaf=f"leaf-{len(self.opened)}", title=title)

    def split_pane(self, handle: str, command: str) -> Pane:
        raise AssertionError("the scheduler never splits a pane")

    def rename_pane(self, handle: str, title: str) -> None:
        raise AssertionError("the scheduler never renames a pane")

    def close_pane(self, handle: str) -> None:
        return None

    def panes(self, workspace: str) -> list[Pane]:
        return []

    def stop_workspace(self, workspace: str) -> None:
        return None


class MechanicalRoleBackendTestCase(unittest.TestCase):
    """One mechanical role, one registry, and a tick that can be run more than once.

    The agent is `retro` rather than `steward` deliberately: the steward's report card is a board
    write, and what these tests are about is which backend holds a head, not what a role reports.
    """

    AGENT = "retro"

    def setUp(self) -> None:
        # /tmp rather than the workspace: a Unix socket address is bounded at about a hundred
        # bytes, and a run root under a workspace path does not fit inside one.
        self.data_dir = Path(tempfile.mkdtemp(prefix="lp-driver-"))
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.root = self.data_dir / "heads"
        self.state_root = Path(tempfile.mkdtemp(prefix="lp-state-"))
        self.addCleanup(shutil.rmtree, self.state_root, ignore_errors=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="lp-ws-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.addCleanup(self._reap_everything)
        self.state = runtime_state.AgentState(self.AGENT, self.state_root / self.AGENT)
        self.prompt_after_start = False
        #: Which profile this tick's health resolution lands on. Named per test because a launch
        #: diverted onto another profile is one of the cases the choice has to survive.
        self.resolved = "head"

    # -- the registry this tick reads ----------------------------------------------------------

    def _registry(self, **profile: object) -> pipeline_heads.Registry:
        return pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {"head": {"resource": "acct", "adapter": "claude", "fallback": [], **profile}},
            {self.AGENT: "head"},
        )

    def _rendered(self, profile, *, prompt=None, workspace="", role="", identity=None,
                  binding="") -> HeadCommand:
        """The command a head is raised with, substituted for the real renderer.

        What the renderer produces is a provider CLI this test has no business starting, and how it
        produces it is `test_head_command`'s subject. Everything the branch reads — the resolved
        profile and its `runtime` — still comes from the real registry through the real
        `_launch_cmd`; only the program the head runs is this fixture.
        """
        return HeadCommand(HEAD_COMMAND, prompt_after_start=self.prompt_after_start,
                           adapter="claude")

    @contextlib.contextmanager
    def _tick(self, registry: pipeline_heads.Registry, *, host):
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(mock.patch.dict(os.environ, {"SECRETARY_DATA_DIR": str(self.data_dir)}))
            enter(mock.patch.object(runtime_state, "STATE_ROOT", self.state_root))
            enter(mock.patch.object(dispatch, "_workspace", return_value=str(self.workspace)))
            enter(mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/retro"}))
            enter(mock.patch.object(dispatch, "_pipeline_paused", return_value=False))
            enter(mock.patch.object(dispatch, "CLAUDE_JSON", self.data_dir / "claude.json"))
            enter(mock.patch.object(dispatch, "render_head_command", self._rendered))
            enter(mock.patch.object(pipeline_heads, "load_registry", return_value=registry))
            enter(mock.patch.object(pipeline_health, "refresh", return_value={}))
            enter(mock.patch.object(pipeline_health, "resolve_head",
                                    return_value=self.resolved))
            yield host

    def run_tick(self, registry: pipeline_heads.Registry, *, host=None) -> int:
        host = ForbiddenSessionHost() if host is None else host
        with self._tick(registry, host=host):
            return dispatch.run(self.AGENT, host=host)

    # -- what the tick left behind -------------------------------------------------------------

    def actions(self) -> list[str]:
        runs = self.state.dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(line)["action"]
                for line in runs.read_text(encoding="utf-8").splitlines()]

    def events(self) -> list[dict]:
        runs = self.state.dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(line) for line in runs.read_text(encoding="utf-8").splitlines()]

    def run_dirs(self) -> list[Path]:
        return sorted(path for path in self.root.glob("*") if path.is_dir()) \
            if self.root.is_dir() else []

    def head_pid(self, run_dir: Path) -> int:
        try:
            record = json.loads((run_dir / protocol.PID_FILE_NAME).read_text(encoding="utf-8"))
            return int(record.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            return 0

    def supervisor_pid(self, run_dir: Path) -> int:
        try:
            return int((run_dir / protocol.SUPERVISOR_PID_NAME).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def head_output(self, run_dir: Path) -> str:
        """What the head has printed, read straight from its own supervisor.

        The head, not a receipt: `local_pty_line_reader` reports the records its own terminal
        handed it, which is the only witness that can say a skill actually reached it.
        """
        socket_path = protocol.socket_path_for(run_dir)
        try:
            with SupervisorClient.connect(socket_path, timeout=5.0) as client:
                return bytes(client.read_output()["bytes_data"]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a supervisor that cannot be asked has printed nothing
            return ""

    def _reap_everything(self) -> None:
        for run_dir in self.run_dirs():
            head, supervisor = self.head_pid(run_dir), self.supervisor_pid(run_dir)
            _kill(head, group=True)
            _kill(head)
            _kill(supervisor)
        for run_dir in self.run_dirs():
            self.await_(lambda pid=self.head_pid(run_dir): not _alive(pid), timeout=5.0, soft=True)

    def await_(self, predicate, *, timeout: float = 15.0, message: str = "", soft: bool = False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        if predicate():
            return True
        if not soft:
            self.fail(message or f"condition never held within {timeout:g}s")
        return False


class BackendChoiceTests(MechanicalRoleBackendTestCase):
    """Criterion 1 and 2: the driver chooses, and choosing changes nothing for a pane."""

    def test_a_profile_with_no_runtime_key_keeps_the_pane_lifecycle(self) -> None:
        host = RecordingSessionHost()

        with mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)):
            self.assertEqual(self.run_tick(self._registry(), host=host), 0)

        self.assertEqual([title for _ws, title, _cmd in host.opened],
                         [f"triggered-agent:{self.AGENT}"])
        self.assertEqual(self.run_dirs(), [], "a pane profile raised a supervised head")
        self.assertEqual(self.actions(), ["created"])

    def test_a_profile_naming_the_pane_backend_keeps_the_pane_lifecycle(self) -> None:
        host = RecordingSessionHost()

        with mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)):
            self.assertEqual(
                self.run_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), host=host), 0
            )

        self.assertEqual([title for _ws, title, _cmd in host.opened],
                         [f"triggered-agent:{self.AGENT}"])
        self.assertEqual(self.run_dirs(), [], "a pane profile raised a supervised head")
        self.assertEqual(self.actions(), ["created"])

    def test_a_profile_naming_the_supervisor_raises_a_head_under_one(self) -> None:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        run_dirs = self.run_dirs()
        self.assertEqual(len(run_dirs), 1, "the tick raised no supervised head")
        self.assertTrue(_alive(self.head_pid(run_dirs[0])))
        self.assertEqual(self.actions(), ["supervised-started"])

    def test_the_backend_is_read_off_the_profile_the_launch_was_rendered_from(self) -> None:
        """Not off a second lookup: the value is the resolved profile's own, as `_launch_cmd`
        handed it back. The profile the registry routes this agent to is `orca-legacy`, and health
        resolves the launch onto a supervised one, so a driver reading the routed profile instead
        of the resolved one would take the pane path here."""
        registry = pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {
                "routed": {"resource": "acct", "adapter": "claude", "fallback": ["supervised"],
                           "runtime": ORCA_LEGACY_RUNTIME},
                "supervised": {"resource": "acct", "adapter": "claude", "fallback": [],
                               "runtime": LOCAL_PTY_RUNTIME},
            },
            {self.AGENT: "routed"},
        )

        self.resolved = "supervised"

        self.assertEqual(self.run_tick(registry), 0)

        self.assertEqual(len(self.run_dirs()), 1)
        self.assertEqual(self.state.load_head_profile(), "supervised")

    def test_a_launch_diverted_onto_a_pane_profile_runs_its_ordinary_tick(self) -> None:
        """The mirror image, and the reason the choice is confirmed after the resolution.

        The registry routes this agent to a supervised head, but this tick's own resolution landed
        on a pane profile — a red resource is exactly how that happens. The tick has to dispatch
        that head the way that backend's heads are dispatched, with the command it already built.
        """
        registry = pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {
                "routed": {"resource": "acct", "adapter": "claude", "fallback": ["pane"],
                           "runtime": LOCAL_PTY_RUNTIME},
                "pane": {"resource": "acct", "adapter": "claude", "fallback": []},
            },
            {self.AGENT: "routed"},
        )
        host = RecordingSessionHost()
        self.resolved = "pane"

        with mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)):
            self.assertEqual(self.run_tick(registry, host=host), 0)

        self.assertEqual(len(host.opened), 1, "the diverted launch never reached a pane")
        self.assertEqual(self.run_dirs(), [])
        self.assertEqual(self.state.load_head_profile(), "pane")
        self.assertEqual(self.actions(), ["created"])


class SupervisedDeliveryTests(MechanicalRoleBackendTestCase):
    """Criterion 3: the skill crosses the backend's own boundary, and nothing else is touched."""

    def test_the_skill_reaches_the_head_across_the_boundary_and_no_pane_is_read(self) -> None:
        self.prompt_after_start = True
        forbidden = {
            name: mock.patch.object(
                dispatch, name,
                side_effect=AssertionError(f"the supervised path called {name}"),
            )
            for name in ("_confirm_delivery", "_is_idle", "_agent_terminals", "_reap_ghosts")
        }

        with contextlib.ExitStack() as stack:
            for patch in forbidden.values():
                stack.enter_context(patch)
            self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        run_dir = self.run_dirs()[0]
        self.await_(
            lambda: "/retro" in self.head_output(run_dir),
            message="the head never reported the skill its own terminal handed it",
        )

    def test_a_head_whose_command_carries_its_prompt_is_not_typed_at(self) -> None:
        """The claude shape: the prompt is on the command line, exactly as it is on a pane.

        There is nothing to deliver, so the receipt is a bring-up and the head is left working.
        """
        self.prompt_after_start = False

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        run_dir = self.run_dirs()[0]
        self.await_(lambda: "UP" in self.head_output(run_dir), message="the head never started")
        self.assertNotIn("/retro", self.head_output(run_dir))


class SupervisedHeadLifetimeTests(MechanicalRoleBackendTestCase):
    """Criteria 4, 5 and 6: the head outlives the tick, and what a later tick makes of it."""

    def raise_one(self) -> Path:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dirs = self.run_dirs()
        self.assertEqual(len(run_dirs), 1)
        return run_dirs[0]

    def test_the_head_and_its_supervisor_outlive_the_tick_that_raised_them(self) -> None:
        run_dir = self.raise_one()

        self.assertTrue(_alive(self.head_pid(run_dir)), "the head died with its tick")
        self.assertTrue(_alive(self.supervisor_pid(run_dir)), "the supervisor died with its tick")
        record = self.state.load_head_run()
        self.assertEqual(record["run_id"], run_dir.name)
        self.assertEqual(record["workspace"], str(self.workspace))
        self.assertEqual(record["role"], self.AGENT)

    def test_the_record_is_written_the_way_its_neighbours_are(self) -> None:
        """One small file in this agent's own state directory, replaced rather than rewritten."""
        self.raise_one()

        self.assertEqual(self.state.head_run_file, self.state.dir / "head_run.json")
        self.assertEqual(list(self.state.dir.glob("*.tmp")), [], "a temporary file was left behind")
        self.state.head_run_file.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(self.state.load_head_run(), "a corrupt record must not fence the role out")

    def test_a_tick_over_a_working_head_raises_no_second_one_and_sends_no_second_skill(self) -> None:
        """Criterion 5, on the precondition secretary-1468 put in front of the spawn."""
        self.prompt_after_start = True
        run_dir = self.raise_one()
        self.await_(lambda: "/retro" in self.head_output(run_dir))
        head, supervisor = self.head_pid(run_dir), self.supervisor_pid(run_dir)
        delivered = self.head_output(run_dir).count("/retro")

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.run_dirs(), [run_dir], "a second run was started beside a live head")
        self.assertEqual((self.head_pid(run_dir), self.supervisor_pid(run_dir)),
                         (head, supervisor), "the live head was replaced")
        self.assertEqual(self.head_output(run_dir).count("/retro"), delivered,
                         "the busy head was sent a second skill")
        self.assertEqual(self.actions(), ["supervised-started", "supervised-busy-skip"])
        self.assertEqual(self.events()[-1]["error"], "head_already_up",
                         "the refusal is the launch identity's, made before anything was spawned")

    def test_a_dead_head_is_an_ordinary_bring_up(self) -> None:
        """Criterion 6: nothing about a finished run fences its role out of the next tick."""
        run_dir = self.raise_one()
        dead = self.head_pid(run_dir)
        _kill(self.supervisor_pid(run_dir))
        _kill(dead, group=True)
        _kill(dead)
        self.await_(lambda: head_process_status(
            str(run_dir / protocol.PID_FILE_NAME)).get("state") == "dead",
            message="the head never actually died")

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.run_dirs(), [run_dir], "the dead run was abandoned rather than reused")
        self.await_(lambda: _alive(self.head_pid(run_dir)) and self.head_pid(run_dir) != dead,
                    message="the tick raised no head over the dead one")
        self.assertEqual(self.actions(), ["supervised-started", "supervised-started"])

    def test_a_record_left_by_a_previous_boot_does_not_fence_the_role_out(self) -> None:
        """The reboot case: a run directory full of files and a pid that means nothing.

        Refusing here would leave a mechanical role off duty for good, since nothing rewrites that
        record except the bring-up the refusal is preventing.
        """
        run_dir = self.raise_one()
        pid_file = run_dir / protocol.PID_FILE_NAME
        stale = self.head_pid(run_dir)
        _kill(self.supervisor_pid(run_dir))
        _kill(stale, group=True)
        _kill(stale)
        self.await_(lambda: not _alive(stale), message="the head never actually died")
        record = json.loads(pid_file.read_text(encoding="utf-8"))
        record["boot_id"] = "00000000-0000-0000-0000-000000000000"
        pid_file.write_text(json.dumps(record), encoding="utf-8")

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.await_(lambda: _alive(self.head_pid(run_dir)),
                    message="a record from another boot became a permanent refusal to go on duty")
        self.assertEqual(self.actions(), ["supervised-started", "supervised-started"])


if __name__ == "__main__":
    unittest.main()
