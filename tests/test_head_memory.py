"""Profile memory limits, scoped launch, and typed head-loss recovery."""

from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.dispatch import review, wait_vitality
from secretary.dispatch.head_vitality_episode import VitalityVerdict
from secretary.runtime.head.local_pty import protocol, scope_bootstrap, scope_launcher
from secretary.runtime.head.local_pty.client import spawn_head
from secretary.runtime.head.local_pty.journal import RUN_EXITED, RUN_STARTED, JournalWriter, read_events
from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from secretary.runtime.head.local_pty.supervisor import Supervisor, SupervisorStartupError
from secretary.runtime.head.memory import (
    DEFAULT_MEMORY_LIMIT_MIB,
    ScopeEvidence,
    head_loss_reason,
    memory_events,
    scope_argv,
    scope_unit,
)
from secretary.runtime.head.spec import HeadSpec, HeadSpecError
from secretary.runtime.local_pty_head import head_run_loss_reason
from secretary.webproto.run_state import _exit_status


class HeadMemoryTests(unittest.TestCase):
    def test_default_and_profile_limits_materialize_as_own_scope_property(self) -> None:
        for role in ("observer", "worker", "review", "po"):
            with self.subTest(role=role):
                default = HeadSpec.from_profile("default", {"adapter": "codex"})
                explicit = HeadSpec.from_profile(
                    "explicit", {"adapter": "codex", "memory_limit_mib": 12288}
                )
                self.assertEqual(default.memory_limit_mib, DEFAULT_MEMORY_LIMIT_MIB)
                self.assertEqual(explicit.memory_limit_mib, 12288)
                run_id = f"{role}-run"
                argv = scope_argv(run_id, explicit.memory_limit_mib, ["/bin/true"])
                self.assertEqual(argv[:6], ["sudo", "-n", "-E", "systemd-run", "--system", "--scope"])
                self.assertIn(f"--property=MemoryMax={12288 * 1024 * 1024}", argv)
                self.assertIn("--property=MemorySwapMax=0", argv)
                self.assertIn(scope_unit(run_id), argv)
                self.assertNotEqual(scope_unit(run_id), scope_unit(f"{role}-other"))

    def test_invalid_profile_limit_is_refused(self) -> None:
        for bad in (0, -1, True, "4096", 1.5):
            with self.subTest(bad=bad), self.assertRaisesRegex(HeadSpecError, "memory_limit_mib"):
                HeadSpec.from_profile("bad", {"adapter": "codex", "memory_limit_mib": bad})

    def test_scope_bootstrap_protects_only_the_supervisor_before_head_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            protection = cgroup / "oom_score_adj"
            scope_bootstrap.install_oom_contract(cgroup, protection)
            self.assertEqual((cgroup / "memory.oom.group").read_text(), "1\n")
            self.assertEqual(protection.read_text(), "-1000\n")
            argv = scope_argv("run", 96, ["/bin/true"])
            self.assertIn("secretary.runtime.head.local_pty.scope_bootstrap", argv)
            self.assertIn("--property=Delegate=yes", argv)
            owned = scope_argv("po-run", 96, ["/bin/true"], owner_unit="secretary-po.service")
            self.assertIn("--property=BindsTo=secretary-po.service", owned)
            self.assertIn("--property=After=secretary-po.service", owned)

    def test_supervisor_refuses_until_its_own_scope_has_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id = "scope-preflight"
            cgroup = Path(temp) / scope_unit(run_id)
            cgroup.mkdir()
            (cgroup / "memory.max").write_text("1048576\n", encoding="ascii")
            (cgroup / "memory.swap.max").write_text("0\n", encoding="ascii")
            (cgroup / "memory.oom.group").write_text("1\n", encoding="ascii")
            (cgroup / "memory.events.local").write_text("max 0\noom_kill 0\noom_group_kill 0\n", encoding="ascii")
            (cgroup / "pids.peak").write_text("1\n", encoding="ascii")
            supervisor = Supervisor(run_dir=Path(temp), run_id=run_id, role="worker",
                                    task="card:1", command="true", memory_limit_mib=1)
            with (mock.patch("secretary.runtime.head.local_pty.scoped_lifecycle.own_cgroup", return_value=cgroup),
                  mock.patch("secretary.runtime.head.local_pty.scoped_lifecycle.supervisor_oom_protected", return_value=True)):
                supervisor._prepare_memory_scope()
                self.assertEqual(supervisor._memory_evidence, ScopeEvidence(cgroup, {"max": 0, "oom_kill": 0, "oom_group_kill": 0}, 1))
                (cgroup / "memory.max").write_text("2097152\n", encoding="ascii")
                with self.assertRaisesRegex(SupervisorStartupError, "MemoryMax=1048576"):
                    supervisor._prepare_memory_scope()
                (cgroup / "memory.max").write_text("1048576\n", encoding="ascii")
                (cgroup / "memory.swap.max").write_text("max\n", encoding="ascii")
                with self.assertRaisesRegex(SupervisorStartupError, "MemorySwapMax=0"):
                    supervisor._prepare_memory_scope()

    def test_synthetic_tiny_limit_kill_persists_typed_reason_for_every_role(self) -> None:
        # A 1 MiB scope's synthetic memory.events.local transition and SIGKILL are the
        # supervisor's two independent witnesses. No production cgroup is touched here.
        for role in ("observer", "worker", "review", "po"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                run_id = f"tiny-{role}"
                self.assertIn("--property=MemoryMax=1048576", scope_argv(run_id, 1, ["/bin/true"]))
                self.assertIn("--property=MemorySwapMax=0", scope_argv(run_id, 1, ["/bin/true"]))
                run_dir = protocol.run_dir_for(temp, run_id)
                run_dir.mkdir(parents=True)
                supervisor = Supervisor(
                    run_dir=run_dir, run_id=run_id, role=role, task="synthetic", command="true",
                    memory_limit_mib=1,
                )
                supervisor._head_pid = 12345
                supervisor._head_status = signal.SIGKILL
                supervisor._memory_evidence = ScopeEvidence(Path(temp), {"max": 0, "oom_kill": 0, "oom_group_kill": 0}, 1)
                (Path(temp) / "memory.events.local").write_text("max 1\noom_kill 1\noom_group_kill 1\n", encoding="ascii")
                (Path(temp) / "pids.peak").write_text("2\n", encoding="ascii")
                supervisor._journal = JournalWriter(run_dir / "journal.jsonl", run_id).open()
                try:
                    with (
                        mock.patch.object(supervisor, "_finish_delivery"),
                        mock.patch.object(supervisor, "_flush_progress"),
                    ):
                        supervisor._finish()
                finally:
                    supervisor._journal.close()
                exit_record = read_events(run_dir / "journal.jsonl").of_kind(RUN_EXITED)[0]
                self.assertEqual(exit_record["head_loss_reason"], "memory_limit")
                self.assertEqual(exit_record["signal"], signal.SIGKILL)
                self.assertEqual(head_run_loss_reason(temp, run_id), "memory_limit")
                self.assertEqual(_exit_status((exit_record,))["head_loss_reason"], "memory_limit")

    def test_other_deaths_are_not_called_memory_exhaustion(self) -> None:
        before = {"max": 0, "oom_kill": 0, "oom_group_kill": 0}
        over = {"max": 1, "oom_kill": 1, "oom_group_kill": 1}
        for number, after in ((None, over), (signal.SIGTERM, over), (signal.SIGKILL, before),
                              (signal.SIGKILL, {"max": 0, "oom_kill": 1}),
                              (signal.SIGKILL, {"max": 1, "oom_kill": 1, "oom_group_kill": 0})):
            self.assertIsNone(head_loss_reason(signal_number=number, before=before, after=after))
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            (cgroup / "memory.events.local").write_text(
                "max 1\noom_kill 1\noom_group_kill 1\n", encoding="ascii"
            )
            self.assertNotIn("head_loss_reason", ScopedHeadLifecycle.exit_fields(
                signal.SIGKILL, ScopeEvidence(cgroup, before), stopping=True,
            ))
        self.assertIsNone(memory_events(None))
        self.assertNotIn("head_loss_reason", _exit_status(({
            "kind": RUN_EXITED, "signal": signal.SIGKILL, "head_loss_reason": "other"
        },)))

    def test_child_oom_then_head_stop_does_not_persist_memory_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id = "child-oom-stop"
            run_dir = protocol.run_dir_for(temp, run_id)
            run_dir.mkdir()
            before = {"max": 0, "oom_kill": 0, "oom_group_kill": 0}
            (run_dir / "memory.events.local").write_text(
                "max 1\noom_kill 1\noom_group_kill 0\n", encoding="ascii"
            )
            supervisor = Supervisor(
                run_dir=run_dir, run_id=run_id, role="worker", task="card:1",
                command="true", memory_limit_mib=1,
            )
            supervisor._head_pid = 12345
            supervisor._head_status = signal.SIGKILL
            supervisor._stopping = True
            supervisor._memory_evidence = ScopeEvidence(run_dir, before)
            supervisor._journal = JournalWriter(run_dir / "journal.jsonl", run_id).open()
            try:
                with (mock.patch.object(supervisor, "_finish_delivery"),
                      mock.patch.object(supervisor, "_flush_progress")):
                    supervisor._finish()
            finally:
                supervisor._journal.close()
            exited = read_events(run_dir / "journal.jsonl").of_kind(RUN_EXITED)[0]
            self.assertEqual(exited["signal"], signal.SIGKILL)
            self.assertTrue(exited["stopping"])
            self.assertNotIn("head_loss_reason", exited)

    def test_child_oom_after_unrelated_head_sigkill_is_not_attributed_to_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            run_id = "child-oom-after-sigkill"
            run_dir = protocol.run_dir_for(temp, run_id)
            run_dir.mkdir()
            before = {"max": 0, "oom_kill": 0, "oom_group_kill": 0}
            # The head died from an external SIGKILL. Its surviving child then exhausted
            # the group before the supervisor read the counters. The peak records the child.
            (cgroup / "memory.events.local").write_text(
                "max 1\noom_kill 1\noom_group_kill 1\n", encoding="ascii"
            )
            (cgroup / "pids.peak").write_text("3\n", encoding="ascii")
            supervisor = Supervisor(
                run_dir=run_dir, run_id=run_id, role="worker", task="card:1",
                command="true", memory_limit_mib=1,
            )
            supervisor._head_pid = 12345
            supervisor._head_status = signal.SIGKILL
            supervisor._memory_evidence = ScopeEvidence(cgroup, before, 1)
            supervisor._journal = JournalWriter(run_dir / "journal.jsonl", run_id).open()
            try:
                with (mock.patch.object(supervisor, "_finish_delivery"),
                      mock.patch.object(supervisor, "_flush_progress")):
                    supervisor._finish()
            finally:
                supervisor._journal.close()
            exited = read_events(run_dir / "journal.jsonl").of_kind(RUN_EXITED)[0]
            self.assertEqual(exited["signal"], signal.SIGKILL)
            self.assertNotIn("head_loss_reason", exited)

    def test_scoped_startup_cancellation_waits_for_supervisor_reap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            journal = run_dir / "journal.jsonl"
            lifecycle = ScopedHeadLifecycle("cancel-run", 96)
            with JournalWriter(journal, "cancel-run") as writer:
                started = writer.append(RUN_STARTED, head_pid=123, supervisor_pid=456)

            def stop(**_kwargs):
                with JournalWriter(journal, "cancel-run") as writer:
                    writer.append(RUN_EXITED, head_pid=123, signal=signal.SIGKILL)

            client = mock.MagicMock()
            client.__enter__.return_value.stop.side_effect = stop
            with mock.patch(
                "secretary.runtime.head.local_pty.client.SupervisorClient.connect",
                return_value=client,
            ) as connect:
                lifecycle.cancel_started(
                    socket_path=run_dir / "socket", journal_path=journal,
                    started_seq=started["seq"],
                )
            connect.assert_called_once()
            self.assertTrue(read_events(journal).of_kind(RUN_EXITED))

    def test_spawn_materializes_scope_before_supervisor_starts(self) -> None:
        for role in ("observer", "worker", "review", "po"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                run_id = f"launch-materialization-{role}"
                run_dir = protocol.run_dir_for(temp, run_id)
                run_dir.mkdir(parents=True)
                protocol.socket_path_for(run_dir).touch()
                started = {"kind": "run.started", "head_pid": 12, "supervisor_pid": 11}
                fake_process = SimpleNamespace(wait=lambda: 0)
                with (
                    mock.patch("secretary.runtime.head.local_pty.client.subprocess.Popen", return_value=fake_process) as popen,
                    mock.patch("secretary.runtime.head.local_pty.client.read_events", side_effect=[
                        SimpleNamespace(events=()), SimpleNamespace(events=(started,))
                    ]),
                    mock.patch("secretary.runtime.head.local_pty.client._identity_written", return_value=True),
                    mock.patch("secretary.runtime.head.local_pty.client._answers", return_value=True),
                ):
                    handle = spawn_head(root=temp, run_id=run_id, role=role, task="card:1",
                                        command="true", memory_limit_mib=1)
                argv = popen.call_args.args[0]
                self.assertIn("secretary.runtime.head.local_pty.scope_launcher", argv)
                self.assertIn("--property=MemoryMax=1048576", argv)
                self.assertIn("--property=MemorySwapMax=0", argv)
                self.assertEqual(argv[argv.index("--memory-limit-mib") + 1], "1")
                self.assertIn("--reuid=", " ".join(argv))
                self.assertNotIn("--daemonize", argv)
                self.assertEqual(handle.head_pid, 12)

    def test_scope_launcher_releases_its_caller_only_after_the_scope_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scope = SimpleNamespace(poll=mock.Mock(side_effect=AssertionError("started scope was polled")))
            started = SimpleNamespace(events=({"kind": RUN_STARTED},))
            with (
                mock.patch("secretary.runtime.head.local_pty.scoped_lifecycle.subprocess.Popen", return_value=scope) as popen,
                mock.patch("secretary.runtime.head.local_pty.journal.read_events", return_value=started),
            ):
                result = scope_launcher.main([temp, str(Path(temp) / "scope.log"), "1", "systemd-run"])
            self.assertEqual(result, 0)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.args[0], ["systemd-run"])

    def test_scope_launcher_propagates_registration_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scope = SimpleNamespace(poll=lambda: 7)
            with mock.patch("secretary.runtime.head.local_pty.scoped_lifecycle.subprocess.Popen", return_value=scope):
                result = scope_launcher.main([temp, str(Path(temp) / "scope.log"), "1", "systemd-run"])
            self.assertEqual(result, 7)

    def test_immediate_scoped_exit_returns_durable_head_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id = "immediate-exit"
            started = {"kind": RUN_STARTED, "seq": 1, "head_pid": 12, "supervisor_pid": 11}
            exited = {"kind": RUN_EXITED, "seq": 2, "head_pid": 12, "exit_code": 7}
            with (
                mock.patch("secretary.runtime.head.local_pty.client.subprocess.Popen", return_value=SimpleNamespace(wait=lambda: 0)),
                mock.patch("secretary.runtime.head.local_pty.client.read_events", side_effect=[
                    SimpleNamespace(events=()), SimpleNamespace(events=(started, exited)),
                ]),
                mock.patch("secretary.runtime.head.local_pty.client._identity_written", return_value=True),
                mock.patch("secretary.runtime.head.local_pty.client._answers", side_effect=AssertionError("dead socket probed")),
            ):
                handle = spawn_head(root=temp, run_id=run_id, role="worker", task="card:1",
                                    command="exit 7", memory_limit_mib=1)
            self.assertEqual(handle.head_pid, 12)
            self.assertEqual(ScopedHeadLifecycle.started_or_exited((started, exited), 0), (started, True))

    def test_dead_status_admits_typed_journal_reason_for_both_card_roles(self) -> None:
        for kind in ("worker", "review"):
            with (
                self.subTest(kind=kind),
                mock.patch.object(review, "_head_run_process_status", return_value={"state": "dead"}),
                mock.patch.object(review, "_heartbeat_is_dead", return_value=True),
                mock.patch.object(review, "_supervised", return_value=True),
            ):
                run = {"run_id": f"{kind}-run"}
                record = SimpleNamespace(workspace="/tmp/work", worker_head_run=run,
                                         review_head_run=run, worker_leaf="", review_leaf="")
                host = SimpleNamespace(mode="real", head_loss_reason=lambda _run: "memory_limit")
                status = review.command_terminal_status(host, {"ref": "card:1"}, record, kind=kind)
                self.assertEqual(status["head_loss_reason"], "memory_limit")

    def test_memory_loss_enters_the_existing_dead_head_recovery(self) -> None:
        for kind in ("worker", "review"):
            with self.subTest(kind=kind), mock.patch.object(
                wait_vitality, "_trigger_wait_watchdog", return_value={"action": "normal-recovery"}
            ) as recover:
                result = wait_vitality._decide_wait_by_verdict(
                    SimpleNamespace(), {"ref": "card:1"}, SimpleNamespace(report_generation=1),
                    {}, {}, "attempt", kind=kind,
                    status={"head_loss_reason": "memory_limit"},
                    episode=SimpleNamespace(verdict=VitalityVerdict.DEAD), now=1,
                    runtime_reason="", activity=None, progress_at=0,
                )
                self.assertEqual(result, {"action": "normal-recovery"})
                self.assertIn("memory_limit", recover.call_args.kwargs["trigger"])
