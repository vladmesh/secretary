"""End-to-end PostgreSQL archive recovery on isolated Compose projects."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psycopg

from secretary import cutover
from secretary.backup import create_backups
from secretary.backup_verify import verify_backup
from secretary.board import migrate, provision, schema
from secretary.board.backend import reset_card_backend
from secretary.board.import_board import BoardSource, RegistryEntry, SourceRow
from secretary.board.postgres_recovery import (
    PostgresRecoveryError,
    endpoint_identity,
    restore_dump,
)
from secretary.board.sql_cards import SqlCardClient
from secretary.board.store import BoardStoreConfig, BoardStoreError
from secretary.cutover import successor
from secretary.data import DataExport, export_board, init_layout
from secretary.restore import restore_postgres_backup
from secretary.sprint_observer import none_choice
from secretary.sprints import SprintWriter, sprint_client
from secretary.tasks import TaskWriter
from tests.sql_backend_fixtures import PostgresBoard


def installed_inventory() -> cutover.UnitInventory:
    """Every declared unit installed, so the rehearsal asserts about a fixture, not a machine.

    `service_reconciliation` inventories systemd itself, and `_systemctl` and `_service_evidence`
    being answered does not cover that third host fact: an unpatched call reads the LoadState of
    twelve units from whatever host the suite runs on, and the phase then acts on whichever of
    them that host happens to have.  Nothing here mutates a unit either way, but a rehearsal whose
    result depends on the machine is not a rehearsal of the installation.
    """
    return cutover.UnitInventory(
        {declaration.name: "loaded" for declaration in cutover.DECLARED_UNITS}
    )


def cutover_source(repository: Path) -> BoardSource:
    """Complete, small Kanboard snapshot for the isolated controller boundary."""

    def row(
        identifier: int,
        reference: str,
        *,
        meta: dict[str, str],
        column: int = 2,
        active: bool = True,
        comments: tuple[dict[str, object], ...] = (),
    ) -> SourceRow:
        return SourceRow(
            raw={
                "id": identifier,
                "reference": reference,
                "title": f"title {reference}",
                "description": "isolated cutover fixture",
                "column_id": column,
                "swimlane_id": 2,
                "is_active": 1 if active else 0,
                "position": 0,
                "date_creation": 1_700_000_000,
                "date_modification": 1_700_000_500,
            },
            meta=meta,
            comments=comments,
        )

    issue_ref = "issue:" + "a" * 20
    source = BoardSource(
        pipeline=(
            row(
                1,
                "product:secretary",
                column=1,
                meta={
                    "record_type": "product",
                    "product_id": "secretary",
                    "product_projects": '["secretary"]',
                },
            ),
            row(
                2,
                issue_ref,
                column=1,
                meta={
                    "record_type": "issue",
                    "issue_product": "secretary",
                    "issue_kind": "bug",
                    "issue_priority": "P1",
                },
            ),
            row(
                3,
                "secretary-1",
                meta={
                    "record_type": "task",
                    "project": "secretary",
                    "task_type": "code",
                    "sprint_ref": "sprint:1",
                },
            ),
            row(
                4,
                "butler-1",
                active=False,
                meta={"record_type": "task", "project": "butler", "task_type": "code"},
                comments=({"id": 40, "date_creation": 1_700_000_040, "comment": "butler note"},),
            ),
            row(
                5,
                "codegen-product-kit-1",
                meta={
                    "record_type": "task",
                    "project": "codegen-product-kit",
                    "task_type": "code",
                    "blocked_by": "butler-1",
                },
                comments=({"id": 50, "date_creation": 1_700_000_050, "comment": "kit note"},),
            ),
        ),
        sprints=(
            row(
                10,
                "sprint:1",
                meta={
                    "sprint_goal": "rehearse the cutover",
                    "sprint_definition_of_done": "the isolated controller passes",
                    "sprint_status": "open",
                    "sprint_product": "secretary",
                    "sprint_issues": json.dumps([issue_ref]),
                    "sprint_reservations": '["secretary"]',
                    "sprint_repositories": json.dumps([str(repository)]),
                    "sprint_observer": '{"kind":"none"}',
                },
            ),
        ),
        pipeline_columns={1: "Issues", 2: "Ready", 3: "In progress", 6: "Blocked", 7: "Done"},
        pipeline_swimlanes={1: "Default swimlane", 2: "secretary"},
        registry=(
            RegistryEntry(
                project_id="secretary",
                repo=str(repository),
                remote=None,
                default_branch="main",
                adapter="secretary",
                orca_binding="secretary",
                enabled=True,
                plane="orchestrator",
                curator_roots=(),
            ),
            RegistryEntry(
                project_id="butler", repo=str(repository.parent / "butler"), remote=None,
                default_branch="main", adapter=None, orca_binding="butler", enabled=True,
                plane="orchestrator", curator_roots=(),
            ),
            RegistryEntry(
                project_id="codegen-product-kit", repo=str(repository.parent / "codegen-product-kit"),
                remote=None, default_branch="main", adapter=None, orca_binding="codegen-product-kit", enabled=True,
                plane="orchestrator", curator_roots=(),
            ),
        ),
        budget_records=(),
        transaction_documents=(),
        audit_records=(),
        source_fence={"before": "isolated", "after": "isolated", "matched": True},
    )
    return source


class PostgresRecoveryFailureTests(unittest.TestCase):
    def test_migration_failure_is_not_masked_by_the_local_psycopg_handler(self) -> None:
        config = BoardStoreConfig(
            host="127.0.0.1",
            port=6543,
            dbname="target",
            owner_user="owner",
            owner_password="owner-secret",
            app_user="app",
            app_password="app-secret",
            read_user="reader",
            read_password="read-secret",
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch("secretary.board.postgres_recovery.resolve", return_value=config),
            mock.patch(
                "secretary.board.postgres_recovery.migrate.migrate_instance",
                side_effect=BoardStoreError("migration failed before preflight"),
            ),
            self.assertRaisesRegex(
                PostgresRecoveryError,
                "PostgreSQL restore target is not usable: migration failed before preflight",
            ),
        ):
            restore_dump(
                Path(tmpdir) / "postgres.dump",
                Path(tmpdir),
                {"source_endpoint_id": "different"},
            )


class PostgresRecoveryIntegrationTests(unittest.TestCase):
    projects: list[tuple[Path, str]]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projects = []
        self.addCleanup(self._cleanup_projects)
        self.source_instance, self.source_config = self._store("source")
        self.target_instance, self.target_config = self._store("target")
        self.environment = mock.patch.dict(
            os.environ, {"SECRETARY_CARD_BACKEND": "postgres", "BOARD_ROLE": ""}
        )
        self.environment.start()
        reset_card_backend()
        self.addCleanup(reset_card_backend)
        self.addCleanup(self.environment.stop)

    def _store(
        self, name: str, *, revision: str | None = None
    ) -> tuple[Path, BoardStoreConfig]:
        instance = self.root / name
        data_dir = self.root / f"{name}-data"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: recovery-test\n"
            f"data_dir: {data_dir}\noffsite:\n"
            "  instance_remote: git@example.invalid:test/recovery.git\n",
            encoding="utf-8",
        )
        (instance / "projects").mkdir()
        repository = self.root / "repository"
        repository.mkdir(exist_ok=True)
        (instance / "projects" / "secretary.yaml").write_text(
            f"id: secretary\nrepo: {repository}\nenabled: false\nadapter: secretary\ndefault_branch: main\n",
            encoding="utf-8",
        )
        (instance / "adapters").mkdir()
        (instance / "adapters" / "secretary.yaml").write_text(
            "setup:\n  commands: ['true']\nsmoke:\n  command: 'true'\n"
            "validation:\n  ci: local\n  command: 'true'\n"
            "artifact_policy:\n  write_project_files: false\n",
            encoding="utf-8",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        config = BoardStoreConfig(
            host="127.0.0.1",
            port=port,
            dbname="secretary",
            owner_user="secretary_owner",
            owner_password=f"{name}-owner-secret",
            app_user=schema.APP_ROLE,
            app_password=f"{name}-app-secret",
            read_user=schema.READ_ROLE,
            read_password=f"{name}-read-secret",
        )
        path = instance / "board-store.env"
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in config.as_environ().items()),
            encoding="utf-8",
        )
        path.chmod(0o600)
        compose = instance / "postgres-compose.yml"
        project = f"secretary-recovery-{name}-{os.getpid()}"
        self.projects.append((instance, project))
        provision.provision(instance, compose_path=compose, project=project)
        if revision is None:
            migrate.migrate_instance(instance)
        else:
            import sqlalchemy as sa
            from alembic import command

            engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("owner")))
            try:
                with engine.connect() as connection:
                    command.upgrade(
                        migrate.alembic_config(
                            connection=connection,
                            passwords=migrate.passwords_for(config),
                        ),
                        revision,
                    )
                    connection.commit()
            finally:
                engine.dispose()
        provision.verify_roles(instance)
        return instance, config

    def _cleanup_projects(self) -> None:
        for instance, project in reversed(self.projects):
            config = instance / "board-store.env"
            compose = instance / "postgres-compose.yml"
            if config.exists() and compose.exists():
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "--project-name",
                        project,
                        "--env-file",
                        str(config),
                        "--file",
                        str(compose),
                        "down",
                        "--volumes",
                        "--remove-orphans",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )

    def _seed(self) -> None:
        data_dir = self.root / "source-data"
        init_layout(data_dir)
        client = SqlCardClient(self.source_config.for_role("owner"), self.source_instance)
        with client.transaction():
            product_key = client.call(
                "createTask", project_id=1, title="Secretary", reference="product:secretary"
            )
            client.call(
                "saveTaskMetadata",
                task_id=product_key,
                values={
                    "record_type": "product",
                    "product_id": "secretary",
                    "product_projects": '["secretary"]',
                    "future_product_key": "opaque",
                },
            )
            issue_key = client.call("createTask", project_id=1, title="Recovery", reference="issue:recovery")
            client.call(
                "saveTaskMetadata",
                task_id=issue_key,
                values={
                    "record_type": "issue",
                    "issue_product": "secretary",
                    "issue_kind": "feature",
                    "issue_priority": "P1",
                },
            )
        writer = TaskWriter(client, data_dir=data_dir)
        sprint_board = sprint_client(self.source_instance)
        self.addCleanup(sprint_board.close)
        sprint_writer = SprintWriter(sprint_board, data_dir=data_dir, instance=self.source_instance)
        sprint_ref = sprint_writer.create(
            role="po",
            actor="test",
            goal="prove recovery",
            repositories=[str(self.root / "repository")],
            product="secretary",
            issues=["issue:recovery"],
            projects=["secretary"],
            observer=none_choice(),
            reference="sprint:recovery-custom",
            request_id="create-nullable-number-sprint",
        )["sprint"]["ref"]
        first = writer.create(
            role="po",
            actor="test",
            project="secretary",
            task_type="code",
            title="Recover me",
            target="ready",
            reference="secretary-1",
            sprint=sprint_ref,
            sprint_override=True,
            sprint_override_reason="integration fixture",
            request_id="create-recovery-one",
        )["task"]
        second = writer.create(
            role="po",
            actor="test",
            project="secretary",
            task_type="code",
            title="Dependent",
            target="ready",
            reference="secretary-2",
            sprint=sprint_ref,
            blocked_by="secretary-1",
            request_id="create-recovery-two",
            seed_ref="a" * 40,
            supersedes="secretary-1",
            sprint_override=True,
            sprint_override_reason="integration fixture",
        )["task"]
        client.call(
            "saveTaskMetadata",
            task_id=int(str(first["id"]).rsplit("_", 1)[1]),
            values={"future_task_key": "opaque", "issues": "issue:recovery"},
        )
        self.assertEqual(second["workspace"]["supersedes"], "secretary-1")
        with client.transaction():
            client._execute(
                "INSERT INTO projects (project_id, enabled, registry_present) VALUES "
                "('butler', true, true), ('codegen-product-kit', true, true)"
            )
        butler_key = client.call(
            "createTask", project_id=1, title="Butler collision", reference="butler-1", column_id=2
        )
        kit_key = client.call(
            "createTask", project_id=1, title="Kit collision", reference="codegen-product-kit-1",
            column_id=2,
        )
        client.call(
            "saveTaskMetadata", task_id=butler_key,
            values={"record_type": "task", "project": "butler", "task_type": "code"},
        )
        client.call(
            "saveTaskMetadata", task_id=kit_key,
            values={
                "project": "codegen-product-kit",
                "record_type": "task",
                "task_type": "code",
                "blocked_by": "butler-1",
            },
        )
        writer.comment(
            role="worker", actor="test", reference="butler-1", body="butler collision comment",
            request_id="comment-butler-collision",
        )
        writer.comment(
            role="worker", actor="test", reference="codegen-product-kit-1",
            body="kit collision comment", request_id="comment-kit-collision",
        )
        client.call("closeTask", task_id=butler_key)
        self.assertNotEqual(butler_key, kit_key)
        sprint_writer.comment(
            role="po",
            actor="test",
            reference=sprint_ref,
            body="sprint recovery comment",
            request_id="sprint-recovery-comment",
        )
        sprint_writer.resume(
            role="po",
            actor="test",
            reference=sprint_ref,
            entry={
                "selected_step": "continue recovery",
                "selected_why": "the dump is ready",
                "rejected_alternatives": "none",
                "current_task": "secretary-1",
                "dod_state": "in progress",
                "next_safe_step": "verify restore",
                "recorded_at": "2026-09-08T00:00:00Z",
            },
            request_id="sprint-recovery-resume",
        )
        sprint_writer.record_budget(
            role="po",
            actor="test",
            reference=sprint_ref,
            event_type="red_ci",
            request_id="sprint-recovery-budget",
        )
        writer.move(
            role="po",
            actor="test",
            reference="secretary-1",
            target="done",
            reason="recovery fixture complete",
            request_id="complete-recovery-one",
            sprint_override=True,
            sprint_override_reason="integration recovery fixture",
        )
        writer.archive(
            role="po",
            actor="test",
            reference="secretary-1",
            reason="retain archived recovery evidence",
            request_id="archive-recovery-one",
        )
        writer.comment(
            role="worker",
            actor="test",
            reference="secretary-1",
            body="post-close evidence",
            request_id="post-close-comment",
        )
        sprint_writer.close(
            role="po",
            actor="test",
            reference=sprint_ref,
            decisions={
                "issues": [
                    {
                        "ref": "issue:recovery",
                        "verdict": "open",
                        "reason": "recovery remains supported",
                    }
                ],
                "cards": [
                    {
                        "ref": "secretary-2",
                        "verdict": "drop",
                        "reason": "fixture closes with dependent work recorded",
                    }
                ],
            },
            reason="recovery fixture closed",
            request_id="close-recovery-sprint",
        )
        client.connection.close()

    def _exports(self, data_dir: Path, instance_dir: Path, **_kwargs) -> dict[str, DataExport]:
        board = export_board(data_dir, instance_dir=instance_dir)
        (data_dir / "memory" / "export.ndjson").write_text("", encoding="utf-8")
        runs = data_dir / "runs"
        for name, body in (
            ("watermarks.json", "{}\n"),
            ("cards.json", "{}\n"),
            ("claims.json", "{}\n"),
            ("runs.ndjson", ""),
        ):
            (runs / name).write_text(body, encoding="utf-8")
        for name in ("transcripts", "artifacts"):
            (data_dir / name / "inventory.json").write_text("{}\n", encoding="utf-8")
        return {
            "board": board,
            "memory": DataExport(data_dir / "memory" / "export.ndjson", 0, "test"),
            "runs": DataExport(runs / "runs.ndjson", 0, "test"),
            "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 0, "test"),
            "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 0, "test"),
        }

    def test_real_cutover_phases_share_one_disposable_postgres_16_boundary(self) -> None:
        """Run the controller methods themselves, not a wholesale Operations fake."""
        instance = self.target_instance
        data = self.root / "target-data"
        init_layout(data)
        pipeline_state = self.root / "pipeline-state"
        pipeline_state.mkdir()
        runtime = instance / "runtime.env"
        runtime.write_text("SECRETARY_CARD_BACKEND=kanboard\n", encoding="utf-8")
        runtime.chmod(0o600)
        (instance / ".gitignore").write_text(
            "board-store.env\npostgres-data/\n",
            encoding="utf-8",
        )
        source = cutover_source(self.root / "repository")
        subprocess.run(["git", "-C", str(instance), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(instance), "config", "user.name", "cutover-test"], check=True)
        subprocess.run(
            ["git", "-C", str(instance), "config", "user.email", "cutover@example.invalid"], check=True
        )
        subprocess.run(["git", "-C", str(instance), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(instance), "commit", "-m", "fixture"], check=True, capture_output=True
        )

        plan = {
            "version": 1,
            "plan_id": "d" * 64,
            "expected_revision": "a" * 40,
            "source": {"fingerprint": "isolated"},
        }
        state = cutover._new_state(plan, "integration", "disposable PostgreSQL 16 rehearsal")
        state["controller_pid"] = os.getpid()
        state["phases"]["global_freeze"] = {"status": "complete"}
        paths = cutover.Paths(instance, data)
        cutover._write_state(paths, state)
        project = next(project for candidate, project in self.projects if candidate == instance)
        operation = cutover.Operations(
            paths,
            state,
            provision_options={
                "compose_path": instance / "postgres-compose.yml",
                "project": project,
            },
            checkpoint_options={"state_dir": pipeline_state},
        )
        real_command = cutover._secretary

        def isolated_command(command_paths, *argv):
            if argv[:2] == ("dispatcher", "production-tick"):
                document = {"status": "ok", "isolated_host_control": True, "would": []}
                return {"output": json.dumps(document), "document": document}
            return real_command(command_paths, *argv)

        freeze = {"paused": True, "mode": "freeze", "actor": cutover.CUTOVER_ACTOR}
        with (
            mock.patch.dict(
                os.environ,
                {
                    "SECRETARY_CARD_BACKEND": "kanboard",
                    cutover.CONTROLLER_ID_ENV: state["identity"],
                },
                clear=False,
            ),
            mock.patch.object(cutover, "_secretary", side_effect=isolated_command),
            mock.patch.object(cutover, "_systemctl", return_value={"action": "isolated"}),
            mock.patch.object(cutover, "_service_evidence", return_value=[]),
            mock.patch.object(cutover, "inventory_units", return_value=installed_inventory()),
            # The guard on the patch above: nothing else in this boundary may reach the host's
            # systemd, and a new phase that did would fail here rather than quietly depend on it.
            mock.patch.object(
                cutover,
                "_unit_load_state",
                side_effect=AssertionError("this boundary must not read the host's systemd"),
            ),
            mock.patch.object(cutover, "_provenance", return_value={"product_root": str(self.root)}),
            mock.patch.object(cutover, "_writer_processes", return_value=[]),
            mock.patch("secretary.board.import_board.read_source", return_value=source),
            mock.patch("secretary.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("secretary.backup._pipeline_status", return_value=freeze),
            mock.patch("secretary.backup.export_all", side_effect=self._exports),
        ):
            provisioned = operation.postgresql_provision_migration_verification()
            quiescent = operation.writer_quiescence_proof()
            state["phases"]["writer_quiescence_proof"] = {
                "status": "complete",
                "evidence": quiescent,
            }
            imported = operation.final_fenced_import()
            state["phases"]["final_fenced_import"] = {"status": "complete", "evidence": imported}
            parity = operation.full_parity()
            recovery_backup = operation.postgresql_recovery_backup()
            activated = operation.selector_activation()
            state["phases"]["selector_activation"] = {
                "status": "complete",
                "evidence": activated,
            }
            reconciled = operation.service_reconciliation()
            accepted = operation.installed_protocol_acceptance()
            state["phases"]["installed_protocol_acceptance"] = {
                "status": "complete",
                "evidence": accepted,
            }
            checkpoint = operation.post_switch_checkpoint()

            # The 2026-09-10 live window: no sprint is open, so the acceptance must open and
            # close its own canary sprint on a registered project instead of refusing after the
            # switch. The seeded sprint still reserves `secretary`, so the canary takes `canary`.
            (instance / "projects" / "canary.yaml").write_text(
                f"id: canary\nrepo: {self.root / 'repository'}\nenabled: false\n"
                "adapter: secretary\ndefault_branch: main\n",
                encoding="utf-8",
            )
            with psycopg.connect(self.target_config.for_role("owner").conninfo()) as connection:
                connection.execute(
                    "INSERT INTO projects (project_id, enabled, registry_present) "
                    "VALUES ('canary', true, true)"
                )
            canary_plan = {**plan, "plan_id": "e" * 64}
            canary_state = cutover._new_state(canary_plan, "integration", "canary acceptance")
            canary_state["controller_pid"] = os.getpid()
            canary_state["phases"]["global_freeze"] = {"status": "complete"}
            canary_state["phases"]["selector_activation"] = {"status": "complete", "evidence": activated}
            # The write barrier admits only the controller whose identity the canonical state
            # names, so the canary controller takes the slot the way a real apply would hold it.
            cutover._write_state(paths, canary_state)
            canary_operation = cutover.Operations(
                paths, canary_state, checkpoint_options={"state_dir": pipeline_state}
            )
            with mock.patch.dict(os.environ, {cutover.CONTROLLER_ID_ENV: canary_state["identity"]}):
                # Close the seeded sprint through the installed protocol first: the canary is
                # the shape of a window with no open sprint, and the installation admits one.
                listing = cutover._secretary(paths, "task", "list", "--sprint", "sprint:1")["document"]
                rows = listing if isinstance(listing, list) else listing.get("tasks") or listing.get("items") or []
                dispositions = "".join(
                    f"  - ref: {row['ref']}\n    verdict: drop\n    reason: canary rehearsal closes the seeded sprint\n"
                    for row in rows
                    if isinstance(row, dict) and row.get("state") != "done"
                )
                declared = cutover._secretary(paths, "sprint", "show", "--ref", "sprint:1")["document"]
                verdicts = "".join(
                    f"  - ref: {ref}\n    verdict: open\n    reason: still open\n"
                    for ref in (declared.get("issues") or [])
                )
                decisions = self.root / "seeded-sprint-decisions.yaml"
                decisions.write_text(
                    ("issues:\n" + verdicts if verdicts else "")
                    + ("cards:\n" + dispositions if dispositions else ""),
                    encoding="utf-8",
                )
                closeout = self.root / "seeded-sprint-closeout.md"
                closeout.write_text("# seeded sprint closed before the canary rehearsal\n", encoding="utf-8")
                cutover._secretary(
                    paths, "sprint", "close", "--role", "po", "--actor", "test", "--ref", "sprint:1",
                    "--reason", "canary rehearsal", "--decisions-file", str(decisions),
                    "--closeout-file", str(closeout), "--data-dir", str(data),
                )
                with mock.patch.object(cutover, "_acceptance_project", return_value="canary"):
                    canary_accepted = canary_operation.installed_protocol_acceptance()
            canary_sprint = cutover._secretary(
                paths, "sprint", "show", "--ref", canary_accepted["sprint"]["ref"]
            )["document"]

        self.assertTrue(canary_accepted["sprint"]["canary"])
        self.assertEqual(canary_sprint["status"], "closed")
        self.assertEqual(canary_sprint["reservations"], ["canary"])
        self.assertIsNotNone(canary_accepted["sprint"]["closed"])
        self.assertEqual(provisioned["migration_head"], migrate.head_revision())
        self.assertEqual(
            quiescent["source"]["fingerprint"],
            imported["import"]["source_consistency"]["before"],
        )
        self.assertTrue(parity["parity"]["ok"])
        self.assertTrue(recovery_backup["archives"])
        self.assertEqual(activated["backend"], "postgres")
        self.assertEqual(reconciled["services"], {"action": "isolated"})
        self.assertGreater(
            accepted["sql_audit"]["committed_events"], activated["sql_audit_baseline"]["committed_events"]
        )
        self.assertTrue(checkpoint["archives"])
        self.assertIsNotNone(checkpoint["acceptance_preserved"])
        self.assertTrue(
            all("postgres_dump" in manifest["components"] for manifest in checkpoint["archive_manifests"])
        )
        print(
            "cutover rehearsal evidence: "
            + json.dumps(
                {
                    "source": "synthetic-kanboard-complete",
                    "target": project,
                    "postgres_image": "postgres:16",
                    "migration_head": provisioned["migration_head"],
                    "parity_counts": parity["counts"],
                    "acceptance_events": accepted["sql_audit"]["committed_events"],
                    "activation_baseline": activated["sql_audit_baseline"]["committed_events"],
                    "recovery_archives": len(recovery_backup["archives"]),
                    "checkpoint_commit": checkpoint["checkpoint"]["commit"],
                    "post_switch_archives": len(checkpoint["archives"]),
                },
                sort_keys=True,
            )
        )

    def test_real_successor_preserves_import_oid_and_builds_empty_distinct_plan(self) -> None:
        import psycopg
        import psycopg.sql

        instance, original = self._store(
            "successor", revision="0006_sprint_transport_key"
        )
        data = self.root / "successor-data"
        init_layout(data)
        paths = cutover.Paths(instance, data)
        (instance / "runtime.env").write_text("SECRETARY_CARD_BACKEND=kanboard\n", encoding="utf-8")
        with psycopg.connect(original.for_role("owner").conninfo()) as connection:
            connection.execute(
                "INSERT INTO projects (project_id, enabled, registry_present) VALUES "
                "('butler', true, true), ('codegen-product-kit', true, true)"
            )
            connection.execute(
                "INSERT INTO tasks (task_ref, project_id, task_number, title, task_type, state, "
                "created_at, updated_at) VALUES "
                "('butler-1', 'butler', 1, 'Butler collision', 'code', 'done', now(), now()), "
                "('codegen-product-kit-1', 'codegen-product-kit', 1, 'Kit collision', 'code', "
                "'ready', now(), now())"
            )
            # A sprint that holds one of the cards through the two INITIALLY DEFERRED foreign
            # keys of the live schema. The 0007 upgrade rewrites `tasks` (volatile default) and
            # then updates every row, so their deferred RI events are queued in the migration's
            # own transaction; without an explicit SET CONSTRAINTS the following ALTER TABLE
            # refuses with "pending trigger events" exactly as the preserved 2026-09-09 target did.
            connection.execute(
                "INSERT INTO sprints (ref, sprint_number, goal, definition_of_done, status, "
                "created_at, updated_at, board_key) VALUES "
                "('sprint:1', 1, 'collision sprint', 'holds butler-1', 'open', now(), now(), 1)"
            )
            connection.execute(
                "UPDATE tasks SET sprint_ref = 'sprint:1' WHERE task_ref = 'butler-1'"
            )
            connection.execute(
                "UPDATE sprints SET current_task_ref = 'butler-1' WHERE ref = 'sprint:1'"
            )
            connection.execute(
                "INSERT INTO task_comments (task_ref, marker, body, actor_role, created_at) "
                "VALUES ('butler-1', 'note', 'butler collision comment', 'worker', now()), "
                "('codegen-product-kit-1', 'note', 'kit collision comment', 'worker', now())"
            )
            connection.execute(
                "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) VALUES "
                "('codegen-product-kit-1', 'butler-1', 'butler-1')"
            )
            connection.execute(
                "INSERT INTO requests (request_id, operation, intent, status, protocol, entity_kind, "
                "ref, created_at, settled_at) VALUES ('collision-audit', 'card.comment', '{}'::jsonb, "
                "'committed', true, 'card', 'butler-1', now(), now())"
            )
            connection.execute(
                "INSERT INTO board_events (event_id, request_id, kind, entity_kind, ref, actor_role, "
                "actor_id, reason, occurred_at, committed, committed_at) VALUES "
                "('collision-event', 'collision-audit', 'entity.updated', 'card', 'butler-1', "
                "'worker', 'fixture', 'commented', now(), true, now())"
            )

        def content_snapshot(config: BoardStoreConfig) -> dict[str, list[tuple[object, ...]]]:
            with psycopg.connect(config.for_role("owner").conninfo()) as connection:
                return {
                    "refs_numbers_board_keys": connection.execute(
                        "SELECT task_ref, project_id, task_number, board_key FROM tasks ORDER BY task_ref"
                    ).fetchall(),
                    "comments": connection.execute(
                        "SELECT task_ref, marker, body, request_id FROM task_comments "
                        "ORDER BY task_ref, comment_id"
                    ).fetchall(),
                    "links": connection.execute(
                        "SELECT task_ref, depends_on, depends_on_task FROM task_dependencies "
                        "ORDER BY task_ref, depends_on"
                    ).fetchall(),
                    "requests": connection.execute(
                        "SELECT request_id, operation, status, ref FROM requests ORDER BY request_id"
                    ).fetchall(),
                    "board_events": connection.execute(
                        "SELECT event_id, request_id, kind, ref, committed FROM board_events "
                        "ORDER BY event_id"
                    ).fetchall(),
                }

        from secretary.board import postgres_recovery

        with psycopg.connect(original.for_role("owner").conninfo()) as connection:
            predecessor_counts = postgres_recovery._table_counts(connection)
            pre_upgrade_content = {
                "refs_and_numbers": connection.execute(
                    "SELECT task_ref, project_id, task_number FROM tasks ORDER BY task_ref"
                ).fetchall(),
                "comments": connection.execute(
                    "SELECT task_ref, marker, body, request_id FROM task_comments "
                    "ORDER BY task_ref, comment_id"
                ).fetchall(),
                "links": connection.execute(
                    "SELECT task_ref, depends_on, depends_on_task FROM task_dependencies "
                    "ORDER BY task_ref, depends_on"
                ).fetchall(),
                "requests": connection.execute(
                    "SELECT request_id, operation, status, ref FROM requests ORDER BY request_id"
                ).fetchall(),
                "board_events": connection.execute(
                    "SELECT event_id, request_id, kind, ref, committed FROM board_events "
                    "ORDER BY event_id"
                ).fetchall(),
            }
        predecessor_metadata = {
            "source_schema": "0006_sprint_transport_key",
            "table_counts": predecessor_counts,
        }
        original_identity = successor.inspect_database(original)
        with psycopg.connect(
            original.for_role("owner").conninfo().replace("dbname='secretary'", "dbname='postgres'")
        ) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datid=%s AND pid<>pg_backend_pid()",
                (original_identity["oid"],),
            ).fetchall()
        report = {
            "counts": predecessor_metadata["table_counts"],
            "parity": {"ok": True},
            "schema_revision": "0006_sprint_transport_key",
            "source_consistency": {"matched": True},
        }
        report_path = data / "cutover" / "artifacts" / ("import-" + "e" * 64 + ".json")
        report_path.parent.mkdir(parents=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        state = {
            "version": 1,
            "identity": "postgres-" + "e" * 20,
            "plan_id": "e" * 64,
            "expected_revision": "b" * 40,
            "actor": "owner",
            "reason": "failed import rehearsal",
            "created_at": "2026-09-09T00:00:00Z",
            "updated_at": "2026-09-09T00:00:00Z",
            "status": "recovered-frozen",
            "backend_before": "kanboard",
            "first_sql_write": None,
            "recovery": {"branch": "kanboard-before-first-write"},
            "phases": {
                "final_fenced_import": {
                    "status": "complete", "evidence": {"report": str(report_path), "import": report}
                },
                "full_parity": {
                    "status": "complete",
                    "evidence": {"parity": {"ok": True}, "counts": predecessor_metadata["table_counts"]},
                },
            },
        }
        cutover._write_state(paths, state)
        token = successor.confirmation(state["plan_id"], original_identity["oid"], original.dbname)
        prepare_args = SimpleNamespace(
            expected_revision="a" * 40,
            actor="operator",
            reason="prepare a clean later target",
            confirm=token,
        )
        original_state_bytes = paths.state.read_bytes()
        with (
            mock.patch.object(cutover, "_provenance", return_value={"installed_revision": "a" * 40}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            self.assertRaisesRegex(cutover.CutoverError, successor.UPGRADE_PREREQUISITE),
        ):
            successor.prepare(prepare_args, paths)
        self.assertEqual(paths.state.read_bytes(), original_state_bytes)
        self.assertEqual(successor.inspect_schema_revision(original), "0006_sprint_transport_key")
        with psycopg.connect(original.for_role("owner").conninfo()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT task_ref, project_id, task_number FROM tasks ORDER BY task_ref"
                ).fetchall(),
                pre_upgrade_content["refs_and_numbers"],
            )

        applied_upgrade = migrate.migrate_instance(instance, reuse_existing_roles=True)
        provision.verify_roles(instance)
        self.assertEqual(applied_upgrade, ("0007_card_transport_key",))
        before_content = content_snapshot(original)
        injected = {name: True for name in (
            "database_rename", "database_create", "migration", "empty_verification",
            "history_publication", "canonical_release", "release_receipt",
        )}
        real_methods = {
            name: getattr(successor.SuccessorOperations, name) for name in injected
        }

        def interrupted(name: str):
            def operation(operation_self):
                evidence = real_methods[name](operation_self)
                if injected[name]:
                    injected[name] = False
                    raise RuntimeError(f"injected after real {name} effect")
                return evidence
            return operation

        attempts = 0
        with (
            mock.patch.object(cutover, "_provenance", return_value={"installed_revision": "a" * 40}),
            mock.patch.object(cutover, "_backend", return_value="kanboard"),
            mock.patch.object(cutover, "_source_evidence", return_value={"fingerprint": "later-kanboard", "parity": {"ok": True}}),
            mock.patch.multiple(
                successor.SuccessorOperations,
                **{name: interrupted(name) for name in injected},
            ),
        ):
            while True:
                attempts += 1
                try:
                    result = successor.prepare(prepare_args, paths)
                except cutover.CutoverError as exc:
                    self.assertTrue(
                        "injected after real" in str(exc) or "release_receipt remains pending" in str(exc)
                    )
                else:
                    break
                if attempts > len(injected) + 2:
                    self.fail("successor interruption sequence did not converge")
            replay = successor.prepare(prepare_args, paths)
            status_before_plan = cutover._read_recovered_history(paths)
            successor_plan = cutover.build_plan(paths, "a" * 40)
            cutover._write_state(
                paths,
                cutover._new_state(successor_plan, "later-operator", "separate future cutover"),
            )
            status_after_plan = cutover._read_recovered_history(paths)
            replay_after_plan = successor.prepare(prepare_args, paths)

        self.assertEqual(attempts, len(injected) + 1)
        self.assertTrue(all(not pending for pending in injected.values()))
        self.assertEqual(
            list(paths.history.glob(f"postgres-v1-{state['plan_id']}.json")),
            [paths.history / f"postgres-v1-{state['plan_id']}.json"],
        )
        self.assertEqual(
            list(paths.history.glob(f"successor-release-{state['plan_id']}.json")),
            [successor.release_receipt_path(paths, state["plan_id"])],
        )
        self.assertEqual(
            list(paths.artifacts.glob(f"successor-{state['plan_id']}-*.dump")),
            [Path(result["successor_preparation"]["dump"]["path"])],
        )

        database = result["successor_preparation"]["database"]
        archived = successor.inspect_database(original, name=database["archive_name"])
        configured = successor.inspect_database(original)
        _, fresh = __import__(
            "secretary.board.postgres_recovery", fromlist=["inspect_source"]
        ).inspect_source(instance)
        self.assertEqual(archived["oid"], original_identity["oid"])
        self.assertFalse(archived["allow_connections"])
        self.assertNotEqual(configured["oid"], original_identity["oid"])
        self.assertTrue(all(count == 0 for count in fresh["table_counts"].values()))
        self.assertEqual(fresh["source_schema"], migrate.head_revision())
        occupied = result["successor_preparation"]["phases"]["occupied_verification"]["evidence"]
        self.assertEqual(occupied["schema_before"], "0007_card_transport_key")
        self.assertEqual(occupied["forward_migrations"], [])

        copy_name = "successor_archive_readback"
        with psycopg.connect(
            original.for_role("owner").conninfo().replace("dbname='secretary'", "dbname='postgres'"),
            autocommit=True,
        ) as admin:
            admin.execute(
                psycopg.sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(
                    psycopg.sql.Identifier(copy_name),
                    psycopg.sql.Identifier(original.owner_user),
                    psycopg.sql.Identifier(database["archive_name"]),
                )
            )
        access_copy = replace(original, dbname=copy_name)
        after_content = content_snapshot(access_copy)
        self.assertEqual(after_content, before_content)
        self.assertEqual(status_after_plan, status_before_plan)
        self.assertTrue(replay_after_plan["idempotent_replay"])
        self.assertEqual(
            replay_after_plan["successor_preparation"]["status"], "complete"
        )
        self.assertNotEqual(successor_plan["plan_id"], state["plan_id"])
        self.assertTrue(replay["idempotent_replay"])
        predecessor = successor_plan["recovered_predecessors"][0]["successor_preparation"]
        self.assertEqual(predecessor["archived_database"]["original_oid"], original_identity["oid"])
        self.assertEqual(predecessor["archived_database"]["successor_oid"], configured["oid"])
        print("successor evidence:", json.dumps({
            "original_oid": original_identity["oid"], "archive_name": archived["name"],
            "successor_oid": configured["oid"], "schema_head": fresh["source_schema"],
            "original_schema_head": "0006_sprint_transport_key",
            "external_upgrade": list(applied_upgrade),
            "counts": predecessor_metadata["table_counts"],
            "dump_sha256": result["successor_preparation"]["dump"]["sha256"],
            "real_postgres_failure_injections": list(injected),
            "post_rotation_content_comparison": sorted(after_content),
            "successor_plan_id": successor_plan["plan_id"],
        }, sort_keys=True))

    def test_full_backup_destroy_source_restore_target_and_rerun(self) -> None:
        self._seed()
        with (
            mock.patch("secretary.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
            mock.patch("secretary.backup._pipeline_action", return_value=None),
            mock.patch("secretary.backup.export_all", side_effect=self._exports),
            mock.patch("secretary.sprints.sprint_client", wraps=sprint_client) as sprint_factory,
        ):
            results = create_backups(self.source_instance, backup_kinds=("full", "core"))
        sprint_factory.assert_called_once_with(self.source_instance)
        by_kind = {result.manifest["backup_kind"]: result for result in results}
        result = by_kind["full"]
        core = by_kind["core"]
        verified = verify_backup(result.archive)
        self.assertEqual(verified.code, 0, verified.findings)
        core_verified = verify_backup(core.archive)
        self.assertEqual(core_verified.code, 0, core_verified.findings)
        self.assertEqual(core.manifest["board_backend"], "postgres")
        self.assertIn("board_history", core.manifest["components"])
        self.assertNotIn("postgres_dump", core.manifest["components"])
        with tarfile.open(core.archive) as archive:
            self.assertIn("secretary-backup/secretary-data/board/audit.json", archive.getnames())
            self.assertIn("secretary-backup/secretary-data/board/audit.ndjson", archive.getnames())
            self.assertNotIn("secretary-backup/engine/postgres.dump", archive.getnames())
        self.assertNotIn("raw_board", result.manifest["components"])
        counts = result.manifest["components"]["postgres_dump"]["table_counts"]
        self.assertEqual(counts["products"], 1)
        self.assertEqual(counts["issues"], 1)
        self.assertEqual(counts["tasks"], 4)
        self.assertEqual(counts["sprints"], 1)
        self.assertEqual(counts["task_dependencies"], 2)
        self.assertEqual(counts["task_supersessions"], 1)
        self.assertEqual(counts["task_issues"], 1)
        self.assertGreaterEqual(counts["task_comments"], 4)
        self.assertEqual(counts["repositories"], 1)
        self.assertEqual(counts["projects"], 3)
        self.assertEqual(counts["sprint_repositories"], 1)
        self.assertEqual(counts["sprint_projects"], 1)
        self.assertEqual(counts["sprint_issues"], 1)
        self.assertEqual(counts["sprint_comments"], 2)
        self.assertEqual(counts["sprint_resumes"], 1)
        self.assertEqual(counts["sprint_budget_events"], 1)
        self.assertEqual(counts["sprint_decisions"], 2)
        self.assertGreater(counts["board_events"], 0)
        self.assertGreaterEqual(counts["requests"], 12)
        self.assertGreater(result.manifest["components"]["postgres_dump"]["bytes"], 0)
        source_probe = SqlCardClient(self.source_config.for_role("read"), self.source_instance)
        self.assertEqual(
            source_probe._query(
                "SELECT request_id FROM sprint_budget_events WHERE sprint_ref = %s",
                ("sprint:recovery-custom",),
            ),
            [("sprint-recovery-budget",)],
        )
        self.assertIn(
            ("complete-recovery-one", True),
            source_probe._query("SELECT request_id, committed FROM board_events ORDER BY request_id"),
        )
        source_probe.close()
        with tarfile.open(result.archive) as archive:
            cards = json.loads(
                archive.extractfile("secretary-backup/secretary-data/board/cards.json").read().decode("utf-8")
            )["cards"]
            sprints = json.loads(
                archive.extractfile("secretary-backup/secretary-data/board/sprints.json")
                .read()
                .decode("utf-8")
            )["sprints"]
            history = json.loads(
                archive.extractfile("secretary-backup/secretary-data/board/audit.json").read().decode("utf-8")
            )["events"]
        cards_by_ref = {card["reference"]: card for card in cards}
        self.assertEqual(
            [card["reference"] for card in cards].count("butler-1"), 1
        )
        self.assertEqual(
            [card["reference"] for card in cards].count("codegen-product-kit-1"), 1
        )
        self.assertTrue(cards_by_ref["butler-1"]["closed"])
        self.assertFalse(cards_by_ref["codegen-product-kit-1"]["closed"])
        self.assertEqual(
            cards_by_ref["codegen-product-kit-1"]["metadata"]["blocked_by"], "butler-1"
        )
        self.assertIn(
            "butler collision comment",
            "\n".join(comment["text"] for comment in cards_by_ref["butler-1"]["comments"]),
        )
        self.assertIn(
            "kit collision comment",
            "\n".join(
                comment["text"] for comment in cards_by_ref["codegen-product-kit-1"]["comments"]
            ),
        )
        self.assertEqual(cards_by_ref["secretary-1"]["metadata"]["issues"], "issue:recovery")
        self.assertEqual(cards_by_ref["secretary-2"]["metadata"]["supersedes"], "secretary-1")
        self.assertEqual(sprints[0]["repositories"], [str(self.root / "repository")])
        self.assertEqual(sprints[0]["resume"]["selected_step"], "continue recovery")
        self.assertEqual(sprints[0]["budget"]["by_type"]["red_ci"], 1)
        self.assertEqual(len(sprints[0]["comments"]), 2)
        history_requests = {event["request_id"] for event in history}
        self.assertTrue(
            {
                "sprint-recovery-budget",
                "sprint-recovery-comment",
                "sprint-recovery-resume",
                "comment-butler-collision",
                "comment-kit-collision",
                "complete-recovery-one",
                "close-recovery-sprint",
            }
            <= history_requests
        )
        with tarfile.open(result.archive) as archive:
            names = archive.getnames()
            secrets = {
                self.source_config.owner_password.encode(),
                self.source_config.app_password.encode(),
                self.source_config.read_password.encode(),
            }
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                self.assertIsNotNone(stream)
                while chunk := stream.read(1024 * 1024):
                    for secret in secrets:
                        self.assertNotIn(secret, chunk)
        self.assertNotIn("secretary-backup/instance/board-store.env", names)

        source_project = self.projects.pop(0)
        self._down(*source_project)
        first = restore_postgres_backup(result.archive, self.target_instance)
        second = restore_postgres_backup(result.archive, self.target_instance)
        self.assertEqual(first, second)
        marker = json.loads((self.root / "target-data" / "postgres-restore.json").read_text(encoding="utf-8"))
        self.assertFalse(marker["processes_started"])
        target_probe = SqlCardClient(self.target_config.for_role("read"), self.target_instance)
        self.addCleanup(target_probe.close)
        self.assertEqual(
            target_probe._query(
                "SELECT request_id FROM sprint_budget_events WHERE sprint_ref = %s",
                ("sprint:recovery-custom",),
            ),
            [("sprint-recovery-budget",)],
        )
        self.assertEqual(
            target_probe._query("SELECT task_ref, issue_id FROM task_issues ORDER BY task_ref, issue_id"),
            [("secretary-1", "recovery")],
        )
        self.assertEqual(
            target_probe._query("SELECT task_ref, supersedes FROM task_supersessions ORDER BY task_ref"),
            [("secretary-2", "secretary-1")],
        )
        self.assertIn(
            ("complete-recovery-one", True),
            target_probe._query("SELECT request_id, committed FROM board_events ORDER BY request_id"),
        )
        self.assertEqual(
            target_probe._query("SELECT DISTINCT request_id FROM sprint_decisions ORDER BY request_id"),
            [("close-recovery-sprint",)],
        )
        self.assertEqual(
            target_probe._query("SELECT project_id FROM projects ORDER BY project_id"),
            [("butler",), ("codegen-product-kit",), ("secretary",)],
        )
        collision_rows = target_probe._query(
            "SELECT task_ref, task_number, board_key, archived FROM tasks "
            "WHERE task_ref IN ('butler-1', 'codegen-product-kit-1') ORDER BY task_ref"
        )
        self.assertEqual(
            [(row[0], row[1], row[3]) for row in collision_rows],
            [("butler-1", 1, True), ("codegen-product-kit-1", 1, False)],
        )
        self.assertNotEqual(collision_rows[0][2], collision_rows[1][2])
        self.assertEqual(
            target_probe._query("SELECT path FROM repositories ORDER BY path"),
            [(str(self.root / "repository"),)],
        )
        print(
            "postgres recovery evidence:",
            json.dumps(
                {
                    "archive_bytes": result.archive.stat().st_size,
                    "dump_bytes": result.manifest["components"]["postgres_dump"]["bytes"],
                    "dump_tool": result.manifest["components"]["postgres_dump"]["tool_version"],
                    "source_head": result.manifest["components"]["postgres_dump"]["source_schema"],
                    "target_head": marker["migration_head"],
                    "table_counts": counts,
                    "idempotent_rerun": first == second,
                    "processes_started": marker["processes_started"],
                    "secrets_absent": True,
                },
                sort_keys=True,
            ),
        )

    @staticmethod
    def _down(instance: Path, project: str) -> None:
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                project,
                "--env-file",
                str(instance / "board-store.env"),
                "--file",
                str(instance / "postgres-compose.yml"),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )


class RecoveryBackupBackendBoundaryTests(unittest.TestCase):
    """Obstacle #5 of the 2026-09-09 attempt, on a real `postgres:16`.

    `postgresql_recovery_backup` runs between the fenced import and selector activation, in the
    same controller process whose earlier phases already read the live Kanboard board.  The card
    backend is decided once per process on purpose (`secretary/board/backend.py`), so exporting
    `SECRETARY_CARD_BACKEND=postgres` around the call does nothing on its own: `create_backups`
    asked the switch and got `kanboard`, and the recovery point of the whole cutover was a
    Kanboard archive.  The operator worked around it by rerunning the phase from a fresh process.
    These cases hold both halves of the repair -- the phase reaches SQL in the warmed process, and
    it hands the next phase back the backend it borrowed from.
    """

    board: PostgresBoard

    @classmethod
    def setUpClass(cls) -> None:
        cls.board = PostgresBoard()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.board.stop()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.board.fresh_database()
        self.addCleanup(self.board.drop_database, self.config.dbname)
        self.instance = self.root / "instance"
        self.instance.mkdir(mode=0o700)
        self.data = self.root / "data"
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: cutover-boundary\n"
            f"data_dir: {self.data}\noffsite:\n"
            "  instance_remote: git@example.invalid:test/boundary.git\n",
            encoding="utf-8",
        )
        (self.instance / "projects").mkdir()
        store = self.instance / "board-store.env"
        store.write_text(
            "".join(f"{key}={value}\n" for key, value in self.config.as_environ().items()),
            encoding="utf-8",
        )
        store.chmod(0o600)
        runtime = self.instance / "runtime.env"
        runtime.write_text("SECRETARY_CARD_BACKEND=kanboard\n", encoding="utf-8")
        runtime.chmod(0o600)
        init_layout(self.data)
        self.paths = cutover.Paths(self.instance, self.data)
        self.state = cutover._new_state(
            {
                "version": 1,
                "plan_id": "e" * 64,
                "expected_revision": "b" * 40,
                "source": {"fingerprint": "boundary"},
            },
            "integration",
            "card backend across the apply boundary",
        )
        self.state["controller_pid"] = os.getpid()
        # The board the earlier phases of this very process already read.  Nothing here is
        # allowed to decide the backend lazily later: `card_backend()` is called now, exactly as
        # `writer_quiescence_proof` and the fenced import call it before this phase is entered.
        self.environment = mock.patch.dict(
            os.environ, {"SECRETARY_CARD_BACKEND": "kanboard", "BOARD_ROLE": ""}, clear=False
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        reset_card_backend()
        self.addCleanup(reset_card_backend)
        from secretary.board.backend import card_backend

        self.assertEqual(card_backend(), "kanboard")

    def _exports(self, data_dir: Path, instance_dir: Path, **_kwargs) -> dict[str, DataExport]:
        board = export_board(data_dir, instance_dir=instance_dir)
        (data_dir / "memory" / "export.ndjson").write_text("", encoding="utf-8")
        runs = data_dir / "runs"
        for name, body in (
            ("watermarks.json", "{}\n"),
            ("cards.json", "{}\n"),
            ("claims.json", "{}\n"),
            ("runs.ndjson", ""),
        ):
            (runs / name).write_text(body, encoding="utf-8")
        for name in ("transcripts", "artifacts"):
            (data_dir / name / "inventory.json").write_text("{}\n", encoding="utf-8")
        return {
            "board": board,
            "memory": DataExport(data_dir / "memory" / "export.ndjson", 0, "test"),
            "runs": DataExport(runs / "runs.ndjson", 0, "test"),
            "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 0, "test"),
            "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 0, "test"),
        }

    def test_recovery_backup_dumps_sql_in_the_process_that_already_decided_kanboard(self) -> None:
        from secretary.board.backend import card_backend

        def kanboard_engine(*_args, **_kwargs):
            raise AssertionError(
                "the pre-switch recovery backup reached the Kanboard engine; the process "
                "decision did not follow the environment across the apply boundary"
            )

        freeze = {"paused": True, "mode": "freeze", "actor": cutover.CUTOVER_ACTOR}
        with (
            mock.patch("secretary.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("secretary.backup._pipeline_status", return_value=freeze),
            mock.patch("secretary.backup.export_all", side_effect=self._exports),
            mock.patch("secretary.backup.raw_kanboard_dump", side_effect=kanboard_engine),
        ):
            evidence = cutover.Operations(self.paths, self.state).postgresql_recovery_backup()

        manifests = evidence["archive_manifests"]
        self.assertEqual(len(manifests), 1)
        components = manifests[0]["components"]
        self.assertIn("postgres_dump", components)
        self.assertIn("board_history", components)
        self.assertNotIn("raw_board", components)
        self.assertEqual(
            components["postgres_dump"]["source_endpoint_id"], endpoint_identity(self.config)
        )
        self.assertGreater(components["postgres_dump"]["bytes"], 0)
        archive = Path(evidence["archives"][0])
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertIn("secretary-backup/engine/postgres.dump", names)
        self.assertNotIn("secretary-backup/secretary-data/board/data", names)

        # The other half of the boundary: activation has not happened yet, so the phase hands the
        # process back the backend it borrowed from -- environment and decision together.
        self.assertEqual(os.environ[cutover.BACKEND_ENV], "kanboard")
        self.assertEqual(card_backend(), "kanboard")

    def test_selector_activation_hands_every_later_phase_the_sql_reader(self) -> None:
        from secretary.board.backend import card_backend

        operation = cutover.Operations(self.paths, self.state)
        evidence = operation.selector_activation()
        self.assertEqual(evidence["backend"], "postgres")
        self.assertEqual(evidence["sql_audit_baseline"]["committed_events"], 0)
        self.assertEqual(os.environ[cutover.BACKEND_ENV], "postgres")
        self.assertEqual(card_backend(), "postgres")
        self.assertIn(
            "SECRETARY_CARD_BACKEND=postgres",
            (self.instance / "runtime.env").read_text(encoding="utf-8"),
        )
        # What a later phase does: build the board client through the switch and read.  Under the
        # inherited `kanboard` decision this raises instead of reaching the store.
        exported = export_board(self.data, instance_dir=self.instance)
        self.assertTrue((self.data / "board" / "audit.ndjson").exists())
        self.assertEqual(exported.count, 0)


if __name__ == "__main__":
    unittest.main()
