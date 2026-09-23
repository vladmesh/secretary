"""secretary-1459: telling "the pane is not drawn" apart from "there is no head".

Every case here is the shape measured on the live installation: one worktree, three ptys, and the
working head sitting behind the one Orca lists as connected while its renderer draws it nowhere.
The operator who read that workspace as "the worker never started" is the reader these assertions
are written for.

The fixtures are the two answers `orca terminal list` actually gave on 2026-08-25, transcribed:
its terminal entries carry `handle`, `ptyId`, `worktreePath`, `tabId`, `leafId`, `connected`,
`writable` and no `paneRuntimeId` at all, and only `--include-visual-layouts` adds the renderer
tree under `visualLayouts`, whose drawn panes are nodes of `type: "terminal"` keyed by handle and
leaf. The previous round of this card asserted against a `paneRuntimeId` field that this call does
not return, which is why fourteen green tests proved nothing about the live system; these
assertions are written against the request the command really sends and the answer it really gets.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import _proc
from secretary.dispatch.head_status import (
    HEAD_ABSENT,
    HEAD_ALIVE,
    HEAD_UNPROVEN,
    PANE_NO_PANE,
    PANE_NO_RUNTIME_PANE,
    PANE_NOT_CONSULTED,
    PANE_UNAVAILABLE,
    PANE_UNKNOWN,
    PANE_VISIBLE,
    head_status,
)
from secretary.dispatch.head_vitality_episode import VitalityEpisode, VitalityVerdict
from secretary.dispatch.heartbeat import run_heartbeat_identity
from secretary.dispatch.state import DispatcherRecord
from secretary.dispatch.watchdog import HEARTBEAT_LIVE_MATCH, pid_file_path
from secretary.runtime.head import HeadRun, HeadSpec, TaskRef
from secretary.runtime.head.local_pty.journal import INPUT_ACCEPTED, RUN_STARTED, TURN_STARTED, JournalWriter
from secretary.runtime.head_runtimes import LOCAL_PTY_RUNTIME

# The measured inventory, in the shape the live CLI returns it: a bare shell, a dropped shell, and
# the worker's own pty -- all listed, and none of them carrying a word about what is drawn.
MEASURED_TERMINALS = [
    {
        "handle": "term-105",
        "ptyId": "105",
        "leafId": "leaf-105",
        "tabId": "tab-1",
        "title": "bash",
        "connected": True,
        "writable": True,
        "orphaned": False,
    },
    {
        "handle": "term-107",
        "ptyId": "107",
        "leafId": "leaf-107",
        "tabId": "tab-1",
        "title": "bash",
        "connected": False,
        "writable": False,
        "orphaned": False,
    },
    {
        "handle": "term-106",
        "ptyId": "106",
        "leafId": "leaf-106",
        "tabId": "tab-2",
        "title": "codex",
        "connected": True,
        "writable": True,
        "orphaned": False,
    },
]


def measured_layouts(workspace: str, drawn: list[dict] | None = None) -> list[dict]:
    """The renderer tree the same call returns beside those ptys, drawn ptys and all.

    Only the shell is drawn by default -- which is exactly the measured workspace: three ptys in
    the model, one pane in the window, and the working head behind one of the two the renderer
    never names.
    """
    if drawn is None:
        drawn = [{"handle": "term-105", "tabId": "tab-1", "leafId": "leaf-105"}]
    return [
        {
            "worktreeId": f"8b3c0771::{workspace}",
            "worktreePath": workspace,
            "root": {
                "type": "group",
                "groupId": "group-1",
                "activeTabId": "tab-1",
                "tabs": [
                    {
                        "tabId": node.get("tabId", "tab-1"),
                        "title": "secretary-1450 worker",
                        "activeLeafId": node.get("leafId", ""),
                        "panes": dict(node, type="terminal", connected=True, active=True),
                    }
                    for node in drawn
                ],
            },
        }
    ]


class _FakeProductionState:
    def __init__(self, records: dict[str, DispatcherRecord]) -> None:
        self._records = records
        self.saves = 0

    def load(self) -> dict:
        return {"phase": "running"}

    def records(self, _payload: dict) -> dict[str, DispatcherRecord]:
        return dict(self._records)

    def save(self, _payload: dict) -> None:
        self.saves += 1

    def put_records(self, _payload: dict, _records: dict) -> None:
        self.saves += 1


class _FakeRuntime:
    def __init__(self, records: dict[str, DispatcherRecord], *, mode: str = "real") -> None:
        self.production_state = _FakeProductionState(records)
        self.host = SimpleNamespace(mode=mode)


class HeadStatusTests(unittest.TestCase):
    ref = "secretary-1450"

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        previous = os.environ.get("SECRETARY_DISPATCHER_BODY_DIR")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        self.addCleanup(self._restore_body_dir, previous)
        self.terminals = [dict(entry) for entry in MEASURED_TERMINALS]
        self.layouts: list[dict] | None = measured_layouts(str(self.workspace))
        self.layout_flag_supported = True
        self.list_fails = False
        self.calls: list[list[str]] = []

    def _restore_body_dir(self, previous: str | None) -> None:
        if previous is None:
            os.environ.pop("SECRETARY_DISPATCHER_BODY_DIR", None)
        else:
            os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = previous

    # -- the live installation, faked at its transport ------------------------------------

    def _orca(self, argv, *, timeout=None, **_kwargs):
        self.calls.append(list(argv))
        operation = argv[2] if list(argv[:2]) == ["orca", "terminal"] else ""
        if operation == "list":
            if self.list_fails:
                return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="orca is down")
            wants_layouts = "--include-visual-layouts" in argv
            if wants_layouts and not self.layout_flag_supported:
                # An Orca old enough not to know the option refuses the whole call, exactly as its
                # argument parser would.
                return subprocess.CompletedProcess(
                    list(argv),
                    2,
                    stdout="",
                    stderr="unknown option --include-visual-layouts",
                )
            result: dict = {
                "terminals": self.terminals,
                "totalCount": len(self.terminals),
                "truncated": False,
            }
            if wants_layouts and self.layouts is not None:
                result["visualLayouts"] = self.layouts
            return subprocess.CompletedProcess(
                list(argv),
                0,
                stdout=json.dumps({"ok": True, "result": result}),
            )
        if operation == "wait":
            # The head is mid-turn, which is what the measured pane read as.
            return subprocess.CompletedProcess(
                list(argv),
                0,
                stdout=json.dumps({"result": {"wait": {"satisfied": False}}}),
            )
        return subprocess.CompletedProcess(list(argv), 0, stdout="{}")

    def _live_pid(self) -> int:
        process = subprocess.Popen(["sleep", "30"])
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        return process.pid

    def _dead_pid(self) -> int:
        """A pid the kernel has already reaped, for a heartbeat that names a gone process."""
        process = subprocess.Popen(["true"])
        process.wait()
        return process.pid

    def _record(self, *, run_id: str = "run-1450", leaf: str = "leaf-106") -> DispatcherRecord:
        return DispatcherRecord(
            worker="worker-1",
            workspace=str(self.workspace),
            handle="term-106",
            head="codex-high",
            review_head="codex-high",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            claimed_at=0.0,
            worker_leaf=leaf,
            worker_pid_file=pid_file_path("worker", self.ref),
            worker_head_run={"run_id": run_id} if run_id else {},
            state="in_progress",
        )

    def _write_heartbeat(self, record: DispatcherRecord, pid: int, *, alive: bool) -> None:
        identity = run_heartbeat_identity(
            record.worker_head_run,
            role="worker",
            task=f"card:{self.ref}",
            leaf=record.worker_leaf,
        )
        if alive:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            identity["proc_starttime_ticks"] = stat[stat.rfind(")") + 2 :].split()[19]
            identity["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        else:
            # Death is classified before the kernel identity, so this models an exited head
            # without depending on a recycled /proc directory.
            identity["proc_starttime_ticks"] = "0"
            identity["boot_id"] = "dead-process"
        identity.update({"version": 1, "pid": pid})
        Path(record.worker_pid_file).write_text(json.dumps(identity), encoding="utf-8")

    def _answer(self, record: DispatcherRecord, *, mode: str = "real") -> dict:
        runtime = _FakeRuntime({self.ref: record}, mode=mode)
        with mock.patch.object(_proc, "run", self._orca):
            result = head_status(runtime, workspace=str(self.workspace))
        self.assertEqual(runtime.production_state.saves, 0, "head-status must write nothing")
        return result

    # -- the case the card exists for -----------------------------------------------------

    def test_a_head_whose_pty_has_no_runtime_pane_is_reported_alive(self) -> None:
        """The measured shape: the pty is listed, the renderer draws it nowhere, the head works."""
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)

        answer = self._answer(record)

        self.assertEqual(answer["status"], "ok")
        head = answer["heads"][0]
        self.assertEqual(head["head"], HEAD_ALIVE)
        self.assertEqual(head["process"], "running")
        self.assertEqual(head["proved_by"], "pid_heartbeat")
        # The pane axis is answered separately and says the opposite of what the window suggests.
        self.assertEqual(head["runtime_pane"], PANE_NO_RUNTIME_PANE)
        self.assertTrue(head["pane"]["connected"])
        self.assertEqual(head["pane"]["leaf"], "leaf-106")
        self.assertIn("renderer tree", head["pane"]["renderer_reason"])
        self.assertIn("ALIVE", head["summary"])
        self.assertIn("NOT visible", head["summary"])
        # And the evidence really was asked for: the flag is in the request the command sent.
        listings = [call for call in self.calls if call[:3] == ["orca", "terminal", "list"]]
        self.assertTrue(listings)
        self.assertIn("--include-visual-layouts", listings[0])
        # One reading of the workspace serves the whole answer.
        self.assertEqual(len(listings), 1)

    def test_a_renderer_channel_this_build_does_not_support_is_unknown_not_a_denial(self) -> None:
        """The other half of the same lie: an unread tree must not read as an undrawn pane."""
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        self.layout_flag_supported = False

        answer = self._answer(record)

        self.assertEqual(answer["runtime_pane_channel"]["state"], "unavailable")
        self.assertFalse(answer["runtime_pane_channel"]["supported"])
        head = answer["heads"][0]
        self.assertEqual(head["runtime_pane"], PANE_UNKNOWN)
        self.assertNotEqual(head["runtime_pane"], PANE_NO_RUNTIME_PANE)
        # The pty itself was still listed, so the head half is unaffected.
        self.assertEqual(head["head"], HEAD_ALIVE)
        self.assertEqual(head["pane"]["leaf"], "leaf-106")
        self.assertIn("unknown", head["summary"])

    def test_a_renderer_that_names_no_tree_for_this_workspace_is_unknown(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        self.layouts = measured_layouts(str(self.root / "somewhere-else"))

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["runtime_pane"], PANE_UNKNOWN)
        self.assertIn("no layout tree for this workspace", head["pane"]["renderer_reason"])
        self.assertEqual(head["head"], HEAD_ALIVE)

    def test_an_identity_the_tree_cannot_be_compared_by_is_unknown_not_a_denial(self) -> None:
        """A handle the session manager may have aliased proves nothing, so it denies nothing."""
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        # A tree whose drawn panes carry no leaf id at all: the primary key is unusable here.
        self.layouts = measured_layouts(
            str(self.workspace), drawn=[{"handle": "term-105-alias", "tabId": "tab-1"}]
        )

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["runtime_pane"], PANE_UNKNOWN)
        self.assertIn("aliased", head["pane"]["renderer_reason"])
        self.assertEqual(head["head"], HEAD_ALIVE)

    def test_a_workspace_whose_renderer_draws_nothing_still_answers(self) -> None:
        """An empty tree is a real answer about the window: nothing here is drawn."""
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        self.layouts = measured_layouts(str(self.workspace), drawn=[])

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["runtime_pane"], PANE_NO_RUNTIME_PANE)
        self.assertEqual(head["head"], HEAD_ALIVE)

    def test_the_answer_names_what_each_source_said_and_which_could_not_answer(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)

        head = self._answer(record)["heads"][0]

        evidence = {entry["source"]: entry for entry in head["evidence"]}
        self.assertEqual(evidence["pid_heartbeat"]["availability"], "available")
        self.assertEqual(evidence["pid_heartbeat"]["process"], "running")
        # The pane reading answered (the head is mid-turn) and is marked advisory on its face.
        self.assertEqual(evidence["pane_advisory"]["turn"], "active")
        self.assertTrue(evidence["pane_advisory"]["advisory"])
        # The provider journal of a fake run cannot be read, and that is a fact about the channel.
        self.assertEqual(evidence["provider_cursor"]["availability"], "unavailable")
        self.assertIn("provider_cursor", head["unavailable_sources"])
        self.assertNotEqual(head["head"], HEAD_ABSENT)

    def test_an_unreadable_pane_channel_never_reads_as_a_head_that_is_gone(self) -> None:
        """The whole `head_vitality` invariant, made visible: a dark channel is not a death."""
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        self.list_fails = True

        answer = self._answer(record)

        self.assertEqual(answer["pane_channel"]["state"], "unavailable")
        self.assertEqual(answer["runtime_pane_channel"]["state"], "unavailable")
        head = answer["heads"][0]
        self.assertEqual(head["head"], HEAD_ALIVE)
        self.assertEqual(head["proved_by"], "pid_heartbeat")
        self.assertEqual(head["runtime_pane"], PANE_UNAVAILABLE)
        self.assertIn("pid heartbeat alone", head["reason"])
        self.assertIn("none of them is evidence that a head is absent", head["invariant"])

    def test_a_head_that_is_really_gone_is_reported_absent_and_names_its_proof(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._dead_pid(), alive=False)

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["head"], HEAD_ABSENT)
        self.assertEqual(head["process"], "dead")
        self.assertEqual(head["proved_by"], "pid_heartbeat")
        self.assertIn("ABSENT", head["summary"])
        evidence = {entry["source"]: entry for entry in head["evidence"]}
        self.assertEqual(evidence["pid_heartbeat"]["availability"], "available")

    def test_a_pty_the_renderer_tree_draws_is_reported_visible(self) -> None:
        record = self._record(leaf="leaf-105")
        self._write_heartbeat(record, self._live_pid(), alive=True)

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["runtime_pane"], PANE_VISIBLE)
        self.assertIn("leaf", head["pane"]["renderer_reason"])
        self.assertEqual(head["head"], HEAD_ALIVE)
        self.assertIn("visible", head["summary"])

    def test_a_pane_no_inventory_names_is_not_the_same_answer_as_no_runtime_pane(self) -> None:
        """A pty the inventory does not list is its own word, and still not a dead head."""
        record = self._record(leaf="leaf-nowhere")
        record.handle = ""
        self._write_heartbeat(record, self._live_pid(), alive=True)

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["runtime_pane"], PANE_NO_PANE)
        self.assertIsNone(head["pane"])
        self.assertEqual(head["head"], HEAD_ALIVE)

    # -- identity ---------------------------------------------------------------------------

    def test_the_answer_is_bound_to_this_run(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        record.worker_vitality_episode = VitalityEpisode(
            run_id="some-older-run",
            verdict=VitalityVerdict.CONFIRMED_STALL,
        )

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["run_id"], "run-1450")
        self.assertIsNone(head["episode"])
        self.assertIn("names another run", head["episode_note"])

    def test_this_runs_own_episode_is_reported_beside_the_snapshot(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        record.worker_vitality_episode = VitalityEpisode(
            run_id="run-1450",
            verdict=VitalityVerdict.HEALTHY_QUIET,
            basis=("pid_heartbeat",),
        )

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["episode"]["verdict"], "healthy_quiet")
        self.assertEqual(head["episode"]["run_id"], "run-1450")
        self.assertNotIn("episode_note", head)

    def test_a_head_frozen_behind_a_dark_provider_reads_its_whole_recovery_story(self) -> None:
        """secretary-1543: the state `issue:7bff833fef6d9d9b404d` sat in for 65 minutes.

        `healthy_quiet` plus a basis token is not enough for the operator standing in front of
        that card: the row has to say which progress source is dark and for how long, how long
        the head has been quiet, what the last progress was, and when the next rung falls due.
        """
        import time as _time

        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        now = _time.time()
        record.worker_progress_at = now - 3_000.0
        record.worker_waiting_since = now - 3_600.0
        record.worker_vitality_episode = VitalityEpisode(
            run_id="run-1450",
            verdict=VitalityVerdict.HEALTHY_QUIET,
            started_at=now - 1_200.0,
            unavailable_since={"provider_cursor": now - 400.0},
            basis=("alive-no-progress-source@pid_heartbeat", "dark:400s@provider_cursor"),
            reason="provider_cursor known to this episode but not answering for 400s",
            updated_at=now,
        )

        episode = self._answer(record)["heads"][0]["episode"]

        dark = episode["dark_progress_sources"]
        self.assertEqual([entry["source"] for entry in dark], ["provider_cursor"])
        self.assertAlmostEqual(dark[0]["dark_seconds"], 400.0, delta=5.0)
        self.assertAlmostEqual(episode["quiet_seconds"], 1_200.0, delta=5.0)
        self.assertEqual(episode["last_progress"]["at"], 0.0)
        self.assertAlmostEqual(episode["last_progress"]["pane_output_at"], now - 3_000.0, delta=5.0)
        summary = self._answer(record)["heads"][0]["summary"]
        self.assertIn("provider_cursor has been dark for", summary)
        self.assertIn("becomes suspected_stall in", summary)
        deadline = episode["next_recovery_deadline"]
        self.assertEqual(deadline["verdict"], "suspected_stall")
        # The freeze expires later than the plain quiet threshold, so it sets the deadline.
        self.assertAlmostEqual(deadline["at"], now - 400.0 + 600.0, delta=5.0)
        self.assertAlmostEqual(deadline["in_seconds"], 200.0, delta=5.0)

    def test_a_confirmed_stall_row_says_the_recovery_path_owns_it(self) -> None:
        import time as _time

        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        now = _time.time()
        record.worker_vitality_episode = VitalityEpisode(
            run_id="run-1450",
            verdict=VitalityVerdict.CONFIRMED_STALL,
            started_at=now - 4_000.0,
            confirmed_since=now - 3_100.0,
            updated_at=now,
        )

        episode = self._answer(record)["heads"][0]["episode"]

        self.assertIsNone(episode["next_recovery_deadline"])
        self.assertIn("recovery path owns", episode["deadline_note"])
        self.assertEqual(episode["missing_progress_sources"], ["provider_cursor"])

    def test_a_head_identity_without_a_durable_run_proves_nothing_either_way(self) -> None:
        record = self._record(run_id="")
        self._write_heartbeat(record, self._live_pid(), alive=True)

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["head"], HEAD_UNPROVEN)
        self.assertIsNone(head["run_id"])
        self.assertIn("no durable HeadRun", head["reason"])
        self.assertNotEqual(head["head"], HEAD_ABSENT)

    # -- scope ------------------------------------------------------------------------------

    def test_only_the_heads_the_dispatcher_holds_in_this_workspace_are_reported(self) -> None:
        here = self._record()
        self._write_heartbeat(here, self._live_pid(), alive=True)
        elsewhere = DispatcherRecord(
            worker="worker-2",
            workspace=str(self.root / "other"),
            handle="term-999",
            head="codex-high",
            review_head="codex-high",
            attempt_id="attempt-2",
            comment_baseline=0,
            review_baseline=0,
            claimed_at=0.0,
            worker_head_run={"run_id": "run-other"},
            state="in_progress",
        )
        runtime = _FakeRuntime({self.ref: here, "secretary-1": elsewhere})

        with mock.patch.object(_proc, "run", self._orca):
            answer = head_status(runtime, workspace=str(self.workspace))

        self.assertEqual([head["ref"] for head in answer["heads"]], [self.ref])
        self.assertEqual([head["role"] for head in answer["heads"]], ["worker"])

    def test_a_workspace_with_no_head_says_so_rather_than_guessing(self) -> None:
        runtime = _FakeRuntime({})

        with mock.patch.object(_proc, "run", self._orca):
            answer = head_status(runtime, workspace=str(self.workspace))

        self.assertEqual(answer["heads"], [])
        self.assertEqual(answer["summary"], ["the dispatcher holds no head in this workspace"])

    def test_a_noop_host_observes_nothing_and_refuses_to_pretend(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)

        answer = self._answer(record, mode="noop")

        self.assertEqual(answer["status"], "degraded")
        self.assertEqual(answer["heads"], [])
        self.assertIn("noop", answer["reason"])

    # -- a card head a local-pty supervisor holds (secretary-1701) --------------------------

    def _supervised_run(self, kind: str, *, run_id: str) -> dict:
        """A local-pty run as the dispatcher records it, with its run directory on disk."""
        role = "reviewer" if kind == "review" else "worker"
        run = HeadRun(
            run_id=run_id,
            spec=HeadSpec(profile_id="claude-local-pty", adapter="claude", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(self.workspace),
            task_ref=TaskRef.card(self.ref),
            role=role,
            leaf=f"leaf-{run_id}",
            pid_file=pid_file_path(kind, self.ref),
        )
        run_dir = self.root / "data" / "heads" / run_id
        run_dir.mkdir(parents=True)
        journal = JournalWriter(run_dir / "journal.jsonl", run_id).open()
        self.addCleanup(journal.close)
        clock = mock.patch(
            "secretary.runtime.head.local_pty.journal.time.time", side_effect=[100.0, 110.0, 120.0]
        )
        with clock:
            journal.append(RUN_STARTED, head_pid=1)
            journal.append(INPUT_ACCEPTED, bytes=12)
            journal.append(TURN_STARTED, turn=1)
        (run_dir / "supervisor.pid").write_text(f"{os.getpid()}\n")
        return run.to_json()

    def _hold_supervisor_lock(self, run_id: str) -> None:
        """Take the run's supervisor lock as a live supervisor would: an exclusive flock, pid inside."""
        path = self.root / "data" / "heads" / run_id / "supervisor.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f"{os.getpid()}\n".encode())

    def _supervised_record(self) -> DispatcherRecord:
        record = self._record(run_id="")
        record.worker_head_run = self._supervised_run("worker", run_id="run-worker-1701")
        record.worker_leaf = record.worker_head_run["leaf"]
        return record

    def _write_run_heartbeat(self, record: DispatcherRecord, kind: str, pid: int) -> None:
        run = record.review_head_run if kind == "review" else record.worker_head_run
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        identity = run_heartbeat_identity(run, role=kind, task=f"card:{self.ref}", leaf=leaf)
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        identity["proc_starttime_ticks"] = stat[stat.rfind(")") + 2 :].split()[19]
        identity["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        identity.update({"version": 1, "pid": pid})
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        Path(pid_file).write_text(json.dumps(identity), encoding="utf-8")

    def _no_orca(self, argv, *, timeout=None, **_kwargs):
        self.fail(f"head-status called {argv!r} for a workspace no Orca pane describes")

    def _supervised_answer(self, records: dict[str, DispatcherRecord], *, host=None, run=None) -> dict:
        runtime = _FakeRuntime(records)
        runtime.data_dir = self.root / "data"  # type: ignore[attr-defined]
        if host is not None:
            runtime.host = host
        with mock.patch.object(_proc, "run", run or self._no_orca):
            result = head_status(runtime, workspace=str(self.workspace), now=130.0)
        self.assertEqual(runtime.production_state.saves, 0, "head-status must write nothing")
        return result

    def test_a_local_pty_worker_is_read_from_its_supervisor_sources_without_orca(self) -> None:
        record = self._supervised_record()
        pid = self._live_pid()
        self._write_run_heartbeat(record, "worker", pid)
        self._hold_supervisor_lock("run-worker-1701")

        answer = self._supervised_answer({self.ref: record})

        self.assertEqual(answer["pane_channel"]["state"], "not_consulted")
        self.assertIn("local-pty", answer["pane_channel"]["reason"])
        self.assertEqual(answer["runtime_pane_channel"]["state"], "not_consulted")
        self.assertEqual(len(answer["heads"]), 1, answer)
        row = answer["heads"][0]
        self.assertEqual((row["role"], row["kind"], row["runtime"]), ("worker", "worker", LOCAL_PTY_RUNTIME))
        self.assertEqual(row["run_id"], "run-worker-1701")
        self.assertEqual(row["head"], HEAD_ALIVE)
        self.assertEqual(row["proved_by"], "pid_heartbeat")
        self.assertEqual(row["process"], {"state": HEARTBEAT_LIVE_MATCH, "pid": pid})
        self.assertTrue(row["heartbeat"]["answered"])
        self.assertEqual(row["lease"]["state"], "held")
        self.assertEqual(row["lease"]["holder_pid"], os.getpid())
        self.assertEqual(row["lease"]["supervisor_pid"], os.getpid())
        self.assertEqual(
            [event["kind"] for event in row["journal"]["tail"]], [RUN_STARTED, INPUT_ACCEPTED, TURN_STARTED]
        )
        # No socket: the supervisor did not answer, and that is a channel, not the head.
        self.assertFalse(row["supervisor"]["answered"])
        self.assertEqual(row["unavailable_sources"], ["supervisor"])
        self.assertIn("local-pty run run-worker-1701", row["summary"])
        self.assertIn("ALIVE", row["summary"])
        self.assertIn("lock held by pid", row["summary"])
        self.assertIn(f"last journal record {TURN_STARTED} 10s ago", row["summary"])
        self.assertIn("did not answer: supervisor", row["summary"])

    def test_an_unreadable_journal_and_a_silent_supervisor_never_read_as_a_gone_head(self) -> None:
        record = self._supervised_record()
        run_dir = self.root / "data" / "heads" / "run-worker-1701"
        (run_dir / "journal.jsonl").unlink()
        (run_dir / "journal.jsonl").mkdir()

        row = self._supervised_answer({self.ref: record})["heads"][0]

        self.assertEqual(row["head"], HEAD_UNPROVEN)
        self.assertNotEqual(row["head"], HEAD_ABSENT)
        self.assertEqual(row["journal"]["state"], "unavailable")
        self.assertEqual(row["lease"]["state"], "unavailable")
        self.assertEqual(
            sorted(row["unavailable_sources"]),
            ["journal", "pid_heartbeat", "supervisor", "supervisor_lock"],
        )
        self.assertIn("UNPROVEN", row["summary"])
        self.assertIn("not about the head", row["summary"])

    def _damage_journal(self, transform) -> dict:
        """Rewrite the worker's journal through `transform` and answer head-status over it."""
        record = self._supervised_record()
        self._write_run_heartbeat(record, "worker", self._live_pid())
        path = self.root / "data" / "heads" / "run-worker-1701" / "journal.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text(transform(lines), encoding="utf-8")
        row = self._supervised_answer({self.ref: record})["heads"][0]
        self.assertEqual(row["head"], HEAD_ALIVE, "a damaged journal says nothing about the head")
        self.assertTrue(row["journal"]["answered"])
        self.assertEqual(row["journal"]["state"], "degraded")
        self.assertNotIn("journal", row["unavailable_sources"])
        self.assertIn("journal degraded", row["summary"])
        return row

    def test_a_journal_record_with_no_numeric_time_degrades_the_journal_instead_of_raising(self) -> None:
        def bad_time(lines):
            last = json.loads(lines[-1])
            last["at"] = "bad-time"
            return "\n".join([*lines[:-1], json.dumps(last)]) + "\n"

        row = self._damage_journal(bad_time)

        self.assertEqual(row["journal"]["tail"][-1]["at"], "bad-time")
        self.assertIn("carry no usable time", row["journal"]["reason"])
        self.assertIn(f"last journal record {TURN_STARTED} at no readable time", row["summary"])

    def test_a_malformed_middle_journal_line_is_reported_as_skipped(self) -> None:
        row = self._damage_journal(lambda lines: "\n".join([lines[0], "{not json", *lines[1:]]) + "\n")

        self.assertEqual(row["journal"]["malformed"], 1)
        self.assertFalse(row["journal"]["truncated_tail"])
        self.assertIn("1 malformed line(s) skipped", row["journal"]["reason"])
        self.assertEqual(
            [event["kind"] for event in row["journal"]["tail"]], [RUN_STARTED, INPUT_ACCEPTED, TURN_STARTED]
        )

    def test_a_torn_final_journal_line_is_reported_as_torn(self) -> None:
        row = self._damage_journal(lambda lines: "\n".join(lines) + '\n{"seq": 4, "kind": "tu')

        self.assertTrue(row["journal"]["truncated_tail"])
        self.assertEqual(row["journal"]["malformed"], 0)
        self.assertIn("the final line is torn", row["journal"]["reason"])
        self.assertIn(f"last journal record {TURN_STARTED} 10s ago", row["summary"])

    def test_a_lock_no_process_holds_is_about_the_supervisor_not_the_head(self) -> None:
        record = self._supervised_record()
        self._write_run_heartbeat(record, "worker", self._live_pid())
        (self.root / "data" / "heads" / "run-worker-1701" / "supervisor.lock").write_text("4242\n")

        row = self._supervised_answer({self.ref: record})["heads"][0]

        self.assertEqual(row["lease"]["state"], "free")
        self.assertEqual(row["lease"]["written_pid"], 4242)
        self.assertEqual(row["head"], HEAD_ALIVE)

    def test_a_local_pty_reviewer_during_review_gets_its_own_row(self) -> None:
        record = self._supervised_record()
        record.review_handle = "review-handle"
        record.review_pid_file = pid_file_path("review", self.ref)
        record.review_head_run = self._supervised_run("review", run_id="run-review-1701")
        record.review_leaf = record.review_head_run["leaf"]
        record.state = "review"
        self._write_run_heartbeat(record, "worker", self._live_pid())
        self._write_run_heartbeat(record, "review", self._live_pid())

        answer = self._supervised_answer({self.ref: record})

        self.assertEqual(answer["pane_channel"]["state"], "not_consulted")
        rows = {row["role"]: row for row in answer["heads"]}
        self.assertEqual(sorted(rows), ["reviewer", "worker"])
        self.assertEqual(rows["reviewer"]["run_id"], "run-review-1701")
        self.assertEqual(rows["reviewer"]["runtime"], LOCAL_PTY_RUNTIME)
        self.assertEqual(rows["reviewer"]["process"]["state"], HEARTBEAT_LIVE_MATCH)

    def test_a_mixed_record_gets_one_row_of_each_kind_and_one_inventory_read(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        record.review_handle = "review-handle"
        record.review_pid_file = pid_file_path("review", self.ref)
        record.review_head_run = self._supervised_run("review", run_id="run-review-1701")
        record.review_leaf = record.review_head_run["leaf"]
        self._write_run_heartbeat(record, "review", self._live_pid())

        answer = self._supervised_answer({self.ref: record}, run=self._orca)

        self.assertEqual(answer["pane_channel"]["state"], "available")
        rows = {row["role"]: row for row in answer["heads"]}
        self.assertEqual(rows["worker"]["runtime"], "orca-legacy")
        self.assertIn("orca-legacy run run-1450", rows["worker"]["summary"])
        self.assertEqual(rows["worker"]["runtime_pane"], PANE_NO_RUNTIME_PANE)
        self.assertEqual(rows["reviewer"]["runtime"], LOCAL_PTY_RUNTIME)
        self.assertEqual(rows["reviewer"]["head"], HEAD_ALIVE)
        listings = [call for call in self.calls if call[:3] == ["orca", "terminal", "list"]]
        self.assertEqual(len(listings), 1)

    def test_a_git_managed_workspace_is_answered_without_orca(self) -> None:
        # Even a row shaped like the legacy case -- a run naming no backend, and a head identity
        # with no durable run -- is answered from its heartbeat rather than from Orca.
        host = SimpleNamespace(mode="real", _is_git_workspace=lambda path: path == str(self.workspace))
        for run_id, verdict in (("run-1450", HEAD_ALIVE), ("", HEAD_UNPROVEN)):
            with self.subTest(run_id=run_id):
                record = self._record(run_id=run_id)
                self._write_heartbeat(record, self._live_pid(), alive=True)

                answer = self._supervised_answer({self.ref: record}, host=host)

                self.assertEqual(answer["pane_channel"]["state"], "not_consulted")
                self.assertIn("git-managed", answer["pane_channel"]["reason"])
                row = answer["heads"][0]
                self.assertEqual(row["head"], verdict)
                self.assertEqual(row["runtime_pane"], PANE_NOT_CONSULTED)
                self.assertIn("not consulted", row["summary"])


class RuntimePaneInventoryTransportTests(unittest.TestCase):
    """The request the command really sends, and the answer the live backend really gives.

    The transcript above is the whole point of this class. The previous round matched a
    `paneRuntimeId` field on a terminal entry; the call the command makes does not return one, so
    these assertions pin the two things that decide the answer instead: that the renderer tree is
    actually asked for, and that membership in it is read from the tree the backend returns.
    """

    workspace = "/home/dev/orca/workspaces/secretary/ws"

    def _host(self, payloads):
        from secretary.runtime.pane_host import OrcaSessionHost

        calls: list[list[str]] = []

        def run_json(args):
            calls.append(list(args))
            answer = payloads(list(args))
            if isinstance(answer, Exception):
                raise answer
            return answer

        return OrcaSessionHost(run_json), calls

    def test_the_inventory_asks_for_the_visual_layouts_and_reads_the_tree(self) -> None:
        host, calls = self._host(
            lambda _args: {
                "terminals": MEASURED_TERMINALS,
                "visualLayouts": measured_layouts(self.workspace),
                "totalCount": 3,
            }
        )

        inventory = host.workspace_inventory(self.workspace)

        self.assertEqual(
            calls[0],
            [
                "orca",
                "terminal",
                "list",
                "--worktree",
                f"path:{self.workspace}",
                "--include-visual-layouts",
                "--json",
            ],
        )
        self.assertEqual({pane.leaf for pane in inventory.panes}, {"leaf-105", "leaf-106", "leaf-107"})
        self.assertTrue(inventory.layout.supported)
        self.assertTrue(inventory.layout.known_workspace)
        # The measured case, at the transport: one pty drawn, the other two listed and not.
        self.assertEqual(inventory.layout.leaves, frozenset({"leaf-105"}))
        self.assertEqual(inventory.layout.handles, frozenset({"term-105"}))
        self.assertEqual(inventory.layout.terminal_nodes, 1)

    def test_a_build_that_refuses_the_option_keeps_its_ptys_and_says_the_tree_is_unread(
        self,
    ) -> None:
        from secretary.runtime.pane_host import PaneHostError

        def payloads(args):
            if "--include-visual-layouts" in args:
                return PaneHostError("unknown option --include-visual-layouts")
            return {"terminals": MEASURED_TERMINALS}

        host, calls = self._host(payloads)

        inventory = host.workspace_inventory(self.workspace)

        self.assertEqual(len(calls), 2)
        self.assertNotIn("--include-visual-layouts", calls[1])
        self.assertEqual(len(inventory.panes), 3)
        self.assertFalse(inventory.layout.supported)
        self.assertIn("--include-visual-layouts", inventory.layout.reason)

    def test_an_answer_carrying_no_layouts_key_leaves_the_channel_unsupported(self) -> None:
        host, _calls = self._host(lambda _args: {"terminals": MEASURED_TERMINALS})

        layout = host.workspace_inventory(self.workspace).layout

        self.assertFalse(layout.supported)
        self.assertEqual(layout.leaves, frozenset())

    def test_the_delivery_path_never_pays_for_the_renderer_tree(self) -> None:
        """`panes()` is the wait tick's call: which pty to write into, not what is drawn."""
        host, calls = self._host(lambda _args: {"terminals": MEASURED_TERMINALS})

        host.panes(self.workspace)

        self.assertNotIn("--include-visual-layouts", calls[0])

    def test_drawn_panes_are_found_wherever_the_tree_nests_them(self) -> None:
        """Robustness, not a claimed contract: a container this module has never seen must not
        silently turn every pty into an undrawn one."""
        host, _calls = self._host(
            lambda _args: {
                "terminals": MEASURED_TERMINALS,
                "visualLayouts": [
                    {
                        "worktreePath": self.workspace,
                        "root": {
                            "type": "group",
                            "tabs": [
                                {
                                    "tabId": "tab-2",
                                    "panes": {
                                        "type": "group",
                                        "direction": "row",
                                        "children": [
                                            {"type": "terminal", "handle": "term-106", "leafId": "leaf-106"},
                                            {"type": "terminal", "handle": "term-105", "leafId": "leaf-105"},
                                        ],
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        )

        layout = host.workspace_inventory(self.workspace).layout

        self.assertEqual(layout.leaves, frozenset({"leaf-105", "leaf-106"}))
        self.assertEqual(layout.terminal_nodes, 2)

    def test_a_supplementary_runtime_pane_id_is_carried_where_a_host_names_one(self) -> None:
        """Kept as supporting evidence only: this call's real answer has no such field."""
        from secretary.runtime.pane_host import OrcaSessionHost

        host = OrcaSessionHost(
            lambda _args: {
                "terminals": [
                    {"handle": "t", "leafId": "l", "connected": True, "paneRuntimeId": -1},
                    {"handle": "u", "leafId": "m", "connected": True},
                ]
            }
        )

        panes = {pane.leaf: pane for pane in host.panes(self.workspace)}

        self.assertEqual(panes["l"].runtime_pane_id, -1)
        self.assertIsNone(panes["m"].runtime_pane_id)


if __name__ == "__main__":
    unittest.main()
