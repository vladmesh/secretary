"""The installation fixture the pause protocol suite is built on.

Support, and one-way: it is the sprint layer's fixture -- one instance, one Product/Issue board, one
Pipeline board, one installed head registry, one data plane -- with the piece the pause needs added
to it, which is a real `DispatcherRuntime` over that same data plane. The pause and the sprints are
read from one installation on purpose: the scope read's whole claim is that a pipeline-wide pause
covers every open sprint of the installation, and a fixture that kept two would not be able to
show it.

Every pause, resume, conflict and freeze in the suite is driven against this fixture and its
`FakeHost`. Nothing here touches a live installation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from secretary.dispatcher import DispatcherRuntime
from secretary.tasks import TaskAudit, TaskReader, TaskWriter
from secretary.webproto.pause_ops import PauseOperationLayer
from secretary.webproto.pause_reads import PauseReadLayer
from tests.fakes.dispatcher import FakeCatalog, FakeHost, FakeSprints
from tests.webproto_sprint_fixtures import SprintProtocolFixture

#: The card the fixture's Pipeline board already holds, in Ready.
EXISTING_CARD = "secretary-12"


class PauseProtocolFixture(SprintProtocolFixture):
    """One installation, one data plane, one dispatcher runtime over it."""

    def setUp(self) -> None:
        super().setUp()
        # A pause mirrors itself into the legacy flag the background roles read, and that flag is
        # resolved from the environment. Without this every pause here would write into the state
        # directory the whole test process shares, and the next suite to ask whether the pipeline
        # is paused would be answered by this one's drain (`tests/test_hermetic_pipeline_state.py`).
        legacy = mock.patch.dict(
            os.environ, {"SECRETARY_LEGACY_PAUSE_FILE": str(self.tmp / "legacy-pause.json")}
        )
        legacy.start()
        self.addCleanup(legacy.stop)
        self.host = FakeHost(self.data_dir / "workspaces", FakeCatalog(instance_dir=self.instance))
        self.runtime = DispatcherRuntime(
            TaskReader(self.board),  # type: ignore[arg-type]
            TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir),  # type: ignore[arg-type]
            TaskAudit(self.data_dir),
            self.data_dir,
            FakeCatalog(instance_dir=self.instance),  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-dispatcher",
            sprints=FakeSprints(),
        )

    # -- the layers under test -----------------------------------------------------------------

    def pause_ops(self, **kwargs: Any) -> PauseOperationLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "runtime": self.runtime,
            "board_client": self.board,
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return PauseOperationLayer(self.instance, **options)

    def pause_reads(self, **kwargs: Any) -> PauseReadLayer:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "board_client": self.board,
            "clock": lambda: self.clock,
        }
        options.update(kwargs)
        return PauseReadLayer(self.instance, **options)

    # -- the state a case wants ----------------------------------------------------------------

    def link_card(self, reference: str, sprint: str, *, state: str = "ready") -> None:
        """Put one of the Pipeline board's cards inside a sprint, as the board records it."""
        task = next(task for task in self.board.tasks if task["reference"] == reference)
        columns = {"ready": 2, "in_progress": 3, "validate": 4, "blocked": 5, "done": 6}
        task["column_id"] = columns[state]
        self.board.call("saveTaskMetadata", task_id=int(task["id"]), values={"sprint_ref": sprint})

    def tracked_head(self, reference: str = EXISTING_CARD, **record: Any) -> None:
        """A dispatcher record for one card with a live worker head on it."""
        self._production(
            {},
            {
                reference: {
                    "state": "claimed",
                    "handle": "pane-1",
                    "worker_leaf": "leaf-1",
                    "workspace": str(self.data_dir / "workspaces" / reference),
                    **record,
                }
            },
        )

    def pause_file(self) -> Path:
        return self.data_dir / "dispatcher" / "pause.json"

    def pause_payload(self) -> dict[str, Any]:
        return json.loads(self.pause_file().read_text(encoding="utf-8"))

    def unreadable(self, path: Path) -> None:
        """A file of the data plane that cannot be parsed: the fault every source read must survive."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

    def production_path(self) -> Path:
        return self.data_dir / "dispatcher" / "production-state.json"

    def data_plane(self) -> dict[str, bytes]:
        """Every file of this installation's data plane, so a write anywhere in it is visible."""
        return {
            str(path.relative_to(self.data_dir)): path.read_bytes()
            for path in sorted(self.data_dir.rglob("*"))
            if path.is_file()
        }

    # -- reading a document --------------------------------------------------------------------

    @staticmethod
    def source_of(document: dict[str, Any], *path: str) -> dict[str, Any]:
        node: Any = document
        for step in path:
            node = node[step]
        return dict(node["source"])

    def assert_available(self, document: dict[str, Any], *path: str) -> None:
        self.assertEqual(self.source_of(document, *path)["state"], "available")

    def assert_unavailable(self, document: dict[str, Any], *path: str) -> dict[str, Any]:
        source = self.source_of(document, *path)
        self.assertEqual(source["state"], "unavailable")
        return source
