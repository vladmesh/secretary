from __future__ import annotations

import argparse
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import cutover

REVISION = "a" * 40
PLAN = {
    "version": 1,
    "plan_id": "1" * 64,
    "confirmation": "CUTOVER-" + "1" * 16,
    "expected_revision": REVISION,
}


def args(**overrides):
    values = {
        "expected_revision": REVISION,
        "actor": "operator@example",
        "reason": "approved maintenance window",
        "confirm": PLAN["confirmation"],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def fake_operations(failed_phase, calls, failure):
    class FakeOperations:
        def __init__(self, _paths, _state):
            pass

        def __getattr__(self, name):
            def operation():
                calls.append(name)
                if name == failed_phase and failure["enabled"]:
                    raise RuntimeError("injected crash")
                if name == "selector_activation":
                    return {"sql_audit_baseline": {"committed_events": 10}}
                if name == "installed_protocol_acceptance":
                    return {"sql_audit": {"committed_events": 10}}
                return {"phase": name}

            return operation

    return FakeOperations


class CutoverFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.instance = root / "instance"
        self.data = root / "data"
        self.instance.mkdir(mode=0o700)
        self.data.mkdir(mode=0o700)
        self.runtime = self.instance / "runtime.env"
        self.runtime.write_text("UNRELATED=kept\nSECRETARY_CARD_BACKEND=kanboard\n", encoding="utf-8")
        self.runtime.chmod(0o640)
        self.paths = cutover.Paths(self.instance, self.data)

    def tearDown(self) -> None:
        self.temporary.cleanup()


class CutoverStateTests(CutoverFixture):
    def test_phase_order_is_the_public_cutover_contract(self) -> None:
        self.assertEqual(
            cutover.PHASES,
            (
                "preflight",
                "current_kanboard_backup_checkpoint",
                "postgresql_provision_migration_verification",
                "global_freeze",
                "writer_quiescence_proof",
                "final_fenced_import",
                "full_parity",
                "postgresql_recovery_backup",
                "selector_activation",
                "service_reconciliation",
                "installed_protocol_acceptance",
                "post_switch_checkpoint",
                "resume_ready",
            ),
        )

    def test_selector_rewrite_preserves_other_entries_owner_and_mode(self) -> None:
        before = self.runtime.stat()
        evidence = cutover._set_backend(self.paths, "postgres")
        after = self.runtime.stat()
        self.assertEqual(
            self.runtime.read_text(encoding="utf-8"),
            "UNRELATED=kept\nSECRETARY_CARD_BACKEND=postgres\n",
        )
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual(evidence["backend"], "postgres")

    def test_selector_rewrite_refuses_duplicate_authorities(self) -> None:
        self.runtime.write_text(
            "SECRETARY_CARD_BACKEND=kanboard\nSECRETARY_CARD_BACKEND=postgres\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(cutover.CutoverError, "duplicate"):
            cutover._set_backend(self.paths, "postgres")

    def test_state_is_private_versioned_and_atomic(self) -> None:
        payload = {"version": 1, "identity": "fixture"}
        cutover._write_state(self.paths, payload)
        self.assertEqual(cutover._read_state(self.paths), payload)
        self.assertEqual(stat.S_IMODE(self.paths.state.stat().st_mode), 0o600)
        self.assertFalse(list(self.paths.state.parent.glob("*.tmp")))

    def test_atomic_state_rename_failure_keeps_the_previous_document(self) -> None:
        original = {"version": 1, "identity": "original"}
        cutover._write_state(self.paths, original)
        with (
            mock.patch("secretary.cutover.os.replace", side_effect=OSError("injected rename")),
            self.assertRaisesRegex(cutover.CutoverError, "publish cutover state"),
        ):
            cutover._write_state(self.paths, {"version": 1, "identity": "replacement"})
        self.assertEqual(cutover._read_state(self.paths), original)

    def test_symlinked_state_is_refused(self) -> None:
        self.paths.state.parent.mkdir(mode=0o700)
        target = self.data / "elsewhere"
        target.write_text("{}", encoding="utf-8")
        self.paths.state.symlink_to(target)
        with self.assertRaisesRegex(cutover.CutoverError, "symlink"):
            cutover._read_state(self.paths)

    def test_durable_freeze_blocks_other_writers_and_allows_only_controller_identity(self) -> None:
        from secretary.cutover.barrier import require_board_write_allowed
        from secretary.tasks import TaskError

        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["controller_pid"] = os.getpid()
        state["phases"]["global_freeze"] = {"status": "complete"}
        cutover._write_state(self.paths, state)
        with self.assertRaisesRegex(TaskError, "writes are fenced"):
            require_board_write_allowed(self.data)
        with mock.patch.dict(
            os.environ,
            {"SECRETARY_CUTOVER_CONTROLLER_ID": state["identity"]},
            clear=False,
        ):
            require_board_write_allowed(self.data)

    def test_explicit_pipeline_resume_releases_completed_cutover_barrier(self) -> None:
        from secretary.cutover.barrier import require_board_write_allowed
        from secretary.dispatcher_pause import ProductionPause

        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["status"] = "resume-ready"
        state["phases"]["global_freeze"] = {"status": "complete"}
        cutover._write_state(self.paths, state)
        with mock.patch.object(ProductionPause, "summary", return_value={"paused": False}):
            require_board_write_allowed(self.data)


class CutoverFailureInjectionTests(CutoverFixture):
    def test_interruption_after_every_phase_resumes_without_repeating_completed_work(self) -> None:
        for failed_index, failed_phase in enumerate(cutover.PHASES):
            with self.subTest(phase=failed_phase), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                paths = cutover.Paths(root / "instance", root / "data")
                paths.instance.mkdir(mode=0o700)
                paths.data.mkdir(mode=0o700)
                calls: list[str] = []
                failure = {"enabled": True}

                with (
                    mock.patch.object(cutover, "build_plan", return_value=PLAN),
                    mock.patch.object(cutover, "Operations", fake_operations(failed_phase, calls, failure)),
                    mock.patch.object(cutover, "_provenance", return_value={}),
                    mock.patch.object(
                        cutover,
                        "_backend",
                        return_value="postgres"
                        if failed_index > cutover.PHASES.index("selector_activation")
                        else "kanboard",
                    ),
                ):
                    with self.assertRaisesRegex(cutover.CutoverError, failed_phase):
                        cutover.apply_cutover(args(), paths)
                    completed = tuple(cutover.PHASES[:failed_index])
                    before = {name: calls.count(name) for name in completed}
                    failure["enabled"] = False
                    result = cutover.apply_cutover(args(), paths)

                self.assertEqual(result["status"], "resume-ready")
                self.assertEqual(result["phases"][failed_phase]["status"], "complete")
                self.assertEqual(calls.count(failed_phase), 2)
                self.assertEqual({name: calls.count(name) for name in completed}, before)
                self.assertTrue(all(count == 1 for count in before.values()))

    def test_retry_refuses_backend_that_disagrees_with_durable_selector_phase(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["phases"]["selector_activation"] = {"status": "complete"}
        cutover._write_state(self.paths, state)
        with (
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_provenance", return_value={}),
            self.assertRaisesRegex(cutover.CutoverError, "expects backend postgres"),
        ):
            cutover.apply_cutover(args(), self.paths)


class CutoverRecoveryTests(CutoverFixture):
    def state(self, *, first_write=None):
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["phases"]["writer_quiescence_proof"] = {
            "status": "complete",
            "evidence": {"source": {"fingerprint": "source-one"}},
        }
        state["phases"]["selector_activation"] = {
            "status": "complete",
            "evidence": {"sql_audit_baseline": {"committed_events": 10}},
        }
        state["first_sql_write"] = first_write
        cutover._write_state(self.paths, state)
        return state

    def test_before_first_write_recovery_can_restore_unchanged_kanboard(self) -> None:
        self.state()
        recovery_args = args(confirm="RECOVER-" + "1" * 16)
        with (
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_sql_event_count", return_value={"committed_events": 10}),
            mock.patch.object(cutover, "_source_evidence", return_value={"fingerprint": "source-one"}),
            mock.patch.object(cutover, "_set_backend", return_value={}) as selector,
            mock.patch.object(cutover, "_systemctl", return_value={}),
        ):
            result = cutover.recover_cutover(recovery_args, self.paths)
        selector.assert_called_once_with(self.paths, "kanboard")
        self.assertEqual(result["recovery"]["branch"], "kanboard-before-first-write")

    def test_committed_post_activation_event_forces_postgres_only_recovery(self) -> None:
        self.state()
        recovery_args = args(confirm="RECOVER-" + "1" * 16)
        with (
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_sql_event_count", return_value={"committed_events": 11}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_set_backend", return_value={}) as selector,
            mock.patch.object(cutover, "_reconcile_postgres", return_value={}),
            mock.patch.object(cutover, "_systemctl", return_value={}),
        ):
            result = cutover.recover_cutover(recovery_args, self.paths)
        selector.assert_called_once_with(self.paths, "postgres")
        self.assertEqual(result["recovery"]["branch"], "postgres-only")
        self.assertEqual(result["first_sql_write"]["evidence"]["committed_events"], 11)

    def test_source_movement_blocks_pre_write_rollback(self) -> None:
        self.state()
        recovery_args = args(confirm="RECOVER-" + "1" * 16)
        with (
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_sql_event_count", return_value={"committed_events": 10}),
            mock.patch.object(cutover, "_source_evidence", return_value={"fingerprint": "moved"}),
            self.assertRaisesRegex(cutover.CutoverError, "source moved"),
        ):
            cutover.recover_cutover(recovery_args, self.paths)


class RoleBackendPropagationTests(unittest.TestCase):
    def test_every_launched_role_receives_selector_and_board_store_file_is_not_a_selector(self) -> None:
        from triggered_agents.runtime import role_env

        roles = ("pipeline", "worker", "reviewer", "observer", "steward", "retro", "curator")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "runtime.env"
            runtime.write_text("SECRETARY_CARD_BACKEND=postgres\n", encoding="utf-8")
            workspace = root / "workspace"
            python = workspace / role_env.WORKSPACE_ENV_DIR / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            python.chmod(0o700)
            for role in roles:
                environment = role_env.runtime_env(
                    role,
                    base_env={},
                    env_file=runtime,
                    workspace=workspace if role in {"worker", "reviewer"} else None,
                )
                self.assertEqual(environment[cutover.BACKEND_ENV], "postgres", role)

            runtime.write_text("", encoding="utf-8")
            (root / "board-store.env").write_text("SECRETARY_DB_HOST=present\n", encoding="utf-8")
            for role in roles:
                environment = role_env.runtime_env(
                    role,
                    base_env={},
                    env_file=runtime,
                    workspace=workspace if role in {"worker", "reviewer"} else None,
                )
                self.assertNotIn(cutover.BACKEND_ENV, environment, role)

    def test_operator_cli_binds_only_the_explicit_instances_runtime_selector(self) -> None:
        from secretary import cli
        from secretary.board.backend import card_backend, reset_card_backend

        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            runtime = instance / "runtime.env"
            runtime.write_text("SECRETARY_CARD_BACKEND=postgres\n", encoding="utf-8")
            runtime.chmod(0o600)
            with (
                mock.patch("secretary.runtime_env.state_repo.is_ignored", return_value=True),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop(cutover.BACKEND_ENV, None)
                reset_card_backend()
                self.assertTrue(cli._bind_instance_card_backend(argparse.Namespace(instance=str(instance))))
                self.assertEqual(card_backend(), "postgres")
                os.environ.pop(cutover.BACKEND_ENV, None)
                reset_card_backend()


if __name__ == "__main__":
    unittest.main()
