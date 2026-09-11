from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import cutover
from secretary.board import provision
from secretary.cutover import successor

REVISION = "a" * 40


def inventory(absent: tuple[str, ...] = ()) -> cutover.UnitInventory:
    return cutover.UnitInventory(
        {
            declaration.name: (
                cutover.ABSENT_LOAD_STATE if declaration.name in absent else "loaded"
            )
            for declaration in cutover.DECLARED_UNITS
        }
    )


# A host that has every precondition installed, in the shape the real
# `_privileged_preconditions` reports it — including the `units` section, which is a fact
# about systemd the fixture answers so no unit test consults the machine it runs on.
# `PrivilegedPreconditionTests` compares this against the real report so the two cannot
# drift again: a fixture that omits a key the product returns is a test passing on a report
# the product never produces.
INSTALLED_PRECONDITIONS = {
    "satisfied": True,
    "compose": {
        "requirement": "compose",
        "path": str(provision.DEFAULT_COMPOSE_PATH),
        "satisfied": True,
        "detail": "installed",
        "status": "ok",
    },
    "sudo_systemctl": {
        "requirement": "sudo",
        "command": ["sudo", "-n", "systemctl", "show", "--property=Version"],
        "user": "secretary",
        "satisfied": True,
        "detail": "works",
    },
    "units": {
        "requirement": "installed required consumer units",
        "satisfied": True,
        "detail": "every declared unit is installed",
        **inventory().evidence(),
    },
    "pipeline_pause": {
        "requirement": "pause",
        "source": "secretary pause-status, judged by backup create",
        "phase": "current_kanboard_backup_checkpoint",
        "freeze_owner": None,
        "pause": {"paused": False, "mode": None, "actor": None, "reason": None},
        "resume_command": None,
        "satisfied": True,
        "detail": "not paused",
    },
    "doctor": {
        "requirement": "doctor",
        "command": "python3 -P -m secretary doctor --json --offline --instance /instance",
        "findings": [],
        "satisfied": True,
        "detail": "no findings",
    },
    "root_commands": [],
}
PLAN = {
    "version": 1,
    "plan_id": "1" * 64,
    "confirmation": "CUTOVER-" + "1" * 16,
    "expected_revision": REVISION,
}
PRODUCT_ROOT = "/opt/secretary/product"
# The components this installation deliberately leaves out: their units were
# never installed, so systemd answers LoadState=not-found for each of them.
ABSENT_OPTIONAL = (
    "secretary-steward.timer",
    "secretary-steward.service",
    "secretary-steward-deep-sweep.timer",
    "secretary-steward-deep-sweep.service",
    "secretary-retro.timer",
    "secretary-retro.service",
)


def shape(value):
    """The keys of a report and the types under them, recursively, without its values."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return type(value).__name__


def systemd_installation(commands: list[list[str]], absent: tuple[str, ...] = ()):
    """A stand-in systemd: declared units answer, the absent ones as not-found.

    Every command the controller issues is recorded, so a test can prove which
    units were read, stopped, started and restarted without a live systemd,
    a live sudo or a mutation of either.
    """

    def run(argv, **_kwargs):
        commands.append(list(argv))
        command = [item for item in argv if item not in ("sudo", "-n")]
        if Path(command[0]).name != "systemctl":
            return subprocess.CompletedProcess(argv, 0, '{"status":"ok"}\n', "")
        if command[1] == "show" and command[-1] == "--property=Version":
            return subprocess.CompletedProcess(argv, 0, "Version=255.4\n", "")
        if command[1] == "show":
            if command[2] in absent:
                return subprocess.CompletedProcess(argv, 0, "LoadState=not-found\n", "")
            return subprocess.CompletedProcess(
                argv, 0, "LoadState=loaded\nActiveState=active\nSubState=running\n", ""
            )
        if command[1] == "cat":
            return subprocess.CompletedProcess(
                argv, 0, f"ExecStart={PRODUCT_ROOT}/bin/secretary\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


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
        # Both privileged apply preconditions are facts about the host. No unit
        # test may consult a live sudo or systemd, so the fixture answers them
        # and PrivilegedPreconditionTests drives the real predicate instead.
        self._preconditions = mock.patch.object(
            cutover, "_privileged_preconditions", return_value=INSTALLED_PRECONDITIONS
        )
        self._preconditions.start()
        self.addCleanup(self.host_preconditions)
        # What systemd has installed is a fact about the host as well.  The
        # fixture answers it with a complete installation and
        # InstalledUnitInventoryTests drives the real reader instead.
        self._inventory = mock.patch.object(cutover, "inventory_units", return_value=inventory())
        self._inventory.start()
        self.addCleanup(self.host_unit_inventory)
        # The pipeline pause and doctor --offline are facts about the installation, read by
        # running its commands.  The fixture answers both as satisfied and
        # InstallationPreconditionTests drives the real readers instead.
        self._installation = mock.patch.multiple(
            cutover,
            _pipeline_pause_precondition=mock.Mock(return_value=INSTALLED_PRECONDITIONS["pipeline_pause"]),
            _doctor_precondition=mock.Mock(return_value=INSTALLED_PRECONDITIONS["doctor"]),
        )
        self._installation.start()
        self.addCleanup(self.installation_preconditions)
        # Free space on the backups volume is a fact about the host as well.  The fixture answers
        # it as ample and BackupSpaceTests sets it per case.
        volume = mock.patch.object(cutover, "_volume_free_bytes", return_value=1 << 50)
        volume.start()
        self.addCleanup(volume.stop)
        # `_serve_backend` is a real process-wide switch, and both controller entrances now
        # reach it.  A case that runs one must not leave the name exported or the decision
        # made for the next case in this interpreter.
        from secretary.board.backend import reset_card_backend

        environment = mock.patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(reset_card_backend)

    def host_preconditions(self) -> None:
        """Stop answering for the host so a test can drive the real predicate."""
        if self._preconditions is not None:
            self._preconditions.stop()
            self._preconditions = None

    def host_unit_inventory(self) -> None:
        """Stop answering for systemd so a test can inventory the declared units."""
        if self._inventory is not None:
            self._inventory.stop()
            self._inventory = None

    def installation_preconditions(self) -> None:
        """Stop answering for the pause and doctor so a test can drive the real readers."""
        if self._installation is not None:
            self._installation.stop()
            self._installation = None

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

    def test_successor_eligibility_is_exact_and_activation_needs_its_writeless_proof(self) -> None:
        state = self.eligible_state()
        self.assertTrue(successor.exact_eligibility(state, "kanboard")["eligible"])
        # An activation that never completed, or completed without recording the audit
        # baseline `recover` compared against, leaves no proof that no SQL write happened.
        for activation in (
            {"status": "running"},
            {"status": "complete"},
            {"status": "complete", "evidence": {"sql_audit_baseline": {"committed_events": "5"}}},
        ):
            with self.subTest(activation=activation):
                state = self.eligible_state()
                state["phases"]["selector_activation"] = activation
                proof = successor.exact_eligibility(state, "kanboard")
                self.assertFalse(proof["eligible"])
                self.assertEqual(proof["reason"], "selector-activation-uncertain")
        # The 2026-09-10 live shape: selector activated, acceptance refused, `recover` read the
        # SQL audit back at the baseline and restored Kanboard before any write.
        state = self.eligible_state()
        state["phases"]["selector_activation"] = {
            "status": "complete",
            "evidence": {"backend": "postgres", "sql_audit_baseline": {"committed_events": 5922}},
        }
        self.assertTrue(successor.exact_eligibility(state, "kanboard")["eligible"])
        state["first_sql_write"] = {"recorded_at": "2026-09-10T11:00:00Z", "evidence": {}}
        proof = successor.exact_eligibility(state, "kanboard")
        self.assertFalse(proof["eligible"])
        self.assertEqual(proof["reason"], "sql-write-or-audit-uncertainty")

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


    def test_acceptance_opens_and_closes_its_own_canary_sprint_when_none_is_open(self) -> None:
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
                document = {"sprints": {"items": [{"ref": "sprint:9", "status": "closed", "reservations": ["secretary"]}]}}
            elif argv[:2] == ("sprint", "create"):
                self.assertIn("--observer", argv)
                self.assertEqual(argv[argv.index("--observer") + 1], "none")
                self.assertEqual(argv[argv.index("--project") + 1], "secretary")
                self.assertEqual(argv[argv.index("--issue") + 1], "issue:acceptance")
                document = {"sprint": {"ref": "sprint:77"}}
            elif argv[:2] == ("issue", "create"):
                document = {"issue": {"ref": "issue:acceptance"}}
            elif argv[:2] == ("task", "create"):
                self.assertEqual(argv[argv.index("--sprint") + 1], "sprint:77")
                document = {"task": {"ref": "secretary-99"}}
            elif argv[:2] == ("sprint", "comment"):
                self.assertEqual(argv[argv.index("--ref") + 1], "sprint:77")
                document = {"comment_id": "evt-sprint", "saved": repeats[key] == 1}
            elif argv[:2] == ("sprint", "close"):
                self.assertEqual(argv[argv.index("--ref") + 1], "sprint:77")
                decisions = Path(argv[argv.index("--decisions-file") + 1]).read_text(encoding="utf-8")
                self.assertIn("issue:acceptance", decisions)
                self.assertIn("already_closed", decisions)
                document = {"kind": "sprint_closed"}
            elif argv[:2] == ("task", "comment") and "cutover-task-comment-" in key:
                document = {"event_id": "evt-task", "replayed": repeats[key] > 1}
            return {"output": json.dumps(document), "document": document}

        with (
            mock.patch.object(cutover, "_secretary", side_effect=command),
            mock.patch.object(cutover, "_acceptance_project", return_value="secretary"),
            mock.patch.object(cutover, "_sql_event_count", return_value={"committed_events": 20}),
        ):
            evidence = operation.installed_protocol_acceptance()

        order = [entry[:2] for entry in seen]
        self.assertLess(order.index(("issue", "create")), order.index(("sprint", "create")))
        self.assertLess(order.index(("sprint", "create")), order.index(("sprint", "comment")))
        self.assertLess(order.index(("task", "archive")), order.index(("sprint", "close")))
        self.assertLess(order.index(("issue", "close")), order.index(("sprint", "close")))
        self.assertTrue(evidence["sprint"]["canary"])
        self.assertEqual(evidence["sprint"]["ref"], "sprint:77")
        self.assertIsNotNone(evidence["sprint"]["closed"])

    def test_acceptance_borrows_an_open_sprint_and_opens_no_canary(self) -> None:
        foreign = {"ref": "sprint:1", "status": "open", "reservations": ["p"], "product": "secretary"}
        own = {"ref": "sprint:2", "status": "open", "reservations": ["q"], "product": "cutover-abc"}
        self.assertEqual(
            cutover._acceptance_context({"sprints": {"items": [foreign]}}, "cutover-abc"),
            ("sprint:1", "p", False),
        )
        self.assertEqual(cutover._acceptance_context({"sprints": {"items": []}}, "cutover-abc"), (None, None, True))
        # A retry after the canary sprint was created finds it open and still owns it.
        self.assertEqual(
            cutover._acceptance_context({"sprints": {"items": [foreign, own]}}, "cutover-abc"),
            ("sprint:2", "q", True),
        )
        with self.assertRaises(cutover.CutoverError):
            cutover._acceptance_context([], "cutover-abc")

    def test_acceptance_retry_reuses_and_closes_its_own_open_canary_sprint(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        operation = cutover.Operations(self.paths, state)
        prefix = state["plan_id"][:16]
        seen: list[tuple[str, ...]] = []
        repeats: dict[str, int] = {}

        def command(_paths, *argv):
            seen.append(argv)
            key = " ".join(argv)
            repeats[key] = repeats.get(key, 0) + 1
            document: object = {"ok": True}
            if argv[:2] == ("sprint", "list"):
                document = {"sprints": {"items": [
                    {"ref": "sprint:77", "status": "open", "reservations": ["secretary"], "product": f"cutover-{prefix}"}
                ]}}
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
        pairs = [entry[:2] for entry in seen]
        self.assertNotIn(("sprint", "create"), pairs)
        self.assertIn(("sprint", "close"), pairs)
        self.assertTrue(evidence["sprint"]["canary"])
        self.assertIsNone(evidence["sprint"]["created"])
        self.assertEqual(evidence["sprint"]["ref"], "sprint:77")


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

    def _serving_kanboard(self) -> None:
        """Warm the process the way the phases before the recovery backup warm it.

        `global_freeze`, `writer_quiescence_proof` and the fenced import all read the live
        Kanboard board first, so by the time the controller reaches the recovery backup the
        per-process decision has already been made and only `reset_card_backend()` can revise it.
        """
        from secretary.board.backend import card_backend, reset_card_backend

        self.addCleanup(reset_card_backend)
        self.addCleanup(os.environ.pop, cutover.BACKEND_ENV, None)
        os.environ[cutover.BACKEND_ENV] = "kanboard"
        reset_card_backend()
        self.assertEqual(card_backend(), "kanboard")

    def test_backup_failure_surfaces_and_restores_the_process_selector(self) -> None:
        from secretary.board.backend import card_backend

        self._serving_kanboard()
        with (
            mock.patch("secretary.backup.create_backups", side_effect=RuntimeError("dump failed")),
            self.assertRaisesRegex(RuntimeError, "dump failed"),
        ):
            self.operation().postgresql_recovery_backup()
        self.assertEqual(os.environ[cutover.BACKEND_ENV], "kanboard")
        self.assertEqual(card_backend(), "kanboard")

    def test_recovery_backup_serves_sql_although_earlier_phases_decided_kanboard(self) -> None:
        """The 09.09 defect: the environment moved, the process decision did not.

        `create_backups` asks `card_backend()` for the engine it is archiving, so a phase that
        exports `postgres` without revising the decision takes a Kanboard backup where the
        recovery point of the whole cutover has to be the SQL one.
        """
        from secretary.board.backend import card_backend

        self._serving_kanboard()
        served: list[str] = []

        def backup(*_args, **_kwargs):
            served.append(card_backend())
            return [
                SimpleNamespace(
                    archive=Path("full.tar"), manifest={"components": {"postgres_dump": {}}}
                )
            ]

        with mock.patch("secretary.backup.create_backups", side_effect=backup):
            evidence = self.operation().postgresql_recovery_backup()
        self.assertEqual(served, ["postgres"])
        self.assertEqual(evidence["archive_manifests"], [{"components": {"postgres_dump": {}}}])
        # The other half: the phase runs before the selector is activated, so the next phase must
        # not inherit PostgreSQL from it -- neither in the environment nor in the decision.
        self.assertEqual(os.environ[cutover.BACKEND_ENV], "kanboard")
        self.assertEqual(card_backend(), "kanboard")

    def test_recovery_backup_rejects_raw_board_and_requires_postgres_dump(self) -> None:
        raw = SimpleNamespace(archive=Path("raw.tar"), manifest={"components": {"raw_board": {}}})
        with (
            mock.patch("secretary.backup.create_backups", return_value=[raw]),
            self.assertRaisesRegex(cutover.CutoverError, "raw Kanboard"),
        ):
            self.operation().postgresql_recovery_backup()

        empty = SimpleNamespace(archive=Path("empty.tar"), manifest={"components": {}})
        with (
            mock.patch("secretary.backup.create_backups", return_value=[empty]),
            self.assertRaisesRegex(cutover.CutoverError, "no PostgreSQL dump"),
        ):
            self.operation().postgresql_recovery_backup()

    def test_selector_activation_leaves_the_process_serving_sql(self) -> None:
        from secretary.board.backend import card_backend

        self._serving_kanboard()
        with mock.patch.object(
            cutover, "_sql_event_count", return_value={"committed_events": 3}
        ):
            evidence = self.operation().selector_activation()
        self.assertEqual(evidence["backend"], "postgres")
        self.assertEqual(os.environ[cutover.BACKEND_ENV], "postgres")
        self.assertEqual(card_backend(), "postgres")
        self.assertIn("SECRETARY_CARD_BACKEND=postgres", self.runtime.read_text(encoding="utf-8"))

    def test_the_controller_has_one_named_way_to_change_the_process_backend(self) -> None:
        """No phase may export the name past `reset_card_backend()`'s back.

        Three inline pairs of `os.environ[...] = ...` were what let one of them forget the second
        half.  `_serve_backend` is the single place that writes the name, and the assertion is on
        the controller's own source rather than on one phase, so a fourth site cannot appear
        quietly.
        """
        source = Path(cutover.__file__).read_text(encoding="utf-8")
        writes = [
            line.strip()
            for line in source.splitlines()
            if "os.environ[BACKEND_ENV]" in line or "os.environ.pop(BACKEND_ENV" in line
        ]
        self.assertEqual(
            writes,
            ["os.environ.pop(BACKEND_ENV, None)", "os.environ[BACKEND_ENV] = backend"],
        )

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

    def test_both_recovery_branches_move_the_decision_through_the_one_named_switch(self) -> None:
        """Recovery is the controller's second entrance and it changes the backend the same way.

        It used to move only the durable selector in `runtime.env`.  Nothing it does afterwards
        asks `card_backend()` today, so the two did not visibly disagree — but "one named switch"
        has to be true on both entrances, or the next step added to a recovery branch inherits a
        process decision the selector it just wrote contradicts.  That is the defect apply had
        removed one card earlier, reappearing here.
        """
        from secretary.board.backend import card_backend, reset_card_backend

        # Each case starts the process serving the backend the branch has to move it off, which
        # is the realistic one: the operator's recover command binds to the selector apply left
        # behind, and the branch is the thing that revises it.
        for branch, committed, served, backend in (
            ("kanboard-before-first-write", 10, "postgres", "kanboard"),
            ("postgres-only", 11, "kanboard", "postgres"),
        ):
            with self.subTest(branch=branch):
                os.environ[cutover.BACKEND_ENV] = served
                reset_card_backend()
                self.assertEqual(card_backend(), served)
                self.state()
                with (
                    mock.patch.object(cutover, "_provenance", return_value={}),
                    mock.patch.object(
                        cutover, "_sql_event_count", return_value={"committed_events": committed}
                    ),
                    mock.patch.object(
                        cutover, "_source_evidence", return_value={"fingerprint": "source-one"}
                    ),
                    mock.patch.object(cutover, "_reconcile_postgres", return_value={}),
                    mock.patch.object(cutover, "_backend", return_value="kanboard"),
                    mock.patch.object(cutover, "_set_backend", return_value={}) as selector,
                    mock.patch.object(cutover, "_systemctl", return_value={}),
                ):
                    result = cutover.recover_cutover(
                        args(confirm="RECOVER-" + "1" * 16), self.paths
                    )

                self.assertEqual(result["recovery"]["branch"], branch)
                selector.assert_called_once_with(self.paths, backend)
                self.assertEqual(os.environ[cutover.BACKEND_ENV], backend)
                self.assertEqual(card_backend(), backend)

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


class PrivilegedPreconditionTests(CutoverFixture):
    """The two install steps only root can take, checked before the first mutation."""

    def setUp(self) -> None:
        super().setUp()
        self.host_preconditions()
        self.compose = Path(self.temporary.name) / "opt" / "postgres-compose.yml"
        self.compose.parent.mkdir(mode=0o755)

    def install_compose(self) -> None:
        self.compose.write_text(provision.COMPOSE_TEXT, encoding="utf-8")
        self.compose.chmod(0o600)

    def sudo(self, *, allowed: bool):
        result = subprocess.CompletedProcess(
            [],
            0 if allowed else 1,
            "Version=255.4\n" if allowed else "",
            "" if allowed else "sudo: a password is required\n",
        )
        return mock.patch.object(cutover.subprocess, "run", return_value=result)

    def test_compose_predicate_is_the_provisioners_own_predicate(self) -> None:
        def drift() -> None:
            self.compose.write_text("services: {}\n", encoding="utf-8")
            self.compose.chmod(0o600)

        def permissions() -> None:
            self.install_compose()
            self.compose.chmod(0o644)

        def not_regular() -> None:
            target = self.compose.with_name("elsewhere.yml")
            target.write_text(provision.COMPOSE_TEXT, encoding="utf-8")
            self.compose.symlink_to(target)

        cases = {
            "missing": lambda: None,
            "drift": drift,
            "permissions": permissions,
            "not-regular": not_regular,
            "ok": self.install_compose,
        }
        for status, prepare in cases.items():
            with self.subTest(status=status):
                self.compose.unlink(missing_ok=True)
                prepare()
                with self.sudo(allowed=True):
                    report = cutover._privileged_preconditions(self.compose, paths=self.paths)
                self.assertEqual(report["compose"]["status"], status)
                self.assertEqual(report["compose"]["satisfied"], status == "ok")
                self.assertEqual(report["satisfied"], status == "ok")
                self.assertEqual(
                    provision.inspect_compose(self.compose).status,
                    report["compose"]["status"],
                )

    def test_sudo_probe_is_read_only_and_reports_a_denied_rule(self) -> None:
        self.install_compose()
        with self.sudo(allowed=True) as run:
            report = cutover._privileged_preconditions(self.compose, paths=self.paths)
        self.assertEqual(
            run.call_args.args[0], ["sudo", "-n", "systemctl", "show", "--property=Version"]
        )
        self.assertTrue(report["sudo_systemctl"]["satisfied"])
        self.assertTrue(report["satisfied"])
        self.assertEqual(report["root_commands"], [])

        with self.sudo(allowed=False):
            denied = cutover._privileged_preconditions(self.compose, paths=self.paths)
        self.assertFalse(denied["sudo_systemctl"]["satisfied"])
        self.assertFalse(denied["satisfied"])
        self.assertIn("password is required", denied["sudo_systemctl"]["detail"])

    def test_refusal_names_both_root_commands_and_prints_no_stored_credential(self) -> None:
        secret = self.instance / "board-store.env"
        secret.write_text("SECRETARY_DB_OWNER_PASSWORD=owner-secret\n", encoding="utf-8")
        with self.sudo(allowed=False), self.assertRaises(cutover.CutoverError) as refusal:
            cutover._require_privileged_preconditions(self.compose, paths=self.paths)

        message = str(refusal.exception)
        self.assertIn("performs no root step", message)
        user, group = cutover._runtime_identity()
        self.assertIn(f"install -m 0600 -o {user} -g {group}", message)
        self.assertIn(str(self.compose), message)
        self.assertIn(provision.COMPOSE_TEXT, message)
        self.assertIn("NOPASSWD: /usr/bin/systemctl", message)
        self.assertIn(f"visudo -cf {cutover.SUDOERS_DROPIN}", message)
        self.assertNotIn("owner-secret", message)

    def test_every_unmet_precondition_refuses_a_new_apply_before_the_state_file_exists(self) -> None:
        scenarios = {
            "missing": (lambda: None, True),
            "drift": (lambda: self.compose.write_text("services: {}\n", encoding="utf-8"), True),
            "permissions": (self.install_compose, True),
            "sudo": (self.install_compose, False),
        }
        for name, (prepare, allowed) in scenarios.items():
            with self.subTest(precondition=name):
                self.compose.unlink(missing_ok=True)
                prepare()
                if name == "permissions":
                    self.compose.chmod(0o644)
                with (
                    mock.patch.object(provision, "DEFAULT_COMPOSE_PATH", self.compose),
                    self.sudo(allowed=allowed),
                    mock.patch.object(cutover, "build_plan") as plan,
                    mock.patch.object(cutover, "Operations") as operations,
                    self.assertRaisesRegex(cutover.CutoverError, "privileged apply preconditions"),
                ):
                    cutover.apply_cutover(args(), self.paths)
                plan.assert_not_called()
                operations.assert_not_called()
                self.assertFalse(self.paths.state.exists())
                self.assertIsNone(cutover._read_state(self.paths))

    def test_retry_refusal_leaves_the_durable_state_byte_and_mtime_identical(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["phases"]["preflight"] = {"status": "complete"}
        cutover._write_state(self.paths, state)
        before = self.paths.state.read_bytes()
        stamp = self.paths.state.stat().st_mtime_ns

        with (
            mock.patch.object(provision, "DEFAULT_COMPOSE_PATH", self.compose),
            self.sudo(allowed=True),
            mock.patch.object(cutover, "Operations") as operations,
            self.assertRaisesRegex(cutover.CutoverError, "privileged apply preconditions"),
        ):
            cutover.apply_cutover(args(), self.paths)

        operations.assert_not_called()
        self.assertEqual(self.paths.state.read_bytes(), before)
        self.assertEqual(self.paths.state.stat().st_mtime_ns, stamp)

    def test_installed_preconditions_let_a_new_apply_reach_every_phase(self) -> None:
        self.install_compose()
        calls: list[str] = []
        with (
            mock.patch.object(provision, "DEFAULT_COMPOSE_PATH", self.compose),
            self.sudo(allowed=True),
            mock.patch.object(cutover, "build_plan", return_value=PLAN),
            mock.patch.object(cutover, "Operations", fake_operations(None, calls, {"enabled": False})),
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
        ):
            result = cutover.apply_cutover(args(), self.paths)

        self.assertEqual(result["status"], "resume-ready")
        self.assertEqual(calls, list(cutover.PHASES))

    def test_the_installed_fixture_reports_the_shape_the_real_predicate_returns(self) -> None:
        """A key the fixture omits is a report no installation ever produces.

        `INSTALLED_PRECONDITIONS` answers for the host in every case built on `CutoverFixture`,
        and it lost a whole section when secretary-1609 added `units` to the real report: the
        suite went on reading a two-section report while apply refused on a three-section one.
        Nothing failed, because only the unsatisfied branch reaches the missing key — which is
        exactly why the agreement is asserted here rather than read off the two literals.  The
        comparison is on the keys and the value types, recursively; the values themselves are
        facts about a host and legitimately differ.
        """
        self.install_compose()
        with self.sudo(allowed=True):
            report = cutover._privileged_preconditions(self.compose, paths=self.paths)

        self.assertTrue(report["satisfied"])
        self.assertEqual(shape(INSTALLED_PRECONDITIONS), shape(report))

    def test_plan_shows_both_preconditions_and_the_root_commands_without_failing(self) -> None:
        buffer = StringIO()
        with (
            mock.patch.object(cutover, "resolve_paths", return_value=self.paths),
            mock.patch.object(cutover, "build_plan", return_value=dict(PLAN)),
            mock.patch.object(provision, "DEFAULT_COMPOSE_PATH", self.compose),
            self.sudo(allowed=False),
            redirect_stdout(buffer),
        ):
            code = cutover.run_cutover(
                argparse.Namespace(
                    cutover_command="plan",
                    instance=str(self.instance),
                    expected_revision=REVISION,
                )
            )

        self.assertEqual(code, 0)
        document = json.loads(buffer.getvalue())
        self.assertEqual(document["confirmation"], PLAN["confirmation"])
        preconditions = document["privileged_preconditions"]
        self.assertFalse(preconditions["satisfied"])
        self.assertFalse(preconditions["compose"]["satisfied"])
        self.assertFalse(preconditions["sudo_systemctl"]["satisfied"])
        self.assertTrue(
            any("NOPASSWD: /usr/bin/systemctl" in command for command in preconditions["root_commands"])
        )
        self.assertTrue(
            any(str(self.compose) in command for command in preconditions["root_commands"])
        )

    def test_service_commands_are_executed_through_the_non_interactive_sudo_contour(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(cutover.subprocess, "run", return_value=completed) as run:
            evidence = cutover._systemctl("stop", ("secretary-web.service",))

        self.assertEqual(
            run.call_args.args[0], ["sudo", "-n", "systemctl", "stop", "secretary-web.service"]
        )
        self.assertEqual(evidence["privileged"], "sudo -n")
        self.assertEqual(
            evidence["results"][0]["command"],
            ["sudo", "-n", "systemctl", "stop", "secretary-web.service"],
        )
        from secretary.host_apply import SystemdUnitInstaller

        self.assertEqual(
            SystemdUnitInstaller().argv(["systemctl", "restart", "unit"]),
            ["sudo", "-n", "systemctl", "restart", "unit"],
        )

    def test_early_recovery_restarts_consumers_through_the_same_sudo_contour(self) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["status"] = "failed-frozen"
        state["phases"]["global_freeze"] = {"status": "complete"}
        cutover._write_state(self.paths, state)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_source_evidence", return_value={"fingerprint": "source"}),
            mock.patch.object(cutover.subprocess, "run", return_value=completed) as run,
        ):
            result = cutover.recover_cutover(args(confirm="RECOVER-" + "1" * 16), self.paths)

        self.assertEqual(result["recovery"]["branch"], "kanboard-before-fingerprint")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), len(cutover.START_UNITS))
        for command, unit in zip(commands, cutover.START_UNITS, strict=True):
            self.assertEqual(command, ["sudo", "-n", "systemctl", "restart", unit])


RUNNING = {"paused": False, "mode": None, "actor": None, "pause_reason": None}
DOCTOR_CLEAN = {"schema_version": 1, "ok": True, "findings": [], "status": {}}
DOCTOR_FINDINGS = {
    "schema_version": 1,
    "ok": False,
    "findings": [
        {"code": "secret_store", "message": "retired catalog entry is still present: kanboard_api_token"},
        {"code": "secret_store", "message": "retired catalog entry is still present: kanboard_api_user"},
        {"code": "secret_store", "message": "retired catalog entry is still present: kanboard_api_url"},
        {"code": "recovery_credential", "resource": "runtime", "message": "runtime credential is not materialized"},
    ],
    "status": {},
}
# How the refusal names each of those findings: doctor's code and message, then its other fields.
DOCTOR_FINDING_LINES = [
    "secret_store: retired catalog entry is still present: kanboard_api_token",
    "secret_store: retired catalog entry is still present: kanboard_api_user",
    "secret_store: retired catalog entry is still present: kanboard_api_url",
    "recovery_credential: runtime credential is not materialized; resource=runtime",
]
THROUGH_CHECKPOINT = ("preflight", "current_kanboard_backup_checkpoint")
THROUGH_FREEZE = (*THROUGH_CHECKPOINT, "postgresql_provision_migration_verification", "global_freeze")


def frozen(actor: str, reason: str = "approved maintenance window") -> dict[str, object]:
    return {"paused": True, "mode": "freeze", "actor": actor, "pause_reason": reason}


class BackupSpaceTests(CutoverFixture):
    """Plan and a new apply refuse a backups volume that cannot hold the window's archives.

    The window's last backup ran out of space on 2026-09-10, after the freeze and the switch.
    Free bytes are answered per case; the archive size is the real estimate over a data dir
    whose model cache would, if it were counted, dwarf everything else.
    """

    CACHE_BYTES = 4 * 1024 * 1024

    def setUp(self) -> None:
        super().setUp()
        orca = mock.patch("secretary.backup.ORCA_STATE_DIRS", ())
        orca.start()
        self.addCleanup(orca.stop)
        memory = self.data / "memory"
        cache = memory / "fastembed-cache" / "models--bge-m3"
        cache.mkdir(parents=True)
        (cache / "model.onnx").write_bytes(b"\0" * self.CACHE_BYTES)
        (memory / "export.ndjson").write_text("{}\n" * 20000, encoding="utf-8")
        self.space = cutover._backup_space(self.paths)

    def plan(self) -> tuple[int, dict]:
        buffer = StringIO()
        with (
            mock.patch.object(cutover, "resolve_paths", return_value=self.paths),
            mock.patch.object(cutover, "build_plan", return_value=dict(PLAN)),
            redirect_stdout(buffer),
        ):
            code = cutover.run_cutover(
                argparse.Namespace(
                    cutover_command="plan",
                    instance=str(self.instance),
                    expected_revision=REVISION,
                )
            )
        return code, json.loads(buffer.getvalue())

    def test_the_window_needs_every_archive_it_writes_and_one_staging_copy(self) -> None:
        self.assertEqual(self.space["archives"], len(cutover.BACKUP_PAUSE_PHASES))
        self.assertEqual(self.space["archives"], 3)
        self.assertEqual(self.space["staging_copies"], 1)
        self.assertEqual(self.space["required_bytes"], 4 * self.space["archive_bytes"])
        self.assertEqual(self.space["backups_dir"], str(self.data / "backups"))
        # No backup has run yet, so the nearest existing directory stands for the volume.
        cutover._volume_free_bytes.assert_called_with(self.data)
        # The estimate leaves out what the archive leaves out: the export is in it, the cache is not.
        self.assertGreater(self.space["archive_bytes"], 60000)
        self.assertLess(self.space["archive_bytes"], self.CACHE_BYTES)

    def test_plan_refuses_and_names_the_volume_the_bytes_and_the_archives(self) -> None:
        free = self.space["required_bytes"] - 1
        with mock.patch.object(cutover, "_volume_free_bytes", return_value=free):
            code, document = self.plan()

        self.assertEqual(code, 1)
        self.assertFalse(document["ok"])
        error = document["error"]
        self.assertIn(f"on volume {self.space['volume']} ", error)
        self.assertIn(f"{free} bytes free", error)
        self.assertIn(f"{self.space['required_bytes']} bytes required", error)
        self.assertIn("for 3 full archives", error)
        self.assertNotIn("privileged_preconditions", document)
        self.assertFalse(self.paths.state.exists())

    def test_plan_with_room_for_the_window_is_the_plan_as_before(self) -> None:
        with mock.patch.object(cutover, "_volume_free_bytes", return_value=self.space["required_bytes"]):
            code, document = self.plan()

        self.assertEqual(code, 0)
        self.assertEqual(document, {**PLAN, "privileged_preconditions": INSTALLED_PRECONDITIONS})
        self.assertFalse(self.paths.state.exists())

    def test_a_new_apply_refuses_on_the_same_decision_before_the_state_file_exists(self) -> None:
        free = self.space["required_bytes"] - 1
        with mock.patch.object(cutover, "_volume_free_bytes", return_value=free):
            _code, document = self.plan()
            with (
                mock.patch.object(cutover, "build_plan", return_value=dict(PLAN)),
                mock.patch.object(cutover, "Operations") as operations,
                self.assertRaises(cutover.CutoverError) as refusal,
            ):
                cutover.apply_cutover(args(), self.paths)

        self.assertEqual(str(refusal.exception), document["error"])
        operations.assert_not_called()
        self.assertFalse(self.paths.state.exists())
        self.assertIsNone(cutover._read_state(self.paths))


class InstallationPreconditionTests(CutoverFixture):
    """The pipeline pause and doctor --offline, proven on the seam of the privileged steps.

    Apply #6 and #7 of the 2026-09-10 acceptance found both in the middle of the window: a
    freeze `recover` had left behind refused the Kanboard checkpoint backup, and a doctor
    with findings refused installed acceptance after freeze, import and the selector switch.
    The pause is read through `backup create`'s own reader and judged by its own predicate;
    doctor runs as the acceptance phase runs it.  Only the command boundary is faked.
    """

    def setUp(self) -> None:
        super().setUp()
        self.host_preconditions()
        self.installation_preconditions()
        compose = Path(self.temporary.name) / "opt" / "postgres-compose.yml"
        compose.parent.mkdir(mode=0o755)
        compose.write_text(provision.COMPOSE_TEXT, encoding="utf-8")
        compose.chmod(0o600)
        self.commands: list[list[str]] = []
        self.pause_reads: list[list[str]] = []
        self.doctor: tuple[int, object] | BaseException = (0, DOCTOR_CLEAN)
        self.pause: dict[str, object] | BaseException = RUNNING
        for patcher in (
            mock.patch.object(provision, "DEFAULT_COMPOSE_PATH", compose),
            mock.patch.object(cutover.subprocess, "run", side_effect=self.host_run),
            mock.patch("secretary.backup._proc.run", side_effect=self.pause_run),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def host_run(self, argv, **_kwargs):
        """sudo answers its probe, doctor answers `self.doctor`, any other command succeeds."""
        self.commands.append(list(argv))
        if "doctor" in argv:
            if isinstance(self.doctor, BaseException):
                raise self.doctor
            code, document = self.doctor
            return subprocess.CompletedProcess(argv, code, json.dumps(document) + "\n", "")
        if list(argv[:2]) == ["sudo", "-n"]:
            return subprocess.CompletedProcess(argv, 0, "Version=255.4\n", "")
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}\n', "")

    def pause_run(self, argv, **_kwargs):
        """`secretary pause-status` answering with the pause protocol document."""
        self.pause_reads.append(list(argv))
        if isinstance(self.pause, BaseException):
            raise self.pause
        return subprocess.CompletedProcess(argv, 0, json.dumps({"state": self.pause}), "")

    def doctor_argv(self) -> list[str]:
        return [sys.executable, "-P", "-m", "secretary", "doctor", "--json", "--offline", "--instance", str(self.instance)]

    def resume_command(self) -> str:
        return f"secretary resume --instance {shlex.quote(str(self.instance))}"

    def retry_state(self, complete: tuple[str, ...]) -> None:
        state = cutover._new_state(PLAN, args().actor, args().reason)
        for name in complete:
            state["phases"][name] = {"status": "complete"}
        state["status"] = "failed-frozen" if "global_freeze" in complete else "failed"
        cutover._write_state(self.paths, state)

    def refused(self) -> str:
        with (
            mock.patch.object(cutover, "build_plan") as plan,
            mock.patch.object(cutover, "Operations") as operations,
            self.assertRaisesRegex(cutover.CutoverError, "privileged apply preconditions") as refusal,
        ):
            cutover.apply_cutover(args(), self.paths)
        plan.assert_not_called()
        operations.assert_not_called()
        return str(refusal.exception)

    def refused_retry(self, complete: tuple[str, ...]) -> str:
        self.retry_state(complete)
        before = self.paths.state.read_bytes()
        stamp = self.paths.state.stat().st_mtime_ns
        message = self.refused()
        self.assertEqual(self.paths.state.read_bytes(), before)
        self.assertEqual(self.paths.state.stat().st_mtime_ns, stamp)
        return message

    def applied(self) -> tuple[dict, list[str]]:
        calls: list[str] = []
        with (
            mock.patch.object(cutover, "build_plan", return_value=PLAN),
            mock.patch.object(cutover, "Operations", fake_operations(None, calls, {"enabled": False})),
            mock.patch.object(cutover, "_provenance", return_value={}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
        ):
            result = cutover.apply_cutover(args(), self.paths)
        return result, calls

    def plan(self) -> dict:
        buffer = StringIO()
        with (
            mock.patch.object(cutover, "resolve_paths", return_value=self.paths),
            mock.patch.object(cutover, "build_plan", return_value=dict(PLAN)),
            redirect_stdout(buffer),
        ):
            code = cutover.run_cutover(
                argparse.Namespace(cutover_command="plan", instance=str(self.instance), expected_revision=REVISION)
            )
        self.assertEqual(code, 0)
        self.assertFalse(self.paths.state.exists())
        return json.loads(buffer.getvalue())["privileged_preconditions"]

    def test_a_foreign_pause_refuses_a_new_apply_naming_its_holder_and_the_resume_command(self) -> None:
        self.pause = frozen("steward", "host maintenance")

        message = self.refused()

        self.assertIn("paused (freeze) by actor steward: host maintenance", message)
        self.assertIn("current_kanboard_backup_checkpoint would refuse it", message)
        self.assertIn("pipeline is already paused; backup create must own the freeze", message)
        self.assertIn(f"`{self.resume_command()}`", message)
        self.assertFalse(self.paths.state.exists())
        # Read the way `backup create` reads it: its own pause-status call on instance.yaml.
        self.assertEqual(len(self.pause_reads), 1)
        self.assertEqual(self.pause_reads[0][-3:], ["pause-status", "--instance", str(self.instance / "instance.yaml")])

    def test_the_freeze_a_recover_leaves_refuses_the_next_identity_before_the_window(self) -> None:
        """Apply #6: the controller's own freeze, left by recover, still breaks a new backup."""
        self.pause = frozen(cutover.CUTOVER_ACTOR)

        message = self.refused()

        self.assertIn(f"paused (freeze) by actor {cutover.CUTOVER_ACTOR}", message)
        self.assertIn("recover leaves its freeze in place on purpose", message)
        self.assertIn(self.resume_command(), message)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(any("resume" in argv for argv in self.pause_reads + self.commands))

    def test_a_retry_past_the_checkpoint_backup_runs_under_its_own_freeze(self) -> None:
        scenarios = {
            "checkpoint done, own freeze": (THROUGH_CHECKPOINT, frozen(cutover.CUTOVER_ACTOR)),
            "checkpoint done, freeze still ahead": (THROUGH_CHECKPOINT, RUNNING),
            "freeze done, own freeze": (THROUGH_FREEZE, frozen(cutover.CUTOVER_ACTOR)),
        }
        for name, (complete, pause) in scenarios.items():
            with self.subTest(name):
                self.retry_state(complete)
                self.pause = pause
                report = cutover._privileged_preconditions(paths=self.paths)
                self.assertTrue(report["pipeline_pause"]["satisfied"], report["pipeline_pause"]["detail"])
                self.assertEqual(report["pipeline_pause"]["phase"], "postgresql_recovery_backup")
                self.assertEqual(report["pipeline_pause"]["freeze_owner"], cutover.CUTOVER_ACTOR)

                result, calls = self.applied()

                self.assertEqual(result["status"], "resume-ready")
                self.assertEqual(calls, [phase for phase in cutover.PHASES if phase not in complete])

    def test_every_pause_a_remaining_phase_would_refuse_leaves_a_retry_byte_and_mtime_identical(self) -> None:
        drain = {"paused": True, "mode": "drain", "actor": "operator", "pause_reason": "inflow"}
        foreign = "pipeline freeze is not owned by the declared backup caller"
        absent = "the declared caller-owned pipeline freeze is absent"
        scenarios = {
            "foreign freeze before global_freeze": (THROUGH_CHECKPOINT, frozen("steward"), True, foreign),
            "drain before global_freeze": (THROUGH_CHECKPOINT, drain, True, foreign),
            "foreign freeze after global_freeze": (THROUGH_FREEZE, frozen("steward"), False, foreign),
            "lifted cutover freeze": (THROUGH_FREEZE, RUNNING, False, absent),
        }
        for name, (complete, pause, resumable, reason) in scenarios.items():
            with self.subTest(name):
                self.pause = pause
                message = self.refused_retry(complete)
                self.assertIn("postgresql_recovery_backup", message)
                self.assertIn(reason, message)
                if resumable:
                    self.assertIn(self.resume_command(), message)
                else:
                    # The freeze global_freeze took is gone or not the controller's: lifting a
                    # pause cannot repair that, so no resume is offered.
                    self.assertNotIn("secretary resume", message)
                    self.assertIn("RECOVER token", message)

    def test_a_retry_behind_every_phase_that_reads_them_consults_neither(self) -> None:
        self.retry_state(
            tuple(
                phase
                for phase in cutover.PHASES
                if phase not in ("resume_ready",)
            )
        )
        self.pause = frozen("steward")
        self.doctor = (1, DOCTOR_FINDINGS)

        report = cutover._privileged_preconditions(paths=self.paths)

        self.assertTrue(report["pipeline_pause"]["satisfied"])
        self.assertTrue(report["doctor"]["satisfied"])
        self.assertEqual(self.pause_reads, [])
        self.assertFalse(any("doctor" in argv for argv in self.commands))

    def test_doctor_findings_refuse_a_new_apply_listing_them_as_doctor_named_them(self) -> None:
        self.doctor = (1, DOCTOR_FINDINGS)

        message = self.refused()

        for line in DOCTOR_FINDING_LINES:
            self.assertIn(f"  - {line}\n", message)
        self.assertIn("installed_protocol_acceptance fails on them", message)
        self.assertIn(f"Reproduce with: {shlex.join(self.doctor_argv())}", message)
        self.assertFalse(self.paths.state.exists())
        # Doctor ran once, exactly as acceptance runs it, and nothing tried to repair a finding.
        issued = [argv for argv in self.commands if argv[:2] != ["sudo", "-n"]]
        self.assertEqual(issued, [self.doctor_argv()])

    def test_doctor_refusal_of_a_retry_leaves_the_durable_state_byte_and_mtime_identical(self) -> None:
        self.pause = frozen(cutover.CUTOVER_ACTOR)
        self.doctor = (1, DOCTOR_FINDINGS)

        message = self.refused_retry(THROUGH_FREEZE)

        self.assertIn(DOCTOR_FINDING_LINES[0], message)
        self.assertNotIn("pipeline is paused", message)

    def test_a_clean_doctor_and_a_running_pipeline_let_a_new_apply_reach_every_phase(self) -> None:
        result, calls = self.applied()

        self.assertEqual(result["status"], "resume-ready")
        self.assertEqual(calls, list(cutover.PHASES))
        self.assertIn(self.doctor_argv(), self.commands)
        self.assertEqual(len(self.pause_reads), 1)

    def test_the_precondition_and_acceptance_share_one_doctor_judgement(self) -> None:
        operation = cutover.Operations(self.paths, cutover._new_state(PLAN, args().actor, args().reason))
        failures = {
            "findings": (1, DOCTOR_FINDINGS),
            "error document": (0, {"error": {"code": "broken"}}),
            "crash": (2, "Traceback: doctor could not start"),
        }
        for name, answer in failures.items():
            with self.subTest(name):
                self.doctor = answer
                self.commands.clear()
                precondition = cutover._privileged_preconditions(paths=self.paths)["doctor"]
                self.assertFalse(precondition["satisfied"])
                with self.assertRaises(cutover.CutoverError) as acceptance:
                    operation.installed_protocol_acceptance()
                self.assertEqual(str(acceptance.exception), precondition["detail"])
                doctor_runs = [argv for argv in self.commands if "doctor" in argv]
                self.assertEqual(doctor_runs, [self.doctor_argv(), self.doctor_argv()])
                # Acceptance stopped at doctor: its status read ran, and no write followed.
                self.assertFalse(any("create" in argv for argv in self.commands))

    def test_plan_prints_both_items_in_every_situation_and_never_fails(self) -> None:
        with self.subTest("blocked"):
            self.pause = frozen(cutover.CUTOVER_ACTOR)
            self.doctor = (1, DOCTOR_FINDINGS)
            report = self.plan()
            self.assertFalse(report["satisfied"])
            self.assertFalse(report["pipeline_pause"]["satisfied"])
            self.assertEqual(report["pipeline_pause"]["pause"]["actor"], cutover.CUTOVER_ACTOR)
            self.assertEqual(report["pipeline_pause"]["resume_command"], self.resume_command())
            self.assertFalse(report["doctor"]["satisfied"])
            self.assertEqual(report["doctor"]["findings"], DOCTOR_FINDING_LINES)
            self.assertEqual(report["doctor"]["command"], shlex.join(self.doctor_argv()))
        with self.subTest("clean"):
            self.pause = RUNNING
            self.doctor = (0, DOCTOR_CLEAN)
            report = self.plan()
            self.assertTrue(report["satisfied"])
            self.assertTrue(report["pipeline_pause"]["satisfied"])
            self.assertTrue(report["doctor"]["satisfied"])
        with self.subTest("unavailable"):
            self.pause = FileNotFoundError("python3")
            self.doctor = OSError("exec format error")
            report = self.plan()
            self.assertFalse(report["pipeline_pause"]["satisfied"])
            self.assertIn("could not be read", report["pipeline_pause"]["detail"])
            self.assertFalse(report["doctor"]["satisfied"])
            self.assertIn("could not run", report["doctor"]["detail"])
        # A report and not a gate: plan read the pause and ran doctor, and changed nothing.
        self.assertTrue(all("pause-status" in argv for argv in self.pause_reads))
        self.assertFalse(any("resume" in argv or "pause" in argv for argv in self.commands))

    def test_the_installed_fixture_reports_the_shape_the_real_items_return(self) -> None:
        report = cutover._privileged_preconditions(paths=self.paths)

        self.assertTrue(report["satisfied"])
        for item in ("pipeline_pause", "doctor"):
            self.assertEqual(shape(INSTALLED_PRECONDITIONS[item]), shape(report[item]))


class InstalledUnitInventoryTests(CutoverFixture):
    """What the installation has decides what a phase acts on, not a fixed list."""

    def setUp(self) -> None:
        super().setUp()
        self.host_preconditions()
        self.host_unit_inventory()
        self.compose = Path(self.temporary.name) / "opt" / "postgres-compose.yml"
        self.compose.parent.mkdir(mode=0o755)
        self.compose.write_text(provision.COMPOSE_TEXT, encoding="utf-8")
        self.compose.chmod(0o600)
        self.commands: list[list[str]] = []

    def systemd(self, *, absent: tuple[str, ...] = ()):
        return mock.patch.object(
            cutover.subprocess, "run", side_effect=systemd_installation(self.commands, absent)
        )

    def acted(self) -> list[list[str]]:
        """Every command that changes a unit, as issued."""
        return [command for command in self.commands if {"stop", "start", "restart"} & set(command)]

    def units_of(self, action: str) -> list[str]:
        return [command[-1] for command in self.commands if action in command]

    def phases(self, absent: tuple[str, ...] = ()):
        state = cutover._new_state(PLAN, args().actor, args().reason)
        with (
            self.systemd(absent=absent),
            mock.patch.object(
                cutover,
                "_provenance",
                return_value={"product_root": PRODUCT_ROOT, "installed_revision": REVISION},
            ),
            mock.patch.object(
                cutover, "_service_evidence", side_effect=cutover._service_evidence
            ) as service_evidence,
        ):
            operations = cutover.Operations(self.paths, state)
            freeze = operations.global_freeze()
            reconciliation = operations.service_reconciliation()
        return freeze, reconciliation, service_evidence

    def test_load_state_of_every_declared_unit_is_read_without_sudo_and_without_mutation(self) -> None:
        with self.systemd(absent=ABSENT_OPTIONAL):
            found = cutover.inventory_units()

        self.assertEqual(
            [command for command in self.commands],
            [
                ["systemctl", "show", declaration.name, "--property=LoadState"]
                for declaration in cutover.DECLARED_UNITS
            ],
        )
        self.assertEqual(self.acted(), [])
        self.assertEqual(found.absent, ABSENT_OPTIONAL)
        self.assertEqual(
            found.present,
            tuple(name for name in cutover.STOP_UNITS if name not in ABSENT_OPTIONAL),
        )
        self.assertEqual(found.missing_required, ())
        self.assertEqual({found.state(name) for name in ABSENT_OPTIONAL}, {cutover.ABSENT_LOAD_STATE})

    def test_unreadable_systemd_is_not_an_absence_and_keeps_the_declared_composition(self) -> None:
        with mock.patch.object(cutover.subprocess, "run", side_effect=OSError("no systemd")):
            found = cutover.inventory_units()

        self.assertEqual(found.absent, ())
        self.assertEqual(found.missing_required, ())
        self.assertEqual(found.stop_units(), cutover.STOP_UNITS)
        self.assertEqual(found.start_units(), cutover.START_UNITS)
        self.assertTrue(
            all(state.startswith("unreadable: ") for state in found.load_states.values())
        )

    def test_installation_without_steward_and_retro_never_touches_the_absent_units(self) -> None:
        freeze, reconciliation, service_evidence = self.phases(absent=ABSENT_OPTIONAL)

        expected_stop = [name for name in cutover.STOP_UNITS if name not in ABSENT_OPTIONAL]
        expected_start = [name for name in cutover.START_UNITS if name not in ABSENT_OPTIONAL]
        self.assertEqual(self.units_of("stop"), expected_stop)
        self.assertEqual(self.units_of("start"), expected_start)
        for command in self.acted():
            self.assertFalse(set(command) & set(ABSENT_OPTIONAL), command)
        # The absent optional units are not silently dropped: the phase evidence
        # names each one with the LoadState that excluded it.
        for evidence in (freeze["inventory"], reconciliation["inventory"]):
            self.assertEqual(
                evidence["excluded"],
                [{"unit": name, "load_state": "not-found", "required": False} for name in ABSENT_OPTIONAL],
            )
            self.assertEqual(evidence["source"], "systemctl show --property=LoadState")
            self.assertEqual(evidence["stop"], expected_stop)
            self.assertEqual(evidence["start"], expected_start)
        self.assertEqual(freeze["services"]["units"], expected_stop)
        self.assertEqual(reconciliation["services"]["units"], expected_start)
        # An absent unit is never proven either: _service_evidence requires
        # LoadState=loaded and would refuse the whole phase over it.
        self.assertEqual(list(service_evidence.call_args.args[0]), expected_start)
        self.assertEqual([item["unit"] for item in reconciliation["units"]], expected_start)

    def test_complete_installation_issues_exactly_the_declared_commands(self) -> None:
        freeze, reconciliation, service_evidence = self.phases()

        self.assertEqual(
            [command for command in self.acted() if "stop" in command],
            [["sudo", "-n", "systemctl", "stop", name] for name in cutover.STOP_UNITS],
        )
        self.assertEqual(
            [command for command in self.acted() if "start" in command],
            [["sudo", "-n", "systemctl", "start", name] for name in cutover.START_UNITS],
        )
        self.assertEqual(freeze["inventory"]["excluded"], [])
        self.assertEqual(reconciliation["inventory"]["excluded"], [])
        self.assertEqual(list(service_evidence.call_args.args[0]), list(cutover.START_UNITS))

    def test_absent_required_unit_refuses_apply_before_the_first_mutation(self) -> None:
        for unit in (
            "secretary-web.service",
            "secretary-dispatcher-production.timer",
            "secretary-curator.timer",
        ):
            with self.subTest(unit=unit):
                self.commands.clear()
                with (
                    self.systemd(absent=(unit,)),
                    mock.patch.object(provision, "DEFAULT_COMPOSE_PATH", self.compose),
                    mock.patch.object(cutover, "build_plan") as plan,
                    mock.patch.object(cutover, "Operations") as operations,
                    self.assertRaisesRegex(
                        cutover.CutoverError, f"required units are not installed: {unit}"
                    ),
                ):
                    cutover.apply_cutover(args(), self.paths)

                plan.assert_not_called()
                operations.assert_not_called()
                self.assertEqual(self.acted(), [])
                self.assertFalse(self.paths.state.exists())
                self.assertIsNone(cutover._read_state(self.paths))

    def test_absent_optional_units_are_a_reported_precondition_and_not_a_refusal(self) -> None:
        with self.systemd(absent=ABSENT_OPTIONAL):
            report = cutover._require_privileged_preconditions(self.compose, paths=self.paths)

        units = report["units"]
        self.assertTrue(report["satisfied"])
        self.assertTrue(units["satisfied"])
        self.assertEqual(units["requirement"], "installed required consumer units")
        for name in ABSENT_OPTIONAL:
            self.assertIn(name, units["detail"])
        self.assertEqual([item["unit"] for item in units["excluded"]], list(ABSENT_OPTIONAL))
        self.assertEqual(self.acted(), [])

    def recovering(self, absent: tuple[str, ...] = ()):
        """Every host fact a recovery reads, answered: systemd, provenance, SQL audit, source."""
        stack = ExitStack()
        stack.enter_context(self.systemd(absent=absent))
        stack.enter_context(mock.patch.object(cutover, "_provenance", return_value={}))
        stack.enter_context(
            mock.patch.object(cutover, "_sql_event_count", return_value={"committed_events": 10})
        )
        stack.enter_context(
            mock.patch.object(
                cutover, "_source_evidence", return_value={"fingerprint": "source-one"}
            )
        )
        return stack

    def test_absent_required_unit_refuses_recovery_without_restarting_or_writing_state(self) -> None:
        """Recovery is the second entrance, and the apply seam's gate is behind it, not in front.

        A unit installed when `_require_privileged_preconditions` passed can be gone by the time
        the operator runs `recover` after the apply that failed.  Inventorying excludes every
        `not-found` unit, so the required one was excluded silently and recovery reported success
        with the web tier still down; before secretary-1609 the fixed list failed loudly on
        `systemctl restart` instead.  What the refusal does with the state document is the other
        half of the answer: it is raised before the first restart, so no unit is touched and no
        state is written, the durable selector the branch had already moved stands, and the
        identical rerun takes the same branch and completes.
        """
        self.runtime.write_text(
            "UNRELATED=kept\nSECRETARY_CARD_BACKEND=postgres\n", encoding="utf-8"
        )
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
        cutover._write_state(self.paths, state)
        before = self.paths.state.read_bytes()
        stamp = self.paths.state.stat().st_mtime_ns
        recovery = args(confirm="RECOVER-" + "1" * 16)

        with self.recovering(absent=("secretary-web.service",)):
            with self.assertRaises(cutover.CutoverError) as refusal:
                cutover.recover_cutover(recovery, self.paths)

        message = str(refusal.exception)
        self.assertIn("missing a required unit", message)
        self.assertIn("secretary-web.service: LoadState=not-found", message)
        self.assertEqual(self.acted(), [])
        self.assertEqual(self.paths.state.read_bytes(), before)
        self.assertEqual(self.paths.state.stat().st_mtime_ns, stamp)
        self.assertEqual(cutover._read_state(self.paths)["status"], state["status"])
        # The durable effect this branch had already written is not undone by the refusal.
        self.assertEqual(
            self.runtime.read_text(encoding="utf-8"),
            "UNRELATED=kept\nSECRETARY_CARD_BACKEND=kanboard\n",
        )

        self.commands.clear()
        with self.recovering():
            result = cutover.recover_cutover(recovery, self.paths)

        self.assertEqual(result["recovery"]["branch"], "kanboard-before-first-write")
        self.assertEqual(
            self.acted(),
            [["sudo", "-n", "systemctl", "restart", name] for name in cutover.START_UNITS],
        )
        self.assertEqual(cutover._read_state(self.paths), result)

    def test_absent_optional_unit_is_still_only_an_exclusion_for_recovery(self) -> None:
        """The gate is on required units; an absent optional one recovers exactly as before."""
        self.runtime.chmod(0o600)
        state = cutover._new_state(PLAN, args().actor, args().reason)
        state["status"] = "failed-frozen"
        state["phases"]["global_freeze"] = {"status": "complete"}
        cutover._write_state(self.paths, state)

        with self.recovering(absent=ABSENT_OPTIONAL):
            result = cutover.recover_cutover(args(confirm="RECOVER-" + "1" * 16), self.paths)

        self.assertEqual(result["recovery"]["branch"], "kanboard-before-fingerprint")
        self.assertEqual(
            [item["unit"] for item in result["recovery"]["evidence"]["inventory"]["excluded"]],
            list(ABSENT_OPTIONAL),
        )

    def test_every_recovery_branch_restarts_only_the_installed_consumers(self) -> None:
        expected = [name for name in cutover.START_UNITS if name not in ABSENT_OPTIONAL]
        branches = ("kanboard-before-fingerprint", "kanboard-before-first-write", "postgres-only")
        for branch in branches:
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as raw:
                self.commands.clear()
                root = Path(raw)
                paths = cutover.Paths(root / "instance", root / "data")
                paths.instance.mkdir(mode=0o700)
                paths.data.mkdir(mode=0o700)
                state = cutover._new_state(PLAN, args().actor, args().reason)
                if branch == "kanboard-before-fingerprint":
                    state["status"] = "failed-frozen"
                    state["phases"]["global_freeze"] = {"status": "complete"}
                else:
                    state["phases"]["writer_quiescence_proof"] = {
                        "status": "complete",
                        "evidence": {"source": {"fingerprint": "source-one"}},
                    }
                    state["phases"]["selector_activation"] = {
                        "status": "complete",
                        "evidence": {"sql_audit_baseline": {"committed_events": 10}},
                    }
                    state["phases"]["final_fenced_import"] = {"status": "complete"}
                cutover._write_state(paths, state)
                committed = 11 if branch == "postgres-only" else 10

                with (
                    self.systemd(absent=ABSENT_OPTIONAL),
                    mock.patch.object(cutover, "_provenance", return_value={}),
                    mock.patch.object(cutover, "_backend", return_value="kanboard"),
                    mock.patch.object(cutover, "_set_backend", return_value={}),
                    mock.patch.object(cutover, "_reconcile_postgres", return_value={}),
                    mock.patch.object(
                        cutover, "_sql_event_count", return_value={"committed_events": committed}
                    ),
                    mock.patch.object(
                        cutover, "_source_evidence", return_value={"fingerprint": "source-one"}
                    ),
                ):
                    result = cutover.recover_cutover(args(confirm="RECOVER-" + "1" * 16), paths)

                self.assertEqual(result["recovery"]["branch"], branch)
                evidence = result["recovery"]["evidence"]
                self.assertEqual(evidence["services"]["units"], expected)
                self.assertEqual(
                    [item["unit"] for item in evidence["inventory"]["excluded"]],
                    list(ABSENT_OPTIONAL),
                )
                self.assertEqual(
                    [command for command in self.acted()],
                    [["sudo", "-n", "systemctl", "restart", name] for name in expected],
                )


if __name__ == "__main__":
    unittest.main()
