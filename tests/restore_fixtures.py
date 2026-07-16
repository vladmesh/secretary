from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from secretary._fsutil import sha256_file
from secretary.backup_policy import ARCHIVE_ROOT
from secretary.data import DataExport, export_memory, init_layout, normalize_board_card
from tests.test_tasks import WriteKanboard


class _EmptyWriteKanboard(WriteKanboard):
    def __init__(self) -> None:
        super().__init__()
        self.tasks = []
        self.metadata = {}
        self.next_task_id = 12

    def call(self, method: str, **params: object) -> object:
        if method == "createTask":
            self.calls.append((method, params))
            task_id = self.next_task_id
            self.next_task_id += 1
            self.tasks.append(
                {
                    "id": task_id, "reference": "", "title": params["title"],
                    "description": params.get("description", ""), "column_id": params["column_id"],
                    "position": 1, "swimlane_id": params.get("swimlane_id") or 0,
                    "date_creation": "1720000200", "date_modification": "1720000200",
                }
            )
            self.metadata[task_id] = {}
            self.comments[task_id] = []
            return task_id
        return super().call(method, **params)


def _write_instance(root: Path, name: str) -> Path:
    return _write_instance_to(root / "instance", name, root / "secretary-data")


def _write_instance_to(
    instance: Path, name: str, data_dir: Path, *, host: bool = False, heads: bool = False,
    reindex: dict[str, object] | None = None,
) -> Path:
    instance.mkdir()
    host_block = "host:\n  unit_prefix: secretary-\n" if host or reindex else ""
    for key, value in (reindex or {}).items():
        host_block += f"  {key}: {value}\n"
    heads_block = "heads:\n  - role: worker\n    model: test-model\n" if heads else ""
    text = (
        "version: 1\n"
        f"name: {name}\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n"
        + host_block
        + heads_block
    )
    (instance / "instance.yaml").write_text(text, encoding="utf-8")
    return instance


def _restore_card(
    *, task_id: int = 12, reference: str = "secretary-1", title: str = "Restore",
    description: str = "body", column: str = "Ready", swimlane: str = "Secretary",
    position: int = 1, comments: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return normalize_board_card(
        {
            "id": task_id, "reference": reference, "title": title, "column": column,
            "swimlane": swimlane, "position": position, "task_type": "code", "project": "secretary",
        },
        {
            "id": task_id, "reference": reference, "title": title, "description": description,
            "column": column, "task_type": "code", "project": "secretary",
            "comments": comments or [],
            "metadata": {"complexity": "standard", "family_preference": "auto"},
        },
    )


def _prepare_producer_data(data_dir: Path) -> None:
    init_layout(data_dir)
    journal = data_dir / "memory" / "facts"
    (journal / "fact.md").write_text("# fact\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=journal, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=journal, check=True, stdout=subprocess.DEVNULL)
    export_memory(data_dir)
    board = data_dir / "board"
    cards = [{"reference": "secretary-1", "column": "Ready"}]
    (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
    (board / "cards.ndjson").write_text("".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8")
    (board / "export.json").write_text("{}", encoding="utf-8")
    raw = board / "kanboard-raw-test"
    (raw / "data").mkdir(parents=True)
    (raw / "manifest.json").write_text("{}", encoding="utf-8")
    (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
    runs = data_dir / "runs"
    for name in ("watermarks.json", "cards.json", "claims.json"):
        (runs / name).write_text("{}", encoding="utf-8")
    (runs / "runs.ndjson").write_text("", encoding="utf-8")
    for component in ("transcripts", "artifacts"):
        directory = data_dir / component
        (directory / "inventory.json").write_text("{}", encoding="utf-8")
    (data_dir / "artifacts" / "report.pdf").write_bytes(b"report")


def _producer_exports(
    data_dir: Path, *, board: int = 1, memory: int = 1, artifacts: int = 1, source: str = "test",
) -> dict[str, DataExport]:
    return {
        "board": DataExport(data_dir / "board" / "cards.json", board, source),
        "memory": DataExport(data_dir / "memory" / "export.ndjson", memory, source),
        "runs": DataExport(data_dir / "runs" / "runs.ndjson", 0, source),
        "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 0, source),
        "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", artifacts, source),
    }


def _core_archive(root: Path, name: str) -> Path:
    payload = root / ARCHIVE_ROOT
    board = payload / "secretary-data" / "board"
    memory = payload / "secretary-data" / "memory" / "facts"
    runs = payload / "secretary-data" / "runs"
    board.mkdir(parents=True)
    memory.mkdir(parents=True)
    (memory / "fact.md").write_text("# fact\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=memory, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=memory, check=True)
    subprocess.run(["git", "add", "."], cwd=memory, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    (memory / "second-fact.md").write_text("# second fact\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=memory, check=True)
    subprocess.run(["git", "commit", "-m", "second fact"], cwd=memory, check=True, stdout=subprocess.DEVNULL)
    shutil.rmtree(memory / ".git" / "hooks")
    (memory / ".git" / "config").unlink()
    runs.mkdir(parents=True)
    (payload / "instance").mkdir()
    (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    (payload / "secretary-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
    cards = [{"reference": "secretary-1", "column": "Ready"}]
    (board / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")
    (board / "cards.ndjson").write_text(
        "".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8"
    )
    (board / "export.json").write_text("{}", encoding="utf-8")
    (payload / "secretary-data" / "memory" / "export.ndjson").write_text(
        '{"id":"fact"}\n{"id":"second-fact"}\n', encoding="utf-8"
    )
    for filename in ("watermarks.json", "cards.json", "claims.json"):
        (runs / filename).write_text("{}", encoding="utf-8")
    manifest = {
        "version": 1,
        "backup_kind": "core",
        "instance": {"identity": {"name": name, "instance_remote": "git@example.invalid:test/instance.git"}},
        "components": {
            "board": {"path": "board/cards.json", "count": len(cards)},
            "memory": {"path": "memory/export.ndjson", "count": 2},
            "runs_state": {
                "path": "runs/watermarks.json",
                "cards": "runs/cards.json",
                "claims": "runs/claims.json",
            },
        },
    }
    _write_checksums(payload, manifest)
    (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / "backup.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname=ARCHIVE_ROOT)
    return archive


def _full_archive(root: Path, name: str) -> Path:
    _core_archive(root, name)
    payload = root / ARCHIVE_ROOT
    raw = payload / "secretary-data" / "board" / "kanboard-raw-test"
    (raw / "data").mkdir(parents=True)
    (raw / "manifest.json").write_text("{}", encoding="utf-8")
    (raw / "data" / "db.sqlite").write_bytes(b"sqlite")
    runs = payload / "secretary-data" / "runs"
    (runs / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    for component in ("transcripts", "artifacts"):
        directory = payload / "secretary-data" / component
        directory.mkdir()
        (directory / "inventory.json").write_text("{}", encoding="utf-8")
    debug = payload / "debug" / "orca-state"
    debug.mkdir(parents=True)
    (debug / "inventory.json").write_text("{}", encoding="utf-8")
    manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
    manifest["backup_kind"] = "full"
    manifest["components"] = {
        "raw_board": {"path": "board/kanboard-raw-test"},
        "board": {"path": "board/cards.json"},
        "memory": {"path": "memory/export.ndjson"},
        "runs_state": {"path": "runs/watermarks.json", "cards": "runs/cards.json", "claims": "runs/claims.json"},
        "runs": {"path": "runs/runs.ndjson"},
        "transcripts": {"path": "transcripts/inventory.json"},
        "artifacts": {"path": "artifacts/inventory.json"},
        "debug_orca_state": {"path": "debug/orca-state/inventory.json"},
    }
    _write_checksums(payload, manifest)
    (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / "full.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname=ARCHIVE_ROOT)
    return archive


def _write_checksums(payload: Path, manifest: dict[str, object]) -> None:
    checksums: dict[str, str] = {}
    for path in sorted(payload.rglob("*")):
        if path.is_file() and path.name != "versions.json":
            checksums[path.relative_to(payload).as_posix()] = sha256_file(path)
    manifest["checksums"] = checksums


def _git_history(journal: Path) -> list[str]:
    result = subprocess.run(
        ["git", "rev-list", "HEAD"],
        cwd=journal,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.splitlines()
