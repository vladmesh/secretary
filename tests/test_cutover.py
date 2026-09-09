from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import cutover
from secretary.cutover import successor

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

    def test_state_is_runtime_readable_owner_writable_versioned_and_atomic(self) -> None:
        payload = {"version": 1, "identity": "fixture"}
        cutover._write_state(self.paths, payload)
        self.assertEqual(cutover._read_state(self.paths), payload)
        self.assertEqual(stat.S_IMODE(self.paths.state.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.paths.state.parent.stat().st_mode), 0o755)
        self.assertTrue(self.paths.state.stat().st_mode & stat.S_IROTH)
        self.assertFalse(self.paths.state.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        self.assertTrue(self.paths.state.parent.stat().st_mode & stat.S_IXOTH)
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

    def test_terminal_cutover_never_rearms_during_a_later_freeze(self) -> None:
        from secretary.cutover.barrier import require_board_write_allowed

        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["status"] = "resume-ready"
        state["phases"]["global_freeze"] = {"status": "complete"}
        cutover._write_state(self.paths, state)
        require_board_write_allowed(self.data)

    def test_runtime_child_may_write_only_with_the_controller_identity(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["controller_pid"] = os.getpid()
        state["phases"]["global_freeze"] = {"status": "complete"}
        cutover._write_state(self.paths, state)
        script = (
            "from secretary.cutover.barrier import require_board_write_allowed; "
            f"require_board_write_allowed({str(self.data)!r})"
        )
        environment = dict(os.environ)
        environment[cutover.CONTROLLER_ID_ENV] = state["identity"]
        allowed = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        refused = subprocess.run(
            [sys.executable, "-c", script],
            env=os.environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("writes are fenced", refused.stderr)


class CutoverSuccessorTests(CutoverFixture):
    def eligible_state(self):
        state = cutover._new_state(PLAN, args().actor, args().reason)
        report = {"parity": {"ok": True}}
        report_path = self.paths.artifacts / f"import-{state['plan_id']}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        state["expected_revision"] = "b" * 40
        state["status"] = "recovered-frozen"
        state["recovery"] = {"branch": "kanboard-before-first-write"}
        state["phases"] = {
            "final_fenced_import": {
                "status": "complete",
                "evidence": {"report": str(report_path), "import": report},
            },
            "full_parity": {
                "status": "complete",
                "evidence": {"parity": {"ok": True}},
            },
        }
        return state

    def test_successor_eligibility_is_exact_and_activation_always_refuses(self) -> None:
        state = self.eligible_state()
        self.assertTrue(successor.exact_eligibility(state, "kanboard")["eligible"])
        state["phases"]["selector_activation"] = {"status": "running"}
        proof = successor.exact_eligibility(state, "kanboard")
        self.assertFalse(proof["eligible"])
        self.assertEqual(proof["reason"], "selector-activation-entered")

        for malformed in (None, {"parity": None}):
            with self.subTest(malformed=malformed):
                state = self.eligible_state()
                state["phases"]["full_parity"]["evidence"] = malformed
                proof = successor.exact_eligibility(state, "kanboard")
                self.assertFalse(proof["eligible"])
                self.assertEqual(proof["reason"], "full-parity-not-clean")

        state = self.eligible_state()
        with (
            mock.patch.object(
                successor, "parse", return_value=SimpleNamespace(dbname="secretary")
            ),
            mock.patch(
                "secretary.head_registry.read_source", return_value={"revision": REVISION}
            ),
            mock.patch.object(
                successor,
                "inspect_database",
                return_value={
                    "oid": 41,
                    "name": "secretary",
                    "owner": "owner",
                    "allow_connections": True,
                },
            ),
            mock.patch.object(
                successor,
                "inspect_schema_revision",
                return_value="0006_sprint_transport_key",
            ),
        ):
            status = successor.status_probe(
                self.paths.instance, state, "kanboard", artifacts=self.paths.artifacts
            )
        self.assertEqual(status["prerequisite"], successor.UPGRADE_PREREQUISITE)
        self.assertEqual(
            status["next_command"],
            f"secretary upgrade --no-pull --instance {self.paths.instance}",
        )

        state["successor_preparation"] = {
            "version": 1,
            "status": "preparing",
            "instance": str(self.paths.instance),
            "actor": args().actor,
            "reason": args().reason,
            "expected_revision": REVISION,
            "confirmation": "token",
            "started_at": "2026-09-09T00:00:00Z",
            "phases": {},
            "database": {},
            "dump": {},
        }
        cutover._write_state(self.paths, state)
        before = self.paths.state.read_bytes()
        run_args = args(confirm="token")
        with (
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(
                successor, "resolve", return_value=SimpleNamespace(dbname="secretary")
            ),
            mock.patch.object(
                successor,
                "inspect_schema_revision",
                return_value="0006_sprint_transport_key",
            ),
            self.assertRaisesRegex(cutover.CutoverError, successor.UPGRADE_PREREQUISITE),
        ):
            successor.prepare(run_args, self.paths)
        self.assertEqual(self.paths.state.read_bytes(), before)

    def test_status_keeps_next_command_while_the_target_is_fenced_or_unreachable(self) -> None:
        state = self.eligible_state()
        state["successor_preparation"] = {
            "version": 1,
            "status": "preparing",
            "instance": str(self.paths.instance),
            "actor": args().actor,
            "reason": args().reason,
            "expected_revision": REVISION,
            "confirmation": "token",
            "started_at": "2026-09-09T00:00:00Z",
            "phases": {
                "connection_fence": {"status": "complete"},
                "database_rename": {"status": "complete"},
            },
            "database": {
                "original_oid": 41,
                "original_name": "secretary",
                "archive_name": "secretary_archive",
                "owner": "owner",
            },
            "dump": {},
        }
        token = successor.confirmation(state["plan_id"], 41, "secretary")
        expected_command = (
            f"secretary cutover prepare-successor --instance {self.paths.instance} "
            f"--expected-revision {REVISION} --actor <actor> --reason <reason> --confirm {token}"
        )
        refused = RuntimeError("could not inspect PostgreSQL schema revision: connection refused")
        with (
            mock.patch.object(
                successor, "parse", return_value=SimpleNamespace(dbname="secretary")
            ),
            mock.patch(
                "secretary.head_registry.read_source", return_value={"revision": REVISION}
            ),
            mock.patch.object(successor, "inspect_database", side_effect=AssertionError("not read")),
            mock.patch.object(successor, "inspect_schema_revision", side_effect=refused) as probe,
        ):
            # Mid-rotation: the configured name is fenced, absent or unmigrated, so status must
            # not touch the database and must still render the identical retry command.
            fenced = successor.status_probe(
                self.paths.instance, state, "kanboard", artifacts=self.paths.artifacts
            )
            self.assertEqual(probe.call_count, 0)
            self.assertEqual(fenced["database"]["oid"], 41)
            self.assertEqual(fenced["confirmation"], token)
            self.assertEqual(fenced["next_command"], expected_command)
            self.assertIsNone(fenced["schema_revision"])
            self.assertNotIn("probe_error", fenced)

            # Before the fence an unreachable schema probe is reported, not allowed to swallow
            # the identity, confirmation and next command.
            state["successor_preparation"]["phases"] = {}
            unreachable = successor.status_probe(
                self.paths.instance, state, "kanboard", artifacts=self.paths.artifacts
            )
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(unreachable["database"]["oid"], 41)
            self.assertEqual(unreachable["confirmation"], token)
            self.assertEqual(unreachable["next_command"], expected_command)
            self.assertIn("connection refused", unreachable["schema_probe_error"])
            self.assertNotIn("probe_error", unreachable)

    def test_prepare_successor_resumes_every_phase_without_repeating_completed_work(self) -> None:
        state = self.eligible_state()
        cutover._write_state(self.paths, state)
        token = successor.confirmation(state["plan_id"], 41, "secretary")
        run_args = args(confirm=token)
        real_operations = successor.SuccessorOperations

        for failed_phase in successor.PHASES:
            with self.subTest(phase=failed_phase):
                cutover._write_state(self.paths, self.eligible_state())
                calls = []
                failure = {"enabled": True}

                class FakeOperations:
                    def __init__(self, operation_paths, operation_state):
                        self.real = real_operations(operation_paths, operation_state)

                    def __getattr__(self, name):
                        def operation(
                            operation_name=name,
                            operation_calls=calls,
                            operation_failure=failure,
                            operation_failed_phase=failed_phase,
                        ):
                            operation_calls.append(operation_name)
                            if operation_name == operation_failed_phase and operation_failure["enabled"]:
                                raise RuntimeError("injected crash")
                            if operation_name == "dump_publication":
                                return {"sha256": "d" * 64, "bytes": 10, "tool_version": "PostgreSQL 16"}
                            if operation_name == "database_create":
                                return {"oid": 42, "name": "secretary"}
                            if operation_name in {"history_publication", "canonical_release"}:
                                return getattr(self.real, operation_name)()
                            if operation_name == "release_receipt":
                                return successor.publish_release_receipt(
                                    self.real.paths, self.real.state
                                )
                            return {"phase": operation_name}
                        return operation

                with (
                    mock.patch.object(cutover, "_provenance", return_value={}),
                    mock.patch.object(cutover, "_backend", return_value="kanboard"),
                    mock.patch.object(successor, "resolve", return_value=SimpleNamespace(dbname="secretary")),
                    mock.patch.object(successor, "inspect_database", return_value={"oid": 41, "name": "secretary", "owner": "owner", "allow_connections": True}),
                    mock.patch.object(
                        successor,
                        "inspect_schema_revision",
                        return_value="0007_card_transport_key",
                    ),
                    mock.patch.object(successor, "_verify_completed_targets", return_value=None),
                    mock.patch.object(successor, "SuccessorOperations", FakeOperations),
                ):
                    with self.assertRaisesRegex(cutover.CutoverError, failed_phase):
                        successor.prepare(run_args, self.paths)
                    completed = tuple(successor.PHASES[: successor.PHASES.index(failed_phase)])
                    before = {name: calls.count(name) for name in completed}
                    failure["enabled"] = False
                    result = successor.prepare(run_args, self.paths)

                self.assertTrue(result["successor_ready"])
                self.assertEqual(calls.count(failed_phase), 2)
                self.assertEqual({name: calls.count(name) for name in completed}, before)
                self.assertIsNone(cutover._read_state(self.paths))
                archive = Path(result["archived_state"])
                archive.chmod(0o600)
                archive.unlink()
                receipt = successor.release_receipt_path(self.paths, state["plan_id"])
                receipt.chmod(0o600)
                receipt.unlink()

    def test_real_history_link_and_canonical_unlink_are_truthful_across_restart(self) -> None:
        state = self.eligible_state()
        state["successor_preparation"] = {
            "version": 1,
            "status": "preparing",
            "instance": str(self.paths.instance),
            "actor": "operator",
            "reason": "filesystem interruption proof",
            "expected_revision": REVISION,
            "confirmation": "token",
            "started_at": "2026-09-09T00:00:00Z",
            "database": {
                "original_oid": 41,
                "original_name": "secretary",
                "archive_name": "secretary_archive_test_41",
                "owner": "owner",
                "successor_oid": 42,
            },
            "dump": {"path": str(self.paths.artifacts / "successor.dump"), "sha256": "d" * 64},
            "phases": {
                **{
                    name: {
                        "status": "complete",
                        "started_at": "2026-09-09T00:00:01Z",
                        "completed_at": "2026-09-09T00:00:01Z",
                        "evidence": {},
                    }
                    for name in successor.PHASES[
                        : successor.PHASES.index("history_publication")
                    ]
                },
                "history_publication": {
                    "status": "intent",
                    "started_at": "2026-09-09T00:00:01Z",
                }
            },
        }
        cutover._write_state(self.paths, state)
        with mock.patch.object(successor, "resolve", return_value=SimpleNamespace(dbname="secretary")):
            operations = successor.SuccessorOperations(self.paths, state)
            operations.history_publication()

            canonical = cutover._read_state(self.paths)
            self.assertEqual(
                canonical["successor_preparation"]["phases"]["history_publication"]["status"],
                "intent",
            )
            history = cutover._read_recovered_history(self.paths)
            self.assertEqual(
                history[0]["state"]["successor_preparation"]["phases"]["history_publication"]["status"],
                "complete",
            )
            self.assertEqual(
                history[0]["state"]["successor_preparation"]["phases"]["canonical_release"]["status"],
                "intent",
            )

            state["successor_preparation"]["phases"]["canonical_release"] = {
                "status": "intent",
                "started_at": "2026-09-09T00:00:02Z",
            }
            cutover._write_state(self.paths, state)
            operations = successor.SuccessorOperations(self.paths, state)
            real_unlink = Path.unlink
            interrupted = {"pending": True}

            def interrupted_unlink(path, *unlink_args, **unlink_kwargs):
                if path == self.paths.state and interrupted["pending"]:
                    interrupted["pending"] = False
                    raise OSError("injected before canonical unlink")
                return real_unlink(path, *unlink_args, **unlink_kwargs)

            with mock.patch.object(Path, "unlink", new=interrupted_unlink):
                with self.assertRaisesRegex(RuntimeError, "injected before canonical unlink"):
                    operations.canonical_release()
            self.assertTrue(self.paths.state.exists())
            operations.canonical_release()

        self.assertFalse(self.paths.state.exists())
        pending = cutover._read_recovered_history(self.paths)[0]
        self.assertEqual(pending["successor_release"]["status"], "pending")
        self.assertEqual(
            pending["state"]["successor_preparation"]["phases"]["canonical_release"]["status"],
            "intent",
        )
        with (
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_provenance", return_value={"revision": REVISION}),
            mock.patch.object(
                cutover,
                "_source_evidence",
                return_value={"fingerprint": "new-source", "parity": {"ok": True}},
            ),
            self.assertRaisesRegex(cutover.CutoverError, "release receipt"),
        ):
            cutover.build_plan(self.paths, REVISION)
        successor.publish_release_receipt(self.paths, pending["state"])
        released_item = cutover._read_recovered_history(self.paths)[0]
        released = released_item["state"]
        self.assertEqual(
            released["successor_preparation"]["phases"]["canonical_release"]["status"],
            "complete",
        )
        self.assertTrue(
            released["successor_preparation"]["phases"]["canonical_release"]["evidence"]["released"]
        )
        with (
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_provenance", return_value={"revision": REVISION}),
            mock.patch.object(
                cutover,
                "_source_evidence",
                return_value={"fingerprint": "new-source", "parity": {"ok": True}},
            ),
        ):
            plan = cutover.build_plan(self.paths, REVISION)
        cutover._write_state(self.paths, cutover._new_state(plan, "owner", "later cutover"))
        self.assertEqual(cutover._read_recovered_history(self.paths)[0], released_item)
        self.assertTrue(
            successor.publish_release_receipt(self.paths, released_item["state"])["immutable"]
        )
        receipt = successor.release_receipt_path(self.paths, state["plan_id"])
        receipt.chmod(0o600)
        receipt.write_text("{}\n", encoding="utf-8")
        receipt.chmod(0o444)
        with self.assertRaisesRegex(cutover.CutoverError, "receipt does not match"):
            cutover._read_recovered_history(self.paths)

    def test_successor_history_and_receipt_malformed_evidence_refuses_cleanly(self) -> None:
        state = self.eligible_state()
        archive = self.paths.history / f"postgres-v1-{state['plan_id']}.json"
        self.paths.history.mkdir(parents=True)
        state["successor_preparation"] = {
            "status": "preparing",
            "phases": {
                "history_publication": {"status": "complete", "evidence": None},
                "canonical_release": {"status": "intent", "started_at": "time"},
                "release_receipt": {"status": "intent", "started_at": "time"},
            },
        }
        archive.write_text(json.dumps(state), encoding="utf-8")
        archive.chmod(0o444)
        with self.assertRaisesRegex(cutover.CutoverError, "malformed release evidence"):
            cutover._read_recovered_history(self.paths)


class CutoverCommandEvidenceTests(CutoverFixture):
    def test_active_controller_can_revalidate_its_plan_while_public_successor_planning_refuses(self) -> None:
        with (
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_provenance", return_value={"revision": REVISION}),
            mock.patch.object(
                cutover,
                "_source_evidence",
                return_value={"fingerprint": "source", "parity": {"ok": True}},
            ),
        ):
            plan = cutover.build_plan(self.paths, REVISION)
            state = cutover._new_state(plan, args().actor, args().reason)
            cutover._write_state(self.paths, state)
            evidence = cutover.Operations(self.paths, state).preflight()
            with self.assertRaisesRegex(cutover.CutoverError, "canonical cutover identity"):
                cutover.build_plan(self.paths, REVISION)

        self.assertEqual(evidence["plan_id"], plan["plan_id"])

    def test_secretary_commands_use_the_package_entrypoint_and_require_json_evidence(self) -> None:
        with mock.patch("secretary.cutover.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"status":"ok"}\n', "")
            evidence = cutover._secretary(self.paths, "status", "--json")
            argv = run.call_args.args[0]
            self.assertEqual(argv[1:4], ["-P", "-m", "secretary"])
            self.assertEqual(evidence["document"], {"status": "ok"})
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            with self.assertRaisesRegex(cutover.CutoverError, "empty success evidence"):
                cutover._secretary(self.paths, "status", "--json")
            run.return_value = subprocess.CompletedProcess([], 0, "not json", "")
            with self.assertRaisesRegex(cutover.CutoverError, "non-JSON"):
                cutover._secretary(self.paths, "status", "--json")

    def test_post_switch_backup_rejects_raw_board_and_requires_postgres_dump(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        operation = cutover.Operations(self.paths, state)
        checkpoint = SimpleNamespace(status="ok", to_json=lambda: {"status": "ok"})
        raw = SimpleNamespace(archive=Path("raw.tar"), manifest={"components": {"raw_board": {}}})
        with (
            mock.patch("secretary.checkpoint.CheckpointWriter.write", return_value=checkpoint),
            mock.patch("secretary.backup.create_backups", return_value=[raw]),
            self.assertRaisesRegex(cutover.CutoverError, "raw Kanboard"),
        ):
            operation.post_switch_checkpoint()

        empty = SimpleNamespace(archive=Path("empty.tar"), manifest={"components": {}})
        with (
            mock.patch("secretary.checkpoint.CheckpointWriter.write", return_value=checkpoint),
            mock.patch("secretary.backup.create_backups", return_value=[empty]),
            self.assertRaisesRegex(cutover.CutoverError, "no PostgreSQL dump"),
        ):
            operation.post_switch_checkpoint()

    def test_acceptance_exercises_and_asserts_the_public_protocol_surface(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        operation = cutover.Operations(self.paths, state)
        seen: list[tuple[str, ...]] = []
        repeats: dict[str, int] = {}

        def command(_paths, *argv):
            seen.append(argv)
            key = " ".join(argv)
            repeats[key] = repeats.get(key, 0) + 1
            document: object = {"ok": True}
            if argv[:2] == ("sprint", "list"):
                document = {
                    "sprints": {
                        "items": [{"ref": "sprint:1", "status": "open", "reservations": ["secretary"]}]
                    }
                }
            elif argv[:2] == ("issue", "create"):
                document = {"issue": {"ref": "issue:acceptance"}}
            elif argv[:2] == ("task", "create"):
                document = {"task": {"ref": "secretary-99"}}
            elif argv[:2] == ("sprint", "comment"):
                document = {"comment_id": "evt-sprint", "saved": repeats[key] == 1}
            elif argv[:2] == ("task", "comment") and "cutover-task-comment-" in key:
                document = {"event_id": "evt-task", "replayed": repeats[key] > 1}
            return {"output": json.dumps(document), "document": document}

        with (
            mock.patch.object(cutover, "_secretary", side_effect=command),
            mock.patch.object(cutover, "_sql_event_count", return_value={"committed_events": 20}),
        ):
            evidence = operation.installed_protocol_acceptance()

        pairs = {entry[:2] for entry in seen}
        self.assertTrue(
            {
                ("product", "create"),
                ("product", "show"),
                ("issue", "create"),
                ("issue", "update-priority"),
                ("issue", "show"),
                ("issue", "close"),
                ("sprint", "comment"),
                ("sprint", "comment-delivery"),
                ("task", "create"),
                ("task", "claim"),
                ("task", "move"),
                ("task", "archive"),
                ("task", "comment"),
                ("task", "show"),
                ("web-read", "system"),
                ("web-read", "task"),
                ("web-read", "commands"),
                ("web-read", "request"),
                ("dispatcher", "production-tick"),
            }
            <= pairs
        )
        self.assertEqual(evidence["write"]["replay"]["document"]["replayed"], True)


class CutoverOperationSeamTests(CutoverFixture):
    def operation(self):
        return cutover.Operations(self.paths, cutover._new_state(PLAN, args().actor, args().reason))

    def test_service_stop_failure_surfaces_from_the_real_freeze_phase(self) -> None:
        with (
            mock.patch.object(cutover, "_secretary", return_value={"document": {"paused": True}}),
            mock.patch.object(cutover, "_systemctl", side_effect=RuntimeError("service stop failed")),
            self.assertRaisesRegex(RuntimeError, "service stop failed"),
        ):
            self.operation().global_freeze()

    def test_service_start_failure_surfaces_from_the_real_reconciliation_phase(self) -> None:
        with (
            mock.patch.object(cutover, "_systemctl", side_effect=RuntimeError("service start failed")),
            self.assertRaisesRegex(RuntimeError, "service start failed"),
        ):
            self.operation().service_reconciliation()

    def test_import_failure_surfaces_from_the_real_import_phase(self) -> None:
        with (
            mock.patch("secretary.board.import_board.run", side_effect=RuntimeError("import failed")),
            self.assertRaisesRegex(RuntimeError, "import failed"),
        ):
            self.operation().final_fenced_import()

    def test_parity_failure_surfaces_from_the_real_parity_phase(self) -> None:
        operation = self.operation()
        operation.state["phases"]["final_fenced_import"] = {
            "evidence": {"import": {"parity": {"ok": False}}}
        }
        with self.assertRaisesRegex(cutover.CutoverError, "parity failed"):
            operation.full_parity()

    def test_backup_failure_surfaces_and_restores_the_process_selector(self) -> None:
        os.environ[cutover.BACKEND_ENV] = "kanboard"
        with (
            mock.patch("secretary.backup.create_backups", side_effect=RuntimeError("dump failed")),
            self.assertRaisesRegex(RuntimeError, "dump failed"),
        ):
            self.operation().postgresql_recovery_backup()
        self.assertEqual(os.environ[cutover.BACKEND_ENV], "kanboard")

    def test_checkpoint_refusal_surfaces_from_the_real_post_switch_phase(self) -> None:
        checkpoint = SimpleNamespace(status="blocked", reason="checkpoint failed")
        with (
            mock.patch("secretary.checkpoint.CheckpointWriter.write", return_value=checkpoint),
            self.assertRaisesRegex(cutover.CutoverError, "checkpoint failed"),
        ):
            self.operation().post_switch_checkpoint()

    def test_first_write_audit_failure_records_an_irreversible_boundary(self) -> None:
        operation = self.operation()
        operation.state["phases"]["selector_activation"] = {
            "status": "complete",
            "evidence": {"sql_audit_baseline": {"committed_events": 10}},
        }
        with mock.patch.object(
            cutover, "_sql_event_count", side_effect=cutover.CutoverError("audit unavailable")
        ):
            cutover._refresh_first_sql_write(self.paths, operation.state)
        self.assertEqual(
            operation.state["first_sql_write"]["evidence"],
            {"unavailable": True, "reason": "audit unavailable"},
        )


class CutoverFailureInjectionTests(CutoverFixture):
    barrier_phases = (
        "writer_quiescence_proof",
        "final_fenced_import",
        "selector_activation",
    )

    def _operations_with_foreign_barrier_probes(self, failed_phase, failure, probes):
        fixture = self

        class ProbeOperations:
            def __init__(self, paths, _state):
                self.paths = paths

            def __getattr__(self, name):
                def operation():
                    if name == failed_phase and failure["enabled"]:
                        raise RuntimeError("injected crash")
                    if name in fixture.barrier_phases:
                        from secretary.cutover.barrier import require_board_write_allowed
                        from secretary.tasks import TaskError

                        durable = cutover._read_state(self.paths)
                        fixture.assertEqual(durable["status"], "applying")
                        controller_identity = os.environ.pop(cutover.CONTROLLER_ID_ENV, None)
                        try:
                            with fixture.assertRaisesRegex(TaskError, "writes are fenced"):
                                require_board_write_allowed(self.paths.data)
                        finally:
                            if controller_identity is not None:
                                os.environ[cutover.CONTROLLER_ID_ENV] = controller_identity
                        probes.append(name)
                    if name == "selector_activation":
                        return {"sql_audit_baseline": {"committed_events": 10}}
                    return {"phase": name}

                return operation

        return ProbeOperations

    def _apply_with_barrier_probes(self, *, failed_phase=None) -> tuple[dict, list[str]]:
        failure = {"enabled": failed_phase is not None}
        probes: list[str] = []
        operations = self._operations_with_foreign_barrier_probes(failed_phase, failure, probes)
        with (
            mock.patch.object(cutover, "build_plan", return_value=PLAN),
            mock.patch.object(cutover, "Operations", operations),
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_sql_event_count", return_value={"committed_events": 10}),
        ):
            if failed_phase is not None:
                with self.assertRaisesRegex(cutover.CutoverError, failed_phase):
                    cutover.apply_cutover(args(), self.paths)
                failure["enabled"] = False
            result = cutover.apply_cutover(args(), self.paths)
        return result, probes

    def test_first_apply_arms_the_real_barrier_through_quiescence_import_and_activation(self) -> None:
        result, probes = self._apply_with_barrier_probes()

        self.assertEqual(result["status"], "resume-ready")
        self.assertEqual(probes, list(self.barrier_phases))

    def test_pre_freeze_failure_retry_rearms_the_real_barrier_before_any_phase(self) -> None:
        result, probes = self._apply_with_barrier_probes(
            failed_phase="current_kanboard_backup_checkpoint"
        )

        self.assertEqual(result["status"], "resume-ready")
        self.assertEqual(probes, list(self.barrier_phases))

    def test_failed_frozen_retry_keeps_the_real_barrier_armed(self) -> None:
        result, probes = self._apply_with_barrier_probes(failed_phase="writer_quiescence_proof")

        self.assertEqual(result["status"], "resume-ready")
        self.assertEqual(probes, list(self.barrier_phases))

    def test_recovered_terminal_identity_cannot_enter_any_phase_again(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["status"] = "recovered-frozen"
        state["recovery"] = {"branch": "no-cutover-effects"}
        cutover._write_state(self.paths, state)

        with (
            mock.patch.object(cutover, "Operations") as operations,
            self.assertRaisesRegex(cutover.CutoverError, "fresh plan and identity"),
        ):
            cutover.apply_cutover(args(), self.paths)
        operations.assert_not_called()

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
        state["phases"]["final_fenced_import"] = {"status": "complete"}
        state["first_sql_write"] = first_write
        cutover._write_state(self.paths, state)
        return state

    def test_post_import_before_first_write_recovery_restores_kanboard_but_stays_terminal(self) -> None:
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
        self.assertFalse(result["successor_eligibility"]["eligible"])
        self.assertEqual(
            result["successor_eligibility"]["reason"], "final-import-effect-terminal"
        )
        self.assertEqual(cutover._read_state(self.paths), result)
        self.assertEqual(cutover._read_recovered_history(self.paths), [])

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
        self.assertEqual(cutover._read_state(self.paths), result)
        self.assertEqual(cutover._read_recovered_history(self.paths), [])

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

    def test_failure_before_freeze_has_no_cutover_effect_to_recover(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["status"] = "failed"
        cutover._write_state(self.paths, state)
        recovery_args = args(confirm="RECOVER-" + "1" * 16)
        with (
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_systemctl") as systemctl,
        ):
            result = cutover.recover_cutover(recovery_args, self.paths)
        self.assertEqual(result["recovery"]["branch"], "no-cutover-effects")
        self.assertTrue(result["successor_ready"])
        self.assertIsNone(cutover._read_state(self.paths))
        systemctl.assert_not_called()

    def test_both_early_recoveries_archive_the_old_identity_and_open_a_distinct_successor(self) -> None:
        from pathlib import Path as ConcretePath

        scenarios = ("no-cutover-effects", "kanboard-before-fingerprint")
        for branch in scenarios:
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                paths = cutover.Paths(root / "instance", root / "data")
                paths.instance.mkdir(mode=0o700)
                paths.data.mkdir(mode=0o700)
                runtime = paths.instance / "runtime.env"
                runtime.write_text("SECRETARY_CARD_BACKEND=kanboard\n", encoding="utf-8")
                stable_source = {"fingerprint": "stable-source", "parity": {"ok": True}}
                provenance = {"installed_revision": REVISION}
                real_unlink = ConcretePath.unlink
                interrupt = {"enabled": True}

                def unlink(path, *unlink_args, **unlink_kwargs):
                    if path == paths.state and interrupt["enabled"]:
                        interrupt["enabled"] = False
                        raise OSError("injected canonical release interruption")
                    return real_unlink(path, *unlink_args, **unlink_kwargs)

                with (
                    mock.patch.object(cutover, "_backend", return_value="kanboard"),
                    mock.patch.object(cutover, "_provenance", return_value=provenance),
                    mock.patch.object(cutover, "_source_evidence", return_value=stable_source),
                    mock.patch.object(cutover, "_systemctl", return_value={}) as systemctl,
                ):
                    original_plan = cutover.build_plan(paths, REVISION)
                    original = cutover._new_state(
                        original_plan, args().actor, args().reason
                    )
                    original["status"] = "failed"
                    if branch == "kanboard-before-fingerprint":
                        original["status"] = "failed-frozen"
                        original["phases"]["global_freeze"] = {"status": "complete"}
                    cutover._write_state(paths, original)
                    recovery_args = args(confirm=f"RECOVER-{original_plan['plan_id'][:16]}")

                    with (
                        mock.patch("pathlib.Path.unlink", side_effect=unlink, autospec=True),
                        self.assertRaisesRegex(
                            cutover.CutoverError, "canonical cutover state"
                        ),
                    ):
                        cutover.recover_cutover(recovery_args, paths)

                    interrupted = cutover._read_state(paths)
                    self.assertEqual(interrupted["identity"], original["identity"])
                    self.assertEqual(interrupted["recovery"]["branch"], branch)
                    archive = Path(interrupted["successor"]["archive"])
                    self.assertTrue(archive.is_file())

                    events: list[str] = []
                    real_open = os.open

                    def tracked_open(path, *open_args, **open_kwargs):
                        candidate = Path(path)
                        if candidate == paths.history:
                            events.append("history-fsync-open")
                        elif candidate == paths.state.parent:
                            events.append("canonical-parent-fsync-open")
                        return real_open(path, *open_args, **open_kwargs)

                    def tracked_unlink(path, *unlink_args, **unlink_kwargs):
                        if path == paths.state:
                            events.append("canonical-unlink")
                        return real_unlink(path, *unlink_args, **unlink_kwargs)

                    with (
                        mock.patch.object(cutover.os, "open", side_effect=tracked_open),
                        mock.patch(
                            "pathlib.Path.unlink", side_effect=tracked_unlink, autospec=True
                        ),
                    ):
                        recovered = cutover.recover_cutover(recovery_args, paths)
                    self.assertTrue(recovered["successor_ready"])
                    self.assertEqual(
                        events[-3:],
                        [
                            "history-fsync-open",
                            "canonical-unlink",
                            "canonical-parent-fsync-open",
                        ],
                    )
                    self.assertIsNone(cutover._read_state(paths))
                    history = cutover._read_recovered_history(paths)
                    self.assertEqual(len(history), 1)
                    self.assertEqual(history[0]["state"], interrupted)
                    self.assertEqual(history[0]["state"]["identity"], original["identity"])
                    self.assertEqual(history[0]["state"]["recovery"]["branch"], branch)
                    self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o444)

                    successor_plan = cutover.build_plan(paths, REVISION)
                    self.assertNotEqual(successor_plan["plan_id"], original_plan["plan_id"])
                    self.assertEqual(
                        successor_plan["recovered_predecessors"][0]["identity"],
                        original["identity"],
                    )
                    with (
                        mock.patch.object(cutover, "Operations") as operations,
                        self.assertRaisesRegex(cutover.CutoverError, "confirmation token"),
                    ):
                        cutover.apply_cutover(
                            args(confirm=original_plan["confirmation"]), paths
                        )
                    operations.assert_not_called()

                    calls: list[str] = []
                    failure = {"enabled": False}
                    with (
                        mock.patch.object(
                            cutover, "Operations", fake_operations(None, calls, failure)
                        ),
                        mock.patch.object(
                            cutover,
                            "_sql_event_count",
                            return_value={"committed_events": 10},
                        ),
                    ):
                        applied = cutover.apply_cutover(
                            args(confirm=successor_plan["confirmation"]), paths
                        )
                    self.assertNotEqual(applied["identity"], original["identity"])
                    self.assertEqual(applied["status"], "resume-ready")
                    self.assertEqual(cutover._read_recovered_history(paths), history)
                    self.assertEqual(
                        systemctl.call_count,
                        0 if branch == "no-cutover-effects" else 1,
                    )

    def test_successor_eligibility_matrix_refuses_every_post_import_or_completed_state(self) -> None:
        scenarios = (
            ("import-before-activation", False, False, "final-import-effect-terminal"),
            (
                "import-entered-failed",
                False,
                False,
                "final-import-occupancy-uncertain-terminal",
            ),
            ("activation-before-write", True, False, "final-import-effect-terminal"),
            ("postgres-only", True, True, "final-import-effect-terminal"),
            ("resume-ready", True, True, "completed-cutover-terminal"),
        )
        for name, activated, sql_write, reason in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                paths = cutover.Paths(root / "instance", root / "data")
                paths.instance.mkdir(mode=0o700)
                paths.data.mkdir(mode=0o700)
                (paths.instance / "runtime.env").write_text(
                    "SECRETARY_CARD_BACKEND=kanboard\n", encoding="utf-8"
                )
                state = cutover._new_state(PLAN, args().actor, args().reason)
                state["status"] = "resume-ready" if name == "resume-ready" else "failed-frozen"
                state["phases"]["global_freeze"] = {"status": "complete"}
                state["phases"]["writer_quiescence_proof"] = {
                    "status": "complete",
                    "evidence": {"source": {"fingerprint": "source-one"}},
                }
                state["phases"]["final_fenced_import"] = {
                    "status": "failed" if name == "import-entered-failed" else "complete"
                }
                if activated:
                    state["phases"]["selector_activation"] = {
                        "status": "complete",
                        "evidence": {"sql_audit_baseline": {"committed_events": 10}},
                    }
                if sql_write:
                    state["first_sql_write"] = {
                        "recorded_at": "2026-09-09T00:00:00Z",
                        "evidence": {"committed_events": 11},
                    }
                if name == "postgres-only":
                    state["status"] = "recovered-frozen"
                    state["recovery"] = {"branch": "postgres-only"}
                cutover._write_state(paths, state)

                with (
                    mock.patch.object(cutover, "_backend", return_value="kanboard"),
                    mock.patch.object(cutover, "_provenance", return_value={}),
                    mock.patch.object(
                        cutover, "_source_evidence", return_value={"fingerprint": "source-one"}
                    ),
                    mock.patch.object(
                        cutover, "_sql_event_count", return_value={"committed_events": 10}
                    ),
                    mock.patch.object(cutover, "_set_backend", return_value={}),
                    mock.patch.object(cutover, "_systemctl", return_value={}) as systemctl,
                    mock.patch.object(cutover, "Operations") as operations,
                ):
                    if name in {
                        "import-before-activation",
                        "import-entered-failed",
                        "activation-before-write",
                    }:
                        recovery_args = args(confirm="RECOVER-" + "1" * 16)
                        state = cutover.recover_cutover(recovery_args, paths)
                        service_calls = systemctl.call_count
                        repeated = cutover.recover_cutover(recovery_args, paths)
                        self.assertEqual(repeated, state)
                        self.assertEqual(systemctl.call_count, service_calls)
                    eligibility = cutover._successor_eligibility(state)
                    self.assertFalse(eligibility["eligible"])
                    self.assertEqual(eligibility["reason"], reason)
                    with self.assertRaisesRegex(cutover.CutoverError, "canonical cutover identity"):
                        cutover.build_plan(paths, REVISION)
                    with self.assertRaisesRegex(cutover.CutoverError, "terminal cutover identity"):
                        cutover.apply_cutover(args(), paths)
                self.assertEqual(
                    systemctl.call_count,
                    1
                    if name
                    in {
                        "import-before-activation",
                        "import-entered-failed",
                        "activation-before-write",
                    }
                    else 0,
                )
                operations.assert_not_called()
                self.assertEqual(cutover._read_state(paths), state)
                self.assertEqual(cutover._read_recovered_history(paths), [])


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
            (root / "board-store.env").write_text(
                "SECRETARY_CARD_BACKEND=postgres\nSECRETARY_DB_HOST=present\n", encoding="utf-8"
            )
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
