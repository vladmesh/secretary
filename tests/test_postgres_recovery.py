"""End-to-end PostgreSQL archive recovery on isolated Compose projects."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.backup import create_backups
from secretary.backup_verify import verify_backup
from secretary.board import migrate, provision, schema
from secretary.board.backend import reset_card_backend
from secretary.board.sql_cards import SqlCardClient
from secretary.board.store import BoardStoreConfig
from secretary.data import DataExport, export_board, init_layout
from secretary.restore import restore_postgres_backup
from secretary.sprint_observer import none_choice
from secretary.sprints import SprintWriter
from secretary.tasks import TaskWriter


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

    def _store(self, name: str) -> tuple[Path, BoardStoreConfig]:
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
        (instance / "projects" / "secretary.yaml").write_text(
            "id: secretary\nrepo: /tmp/secretary-recovery-fixture\n"
            "enabled: false\nadapter: secretary\ndefault_branch: main\n",
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
            host="127.0.0.1", port=port, dbname="secretary",
            owner_user="secretary_owner", owner_password=f"{name}-owner-secret",
            app_user=schema.APP_ROLE, app_password=f"{name}-app-secret",
            read_user=schema.READ_ROLE, read_password=f"{name}-read-secret",
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
        migrate.migrate_instance(instance)
        provision.verify_roles(instance)
        return instance, config

    def _cleanup_projects(self) -> None:
        for instance, project in reversed(self.projects):
            config = instance / "board-store.env"
            compose = instance / "postgres-compose.yml"
            if config.exists() and compose.exists():
                subprocess.run(
                    [
                        "docker", "compose", "--project-name", project,
                        "--env-file", str(config), "--file", str(compose),
                        "down", "--volumes", "--remove-orphans",
                    ],
                    check=False, capture_output=True, text=True, timeout=180,
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
                "saveTaskMetadata", task_id=product_key,
                values={
                    "record_type": "product", "product_id": "secretary",
                    "product_projects": '["secretary"]', "future_product_key": "opaque",
                },
            )
            issue_key = client.call(
                "createTask", project_id=1, title="Recovery", reference="issue:recovery"
            )
            client.call(
                "saveTaskMetadata", task_id=issue_key,
                values={
                    "record_type": "issue", "issue_product": "secretary",
                    "issue_kind": "feature", "issue_priority": "P1",
                },
            )
        writer = TaskWriter(client, data_dir=data_dir)
        sprint_ref = SprintWriter(client, data_dir=data_dir, instance=self.source_instance).create(
            role="po", actor="test", goal="prove recovery", repositories=[],
            product="secretary", issues=["issue:recovery"], projects=["secretary"],
            observer=none_choice(), reference="sprint:recovery-custom",
            request_id="create-nullable-number-sprint",
        )["sprint"]["ref"]
        first = writer.create(
            role="po", actor="test", project="secretary", task_type="code",
            title="Recover me", target="ready", reference="secretary-1", sprint=sprint_ref,
            sprint_override=True, sprint_override_reason="integration fixture",
            request_id="create-recovery-one",
        )["task"]
        writer.create(
            role="po", actor="test", project="secretary", task_type="code",
            title="Dependent", target="ready", reference="secretary-2", sprint=sprint_ref,
            blocked_by="secretary-1", request_id="create-recovery-two",
            sprint_override=True, sprint_override_reason="integration fixture",
        )
        client.call(
            "saveTaskMetadata", task_id=int(str(first["id"]).rsplit("_", 1)[1]),
            values={"future_task_key": "opaque"},
        )
        client.call(
            "moveTaskPosition", project_id=1,
            task_id=int(str(first["id"]).rsplit("_", 1)[1]), column_id=7, position=1,
        )
        client.call("closeTask", task_id=int(str(first["id"]).rsplit("_", 1)[1]))
        writer.comment(
            role="worker", actor="test", reference="secretary-1",
            body="post-close evidence", request_id="post-close-comment",
        )
        client.connection.close()

    def _exports(self, data_dir: Path, instance_dir: Path, **_kwargs) -> dict[str, DataExport]:
        board = export_board(data_dir, instance_dir=instance_dir)
        (data_dir / "memory" / "export.ndjson").write_text("", encoding="utf-8")
        runs = data_dir / "runs"
        for name, body in (
            ("watermarks.json", "{}\n"), ("cards.json", "{}\n"),
            ("claims.json", "{}\n"), ("runs.ndjson", ""),
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

    def test_full_backup_destroy_source_restore_target_and_rerun(self) -> None:
        self._seed()
        with (
            mock.patch("secretary.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("secretary.backup._pipeline_status", return_value={"paused": False}),
            mock.patch("secretary.backup._pipeline_action", return_value=None),
            mock.patch("secretary.backup.export_all", side_effect=self._exports),
        ):
            result = create_backups(self.source_instance)[0]
        verified = verify_backup(result.archive)
        self.assertEqual(verified.code, 0, verified.findings)
        self.assertNotIn("raw_board", result.manifest["components"])
        counts = result.manifest["components"]["postgres_dump"]["table_counts"]
        self.assertEqual(counts["products"], 1)
        self.assertEqual(counts["issues"], 1)
        self.assertEqual(counts["tasks"], 2)
        self.assertEqual(counts["sprints"], 1)
        self.assertEqual(counts["task_dependencies"], 1)
        self.assertEqual(counts["task_comments"], 1)
        self.assertGreaterEqual(counts["requests"], 4)
        self.assertGreater(result.manifest["components"]["postgres_dump"]["bytes"], 0)
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
        marker = json.loads(
            (self.root / "target-data" / "postgres-restore.json").read_text(encoding="utf-8")
        )
        self.assertFalse(marker["processes_started"])
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
                "docker", "compose", "--project-name", project,
                "--env-file", str(instance / "board-store.env"),
                "--file", str(instance / "postgres-compose.yml"),
                "down", "--volumes", "--remove-orphans",
            ],
            check=True, capture_output=True, text=True, timeout=180,
        )


if __name__ == "__main__":
    unittest.main()
