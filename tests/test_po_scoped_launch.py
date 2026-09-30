"""The PO service's turn enters the same scoped lifecycle as dispatcher heads."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.po.runner import PoRunner
from secretary.runtime.head.local_pty.client import HeadHandle, LocalPtySpawnError
from secretary.runtime.head.local_pty.journal import RUN_EXITED, RUN_STARTED, JournalWriter
from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from secretary.runtime.head.spec import HeadSpec
from secretary.runtime.heads import Registry


class PoScopedLaunchTests(unittest.TestCase):
    def test_service_runner_uses_its_installation_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance = Path(temp) / "instance"
            (instance / "heads").mkdir(parents=True)
            (instance / "heads" / "heads.yaml").touch()
            registry = Registry({}, {"po-claude": {
                "adapter": "claude", "model": "opus", "effort": "high", "memory_limit_mib": 3072,
            }})
            with (mock.patch("secretary.po.runner.PoStore.for_instance", return_value=SimpleNamespace()),
                  mock.patch("secretary.po.runner.load_registry", return_value=registry) as load):
                runner = PoRunner.for_instance(instance, Path(temp) / "data")
            load.assert_called_once_with(instance / "heads" / "heads.yaml")
            spec = runner._head_spec(SimpleNamespace(cli="claude", model="opus", effort="high"))
            self.assertEqual((spec.profile_id, spec.memory_limit_mib), ("po-claude", 3072))

    def test_po_turn_launches_supervised_scope_and_immediate_exit_is_waitable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SimpleNamespace(turn_request_id=lambda *_: None, record_process=lambda *_: True)
            spec = HeadSpec.from_profile("po-codex", {
                "adapter": "codex", "model": "gpt-6-sol", "effort": "high", "memory_limit_mib": 4096,
            })
            runner = PoRunner(store, root, head_specs={spec.profile_id: spec})
            session = SimpleNamespace(
                session_id="session", cli="codex", model="gpt-6-sol", effort="high", cwd=str(root),
            )
            files = runner.files("session", 1)
            files.directory.mkdir(parents=True)
            files.prompt.write_text("hello", encoding="utf-8")

            def spawn(**kwargs):
                self.assertEqual(kwargs["role"], "po")
                self.assertEqual(kwargs["memory_limit_mib"], 4096)
                self.assertEqual(kwargs["owner_unit"], "secretary-po.service")
                self.assertIn("po-codex", kwargs["task"])
                self.assertIn("< ", kwargs["command"])
                run_dir = root / "head"
                run_dir.mkdir()
                journal = run_dir / "journal.jsonl"
                with JournalWriter(journal, kwargs["run_id"]) as writer:
                    writer.append(RUN_EXITED, head_pid=123, exit_code=None, signal=9,
                                  head_loss_reason="memory_limit")
                return HeadHandle(
                    run_dir=run_dir, run_id=kwargs["run_id"], role="po", task=kwargs["task"],
                    socket_path=run_dir / "socket", journal_path=journal, pid_file=run_dir / "head.pid",
                    supervisor_pid=456, head_pid=123,
                )

            with mock.patch("secretary.po.runner.spawn_head", side_effect=spawn) as spawned:
                process = runner._launch(session, 1, ["/bin/true"], files)
            self.assertEqual(process.pid, 123)
            self.assertEqual(process.wait(), -9)
            self.assertEqual(process.head_loss_reason, "memory_limit")
            self.assertEqual(spawned.call_count, 1)

    def test_delayed_heartbeat_cancels_started_scope_before_turn_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cancelled = []
            settled = []
            store = SimpleNamespace(
                turn_request_id=lambda *_: None,
                finish_turn=lambda *_args, **_kwargs: settled.append(bool(cancelled)) or True,
            )
            spec = HeadSpec.from_profile("po-codex", {"adapter": "codex", "memory_limit_mib": 96})
            runner = PoRunner(store, root, head_specs={spec.profile_id: spec})
            session = SimpleNamespace(
                session_id="session", cli="codex", model=None, effort="default", cwd=str(root),
            )
            files = runner.files("session", 1)
            files.directory.mkdir(parents=True)
            files.prompt.write_text("hello", encoding="utf-8")

            def start(_argv, **kwargs):
                run_dir = next((root / "po-heads").iterdir())
                with JournalWriter(run_dir / "journal.jsonl", run_dir.name) as writer:
                    writer.append(RUN_STARTED, head_pid=123, supervisor_pid=456)
                return SimpleNamespace(wait=lambda: 0)

            def cancel(_self, *, socket_path, journal_path, started_seq):
                self.assertEqual(socket_path.parent, journal_path.parent)
                self.assertGreater(started_seq, 0)
                cancelled.append(True)

            with (
                mock.patch("secretary.runtime.head.local_pty.client.subprocess.Popen", side_effect=start),
                mock.patch("secretary.runtime.head.local_pty.client._identity_written", return_value=False),
                mock.patch("secretary.runtime.head.local_pty.client.SPAWN_TIMEOUT_SECONDS", 0.02),
                mock.patch.object(ScopedHeadLifecycle, "cancel_started", cancel),
            ):
                with self.assertRaisesRegex(RuntimeError, "did not answer"):
                    runner._launch(session, 1, ["/bin/true"], files)
            self.assertEqual(settled, [True])

    def test_failed_scope_cleanup_does_not_settle_a_live_po_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = SimpleNamespace(
                turn_request_id=lambda *_: None,
                finish_turn=mock.Mock(),
            )
            runner = PoRunner(store, Path(temp))
            session = SimpleNamespace(
                session_id="session", cli="codex", model=None, effort="default", cwd=temp,
            )
            files = runner.files("session", 1)
            with mock.patch(
                "secretary.po.runner.spawn_head",
                side_effect=LocalPtySpawnError(
                    "cleanup_failed", "scope is still alive", cleanup_complete=False,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "scope is still alive"):
                    runner._launch(session, 1, ["/bin/true"], files)
            store.finish_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
