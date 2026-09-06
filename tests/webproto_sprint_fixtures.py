"""The installation fixture the two sprint suites are both built on.

Support, and one-way: the layer suite (`tests/test_web_sprint_protocol.py`) and the transport suite
(`tests/test_web_sprint_transport.py`) both need one instance, one Product/Issue board, one
installed head registry and one data plane, and they need the *same* one -- a transport test that
built its own would drift from the layer it is supposed to be driving. Nothing here imports a test
module; see `tests/test_architecture.py::SourceLayoutTests`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from secretary.sprint_observer import OBSERVER_FIELD
from secretary.sprints import SPRINT_BOARD_NAME, ensure_sprint_board
from secretary.webproto.errors import OperationPending, ReadError
from secretary.webproto.sprint_ops import PENDING_REASON, SprintOperationLayer
from secretary.webproto.sprint_reads import SprintReadLayer
from tests.fakes.sprints import ProductSprintKanboard
from tests.head_registry import write_installed_pair

OBSERVER_PROFILE = "codex-observer"
WORKER_PROFILE = "claude-worker"
REVIEWER_PROFILE = "codex-reviewer"

#: A registry of exactly three profiles, each pinning a different model and effort so that the
#: catalogue has something to reflect. `retired-observer` is deliberately not in it: it is what the
#: tests declare when they want a profile this installation does not have.
HEAD_SNAPSHOT = yaml.safe_dump(
    {
        "resources": {
            "claude-sub": {"account": "claude-subscription"},
            "openai-sub": {"account": "openai-subscription"},
        },
        "profiles": {
            OBSERVER_PROFILE: {
                "adapter": "codex",
                "resource": "openai-sub",
                "model": "gpt-5.6-terra",
                "effort": "high",
            },
            WORKER_PROFILE: {"adapter": "claude", "resource": "claude-sub", "model": "opus"},
            REVIEWER_PROFILE: {
                "adapter": "codex",
                "resource": "openai-sub",
                "model": "gpt-5.6-sol",
                "effort": "medium",
            },
        },
        "role_defaults": {
            "new_card": WORKER_PROFILE,
            "reviewer": REVIEWER_PROFILE,
            "observer": OBSERVER_PROFILE,
        },
    },
    sort_keys=False,
)


class SprintProtocolFixture(unittest.TestCase):
    """One instance, one Product/Issue board, one installed head registry, one data plane."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data_dir = self.tmp / "data"
        for relative in ("board", "dispatcher", "sprints"):
            (self.data_dir / relative).mkdir(parents=True)
        self.board = ProductSprintKanboard()
        self.instance = self._instance()
        self._production({})
        self.clock = 1788652800.0

    # -- fixture pieces ------------------------------------------------------------------------

    def _instance(self) -> Path:
        instance_dir = self.tmp / "instance"
        (instance_dir / "projects").mkdir(parents=True)
        (instance_dir / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        for project in ("secretary", "secretary-instance", "other"):
            repo = self.tmp / "repos" / project
            repo.mkdir(parents=True)
            (instance_dir / "projects" / f"{project}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "id": project,
                        "repo": str(repo),
                        "enabled": True,
                        "adapter": "secretary",
                        "default_branch": "main",
                    }
                ),
                encoding="utf-8",
            )
        write_installed_pair(instance_dir, HEAD_SNAPSHOT)
        return instance_dir

    def _production(
        self, observers: dict[str, Any], records: dict[str, Any] | None = None
    ) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").write_text(
            json.dumps(
                {"phase": "production", "records": records or {}, "observers": observers}
            ),
            encoding="utf-8",
        )

    # -- the layers under test -----------------------------------------------------------------

    def ops(self, **kwargs) -> SprintOperationLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return SprintOperationLayer(self.instance, **options)

    def reads(self, **kwargs) -> SprintReadLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return SprintReadLayer(self.instance, **options)

    def create(self, **kwargs) -> dict[str, Any]:
        """Open the fixture's sprint: its product, its open issue, one registered project."""
        request = {
            "request_id": "req-1",
            "actor": "operator",
            "product": "secretary",
            "goal": "Give webproto a sprint create",
            "definition_of_done": "the operation exists and is tested",
            "issues": ["issue:open"],
            "projects": ["secretary"],
            "observer": OBSERVER_PROFILE,
        }
        request.update(kwargs)
        return self.ops().sprint_create(**request)

    def add_sprint_row(
        self,
        reference: str,
        *,
        status: str = "open",
        goal: str = "an installed sprint",
        current_task: str | None = None,
        observer: str | None = OBSERVER_PROFILE,
        resume: dict[str, Any] | None = None,
    ) -> str:
        """Put a sprint on the board directly, as the installation's history holds one.

        A read is the subject here, and going through the writer for every row would buy nothing but
        the admission rules: two open sprints, sixty closed ones and a sprint that ended on a card
        are all states this installation is really in, and none of them can be reached by opening
        sixty sprints in a test. The writes this fixture does exercise go through `create`.
        """
        board = ensure_sprint_board(self.board)
        column = self.board.columns[board][0]["id"]
        task_id = int(
            self.board.call(
                "createTask", project_id=board, title=goal, column_id=column, reference=reference
            )
        )
        values = {
            "sprint_goal": goal,
            "sprint_definition_of_done": "stated when the sprint was opened",
            "sprint_status": status,
            "sprint_product": "secretary",
            "sprint_issues": json.dumps(["issue:open"]),
            "sprint_reservations": json.dumps([]),
        }
        if current_task is not None:
            values["sprint_current_task"] = current_task
        if observer is not None:
            values[OBSERVER_FIELD] = json.dumps({"kind": "head", "profile": observer})
        if resume is not None:
            values["sprint_resume"] = json.dumps(resume)
        self.board.call("saveTaskMetadata", task_id=task_id, values=values)
        return reference

    # -- what the board holds ------------------------------------------------------------------

    def sprint_rows(self) -> list[dict[str, Any]]:
        board = self.board.projects.get(SPRINT_BOARD_NAME)
        if board is None:
            return []
        return [
            task
            for task in self.board.tasks
            if task["project_id"] == board and str(task.get("reference") or "").startswith("sprint:")
        ]

    def metadata_of(self, reference: str) -> dict[str, str]:
        row = next(task for task in self.sprint_rows() if task["reference"] == reference)
        return self.board.metadata[int(row["id"])]

    def reference_of(self, document: dict[str, Any]) -> str:
        return str(document["sprint"]["ref"])

    def assert_pending_after_create(self, refused: OperationPending, *, cause: str) -> dict[str, Any]:
        """The one answer every post-create failure owes, whichever primitive raised it.

        Returns the single sprint row that exists, so a caller can go on to prove the repeat picks
        that one up rather than opening a second.
        """
        # Typed, and not the primitive's own vocabulary: a caller that catches `ReadError` has it.
        self.assertIsInstance(refused, ReadError)
        self.assertEqual(refused.code, "backend_unavailable")
        self.assertEqual(refused.data["reason"], PENDING_REASON)
        action = refused.data["action"]
        self.assertTrue(action["repeat_request"])
        self.assertEqual(action["request_id"], "req-1")
        self.assertEqual(action["operation"], "sprint_create")
        rows = self.sprint_rows()
        self.assertEqual(len(rows), 1)
        reference = str(rows[0]["reference"])
        self.assertEqual(action["reference"], reference)
        # The durable fact outranks the cause, in the message as well as in the data: a caller that
        # reads the reason first and acts on it opens a second sprint.
        message = refused.message
        self.assertIn(cause, message)
        self.assertLess(message.index(reference), message.index(cause))
        self.assertLess(message.index("repeat the same"), message.index(cause))
        return rows[0]
