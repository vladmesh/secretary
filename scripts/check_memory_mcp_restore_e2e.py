#!/usr/bin/env python3
"""Required in-repository gate for secretary's memory restore handoff.

Run with ``SECRETARY_MEMORY_TEST_PYTHON=/path/to/python``. The selected Python
must provide the ``secretary[memory]`` dependencies. A missing environment is a
failure; no external memory-mcp checkout is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, stdout=subprocess.DEVNULL)


def _fact(tags: str, text: str) -> str:
    return (
        "---\n"
        f"tags: [{tags}]\n"
        "source: secretary-restore-e2e\n"
        "created: 2026-07-16\n"
        "pinned: false\n"
        "---\n"
        f"{text}\n"
    )


class _BoardFixture:
    """Supported-board fixture that exercises TaskReader and TaskWriter."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, object]] = []
        self.metadata: dict[int, dict[str, str]] = {}

    def call(self, method: str, **params: object) -> object:
        if method == "getProjectByName":
            return {"id": 7}
        if method == "getColumns":
            return [
                {"id": 1, "title": "Ideas"}, {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"}, {"id": 6, "title": "Done"},
            ]
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            return self.tasks
        if method == "getTaskByReference":
            return next((task for task in self.tasks if task["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "getAllComments":
            return []
        if method == "createTask":
            task_id = len(self.tasks) + 1
            self.tasks.append({
                "id": task_id, "reference": "", "title": params["title"],
                "description": params.get("description", ""), "column_id": params["column_id"],
                "position": task_id, "swimlane_id": params.get("swimlane_id") or 0,
                "date_creation": "1720000200", "date_modification": "1720000200",
            })
            self.metadata[task_id] = {}
            return task_id
        if method == "updateTask":
            task = next(task for task in self.tasks if task["id"] == params["id"])
            if "reference" in params:
                task["reference"] = params["reference"]
            return True
        if method == "moveTaskPosition":
            self.tasks[0]["column_id"] = params["column_id"]
            return True
        if method == "saveTaskMetadata":
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        raise AssertionError(f"unexpected board method: {method}")


def main() -> int:
    python = Path(os.environ.get("SECRETARY_MEMORY_TEST_PYTHON", "")).expanduser()
    if not python.is_file():
        print("secretary memory restore e2e: test Python is unavailable", file=sys.stderr)
        return 2
    if Path(sys.executable).absolute() != python.absolute():
        os.execv(str(python), [str(python), str(Path(__file__).resolve())])

    sys.path.insert(0, str(ROOT))
    try:
        import numpy as np
        from secretary import memory_service as server
        from secretary import state_repo
        from secretary.cli import main as secretary_main
        from secretary.config import validate_instance
        from secretary.data import export_memory, normalize_board_card
        from secretary.host import CollectResult, HostInventory, build_plan
        from secretary.restore import (
            bootstrap_empty,
            import_normalized_board,
            rebuild_memory_index,
            restore_findings,
            restore_state,
        )
        import secretary.restore_commands as restore_commands
    except ImportError as error:
        print(f"secretary memory restore e2e: dependency unavailable: {error}", file=sys.stderr)
        return 2

    def fake_embed(text: str) -> np.ndarray:
        vector = np.zeros(4, dtype=np.float32)
        vector[0 if "alpha" in text.lower() else 1] = 1.0
        return vector

    with tempfile.TemporaryDirectory(prefix="secretary-memory-restore-") as temporary:
        data_dir = Path(temporary) / "secretary-data"
        instance = Path(temporary) / "instance"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\n"
            "name: restore-e2e\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:restore/e2e.git\n"
            "host:\n  unit_prefix: secretary-\n",
            encoding="utf-8",
        )
        # Bootstrap creates the target. The normalized board and facts below model
        # the data restored into that target from a verified core archive.
        bootstrap_empty(instance)
        card = normalize_board_card(
            {
                "id": 1, "reference": "secretary-restore-e2e", "title": "Restore e2e",
                "column": "Ready", "swimlane": "Secretary", "position": 1,
                "task_type": "code", "project": "secretary",
            },
            {
                "id": 1, "reference": "secretary-restore-e2e", "title": "Restore e2e",
                "description": "cross-repository restore fixture", "column": "Ready",
                "task_type": "code", "project": "secretary", "comments": [],
                "metadata": {"resolved_head": "", "resolved_review_head": ""},
            },
        )
        (data_dir / "board" / "cards.json").write_text(
            json.dumps({"version": 1, "cards": [card]}), encoding="utf-8"
        )
        # Canon is `state/memory/facts` in the private repo, not the data dir.
        _run(["git", "init", "--quiet", "--initial-branch", "main"], cwd=instance)
        _run(["git", "config", "user.name", "restore-e2e"], cwd=instance)
        _run(["git", "config", "user.email", "restore-e2e@example.invalid"], cwd=instance)
        facts = state_repo.memory_facts_dir(instance)
        (facts / "global").mkdir(parents=True)
        (facts / "global" / "alpha.md").write_text(
            _fact("restore", "Alpha restore fact is searchable."), encoding="utf-8"
        )
        (facts / "global" / "beta.md").write_text(
            _fact("restore", "Beta restore fact remains readable."), encoding="utf-8"
        )
        _run(["git", "add", "."], cwd=instance)
        _run(["git", "commit", "-m", "restore facts"], cwd=instance)
        export_memory(data_dir, instance)
        if import_normalized_board(data_dir, client=_BoardFixture()) != 1:
            raise AssertionError("normalized board import did not restore one card")

        def rebuild(canon: Path, export: Path, target: Path) -> dict:
            return server.offline_rebuild(
                canon, export, target, "secretary-restore-e2e", 4, document_embed=fake_embed
            )

        indexed = rebuild_memory_index(data_dir, instance, runner=rebuild)
        if indexed != 2 or restore_state(data_dir).get("memory_index") != "complete":
            raise AssertionError("secretary did not record a complete memory rebuild")

        database = data_dir / "memory" / "index.sqlite"
        server.DB_PATH = str(database)
        server.CANON = facts
        server.CANON_EXPORT = data_dir / "memory" / "export.ndjson"
        server.embed_doc = fake_embed
        server.embed_query = fake_embed
        server.mark_search_ready()
        rows = server.memory_list(limit=10)
        if len(rows) != 2:
            raise AssertionError(f"memory_list returned {len(rows)} facts, expected 2")
        alpha = next(row for row in rows if "Alpha" in row["text"])
        if server.memory_get(alpha["id"])["text"] != alpha["text"]:
            raise AssertionError("memory_get did not read the rebuilt index")
        hits = server.memory_search("alpha restore", k=1, caller="worker")
        if not isinstance(hits, list) or not hits or hits[0]["id"] != alpha["id"]:
            raise AssertionError("memory_search did not read the rebuilt index")
        if "memory index has not been rebuilt" in restore_findings(data_dir):
            raise AssertionError("restore state failed memory parity")

        report = validate_instance(instance)
        desired = build_plan(report.instance, report.bindings)
        (data_dir / "host-managed.json").write_text(
            json.dumps({"version": 1, "resources": [resource.__dict__ for resource in desired]}),
            encoding="utf-8",
        )
        fixture = Path(temporary) / "host-fixture"
        fixture.mkdir()
        (fixture / "units.txt").write_text(
            "\n".join(resource.name for resource in desired if resource.kind == "unit"), encoding="utf-8"
        )
        if secretary_main([
            "reconcile", "plan", "--instance", str(instance), "--host-fixture", str(fixture),
        ]) != 0:
            raise AssertionError("reconcile plan did not confirm the restored desired state")
        inventory = HostInventory(units={resource.name for resource in desired if resource.kind == "unit"})
        source = mock.Mock()
        source.collect.return_value = CollectResult(inventory=inventory)
        with mock.patch.object(restore_commands, "LiveHostSource", return_value=source):
            if secretary_main(["restore-reconcile", "--instance", str(instance)]) != 0:
                raise AssertionError("restore reconcile did not confirm live managed state")
        if restore_findings(data_dir):
            raise AssertionError(f"restore state is not clean: {restore_findings(data_dir)}")
        if secretary_main(["doctor", "--offline", "--instance", str(instance)]) != 0:
            raise AssertionError("doctor did not report the completed restore as healthy")

    print("secretary memory restore e2e: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
