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

The pane half is asserted the other way round, and at the bottom: on the supervised path the
session manager is a host that raises on contact AND `orca_rpc.call` — the one place this driver
reaches the session store directly, under `_reap_ghosts` — records and raises. Nothing in between
is stubbed out, so no helper can stand in front of a call and hide it: a mock of `_reap_ghosts`
that answers plausibly is exactly what let the last round's tests pass over a breach.
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
    fields = stat[close + 2 :].split()
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
        self.stopped: list[str] = []

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
        self.stopped.append(workspace)
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
        #: How many times this tick opened the head registry. One is the contract.
        self.reads = 0

    # -- the registry this tick reads ----------------------------------------------------------

    def _registry(self, **profile: object) -> pipeline_heads.Registry:
        return pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {"head": {"resource": "acct", "adapter": "claude", "fallback": [], **profile}},
            {self.AGENT: "head"},
        )

    def _rendered(
        self, profile, *, prompt=None, workspace="", role="", identity=None, binding=""
    ) -> HeadCommand:
        """The command a head is raised with, substituted for the real renderer.

        What the renderer produces is a provider CLI this test has no business starting, and how it
        produces it is `test_head_command`'s subject. Everything the branch reads — the resolved
        profile and its `runtime` — still comes from the real registry through the real
        `_launch_cmd`; only the program the head runs is this fixture.
        """
        return HeadCommand(HEAD_COMMAND, prompt_after_start=self.prompt_after_start, adapter="claude")

    def _reads(self, registry):
        """What each `load_registry()` of this tick answers, and how many it took.

        A registry is one answer for every read. A list is the readings in order, the last one
        standing for any further read: a second reading gets the second answer, which is what an
        ordinary profile publication landing mid-tick used to do to a tick that read twice. A tick
        that takes one reading never sees past the first entry, and `self.reads` is what says so.
        """
        answers = list(registry) if isinstance(registry, list) else [registry]

        def read() -> pipeline_heads.Registry:
            self.reads += 1
            return answers.pop(0) if len(answers) > 1 else answers[0]

        return read

    @contextlib.contextmanager
    def _orca_banned(self):
        """Every direct call into Orca's session store, recorded and refused.

        `orca_rpc.call` is the bottom of the one route this driver has to the session store that
        `SessionHost` does not carry (`_reap_ghosts` uses it). Recording as well as raising is
        deliberate: `_reap_ghosts` swallows its own failures, so a test that only raised would
        prove nothing about a call that was in fact made.
        """
        made: list[str] = []

        def banned(method, *args, **kwargs):
            made.append(method)
            raise AssertionError(f"the supervised path reached Orca: {method}")

        with mock.patch.object(dispatch.orca_rpc, "call", side_effect=banned):
            yield made

    @contextlib.contextmanager
    def _tick(self, registry, *, host):
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(
                mock.patch.dict(
                    os.environ,
                    {
                        "SECRETARY_DATA_DIR": str(self.data_dir),
                        # The grant helper is a Secretary-owned launch boundary.  Keep this
                        # subprocess on the product tree this fixture is testing instead of an
                        # ambient installation's selected checkout.
                        "TA_SECRETARY_REPO": str(Path(__file__).resolve().parents[1]),
                    },
                )
            )
            enter(mock.patch.object(runtime_state, "STATE_ROOT", self.state_root))
            enter(mock.patch.object(dispatch, "_workspace", return_value=str(self.workspace)))
            enter(mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/retro"}))
            enter(mock.patch.object(dispatch, "_pipeline_paused", return_value=False))
            enter(mock.patch.object(dispatch, "CLAUDE_JSON", self.data_dir / "claude.json"))
            enter(mock.patch.object(dispatch, "render_head_command", self._rendered))
            enter(mock.patch.object(pipeline_heads, "load_registry", side_effect=self._reads(registry)))
            enter(mock.patch.object(pipeline_health, "refresh", return_value={}))
            enter(mock.patch.object(pipeline_health, "resolve_head", return_value=self.resolved))
            yield host

    def run_tick(self, registry, *, host=None) -> int:
        host = ForbiddenSessionHost() if host is None else host
        with self._tick(registry, host=host):
            return dispatch.run(self.AGENT, host=host, report_board=getattr(self, "board", None))

    # -- what the tick left behind -------------------------------------------------------------

    def actions(self) -> list[str]:
        runs = self.state.dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(line)["action"] for line in runs.read_text(encoding="utf-8").splitlines()]

    def events(self) -> list[dict]:
        runs = self.state.dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(line) for line in runs.read_text(encoding="utf-8").splitlines()]

    def run_dirs(self) -> list[Path]:
        return sorted(path for path in self.root.glob("*") if path.is_dir()) if self.root.is_dir() else []

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

        self.assertEqual([title for _ws, title, _cmd in host.opened], [f"triggered-agent:{self.AGENT}"])
        self.assertEqual(self.run_dirs(), [], "a pane profile raised a supervised head")
        self.assertEqual(self.actions(), ["created"])

    def test_a_profile_naming_the_pane_backend_keeps_the_pane_lifecycle(self) -> None:
        host = RecordingSessionHost()

        with mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)):
            self.assertEqual(self.run_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), host=host), 0)

        self.assertEqual([title for _ws, title, _cmd in host.opened], [f"triggered-agent:{self.AGENT}"])
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
                "routed": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": ["supervised"],
                    "runtime": ORCA_LEGACY_RUNTIME,
                },
                "supervised": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": [],
                    "runtime": LOCAL_PTY_RUNTIME,
                },
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
                "routed": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": ["pane"],
                    "runtime": LOCAL_PTY_RUNTIME,
                },
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


class OneRegistryReadingTests(MechanicalRoleBackendTestCase):
    """Criteria 1, 2 and 3 against the reading itself: a tick opens the registry once.

    A tick asks the registry two things — could this agent land on a supervised head at all, and
    which profile did this launch resolve to — and it used to open the registry four times to do
    it. An ordinary profile publication fits between those readings, so the cheap answer could say
    `orca-legacy` while the resolution said `local-pty`, and the tick then reaped ghost tabs and
    listed panes on the first answer before handing its head to a supervisor on the second. The
    repair is not another guard in front of another verb: it is the one reading. Both answers come
    out of it, so the tick belongs to one backend from its first verb to its last.
    """

    def _published_between_the_old_readings(self) -> list[pipeline_heads.Registry]:
        """The registry a publication lands in the middle of, as the old four readings saw it.

        The first two readings answer `orca-legacy` for this agent's profile and the last two
        answer `local-pty` for the very same one — the ordinary `secretary upgrade` against a
        scheduled tick. A tick that reads once never reaches the third entry at all; a tick that
        read four times acted on both.
        """
        before = self._registry(runtime=ORCA_LEGACY_RUNTIME)
        after = self._registry(runtime=LOCAL_PTY_RUNTIME)
        return [before, before, after, after]

    def test_a_pane_tick_opens_the_registry_once(self) -> None:
        """Criterion 2, at the cost the pre-scan used to add: a pane tick pays for one reading."""
        host = RecordingSessionHost()

        with mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)):
            self.assertEqual(self.run_tick(self._registry(), host=host), 0)

        self.assertEqual(self.reads, 1, "the pane tick opened the head registry more than once")
        self.assertEqual(len(host.opened), 1)

    def test_a_supervised_tick_opens_the_registry_once(self) -> None:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.reads, 1, "the supervised tick opened the head registry more than once")
        self.assertEqual(len(self.run_dirs()), 1)

    def test_a_supervised_tick_makes_no_orca_call_at_all(self) -> None:
        """Criterion 3 with nothing stubbed in between.

        `_reap_ghosts`, `_agent_terminals` and the rest are left exactly as the driver has them;
        what fails the test is the session manager and `orca_rpc.call` themselves, so any route to
        Orca this tick took would be recorded whether or not its caller swallowed the failure.
        """
        self.prompt_after_start = True

        with self._orca_banned() as made:
            self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(made, [], "the supervised tick reached Orca's session store")
        run_dir = self.run_dirs()[0]
        self.await_(
            lambda: "/retro" in self.head_output(run_dir),
            message="the head never reported the skill its own terminal handed it",
        )

    def test_a_publication_mid_tick_is_not_read_by_the_tick_it_lands_in(self) -> None:
        """The one reading is the tick's whole answer, and the tick is a pane tick end to end.

        This is the reviewer's scenario for the previous submission: those same four readings, and
        `_reap_ghosts` left alone. With four readings the tick reaped ghost tabs and listed panes
        on the first answer and then raised a supervised head on the last one — Orca's session
        lifecycle touched for a head a supervisor ends up holding. With one reading there is no
        second answer to act on: this tick is the pane tick its reading described, and the
        publication is the next tick's business.
        """
        host = RecordingSessionHost()
        reaped: list[str] = []

        def reap(method, *args, **kwargs):
            reaped.append(method)
            return {"result": {"snapshots": []}}

        with mock.patch.object(dispatch.orca_rpc, "call", side_effect=reap):
            self.assertEqual(self.run_tick(self._published_between_the_old_readings(), host=host), 0)

        self.assertEqual(
            self.run_dirs(), [], "the tick acted on Orca and then handed its head to a supervisor"
        )
        self.assertEqual(self.reads, 1, "the tick read the registry the publication changed")
        self.assertEqual([title for _ws, title, _cmd in host.opened], [f"triggered-agent:{self.AGENT}"])
        self.assertEqual(self.actions(), ["created"])
        self.assertTrue(reaped, "the pane tick was expected to reap its own ghost tabs")

    def test_a_publication_before_the_tick_is_the_whole_tick(self) -> None:
        """The mirror: the reading the tick takes is the supervised one, so the tick is supervised
        from its first verb, and the readings a four-reading tick would have taken afterwards are
        never taken at all."""
        after = self._registry(runtime=LOCAL_PTY_RUNTIME)
        stale = self._registry(runtime=ORCA_LEGACY_RUNTIME)

        with self._orca_banned() as made:
            self.assertEqual(self.run_tick([after, stale, stale, stale]), 0)

        self.assertEqual(made, [], "the supervised tick reached Orca's session store")
        self.assertEqual(self.reads, 1)
        self.assertEqual(len(self.run_dirs()), 1, "the tick raised no supervised head")
        self.assertEqual(self.actions(), ["supervised-started"])


class DivertedLaunchReportCardTests(MechanicalRoleBackendTestCase):
    """Criterion 2's other half: a tick that dispatches nothing leaves no report card.

    One branch can reach an early exit already holding a command, and so already holding the
    report card the command carries: a launch this tick resolved onto a pane profile after the
    registry routed the agent to a supervised one. Every other skip is reached before any command
    exists, which is what "a busy-skip never creates a card" has always meant; this one closes the
    card it is holding instead, so the two say the same thing.
    """

    def _diverted(self) -> pipeline_heads.Registry:
        return pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {
                "routed": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": ["pane"],
                    "runtime": LOCAL_PTY_RUNTIME,
                },
                "pane": {"resource": "acct", "adapter": "claude", "fallback": []},
            },
            {self.AGENT: "routed"},
        )

    def test_an_active_report_skip_releases_the_card_the_diverted_launch_built(self) -> None:
        self.resolved = "pane"
        released: list[object] = []

        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(
                mock.patch.object(
                    dispatch,
                    "_fresh_steward_report_in_progress",
                    return_value={"reference": "secretary-report"},
                )
            )
            enter(
                mock.patch.object(
                    dispatch,
                    "_release_steward_report",
                    side_effect=lambda state, event, cmd, note, **_kwargs: released.append(cmd),
                )
            )
            self.assertEqual(self.run_tick(self._diverted()), 0)

        self.assertEqual(len(released), 1, "the diverted launch's report card was left open")
        self.assertEqual(self.actions(), ["active-report-skip"])
        self.assertEqual(self.run_dirs(), [], "a skipped tick raised a supervised head")


class SupervisedDeliveryTests(MechanicalRoleBackendTestCase):
    """Criterion 3: the skill crosses the backend's own boundary, and nothing else is touched."""

    def test_the_skill_reaches_the_head_across_the_boundary_and_no_pane_is_read(self) -> None:
        self.prompt_after_start = True
        forbidden = {
            name: mock.patch.object(
                dispatch,
                name,
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
        self.assertEqual(
            (self.head_pid(run_dir), self.supervisor_pid(run_dir)),
            (head, supervisor),
            "the live head was replaced",
        )
        self.assertEqual(
            self.head_output(run_dir).count("/retro"), delivered, "the busy head was sent a second skill"
        )
        self.assertEqual(self.actions(), ["supervised-started", "supervised-busy-skip"])
        self.assertEqual(
            self.events()[-1]["error"],
            "head_already_up",
            "the refusal is the launch identity's, made before anything was spawned",
        )

    def test_a_dead_head_is_an_ordinary_bring_up(self) -> None:
        """Criterion 6: nothing about a finished run fences its role out of the next tick."""
        run_dir = self.raise_one()
        dead = self.head_pid(run_dir)
        _kill(self.supervisor_pid(run_dir))
        _kill(dead, group=True)
        _kill(dead)
        self.await_(
            lambda: head_process_status(str(run_dir / protocol.PID_FILE_NAME)).get("state") == "dead",
            message="the head never actually died",
        )

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.run_dirs(), [run_dir], "the dead run was abandoned rather than reused")
        self.await_(
            lambda: _alive(self.head_pid(run_dir)) and self.head_pid(run_dir) != dead,
            message="the tick raised no head over the dead one",
        )
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

        self.await_(
            lambda: _alive(self.head_pid(run_dir)),
            message="a record from another boot became a permanent refusal to go on duty",
        )
        self.assertEqual(self.actions(), ["supervised-started", "supervised-started"])


class BackendHandoverTests(MechanicalRoleBackendTestCase):
    """One role, one owner of its head, and a change of backend is a tick of its own.

    Publishing a `runtime` for a role whose head is already up is an ordinary `secretary upgrade`
    against a scheduled tick, and it used to leave the old head running while a second one was
    raised on the new backend — a pane beside a supervised head one way, a supervised head beside a
    pane the other. Both directions are here, and each of them asserts the same three things: no
    intermediate state of the role has two live heads, the handover tick dispatches nothing and
    leaves no report card, and the head on the new backend is raised by the tick after it.
    """

    def _pane_tick(self, registry, host):
        with mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)):
            return self.run_tick(registry, host=host)

    def test_a_pane_head_is_handed_to_a_supervisor_before_one_is_raised(self) -> None:
        """`orca-legacy -> local-pty`, the direction the live installation's steward is in."""
        host = RecordingSessionHost()
        self.assertEqual(self._pane_tick(self._registry(), host=host), 0)
        self.assertEqual(len(host.opened), 1, "the first tick raised no pane")
        self.assertIsNotNone(self.state.load_terminal_handle())

        # The profile of this same role is republished onto the supervisor, between ticks.
        self.assertEqual(self._pane_tick(self._registry(runtime=LOCAL_PTY_RUNTIME), host=host), 0)

        self.assertEqual(
            self.run_dirs(), [], "the handover tick raised a supervised head beside the live pane"
        )
        self.assertEqual(len(host.opened), 1, "the handover tick opened a second pane")
        self.assertEqual(
            host.stopped,
            [str(self.workspace)],
            "the pane that held this role's head was not closed on the pane's path",
        )
        self.assertIsNone(
            self.state.load_terminal_handle(), "the pane is still recorded as the owner of this role's head"
        )
        self.assertIsNone(
            self.state.load_head_run(), "the handover tick wrote a supervised owner it never raised"
        )
        self.assertIsNone(self.state.load_active_report(), "the handover tick left a report card behind")
        self.assertEqual(self.actions(), ["created", "handover-to-supervised"])

        # The next tick, with no live head left anywhere, raises one on the new backend.
        with self._orca_banned() as made:
            self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(made, [], "the bring-up after the handover reached Orca")
        self.assertEqual(len(self.run_dirs()), 1, "the tick after the handover raised no head")
        self.assertTrue(_alive(self.head_pid(self.run_dirs()[0])))
        self.assertEqual(self.actions()[-1], "supervised-started")

    def test_a_supervised_head_is_handed_back_to_a_pane_before_one_is_opened(self) -> None:
        """`local-pty -> orca-legacy`, closed across the boundary that raised the head."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dir = self.run_dirs()[0]
        head, supervisor = self.head_pid(run_dir), self.supervisor_pid(run_dir)
        self.assertTrue(_alive(head), "the first tick raised no live head")

        host = RecordingSessionHost()
        self.assertEqual(self._pane_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), host=host), 0)

        self.assertEqual(host.opened, [], "the handover tick opened a pane beside a live supervised head")
        self.await_(
            lambda: not _alive(head),
            message="the supervised head outlived the tick that handed the role back",
        )
        self.await_(lambda: not _alive(supervisor), soft=True)
        self.assertIsNone(self.state.load_head_run(), "the supervised head is still recorded as the owner")
        self.assertIsNone(
            self.state.load_terminal_handle(), "the handover tick recorded a pane it never opened"
        )
        self.assertIsNone(self.state.load_active_report(), "the handover tick left a report card behind")
        self.assertEqual(self.actions(), ["supervised-started", "handover-to-pane"])

        # The next tick, with no live head left anywhere, opens the pane.
        self.assertEqual(self._pane_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), host=host), 0)

        self.assertEqual([title for _ws, title, _cmd in host.opened], [f"triggered-agent:{self.AGENT}"])
        self.assertEqual(
            self.run_dirs(), [run_dir], "the tick after the handover raised another supervised head"
        )
        self.assertEqual(self.actions()[-1], "created")

    def test_a_pane_that_will_not_confirm_it_stopped_raises_nothing(self) -> None:
        """Criterion 5 of the contract, on the side this driver cannot ask the boundary about."""
        host = RecordingSessionHost()
        self.assertEqual(self._pane_tick(self._registry(), host=host), 0)

        with mock.patch.object(dispatch, "_stop_and_confirm", return_value=False):
            self.assertEqual(self._pane_tick(self._registry(runtime=LOCAL_PTY_RUNTIME), host=host), 0)

        self.assertEqual(
            self.run_dirs(), [], "a pane that would not confirm its stop still got a second head"
        )
        self.assertIsNotNone(
            self.state.load_terminal_handle(), "the owner was forgotten without its stop being confirmed"
        )
        self.assertEqual(self.actions(), ["created", "handover-stop-failed"])

    def test_a_supervised_head_that_has_already_ended_is_not_a_handover(self) -> None:
        """A record whose head is provably gone names no owner, so the pane tick runs at once."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dir = self.run_dirs()[0]
        dead = self.head_pid(run_dir)
        _kill(self.supervisor_pid(run_dir))
        _kill(dead, group=True)
        _kill(dead)
        self.await_(
            lambda: head_process_status(str(run_dir / protocol.PID_FILE_NAME)).get("state") == "dead",
            message="the head never actually died",
        )

        host = RecordingSessionHost()
        self.assertEqual(self._pane_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), host=host), 0)

        self.assertEqual(len(host.opened), 1, "the pane tick was skipped for a head that had ended")
        self.assertIsNone(self.state.load_head_run())
        self.assertEqual(self.actions(), ["supervised-started", "handover-owner-gone", "created"])


class StewardBoard:
    """The pipeline board a steward tick writes to, as much of it as a tick can tell apart.

    A report card is created In progress and moved out of it, and the whole question these tests
    ask is which cards exist and which column they end in — so this is the board reduced to
    exactly that, with no transport under it.
    """

    def __init__(self) -> None:
        self.cards: list[dict] = []
        self.moves: list[tuple[str, str, str]] = []

    def create_report(self, *, project: str, title: str, slug: str) -> str:
        card = {
            "reference": f"secretary-report-{len(self.cards) + 1}",
            "column": "In progress",
            "steward_report": "1",
            "date_moved": time.time(),
            "title": title,
            "slug": slug,
            "project": project,
        }
        self.cards.append(card)
        return str(card["reference"])

    def in_progress_reports(self, *, project: str) -> list[dict]:
        return [card for card in self.cards if card["column"] == "In progress" and card["project"] == project]

    def move_report(self, *, reference: str, target: str, reason: str) -> None:
        column = {"done": "Done", "blocked": "Blocked"}[target]
        for card in self.cards:
            if card["reference"] == reference:
                card["column"] = column
                card["date_moved"] = time.time()
                self.moves.append(("steward", reference, column))
                return
        raise AssertionError(f"a tick moved a card that was never created: {reference}")

    def in_progress(self) -> list[str]:
        return [card["reference"] for card in self.cards if card["column"] == "In progress"]


class StewardBackendHandoverTests(MechanicalRoleBackendTestCase):
    """The handover of the one mechanical role that reports on itself.

    `retro` has no reporting contract, so the handover tests above can say nothing about it: a
    steward dispatch creates a report card as it renders the skill naming it, hands it to the head
    it launches, and records in `active_report.json` which head is writing which card. A handover
    stops that head. Everything below is about what that owes the card.

    Two things, and this is the case that used to get both wrong. The card the stopped head was
    writing is nobody else's to finish, so it is closed as part of stopping its writer — a card
    left In progress under a head this driver has just ended is a sweep later steward reporting
    reads as still under way, and the record naming its writer is about to be forgotten, so no
    later tick could even tell. And the handover creates no card of its own: it is decided before
    a command is built, so the tick that dispatches nothing never files a report to discover it.
    """

    AGENT = "steward"

    def setUp(self) -> None:
        super().setUp()
        self.board = StewardBoard()

    @contextlib.contextmanager
    def _tick(self, registry, *, host):
        with (
            super()._tick(registry, host=host) as running,
            mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/steward"}),
        ):
            yield running

    def _pane_tick(self, registry, host):
        with mock.patch.object(dispatch, "_reap_ghosts", return_value=(0, True)):
            return self.run_tick(registry, host=host)

    def _standing_report(self) -> str:
        """The card the head this role currently has is writing, as the record names it."""
        record = self.state.load_active_report() or {}
        reference = record.get("reference")
        self.assertTrue(reference, "the tick that raised this role's head recorded no report card")
        self.assertEqual(self.board.in_progress(), [reference])
        return reference

    def _assert_the_handover_owed_nothing(self, standing: str) -> None:
        """What a handover tick leaves behind, in both directions and for both cards."""
        self.assertEqual(len(self.board.cards), 1, "the handover tick created a report card of its own")
        self.assertEqual(
            self.board.in_progress(), [], "the card the stopped head was writing was left In progress"
        )
        self.assertIn(
            (self.AGENT, standing, "Done"),
            self.board.moves,
            "the card of the head this tick stopped was never closed",
        )
        self.assertIsNone(
            self.state.load_active_report(), "a record of a head that no longer exists was left standing"
        )

    def test_a_pane_head_hands_its_report_over_with_the_role(self) -> None:
        """`orca-legacy -> local-pty`, the direction the live installation's steward is in."""
        host = RecordingSessionHost()
        self.assertEqual(self._pane_tick(self._registry(), host=host), 0)
        standing = self._standing_report()

        self.assertEqual(self._pane_tick(self._registry(runtime=LOCAL_PTY_RUNTIME), host=host), 0)

        self._assert_the_handover_owed_nothing(standing)
        self.assertEqual(len(host.opened), 1, "the handover tick opened a second pane")
        self.assertEqual(host.stopped, [str(self.workspace)])
        self.assertEqual(self.run_dirs(), [], "the handover tick raised a supervised head")
        self.assertIsNone(self.state.load_terminal_handle())
        self.assertEqual(self.actions()[-2:], ["owner-report-release", "handover-to-supervised"])

        # The tick after it raises the head on the new backend, and files exactly one card for it.
        with self._orca_banned() as made:
            self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(made, [], "the bring-up after the handover reached Orca")
        self.assertEqual(len(self.run_dirs()), 1, "the tick after the handover raised no head")
        self.assertTrue(_alive(self.head_pid(self.run_dirs()[0])))
        self.assertEqual(
            len(self.board.cards), 2, "the tick that raised the head filed no card, or filed more than one"
        )
        self.assertEqual(self.board.in_progress(), [self.board.cards[-1]["reference"]])
        self.assertEqual(
            (self.state.load_active_report() or {}).get("reference"),
            self.board.cards[-1]["reference"],
            "the head that was raised is not recorded as writing the new card",
        )

    def test_a_supervised_head_hands_its_report_back_with_the_role(self) -> None:
        """`local-pty -> orca-legacy`, closed across the boundary that raised the head."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dir = self.run_dirs()[0]
        head = self.head_pid(run_dir)
        self.assertTrue(_alive(head), "the first tick raised no live head")
        standing = self._standing_report()

        host = RecordingSessionHost()
        self.assertEqual(self._pane_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), host=host), 0)

        self._assert_the_handover_owed_nothing(standing)
        self.assertEqual(host.opened, [], "the handover tick opened a pane beside a live supervised head")
        self.await_(
            lambda: not _alive(head),
            message="the supervised head outlived the tick that handed the role back",
        )
        self.assertIsNone(self.state.load_head_run())
        self.assertEqual(self.actions()[-2:], ["owner-report-release", "handover-to-pane"])

        # The tick after it opens the pane, and files exactly one card for it.
        self.assertEqual(self._pane_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), host=host), 0)

        self.assertEqual([title for _ws, title, _cmd in host.opened], [f"triggered-agent:{self.AGENT}"])
        self.assertEqual(
            self.run_dirs(), [run_dir], "the tick after the handover raised another supervised head"
        )
        self.assertEqual(
            len(self.board.cards), 2, "the tick that opened the pane filed no card, or filed more than one"
        )
        self.assertEqual(self.board.in_progress(), [self.board.cards[-1]["reference"]])
        self.assertEqual(
            (self.state.load_active_report() or {}).get("reference"),
            self.board.cards[-1]["reference"],
            "the head that was opened is not recorded as writing the new card",
        )

    def test_a_pane_that_will_not_confirm_it_stopped_keeps_its_head_and_its_report(self) -> None:
        """Fail-closed is fail-closed for the card too: the writer is still up, so it still owns
        the report, and the record still says which head has it."""
        host = RecordingSessionHost()
        self.assertEqual(self._pane_tick(self._registry(), host=host), 0)
        standing = self._standing_report()

        with mock.patch.object(dispatch, "_stop_and_confirm", return_value=False):
            self.assertEqual(self._pane_tick(self._registry(runtime=LOCAL_PTY_RUNTIME), host=host), 0)

        self.assertEqual(len(self.board.cards), 1, "the refused handover created a report card")
        self.assertEqual(
            self.board.in_progress(), [standing], "the card of a head that is still up was closed"
        )
        self.assertEqual((self.state.load_active_report() or {}).get("reference"), standing)
        self.assertIsNotNone(self.state.load_terminal_handle())
        self.assertEqual(self.run_dirs(), [])
        self.assertEqual(self.actions(), ["created", "handover-stop-failed"])

    def test_a_working_supervised_head_keeps_the_report_it_is_writing(self) -> None:
        """The busy-skip is the other tick that dispatches nothing, and it is not a handover: the
        head that is up is the one writing the standing card, so that card is untouched and the
        card this tick made is the one that is closed."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        standing = self._standing_report()

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(len(self.board.cards), 2, "the busy tick filed no card of its own")
        self.assertEqual(
            self.board.in_progress(), [standing], "the busy head's own report was closed under it"
        )
        self.assertIn((self.AGENT, self.board.cards[-1]["reference"], "Done"), self.board.moves)
        self.assertEqual(
            (self.state.load_active_report() or {}).get("reference"),
            standing,
            "the record stopped naming the card the live head is writing",
        )
        self.assertEqual(self.actions()[-1], "supervised-busy-skip")


if __name__ == "__main__":
    unittest.main()
