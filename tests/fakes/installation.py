from __future__ import annotations

import json
import subprocess
from pathlib import Path

# The checkout these tests run out of, which is the one they have. Nothing resolves it for them:
# an install materializes the configured checkout or `~/secretary`, and neither exists on a machine
# that only checked this branch out somewhere.
PRODUCT_ROOT = Path(__file__).resolve().parents[2]

CARD = {
    "reference": "secretary-1",
    "title": "Recovered",
    "description": "from checkpoint",
    "column": "Ready",
    "swimlane": "secretary",
    "position": 1,
    "fields": {"project": "secretary", "task_type": "code"},
    "metadata": {"record_type": "task"},
    "comments": [],
}

SPRINT = {
    "reference": "sprint:41",
    "goal": "Ship sprint entities",
    "definition_of_done": "restore rebuilds it",
    "repositories": ["secretary"],
    "status": "closed",
    "budget": {"by_type": {"red_ci": 1}},
    "current_task": "secretary-1",
    "resume": None,
    "audit": {
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z",
        "board": "Secretary sprints",
    },
    "comments": [{"ts": "2026-07-01T10:00:00Z", "text": "[po]\\nnote"}],
}


def _checkpoint(instance: Path, data_dir: Path, *, sprints: list[dict] | None = None) -> None:
    board = instance / "state" / "board"
    runs = instance / "state" / "runs"
    facts = instance / "state" / "memory" / "facts"
    board.mkdir(parents=True)
    runs.mkdir(parents=True)
    facts.mkdir(parents=True)
    (instance / ".gitignore").write_text("runtime.env\n", encoding="utf-8")
    (instance / "instance.yaml").write_text(
        "version: 1\n"
        "name: recovered\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: placeholder\n"
        "host:\n  unit_prefix: secretary-\n",
        encoding="utf-8",
    )
    (board / "cards.ndjson").write_text(json.dumps(CARD) + "\n", encoding="utf-8")
    (board / "events.ndjson").write_text("", encoding="utf-8")
    summary = {"card_count": 1}
    if sprints is not None:
        (board / "sprints.ndjson").write_text(
            "".join(json.dumps(sprint) + "\n" for sprint in sprints), encoding="utf-8"
        )
        summary["sprint_count"] = len(sprints)
    (board / "export.json").write_text(json.dumps(summary), encoding="utf-8")
    (runs / "runs.ndjson").write_text("", encoding="utf-8")
    (runs / "claims.json").write_text('{"claims": {}}', encoding="utf-8")
    (runs / "watermarks.json").write_text('{"files": []}', encoding="utf-8")
    (runs / "export.json").write_text(
        json.dumps({"run_record_count": 0, "claim_count": 0, "watermark_count": 0}),
        encoding="utf-8",
    )
    (facts / "fact.md").write_text("# recovered fact\n", encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
