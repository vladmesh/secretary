"""secretary-1459: telling "the pane is not drawn" apart from "there is no head".

Every case here is the shape measured on the live installation on 2026-08-24 for card
secretary-1450 (`issue:84c0ae4f796f994a7c1d`): one worktree, three ptys, and the working head
sitting behind the one Orca lists as connected with `paneRuntimeId: -1`. The operator who read
that workspace as "the worker never started" is the reader these assertions are written for.
"""

from __future__ import annotations

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
    PANE_UNAVAILABLE,
    PANE_VISIBLE,
    head_status,
)
from secretary.dispatch.head_vitality_episode import VitalityEpisode, VitalityVerdict
from secretary.dispatcher_heartbeat import run_heartbeat_identity
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_watchdog import pid_file_path

# The measured inventory, verbatim in shape: a bare shell that owns the only runtime pane, a
# dropped shell, and the worker's own pty -- listed, connected, and drawn by nothing.
MEASURED_TERMINALS = [
    {"handle": "term-105", "leafId": "leaf-105", "title": "bash",
     "connected": True, "paneRuntimeId": 1},
    {"handle": "term-107", "leafId": "leaf-107", "title": "bash",
     "connected": False, "paneRuntimeId": -1},
    {"handle": "term-106", "leafId": "leaf-106", "title": "codex",
     "connected": True, "paneRuntimeId": -1},
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
            return subprocess.CompletedProcess(
                list(argv), 0,
                stdout=json.dumps({"ok": True, "result": {"terminals": self.terminals}}),
            )
        if operation == "wait":
            # The head is mid-turn, which is what the measured pane read as.
            return subprocess.CompletedProcess(
                list(argv), 0, stdout=json.dumps({"result": {"wait": {"satisfied": False}}}),
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
            record.worker_head_run, role="worker", task=f"card:{self.ref}",
            leaf=record.worker_leaf,
        )
        if alive:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            identity["proc_starttime_ticks"] = stat[stat.rfind(")") + 2:].split()[19]
            identity["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()
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
        """The measured shape: `orca terminal list` shows the pty, nothing draws it, head works."""
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
        self.assertEqual(head["pane"]["runtime_pane_id"], -1)
        self.assertTrue(head["pane"]["connected"])
        self.assertIn("ALIVE", head["summary"])
        self.assertIn("NOT visible", head["summary"])

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

    def test_a_pane_with_a_runtime_pane_is_reported_visible(self) -> None:
        record = self._record(leaf="leaf-105")
        self._write_heartbeat(record, self._live_pid(), alive=True)

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["runtime_pane"], PANE_VISIBLE)
        self.assertEqual(head["pane"]["runtime_pane_id"], 1)
        self.assertEqual(head["head"], HEAD_ALIVE)

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
            run_id="some-older-run", verdict=VitalityVerdict.CONFIRMED_STALL,
        )

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["run_id"], "run-1450")
        self.assertIsNone(head["episode"])
        self.assertIn("names another run", head["episode_note"])

    def test_this_runs_own_episode_is_reported_beside_the_snapshot(self) -> None:
        record = self._record()
        self._write_heartbeat(record, self._live_pid(), alive=True)
        record.worker_vitality_episode = VitalityEpisode(
            run_id="run-1450", verdict=VitalityVerdict.HEALTHY_QUIET, basis=("pid_heartbeat",),
        )

        head = self._answer(record)["heads"][0]

        self.assertEqual(head["episode"]["verdict"], "healthy_quiet")
        self.assertEqual(head["episode"]["run_id"], "run-1450")
        self.assertNotIn("episode_note", head)

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
            worker="worker-2", workspace=str(self.root / "other"), handle="term-999",
            head="codex-high", review_head="codex-high", attempt_id="attempt-2",
            comment_baseline=0, review_baseline=0, claimed_at=0.0,
            worker_head_run={"run_id": "run-other"}, state="in_progress",
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


class PaneRuntimeIdInventoryTests(unittest.TestCase):
    """The inventory field the whole distinction rests on, read from Orca's own answer."""

    def test_the_inventory_carries_the_runtime_pane_orca_named(self) -> None:
        from triggered_agents.runtime.pane_host import OrcaSessionHost

        host = OrcaSessionHost(lambda _args: {"terminals": MEASURED_TERMINALS})

        panes = {pane.leaf: pane for pane in host.panes("/ws")}

        self.assertEqual(panes["leaf-105"].runtime_pane_id, 1)
        self.assertEqual(panes["leaf-106"].runtime_pane_id, -1)
        self.assertTrue(panes["leaf-106"].connected)

    def test_a_session_manager_that_names_no_runtime_pane_leaves_it_unknown(self) -> None:
        """"The host said nothing" and "the host said there is no pane" are different facts."""
        from triggered_agents.runtime.pane_host import OrcaSessionHost

        host = OrcaSessionHost(
            lambda _args: {"terminals": [{"handle": "t", "leafId": "l", "connected": True}]}
        )

        self.assertIsNone(host.panes("/ws")[0].runtime_pane_id)


if __name__ == "__main__":
    unittest.main()
