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

Since secretary-1723 (A20 step 5) the command reads no pane inventory at all: a legacy record is
shown as legacy and read through its pid heartbeat. The Orca pane host the fixtures above were
measured against was deleted in secretary-1725, and its transport tests with it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import _proc
from secretary.dispatch import head_status as head_status_module
from secretary.dispatch.head_status import (
    HEAD_ABSENT,
    HEAD_ALIVE,
    HEAD_UNPROVEN,
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

    # -- a legacy record (secretary-1723: no pane inventory) ---------------------------------

    def test_a_legacy_record_is_shown_as_legacy_with_no_pane_inventory(self) -> None:
        """A20 step 5: the row is a legacy record read through its pid heartbeat, and Orca is not asked."""
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)

        answer = self._answer(record)

        self.assertEqual(answer["status"], "ok")
        self.assertEqual(self.calls, [], "head-status asked the session manager about a legacy record")
        self.assertNotIn("pane_channel", answer)
        self.assertNotIn("runtime_pane_channel", answer)
        head = answer["heads"][0]
        self.assertEqual((head["runtime"], head["legacy_record"]), ("orca-legacy", True))
        self.assertNotIn("runtime_pane", head)
        self.assertNotIn("pane", head)
        self.assertEqual(head["head"], HEAD_ALIVE)
        self.assertEqual(head["proved_by"], "pid_heartbeat")
        self.assertIn("ALIVE", head["summary"])
        self.assertIn("legacy record", head["summary"])
        self.assertIn("no pane inventory is read", head["summary"])

    def test_the_answer_names_what_each_source_said_and_which_could_not_answer(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)

        head = self._answer(record)["heads"][0]

        evidence = {entry["source"]: entry for entry in head["evidence"]}
        self.assertEqual(evidence["pid_heartbeat"]["availability"], "available")
        self.assertEqual(evidence["pid_heartbeat"]["process"], "running")
        # A legacy record's provider cursor is never read, which is not the same as one that failed.
        self.assertEqual(evidence["provider_cursor"]["availability"], "not_observed")
        self.assertNotIn("pane_advisory", evidence)
        self.assertEqual(head["unavailable_sources"], [])
        self.assertNotEqual(head["head"], HEAD_ABSENT)

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

        self.assertNotIn("pane_channel", answer)
        self.assertNotIn("runtime_pane_channel", answer)
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

        self.assertNotIn("at", row["journal"]["tail"][-1], "only a usable time reaches the row")
        self.assertIn("carry no usable time", row["journal"]["reason"])
        self.assertIn(f"last journal record {TURN_STARTED} at no readable time", row["summary"])

    @staticmethod
    def _last_at(raw: str):
        """A journal transform that writes the final record's `at` as the raw JSON text `raw`."""

        def transform(lines):
            last = json.loads(lines[-1])
            last["at"] = "@AT@"
            return "\n".join([*lines[:-1], json.dumps(last).replace('"@AT@"', raw)]) + "\n"

        return transform

    def test_a_journal_time_too_large_for_a_float_degrades_the_journal_instead_of_raising(self) -> None:
        row = self._damage_journal(self._last_at(str(10**400)))

        self.assertNotIn("at", row["journal"]["tail"][-1])
        self.assertIn("carry no usable time", row["journal"]["reason"])
        self.assertIn(f"last journal record {TURN_STARTED} at no readable time", row["summary"])

    def test_no_hostile_journal_time_fails_head_status_or_moves_the_verdict(self) -> None:
        hostile = {
            "huge int": str(10**400),
            "negative": "-1",
            "zero": "0",
            "bool": "true",
            "nan string": '"nan"',
            "infinity": "1e999",
            "list": "[]",
            "object": "{}",
        }
        for name, raw in hostile.items():
            with self.subTest(at=name):
                shutil.rmtree(self.root / "data" / "heads", ignore_errors=True)
                record = self._supervised_record()
                self._write_run_heartbeat(record, "worker", self._live_pid())
                path = self.root / "data" / "heads" / "run-worker-1701" / "journal.jsonl"
                path.write_text(
                    self._last_at(raw)(path.read_text(encoding="utf-8").splitlines()), encoding="utf-8"
                )

                row = self._supervised_answer({self.ref: record})["heads"][0]

                self.assertEqual((row["head"], row["proved_by"]), (HEAD_ALIVE, "pid_heartbeat"))
                self.assertIn(row["journal"]["state"], ("degraded", "unavailable"))
                for event in row["journal"]["tail"]:
                    if "at" in event:
                        self.assertIsInstance(event["at"], float)
                        self.assertTrue(math.isfinite(event["at"]) and event["at"] > 0)
                self.assertTrue(row["summary"])

    def test_a_journal_reader_that_raises_anything_is_an_unavailable_source(self) -> None:
        record = self._supervised_record()
        self._write_run_heartbeat(record, "worker", self._live_pid())
        broken = mock.patch.object(
            head_status_module, "head_run_journal_read", side_effect=RuntimeError("reader broke")
        )

        with broken:
            row = self._supervised_answer({self.ref: record})["heads"][0]

        self.assertEqual(
            row["head"], HEAD_ALIVE, "a journal that cannot be interpreted says nothing about the head"
        )
        self.assertFalse(row["journal"]["answered"])
        self.assertEqual(row["journal"]["state"], "unavailable")
        self.assertEqual(row["journal"]["tail"], [])
        self.assertIn(
            "could not be read or interpreted (RuntimeError: reader broke)", row["journal"]["reason"]
        )
        self.assertIn("journal", row["unavailable_sources"])

    def test_no_supervised_source_that_fails_to_read_or_interpret_fails_head_status(self) -> None:
        run_dir = self.root / "data" / "heads" / "run-worker-1701"

        def write(name: str, content: bytes):
            return lambda record, stack: (run_dir / name).write_bytes(content)

        def garbage_process(**_kwargs):
            return {"known": True, "state": 7, "pid": "not-a-pid", "reason": ["x"]}

        def patched(name: str, **kwargs):
            return lambda record, stack: stack.enter_context(
                mock.patch.object(head_status_module, name, **kwargs)
            )

        # (case, damage, the source that must not answer, the exception its reason names or None)
        cases = [
            ("invalid-UTF-8 lock", write("supervisor.lock", b"\xff\n"), "supervisor_lock", None),
            ("invalid-UTF-8 pid file", write("supervisor.pid", b"\xfe\xff\n"), "supervisor_lock", None),
            ("pid file of 1e999", write("supervisor.pid", b"1e999\n"), "supervisor_lock", None),
            (
                "lease reader raises",
                patched("head_run_supervisor_lease", side_effect=RuntimeError("lease broke")),
                "supervisor_lock",
                "RuntimeError",
            ),
            (
                "supervisor reader raises",
                patched("build_head_runtime", side_effect=RuntimeError("supervisor broke")),
                "supervisor",
                "RuntimeError",
            ),
            (
                "journal reader raises",
                patched("head_run_journal_read", side_effect=RuntimeError("journal broke")),
                "journal",
                "RuntimeError",
            ),
            (
                "heartbeat mapping gets garbage types",
                patched("head_run_process_status", side_effect=garbage_process),
                "pid_heartbeat",
                "TypeError",
            ),
            (
                "invalid-UTF-8 heartbeat file",
                lambda record, stack: Path(record.worker_pid_file).write_bytes(b"\xff{"),
                "pid_heartbeat",
                "UnicodeDecodeError",
            ),
        ]
        for case, damage, source, raised in cases:
            with self.subTest(case=case):
                shutil.rmtree(self.root / "data" / "heads", ignore_errors=True)
                record = self._supervised_record()
                self._write_run_heartbeat(record, "worker", self._live_pid())
                with contextlib.ExitStack() as stack:
                    damage(record, stack)
                    rows = self._supervised_answer({self.ref: record})["heads"]

                self.assertEqual(len(rows), 1)
                row = rows[0]
                key = {"pid_heartbeat": "heartbeat", "supervisor_lock": "lease"}.get(source, source)
                self.assertIn(source, row["unavailable_sources"])
                self.assertFalse(row[key]["answered"])
                self.assertEqual(row[key]["state"], "unavailable")
                if raised:
                    self.assertIn(f"could not be read or interpreted ({raised}: ", row[key]["reason"])
                if source == "pid_heartbeat":
                    self.assertEqual(
                        row["head"], HEAD_UNPROVEN, "a heartbeat that did not answer proves nothing"
                    )
                else:
                    self.assertEqual((row["head"], row["proved_by"]), (HEAD_ALIVE, "pid_heartbeat"))
                self.assertIn("did not answer", row["summary"])

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

        self.assertNotIn("pane_channel", answer)
        rows = {row["role"]: row for row in answer["heads"]}
        self.assertEqual(sorted(rows), ["reviewer", "worker"])
        self.assertEqual(rows["reviewer"]["run_id"], "run-review-1701")
        self.assertEqual(rows["reviewer"]["runtime"], LOCAL_PTY_RUNTIME)
        self.assertEqual(rows["reviewer"]["process"]["state"], HEARTBEAT_LIVE_MATCH)

    def test_a_mixed_record_gets_one_row_of_each_kind_and_no_inventory_read(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        record.review_handle = "review-handle"
        record.review_pid_file = pid_file_path("review", self.ref)
        record.review_head_run = self._supervised_run("review", run_id="run-review-1701")
        record.review_leaf = record.review_head_run["leaf"]
        self._write_run_heartbeat(record, "review", self._live_pid())

        answer = self._supervised_answer({self.ref: record})

        self.assertNotIn("pane_channel", answer)
        rows = {row["role"]: row for row in answer["heads"]}
        self.assertEqual(rows["worker"]["runtime"], "orca-legacy")
        self.assertTrue(rows["worker"]["legacy_record"])
        self.assertIn("orca-legacy run run-1450", rows["worker"]["summary"])
        self.assertNotIn("runtime_pane", rows["worker"])
        self.assertEqual(rows["worker"]["head"], HEAD_ALIVE)
        self.assertEqual(rows["reviewer"]["runtime"], LOCAL_PTY_RUNTIME)
        self.assertEqual(rows["reviewer"]["head"], HEAD_ALIVE)

    def test_a_git_managed_workspace_is_answered_without_orca(self) -> None:
        # Even a row shaped like the legacy case -- a run naming no backend, and a head identity
        # with no durable run -- is answered from its heartbeat rather than from Orca.
        host = SimpleNamespace(mode="real", _is_git_workspace=lambda path: path == str(self.workspace))
        for run_id, verdict in (("run-1450", HEAD_ALIVE), ("", HEAD_UNPROVEN)):
            with self.subTest(run_id=run_id):
                record = self._record(run_id=run_id)
                self._write_heartbeat(record, self._live_pid(), alive=True)

                answer = self._supervised_answer({self.ref: record}, host=host)

                self.assertNotIn("pane_channel", answer)
                row = answer["heads"][0]
                self.assertEqual(row["head"], verdict)
                self.assertTrue(row["legacy_record"])
                self.assertNotIn("runtime_pane", row)
                self.assertIn("no pane inventory is read", row["summary"])


if __name__ == "__main__":
    unittest.main()
