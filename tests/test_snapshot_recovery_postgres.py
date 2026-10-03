"""`ummanu recover` from an exporter snapshot into a real PostgreSQL board store.

The source installation writes its board into one database of a throwaway `postgres:16`, and a real
`SnapshotExporter` window cuts it into a bare repository that is pushed to a local bare remote. The
recovery target is a second, empty database. Recovery then runs through `install()` for real: the
bare clone, the manifest check, the live root, the secret store step, the checkpoint, the board and
sprint import with parity, the memory reindex (only the embedding model is stood in for) and the
head registry regeneration. Project checkouts, CODEX_HOME and the host steps other than the head
registry are host provisioning and stay out, as in `tests/test_fresh_postgres_install.py`.

`board-store.env` is a host-local file: bootstrap's provisioning writes it, never the snapshot. The
test writes it into the live root right after the clone step, where bootstrap would have left it.
"""

from __future__ import annotations

import getpass
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import PRODUCT_ROOT
from tests.fakes.snapshot_remote import HEAD, exporter_remote, git
from tests.sql_backend_fixtures import PostgresBoard
from ummanu import installation, upgrade
from ummanu.board import store
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.store import BoardStoreConfig
from ummanu.checkpoint import SNAPSHOT_BASE_REF, SNAPSHOT_REF, SnapshotExporter, tick_checkpoint_writer
from ummanu.data import init_layout
from ummanu.head_registry import installed_heads, installed_pair
from ummanu.memory_journal import export_memory_snapshot, verify_memory_journal
from ummanu.restore import DEFAULT_MEMORY_DIM, restore_state
from ummanu.sprint_observer import none_choice
from ummanu.sprints import SprintReader, SprintWriter, sprint_client
from ummanu.tasks import TaskWriter


def _write_store_file(instance: Path, config: BoardStoreConfig) -> None:
    """`board-store.env` for one database, private and outside the export, as `provision` leaves it."""
    store.ensure_ignored(instance)
    path = store.store_path(instance)
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in config.as_environ().items()), encoding="utf-8"
    )
    path.chmod(0o600)


class _Embedder:
    """A deterministic stand-in for the fastembed model: the index is real, the vectors are not."""

    def embed_many(self, texts: list[str]):
        import numpy as np

        vectors = []
        for text in texts:
            raw = np.arange(1, DEFAULT_MEMORY_DIM + 1, dtype=np.float32) * (1 + len(text) % 7)
            vectors.append(raw / np.linalg.norm(raw))
        return vectors

    def __call__(self, text: str):
        return self.embed_many([text])[0]


class SnapshotRecoveryPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-recovery-postgres-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        board = PostgresBoard.shared()
        self.source_config = board.fresh_database()
        self.target_config = board.fresh_database()
        self.fixture = exporter_remote(self.root, cut=False)
        _write_store_file(self.fixture.source, self.source_config)
        init_layout(self.fixture.source_data)
        self._seed_source()
        state_dir = self.root / "source-pipeline-state"
        state_dir.mkdir()
        self.tip = self.fixture.cut(stand_in=False, state_dir=state_dir)

    def _seed_source(self) -> None:
        """A product, an issue, two cards and a closed sprint, written by the source's own writers."""
        source, data_dir = self.fixture.source, self.fixture.source_data
        client = SqlCardClient(self.source_config.for_role("owner"), source)
        self.addCleanup(client.close)
        with client.transaction():
            product = client.call("createTask", project_id=1, title="Ummanu", reference="product:ummanu")
            client.call(
                "saveTaskMetadata",
                task_id=product,
                values={"record_type": "product", "product_id": "ummanu", "product_projects": '["ummanu"]'},
            )
            issue = client.call("createTask", project_id=1, title="Recovery", reference="issue:recovery")
            client.call(
                "saveTaskMetadata",
                task_id=issue,
                values={
                    "record_type": "issue",
                    "issue_product": "ummanu",
                    "issue_kind": "feature",
                    "issue_priority": "P1",
                },
            )
        sprints = sprint_client(source)
        self.addCleanup(sprints.close)
        sprint_writer = SprintWriter(sprints, data_dir=data_dir, instance=source)
        sprint = sprint_writer.create(
            role="po",
            actor="test",
            goal="recover from the snapshot",
            repositories=[str(self.root / "repository")],
            product="ummanu",
            issues=["issue:recovery"],
            projects=["ummanu"],
            observer=none_choice(),
            reference="sprint:snapshot",
            request_id="create-snapshot-sprint",
        )["sprint"]["ref"]
        writer = TaskWriter(client, data_dir=data_dir)
        for number in (1, 2):
            writer.create(
                role="po",
                actor="test",
                project="ummanu",
                task_type="code",
                title=f"Recovered card {number}",
                target="ready",
                reference=f"ummanu-{number}",
                sprint=sprint,
                sprint_override=True,
                sprint_override_reason="integration fixture",
                request_id=f"create-snapshot-card-{number}",
            )
        writer.move(
            role="po",
            actor="test",
            reference="ummanu-1",
            target="done",
            reason="snapshot fixture complete",
            request_id="complete-snapshot-card",
            sprint_override=True,
            sprint_override_reason="integration fixture",
        )
        writer.archive(
            role="po",
            actor="test",
            reference="ummanu-1",
            reason="retain archived snapshot evidence",
            request_id="archive-snapshot-card",
        )
        sprint_writer.close(
            role="po",
            actor="test",
            reference=sprint,
            decisions={
                "issues": [
                    {"ref": "issue:recovery", "verdict": "open", "reason": "recovery stays supported"}
                ],
                "cards": [{"ref": "ummanu-2", "verdict": "drop", "reason": "fixture closes with work left"}],
            },
            reason="snapshot fixture closed",
            request_id="close-snapshot-sprint",
        )

    def test_a_snapshot_remote_recovers_into_an_empty_store_at_parity(self) -> None:
        fixture = self.fixture
        target, data_dir = fixture.target, fixture.data_dir
        summary = json.loads(
            subprocess.run(
                ["git", "-C", str(fixture.remote), "cat-file", "blob", f"{self.tip}:state/board/export.json"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout
        )
        self.assertGreaterEqual(summary["card_count"], 4)
        self.assertEqual(summary["sprint_count"], 1)
        real_checkout = installation._snapshot_checkout

        def checkout_then_provision(*args, **kwargs):
            checkout = real_checkout(*args, **kwargs)
            # Bootstrap's provisioning, not the snapshot, is where the store credential comes from.
            self.assertFalse((target / "board-store.env").exists())
            _write_store_file(target, self.target_config)
            return checkout

        def head_registry_only(context, steps=installation.STEPS):
            return upgrade.run_steps(
                context, steps=tuple(step for step in steps if step is upgrade.step_head_registry)
            )

        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote=str(fixture.remote),
            installation_user=getpass.getuser(),
            recover=True,
            adopt=False,
            dry_run=False,
            runtime_env=None,
            product_root=str(PRODUCT_ROOT),
            bootstrap_credential_file=None,
            bootstrap_credential_stdin=False,
            recovery_phrase_file=None,
            recovery_phrase_stdin=False,
            host_fixture=None,
        )
        with ExitStack() as stack:
            for patch in (
                mock.patch("ummanu.installation._snapshot_checkout", side_effect=checkout_then_provision),
                mock.patch("ummanu.installation._ensure_installation_user"),
                mock.patch("ummanu.installation._set_installation_owner"),
                mock.patch("ummanu.installation.provision_project_checkouts", return_value=[]),
                mock.patch("ummanu.installation.provision_codex_home", return_value=0),
                mock.patch("ummanu.installation.run_steps", side_effect=head_registry_only),
                mock.patch("ummanu.memory_service.build_document_embedder", return_value=_Embedder()),
            ):
                stack.enter_context(patch)
            result = installation.install(args)

        steps = {step.name: (step.status, step.detail) for step in result.steps}
        self.assertEqual(result.status, "ok", result.render())
        self.assertEqual(steps["board"], ("changed", f"{summary['card_count']} card(s) at parity"))
        # Board and sprints arrived with the counts the tree's export.json declares.
        state = restore_state(data_dir)
        self.assertEqual(state["board_parity"], "complete")
        self.assertEqual(state["board_count"], summary["card_count"])
        self.assertEqual(state["sprint_parity"], "complete")
        self.assertEqual(state["sprint_count"], summary["sprint_count"])
        sprints = sprint_client(target)
        self.addCleanup(sprints.close)
        self.assertEqual(SprintReader(sprints, data_dir=data_dir).show("sprint:snapshot")["status"], "closed")
        # The snapshot repository, its marker and the plain live root.
        repository = data_dir / "backup" / "instance.git"
        self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), self.tip)
        self.assertEqual(git(repository, "cat-file", "blob", SNAPSHOT_BASE_REF), self.tip)
        for absent in (".git", "state/board", "state/runs", "snapshot-manifest.json"):
            self.assertFalse((target / absent).exists(), absent)
        # Memory: the index rebuilt from the recovered facts verifies against the canon.
        self.assertEqual(steps["memory"], ("changed", "rebuilt index for 1 fact(s)"))
        export_memory_snapshot(data_dir, target)
        verified = verify_memory_journal(data_dir, target)
        self.assertTrue(verified.ok, verified.findings)
        # Heads regenerated into the data directory; the first tick is the exporter's.
        self.assertEqual(installed_pair(target).snapshot.parent, data_dir / "heads")
        self.assertEqual(installed_heads(target)["role_defaults"]["new_card"], HEAD)
        writer = tick_checkpoint_writer(data_dir, target)
        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, repository.resolve())


if __name__ == "__main__":
    unittest.main()
