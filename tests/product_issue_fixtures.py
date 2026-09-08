from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskError
from tests.fakes.product_issues import ProductBoard


class ProductIssueFixture:
    """Backend-neutral setup and observations for the Product/Issue contract.

    A PostgreSQL contract subclass only has to override ``make_store`` (and set
    ``BACKEND``).  Test bodies use ProductIssueStore operations and the observations
    below; the Kanboard fake remains private to this fixture.
    """

    BACKEND = "kanboard"
    KANBOARD_ONLY: ClassVar[dict[str, str]] = {}

    def setUp(self) -> None:
        super().setUp()
        reason = self.KANBOARD_ONLY.get(self._testMethodName)
        if reason and self.BACKEND != "kanboard":
            self.skipTest(reason)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
        self.store = self.make_store()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        super().tearDown()

    def make_store(self) -> ProductIssueStore:
        """The sole backend factory seam used by the shared contract."""
        self.client = ProductBoard()
        return ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)

    # Domain observations.  SQL fixtures may override these where their disposal boundary
    # makes a more direct observation appropriate.
    def product(self, product_id: str) -> dict:
        return self.store.show_product(product_id)

    def issue(self, reference: str) -> dict:
        return self.store.show_issue(reference)

    def product_project_binding(self, product_id: str) -> list[str]:
        return list(self.product(product_id)["projects"])

    def issue_product_binding(self, reference: str) -> str:
        return str(self.issue(reference)["product"])

    def issue_comments(self, reference: str) -> list[str]:
        return [str(row["text"]) for row in self.issue(reference)["history"]["comments"]]

    def issue_history(self, reference: str) -> dict:
        return dict(self.issue(reference)["history"])

    def audit_events(self) -> list[dict]:
        return self.store.audit.events()

    def request_state(self) -> dict[str, object]:
        return {
            "transactions": self.store.list_transactions(),
            "audit": self.store.audit.status(),
        }

    def lane_binding(self, reference: str) -> object:
        """Observe a record's lane through the client protocol, never fake storage."""
        return self.lane_binding_for(self.client, reference)

    def lane_store(
        self, lanes: list[dict[str, object]], *, root: Path | None = None
    ) -> tuple[object, ProductIssueStore]:
        """Create a disposable store with an arranged lane catalog."""
        client = ProductBoard()
        client.swimlanes = [dict(lane) for lane in lanes]
        case_root = root or self.root
        return client, ProductIssueStore(client, data_dir=case_root / "data", instance=case_root)

    def prepend_lane(self, client: object, lane: dict[str, object]) -> None:
        client.swimlanes.insert(0, dict(lane))

    def lane_binding_for(self, client: object, reference: str) -> object:
        row = client.call("getTaskByReference", reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible: {reference}")
        lane_id = row.get("swimlane_id")
        lanes = client.call("getActiveSwimlanes", project_id=1)
        if not isinstance(lanes, list):
            self.fail("backend returned no lane list")
        return next((lane["name"] for lane in lanes if lane.get("id") == lane_id), None)

    @contextlib.contextmanager
    def named_failure(
        self,
        boundary: str,
        *,
        result: object = False,
        error: TaskError | None = None,
    ) -> Iterator[None]:
        """Fail one named write boundary; portable bodies never name an RPC method."""
        methods = {
            "record_create": "createTask",
            "record_metadata": "saveTaskMetadata",
            "record_comment": "createComment",
            "record_close": "closeTask",
            "record_reference": "updateTask",
        }
        method = methods[boundary]
        original = self.client.call
        armed = True

        def call(name: str, **params: object) -> object:
            nonlocal armed
            if armed and name == method:
                armed = False
                if error is not None:
                    raise error
                return result
            return original(name, **params)

        self.client.call = call  # type: ignore[method-assign]
        try:
            yield
        finally:
            self.client.call = original  # type: ignore[method-assign]

    def assert_product_absent(self, product_id: str) -> None:
        with self.assertRaises(TaskError):
            self.product(product_id)
