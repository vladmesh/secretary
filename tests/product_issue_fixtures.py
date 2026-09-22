"""The Product/Issue contract's setup and observations, over a real PostgreSQL store.

Products and Issues have one implementation, PostgreSQL (secretary-1670), so every store below is
a migrated database of the test's own (`tests.sql_backend_fixtures.PostgresBoard.shared`).
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path

from secretary.board.sql_cards import SqlCardClient
from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskError
from tests.sql_backend_fixtures import PostgresBoard


class ProductIssueFixture:
    """Setup and observations for the Product/Issue contract.

    Test bodies use ProductIssueStore operations and the observations below.
    """

    def setUp(self) -> None:
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
        self._clients: dict[int, SqlCardClient] = {}
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
        """A store over an empty database of its own, with this lane catalogue arranged by name."""
        board = PostgresBoard.shared()
        config = board.fresh_database()
        client = SqlCardClient(config.for_role("app"), root)
        client._lanes = sorted(str(lane["name"]) for lane in (lanes or []))
        store = ProductIssueStore(client, data_dir=root / "data", instance=root)
        self._clients[id(store)] = client

        def dispose() -> None:
            client.close()
            board.release_database(config.dbname)

        self.addCleanup(dispose)  # type: ignore[attr-defined]
        return store

    def _client_for(self, store: ProductIssueStore) -> SqlCardClient:
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

    def record_count(self, reference: str, *, store: ProductIssueStore | None = None) -> int:
        """Count persisted rows, even when their Product/Issue projection is incomplete."""
        client = self._client_for(store or self.store)
        kind, identifier = reference.split(":", 1)
        table, column = ("products", "product_id") if kind == "product" else ("issues", "issue_id")
        return int(client._query(f"SELECT count(*) FROM {table} WHERE {column} = %s", (identifier,))[0][0])

    def lane_binding(self, reference: str, *, store: ProductIssueStore | None = None) -> object:
        """Observe a record's lane through the backend protocol."""
        client = self._client_for(store or self.store)
        row = client.call("getTaskByReference", project_id=1, reference=reference)
        if not isinstance(row, dict):
            self.fail(f"record is not visible: {reference}")  # type: ignore[attr-defined]
        lanes = client.call("getActiveSwimlanes", project_id=1)
        return next((lane["name"] for lane in lanes if lane["id"] == row["swimlane_id"]), None)

    def add_external_lane(
        self,
        lane: dict[str, object],
        *,
        store: ProductIssueStore | None = None,
        first: bool = False,
    ) -> None:
        """Arrange a lane written by another actor at the fixture boundary."""
        names = self._client_for(store or self.store)._lane_names()
        names.insert(0 if first else len(names), str(lane["name"]))

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

        def call(method_name: str, **params: object) -> object:
            nonlocal armed
            if armed and method_name == method:
                armed = False
                if error is not None:
                    raise error
                return result
            return original(method_name, **params)

        self.client.call = call  # type: ignore[method-assign]
        try:
            yield
        finally:
            self.client.call = original  # type: ignore[method-assign]

    def assert_product_absent(self, product_id: str) -> None:
        with self.assertRaises(TaskError) as raised:
            self.product(product_id)
        self.assertEqual(raised.exception.code, "not_found")
