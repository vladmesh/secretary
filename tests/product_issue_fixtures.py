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
        self._clients: dict[int, ProductBoard] = {}
        self.store = self.make_store(root=self.root)
        self.client = self._client_for(self.store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        super().tearDown()

    def make_store(
        self,
        *,
        root: Path,
        lanes: list[dict[str, object]] | None = None,
    ) -> ProductIssueStore:
        """The sole backend factory seam used by the shared contract.

        A future SQL fixture overrides this method and, where its lane disposal boundary differs,
        the lane fixture methods below.
        The shared bodies neither receive nor identify the concrete client.
        """
        client = ProductBoard()
        if lanes is not None:
            client.swimlanes = [dict(lane) for lane in lanes]
        store = ProductIssueStore(client, data_dir=root / "data", instance=root)
        self._clients[id(store)] = client
        return store

    def _client_for(self, store: ProductIssueStore) -> ProductBoard:
        return self._clients[id(store)]

    def store_with_lanes(
        self, lanes: list[dict[str, object]], *, root: Path | None = None
    ) -> ProductIssueStore:
        """Create a disposable case with the backend's lane catalogue arranged by name."""
        return self.make_store(root=root or self.root, lanes=lanes)

    def existing_project_lanes(self) -> list[dict[str, object]]:
        """The named lanes used when the product lane must be provisioned."""
        return [
            {"id": 4, "name": "secretary", "position": 1},
            {"id": 7, "name": "codegen-orchestrator", "position": 2},
            {"id": 9, "name": "service-template", "position": 3},
        ]

    # Domain observations.  SQL fixtures may override these where their disposal boundary
    # makes a more direct observation appropriate.
    def create_product(self, *, store: ProductIssueStore | None = None, **values: object) -> dict:
        return (store or self.store).create_product(**values)

    def create_issue(self, *, store: ProductIssueStore | None = None, **values: object) -> dict:
        return (store or self.store).create_issue(**values)

    def product(self, product_id: str, *, store: ProductIssueStore | None = None) -> dict:
        return (store or self.store).show_product(product_id)

    def issue(self, reference: str, *, store: ProductIssueStore | None = None) -> dict:
        return (store or self.store).show_issue(reference)

    def product_project_binding(self, product_id: str) -> list[str]:
        return list(self.product(product_id)["projects"])

    def issue_product_binding(self, reference: str) -> str:
        return str(self.issue(reference)["product"])

    def issue_comments(
        self, reference: str, *, store: ProductIssueStore | None = None
    ) -> list[str]:
        return [str(row["text"]) for row in self.issue(reference, store=store)["history"]["comments"]]

    def issue_history(self, reference: str, *, store: ProductIssueStore | None = None) -> dict:
        return dict(self.issue(reference, store=store)["history"])

    def audit_events(self, *, store: ProductIssueStore | None = None) -> list[dict]:
        return (store or self.store).audit.events()

    def request_state(self, *, store: ProductIssueStore | None = None) -> dict[str, object]:
        selected = store or self.store
        return {
            "transactions": selected.transactions.status(),
            "audit": selected.audit.status(),
        }

    def lane_binding(self, reference: str, *, store: ProductIssueStore | None = None) -> object:
        """Observe a record's lane through the backend protocol, never fake storage."""
        client = self._client_for(store or self.store)
        row = client.call("getTaskByReference", reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible: {reference}")
        lane_id = row.get("swimlane_id")
        lanes = client.call("getActiveSwimlanes", project_id=1)
        if not isinstance(lanes, list):
            self.fail("backend returned no lane list")
        return next((lane["name"] for lane in lanes if lane.get("id") == lane_id), None)

    def add_external_lane(
        self,
        lane: dict[str, object],
        *,
        store: ProductIssueStore | None = None,
        first: bool = False,
    ) -> None:
        """Arrange a lane written by another actor at the fixture boundary."""
        lanes = self._client_for(store or self.store).swimlanes
        lanes.insert(0 if first else len(lanes), dict(lane))

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
