"""secretary-1698: a head the dispatcher raises on `local-pty` runs its command, under one heartbeat.

The dispatcher wrapped a head's command in `with_pid_heartbeat` and handed it to a backend whose
supervisor wraps what it is handed a second time. The shell text then read `writer; exec env writer;
exec env head`: the second statement `exec`ed the inner writer, which exited 0, and the head never
ran (issue:c91642ca362fd25bf433, head-run 0dbbaa7eb1524e5fa32630774f6860b8: `head_exited` after 140 ms).

So these tests go through the dispatcher's own launch code — `prepare_observer` for the observer,
`_launch` for a worker and a reviewer — onto a real supervisor, a real pty and a real process. The
head is a harmless stand-in that records its pid and argv and stays up. What is asserted is what a
tick later depends on: the journal says the head command it was given is the whole head command,
that head is the process running, and the pid file the dispatcher reads classifies it as a live match
for the identity the dispatcher expects, with the observer's task spelled `sprint:<ID>` once.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.dispatch.heartbeat import heartbeat_identity, sprint_task
from secretary.dispatch.host import CommandHostRuntime
from secretary.dispatch.observer import ObserverRecord, observer_head_is_dead, observer_head_status
from secretary.dispatch.watchdog import (
    HEARTBEAT_IDENTITY_MISMATCH,
    HEARTBEAT_LIVE_MATCH,
    head_process_status,
    head_run_process_status,
)
from secretary.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from secretary.runtime.head.command import with_pid_heartbeat
from secretary.runtime.head.identity import publish_heartbeat
from secretary.runtime.head.local_pty import protocol
from secretary.runtime.head.local_pty.journal import RUN_STARTED, read_events
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from tests.fakes.dispatcher import FakeCatalog

PROFILE = "claude-local-pty"
#: The stand-in head: it writes down which process it is and what it was run with, then stays up.
STAND_IN = """import json, os, sys, time
with open(sys.argv[1] + '.tmp', 'w', encoding='utf-8') as handle:
    json.dump({'pid': os.getpid(), 'argv': sys.argv[1:]}, handle)
os.replace(sys.argv[1] + '.tmp', sys.argv[1])
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    time.sleep(0.2)
"""
#: One heartbeat writer is one of these in the shell text a head's `/bin/sh -c` runs.
WRITER_MARK = "path, pid, identity = sys.argv[1:]"


class _StandInCatalog(FakeCatalog):
    """A `local-pty` Claude profile whose rendered head command is the stand-in."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.profiles[PROFILE] = {
            "adapter": "claude",
            "model": "opus",
            "resource": "claude-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }
        self.command = command

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> HeadCommand:
        return HeadCommand(self.command, adapter="claude")

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str) -> None:
        return None


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :].split()[:1] != ["Z"]


def _kill(pid: int, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGKILL) if group else os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class LocalPtyDispatcherLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        # /tmp rather than the workspace: the run directory holds a Unix socket, whose address the
        # kernel bounds at about a hundred bytes.
        self.root = Path(tempfile.mkdtemp(prefix="lp-dispatch-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._reap)
        self.record = self.root / "stand-in.json"
        self.command = (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(STAND_IN)} {shlex.quote(str(self.record))}"
        )
        environment = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
                "SECRETARY_CLAUDE_PROJECTS": str(self.root / "claude-projects"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.host = CommandHostRuntime(_StandInCatalog(self.command), self.root / "data", mode="real")  # type: ignore[arg-type]

    # -- processes -----------------------------------------------------------------------------

    def _run_dirs(self) -> list[Path]:
        heads = self.root / "data" / "heads"
        return sorted(path for path in heads.glob("*") if path.is_dir()) if heads.exists() else []

    def _started(self, run_id: str) -> dict[str, Any]:
        events = read_events(self.root / "data" / "heads" / run_id / protocol.JOURNAL_NAME).events
        started = [event for event in events if event.get("kind") == RUN_STARTED]
        self.assertEqual(len(started), 1, "one bring-up is one run.started")
        return started[0]

    def _reap(self) -> None:
        for run_dir in self._run_dirs():
            for event in read_events(run_dir / protocol.JOURNAL_NAME).events:
                if event.get("kind") == RUN_STARTED:
                    _kill(int(event.get("head_pid") or 0), group=True)
                    _kill(int(event.get("head_pid") or 0))
                    _kill(int(event.get("supervisor_pid") or 0))

    def _stand_in(self) -> dict[str, Any]:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                return json.loads(self.record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.02)
        self.fail("the head command never ran: the stand-in wrote no record of itself")

    # -- what every launch path owes -----------------------------------------------------------

    def _assert_the_head_command_runs(self, run: HeadRun, pid_file: str) -> int:
        started = self._started(run.run_id)
        self.assertEqual(
            started["command"],
            self.command,
            "the runtime was handed something other than the head command",
        )
        # The supervisor adds the one writer to what it was handed, so this is the shell text.
        self.assertEqual(with_pid_heartbeat(started["command"], pid_file).count(WRITER_MARK), 1)
        stand_in = self._stand_in()
        self.assertEqual(stand_in["argv"], [str(self.record)], "the process running is not the head")
        self.assertEqual(stand_in["pid"], started["head_pid"], "the heartbeat's `$$` is not the head")
        # The failure this card fixes ended the head within 140 ms; this one is still up after that.
        time.sleep(0.5)
        self.assertTrue(_alive(stand_in["pid"]), "the head exited after its bring-up")
        self.assertFalse(
            (self.root / "data" / "heads" / run.run_id / protocol.PID_FILE_NAME).exists(),
            "a second heartbeat record was written beside the one the dispatcher reads",
        )
        return int(stand_in["pid"])

    # -- the observer ----------------------------------------------------------------------------

    def test_an_observer_raised_on_local_pty_runs_its_head_and_reads_as_a_live_match(self) -> None:
        workspace = self.root / "workspaces" / "observer"
        workspace.mkdir(parents=True)
        with mock.patch.object(CommandHostRuntime, "_create_observer_workspace", return_value=workspace):
            launched = self.host.prepare_observer({"ref": "sprint:1459"}, PROFILE, prompt="# Sprint\n")

        run = HeadRun.from_json(launched["head_run"])
        self.assertEqual(run.spec.runtime, LOCAL_PTY_RUNTIME)
        self.assertEqual(launched["pid_file"], self.host.observer_pid_file("sprint:1459"))
        head = self._assert_the_head_command_runs(run, launched["pid_file"])

        record = ObserverRecord(
            sprint="sprint:1459",
            workspace=str(workspace),
            leaf=str(launched["leaf"]),
            pid_file=str(launched["pid_file"]),
            head_run=dict(launched["head_run"]),
        )
        status = observer_head_status(record)
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
        self.assertEqual(status["pid"], head)
        self.assertEqual(status["record"]["task"], "sprint:1459", "the observer's task is prefixed twice")
        self.assertEqual(status["record"]["run_id"], run.run_id)
        self.assertEqual(status["record"]["leaf"], run.leaf)
        handoff = json.loads(Path(f"{launched['pid_file']}.leaf").read_text(encoding="utf-8"))
        self.assertEqual(handoff["expected"]["task"], "sprint:1459")

    # -- a worker and a reviewer -------------------------------------------------------------------

    def _launch_card_head(self, role: str) -> tuple[Any, str]:
        workspace = self.root / "workspaces" / role
        workspace.mkdir(parents=True)
        (workspace / "TASK.md").write_text("# Task\n", encoding="utf-8")
        with (
            mock.patch.object(CommandHostRuntime, "_require_production_runtime"),
            mock.patch.object(CommandHostRuntime, "_require_workspace_environment"),
        ):
            launched = self.host._launch(
                str(workspace),
                f"secretary-9001 {role}",
                PROFILE,
                str(workspace / "TASK.md"),
                role=role,
                env_name="SECRETARY_1698_NO_COMMAND_OVERRIDE",
                task={"ref": "secretary-9001"},
            )
        kind = "review" if role == "reviewer" else "worker"
        return launched, str(Path(os.environ["SECRETARY_DISPATCHER_BODY_DIR"]) / f"secretary-{kind}-pid-")

    def test_a_worker_and_a_reviewer_raised_on_local_pty_run_their_heads(self) -> None:
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                self.record.unlink(missing_ok=True)
                launched, prefix = self._launch_card_head(role)
                run = HeadRun.from_json(launched.head_run)
                self.assertEqual(run.spec.runtime, LOCAL_PTY_RUNTIME)
                self.assertTrue(run.pid_file.startswith(prefix), run.pid_file)
                head = self._assert_the_head_command_runs(run, run.pid_file)

                status = head_run_process_status(
                    run.pid_file,
                    run=launched.head_run,
                    role=role,
                    leaf=launched.leaf,
                )
                self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
                self.assertEqual(status["pid"], head)
                self.assertEqual(status["record"]["task"], "card:secretary-9001")
                self.assertEqual(status["record"]["role"], role)
                self._reap()


class ObserverTaskIdentityTests(unittest.TestCase):
    """AC3: an observer's `task` is `sprint:<ID>` once, and a pre-1698 record is still recognised."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lp-identity-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run = HeadRun(
            run_id="observer-run-1459",
            spec=HeadSpec(profile_id="claude-opus-high", adapter="claude"),
            workspace=str(self.root / "observer"),
            task_ref=TaskRef.sprint("sprint:1459"),
            role="observer",
            leaf="leaf-1459",
            pid_file=str(self.root / "observer.pid"),
        )

    def _record(self) -> ObserverRecord:
        return ObserverRecord(
            sprint="sprint:1459",
            leaf=self.run.leaf,
            pid_file=self.run.pid_file,
            head_run=self.run.to_json(),
        )

    def test_the_observer_binding_names_the_sprint_once_wherever_it_is_built(self) -> None:
        expected = "sprint:1459"
        self.assertEqual(sprint_task("sprint:1459"), expected)
        self.assertEqual(sprint_task("1459"), expected)
        identity = heartbeat_identity(run_id="r", role="observer", task_ref=self.run.task_ref.to_json())
        self.assertEqual(identity["task"], expected, "the heartbeat record's task")
        self.assertEqual(
            heartbeat_identity(run_id="r", role="observer", task=sprint_task("sprint:1459"))["task"],
            expected,
            "the fallback the stop paths and observer_head_status hand over",
        )
        self.assertEqual(
            heartbeat_identity(run_id="r", role="worker", task_ref=TaskRef.card("secretary-1698").to_json())[
                "task"
            ],
            "card:secretary-1698",
        )

    def test_a_new_observer_record_is_a_live_match_with_the_single_prefix(self) -> None:
        publish_heartbeat(
            self.run.pid_file,
            {"run_id": self.run.run_id, "role": "observer", "task": "sprint:1459", "leaf": self.run.leaf},
        )
        status = observer_head_status(self._record())
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)

    def test_a_running_orca_observer_written_before_the_fix_stays_alive(self) -> None:
        """The record sprint:1459's own Orca observer carries: `sprint:sprint:1459`.

        Read as foreign it would be an identity mismatch, and read as dead it would be replaced
        mid-sprint by the upgrade. It is the same run id, so it is the same launch.
        """
        publish_heartbeat(
            self.run.pid_file,
            {
                "run_id": self.run.run_id,
                "role": "observer",
                "task": "sprint:sprint:1459",
                "leaf": self.run.leaf,
            },
        )
        status = observer_head_status(self._record())
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
        self.assertFalse(observer_head_is_dead(status))

    def test_a_legacy_spelling_does_not_excuse_another_run(self) -> None:
        publish_heartbeat(
            self.run.pid_file,
            {
                "run_id": "somebody-else",
                "role": "observer",
                "task": "sprint:sprint:1459",
                "leaf": self.run.leaf,
            },
        )
        self.assertEqual(observer_head_status(self._record())["state"], HEARTBEAT_IDENTITY_MISMATCH)

    def test_a_local_pty_head_written_before_the_fix_keeps_its_bare_reference(self) -> None:
        pid_file = str(self.root / "steward.pid")
        publish_heartbeat(pid_file, {"run_id": "steward-run", "role": "steward", "task": "steward"})
        expected = heartbeat_identity(
            run_id="steward-run", role="steward", task_ref=TaskRef.standing("steward").to_json()
        )
        self.assertEqual(expected["task"], "standing:steward")
        self.assertEqual(head_process_status(pid_file, expected=expected)["state"], HEARTBEAT_LIVE_MATCH)

    def test_the_stop_paths_expect_the_single_prefix(self) -> None:
        host = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        record = self._record()
        record.workspace = str(self.root / "observer")
        seen: list[str] = []

        def remember(*_args: object, **kwargs: object) -> None:
            seen.append(str(kwargs.get("task")))

        with (
            mock.patch.object(CommandHostRuntime, "_guard_head_run", side_effect=remember),
            mock.patch.object(CommandHostRuntime, "_stop_observer_terminals", side_effect=remember),
            mock.patch.object(CommandHostRuntime, "_confirm_head_process_gone", side_effect=remember),
            mock.patch.object(CommandHostRuntime, "_observer_workspace_registered", return_value=True),
            mock.patch.object(CommandHostRuntime, "_run_json", return_value={}),
        ):
            host._stop_observer_head(record)

        self.assertEqual(seen, ["sprint:1459"] * 3)


if __name__ == "__main__":
    unittest.main()
