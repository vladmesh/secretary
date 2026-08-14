from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.board.kanboard import KanboardBoardHost
from secretary.board.transitions import BoardProtocolError
from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskAudit, TaskError, TaskWriter
from tests.test_tasks import WriteKanboard
from tests.observer_identity import as_observer


class ProductBoard(WriteKanboard):
    """Kanboard fixture with the Product/Issue layout and real status filtering."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks[0]["id"] = 12

    def call(self, method: str, **params: object) -> object:
        if method == "getColumns":
            return [
                {"id": 1, "title": "Issues"}, {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"}, {"id": 6, "title": "Done"},
            ]
        if method == "getAllTasks":
            self.calls.append((method, params))
            status = params.get("status_id")
            if status == 1:
                return [task for task in self.tasks if int(task.get("is_active", 1) or 0) != 0]
            if status == 0:
                return [task for task in self.tasks if int(task.get("is_active", 1) or 0) == 0]
            return []
        return super().call(method, **params)


class LiveSwimlaneBoard(ProductBoard):
    """The live Pipeline layout: lanes named after projects, no lane of the product `codegen`.

    Kanboard refuses a create into a lane the board does not have and answers that refusal with
    `false` instead of an error, which is what the live board does for `swimlane_id=0`.
    """

    LANES = [
        {"id": 9, "name": "service-template", "position": 3},
        {"id": 4, "name": "secretary", "position": 1},
        {"id": 7, "name": "codegen-orchestrator", "position": 2},
    ]

    def __init__(self) -> None:
        super().__init__()
        self.swimlanes = [dict(lane) for lane in self.LANES]

    def call(self, method: str, **params: object) -> object:
        if method == "createTask" and params.get("swimlane_id") not in {
            int(lane["id"]) for lane in self.swimlanes
        }:
            self.calls.append((method, params))
            return False
        return super().call(method, **params)


class NoSwimlaneBoard(ProductBoard):
    """A board with no swimlane at all, the layout the earlier fixtures describe."""

    def __init__(self) -> None:
        super().__init__()
        self.swimlanes = []

    def call(self, method: str, **params: object) -> object:
        if method == "createTask" and params.get("swimlane_id") not in {
            int(lane["id"]) for lane in self.swimlanes
        }:
            self.calls.append((method, params))
            return False
        return super().call(method, **params)


class ProductIssueSwimlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _store(self, client) -> ProductIssueStore:
        return ProductIssueStore(client, data_dir=self.root / "data", instance=self.root)

    def _created(self, client, reference: str) -> dict:
        return next(task for task in client.tasks if task.get("reference") == reference)

    def _lane_names(self, client) -> dict[int, str]:
        return {int(lane["id"]): str(lane["name"]) for lane in client.swimlanes}

    def test_a_record_takes_the_lane_named_after_its_product(self) -> None:
        """Both records land in the `secretary` lane, and that lane is not the board's first.

        The rule this replaces took the board's first active lane, so it answered lane 4 here by
        coincidence of position rather than by the product.  The board is therefore reordered so
        that the coincidence cannot hold: the product lane is now last, and still chosen.
        """
        client = LiveSwimlaneBoard()
        client.swimlanes = [
            {"id": 9, "name": "service-template", "position": 1},
            {"id": 7, "name": "codegen-orchestrator", "position": 2},
            {"id": 4, "name": "secretary", "position": 3},
        ]
        store = self._store(client)

        product = store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="live-product",
        )
        issue = store.create_issue(
            product="secretary", issue_kind="bug", priority="P1", title="Crash", description="",
            actor="po", request_id="live-issue",
        )

        self.assertEqual(product["id"], "secretary")
        lanes = [params["swimlane_id"] for method, params in client.calls if method == "createTask"]
        self.assertEqual(lanes, [4, 4])
        self.assertEqual(self._created(client, "product:secretary")["swimlane_id"], 4)
        self.assertEqual(self._created(client, issue["ref"])["swimlane_id"], 4)
        # Nothing was added: the board already had the lane the product names.
        self.assertNotIn("addSwimlane", [method for method, _ in client.calls])
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})

    def test_the_lane_does_not_depend_on_swimlane_order_or_on_a_default_lane(self) -> None:
        """The same product takes the same lane whatever order the board lists, `Default` first.

        The live Pipeline board has lanes named after projects and a `Default swimlane`; the old
        rule made the record follow whichever of them happened to be first.
        """
        chosen = []
        for order in (
            [
                {"id": 1, "name": "Default swimlane", "position": 1},
                {"id": 4, "name": "secretary", "position": 2},
                {"id": 9, "name": "service-template", "position": 3},
            ],
            [
                {"id": 9, "name": "service-template", "position": 1},
                {"id": 4, "name": "secretary", "position": 2},
                {"id": 1, "name": "Default swimlane", "position": 3},
            ],
            # Board order need not agree with the positions the board reports, either.
            [
                {"id": 4, "name": "secretary", "position": 7},
                {"id": 1, "name": "Default swimlane", "position": 0},
            ],
        ):
            with self.subTest(first=order[0]["name"]), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                (root / "projects").mkdir()
                (root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
                client = LiveSwimlaneBoard()
                client.swimlanes = [dict(lane) for lane in order]
                store = ProductIssueStore(client, data_dir=root / "data", instance=root)

                store.create_product(
                    product_id="secretary", projects=["secretary"], title="Secretary",
                    description="", actor="po", request_id="ordered-product",
                )

                row = self._created(client, "product:secretary")
                self.assertEqual(self._lane_names(client)[int(row["swimlane_id"])], "secretary")
                chosen.append(int(row["swimlane_id"]))
        self.assertEqual(chosen, [4, 4, 4])

    def test_a_product_without_a_lane_gets_one_named_after_it(self) -> None:
        """`codegen` is bound to two projects and has no lane of its own on the live board.

        Its lane cannot be derived from those bindings, which is why the rule names the lane after
        the product and creates it on demand.
        """
        (self.root / "projects" / "codegen-orchestrator.yaml").write_text(
            "id: codegen-orchestrator\n", encoding="utf-8",
        )
        (self.root / "projects" / "service-template.yaml").write_text(
            "id: service-template\n", encoding="utf-8",
        )
        client = LiveSwimlaneBoard()
        store = self._store(client)

        store.create_product(
            product_id="codegen", projects=["codegen-orchestrator", "service-template"],
            title="Codegen", description="", actor="po", request_id="codegen-product",
        )
        issue = store.create_issue(
            product="codegen", issue_kind="feature", priority="P2", title="Template drift",
            description="", actor="po", request_id="codegen-issue",
        )

        added = [params["name"] for method, params in client.calls if method == "addSwimlane"]
        self.assertEqual(added, ["codegen"])
        lane = self._created(client, "product:codegen")["swimlane_id"]
        self.assertEqual(self._lane_names(client)[int(lane)], "codegen")
        # The Issue reuses the lane its product created rather than adding a second one.
        self.assertEqual(self._created(client, issue["ref"])["swimlane_id"], lane)
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})

    def test_a_board_without_swimlanes_gets_the_product_lane(self) -> None:
        client = NoSwimlaneBoard()
        store = self._store(client)

        store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="plain-product",
        )

        lane = self._created(client, "product:secretary")["swimlane_id"]
        self.assertEqual(self._lane_names(client)[int(lane)], "secretary")
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})

    def test_a_lane_another_writer_added_between_the_two_calls_is_reused(self) -> None:
        """A refused `addSwimlane` is read back, not reported: the lane exists, that is the answer."""
        client = NoSwimlaneBoard()

        original_call = client.call

        def race(method: str, **params: object) -> object:
            if method == "addSwimlane" and not client.swimlanes:
                client.swimlanes.append({"id": 21, "name": "secretary", "position": 1})
            return original_call(method, **params)

        client.call = race  # type: ignore[method-assign]
        store = self._store(client)

        store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="raced-product",
        )

        self.assertEqual(self._created(client, "product:secretary")["swimlane_id"], 21)
        self.assertEqual([lane["name"] for lane in client.swimlanes], ["secretary"])

    def test_a_repeated_delivery_lands_in_the_lane_the_first_one_chose(self) -> None:
        """The lane is a function of the record, so a redelivered create cannot move it.

        Between the two deliveries the board gains a `Default swimlane` and puts it first, which
        under the lane rule this replaces would have been the lane of the second attempt.
        """
        client = LiveSwimlaneBoard()
        store = self._store(client)
        store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="redelivered-product",
        )
        issue = store.create_issue(
            product="secretary", issue_kind="bug", priority="P0", title="Crash",
            description="", actor="po", request_id="redelivered-issue",
        )

        client.swimlanes.insert(0, {"id": 33, "name": "Default swimlane", "position": 0})
        again = store.create_issue(
            product="secretary", issue_kind="bug", priority="P0", title="Crash",
            description="", actor="po", request_id="redelivered-issue",
        )
        product_again = store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="redelivered-product",
        )

        self.assertEqual(again["ref"], issue["ref"])
        self.assertEqual(product_again["id"], "secretary")
        rows = [task for task in client.tasks if task.get("reference") in {issue["ref"], "product:secretary"}]
        self.assertEqual(sorted(int(row["swimlane_id"]) for row in rows), [4, 4])
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})

    def test_a_retried_staged_create_takes_the_lane_of_its_staged_product(self) -> None:
        """The staged transaction writer reads the product from its own intent, not from the board.

        A create staged before the board was reordered therefore finishes in the same lane its
        first attempt would have chosen.
        """
        client = LiveSwimlaneBoard()
        store = self._store(client)
        intent = {
            "record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]',
            "title": "Secretary", "description": "", "actor": "po",
        }
        event = store._transaction_event(
            kind="product_created", actor="po", reference="product:secretary",
            request_id="staged", intent=intent,
        )
        store.transactions.begin("staged", kind="product_created", intent=intent, event=event)
        client.swimlanes.insert(0, {"id": 33, "name": "Default swimlane", "position": 0})

        product = store.retry_transaction("staged")

        self.assertEqual(product["id"], "secretary")
        self.assertEqual(self._created(client, "product:secretary")["swimlane_id"], 4)
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})

    def test_refused_create_is_terminal_and_leaves_no_transaction_behind(self) -> None:
        class RefusingBoard(ProductBoard):
            def call(self, method: str, **params: object) -> object:
                if method == "createTask":
                    self.calls.append((method, params))
                    return False
                return super().call(method, **params)

        client = RefusingBoard()
        store = self._store(client)

        for attempt in ("first", "second"):
            with self.subTest(attempt=attempt), self.assertRaises(TaskError) as raised:
                store.create_product(
                    product_id="secretary", projects=["secretary"], title="Secretary", description="",
                    actor="po", request_id="refused",
                )
            self.assertEqual(raised.exception.code, "backend_rejected")

        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(store.list_transactions(), [])
        self.assertEqual(store.audit.events(), [])
        self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})

    def test_nonpositive_create_reply_with_a_marker_is_repaired_without_a_second_create(self) -> None:
        client = ProductBoard()
        store = self._store(client)
        original_call = client.call
        rejected_metadata = False

        def null_reply_after_marker(method: str, **params: object) -> object:
            nonlocal rejected_metadata
            result = original_call(method, **params)
            if method == "createTask":
                # Exercise marker correlation independently of the requested
                # reference: a reply cannot prove that this row was refused.
                client.tasks[-1]["reference"] = ""
                return None
            if method == "saveTaskMetadata" and not rejected_metadata:
                rejected_metadata = True
                return False
            return result

        client.call = null_reply_after_marker  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="null-marker",
            )

        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(store.list_transactions(), [{
            "request_id": "null-marker", "kind": "entity.created", "ref": "product:secretary",
            "progress": ["typed_event"],
        }])
        product = store.retry_transaction("null-marker")
        self.assertEqual(product["id"], "secretary")
        self.assertEqual(len([call for call in client.calls if call[0] == "createTask"]), 1)
        self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})

    def test_nonpositive_create_proof_failures_keep_product_and_issue_pending(self) -> None:
        """Uncertain correlations are post-write evidence, never a discard signal."""
        for entity_kind in ("product", "issue"):
            for failure in ("transport", "json_rpc", "malformed_list", "ambiguous_marker"):
                with self.subTest(entity_kind=entity_kind, failure=failure), tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    (root / "projects").mkdir()
                    (root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
                    client = ProductBoard()
                    store = ProductIssueStore(client, data_dir=root / "data", instance=root)
                    if entity_kind == "issue":
                        store.create_product(
                            product_id="secretary", projects=["secretary"], title="Secretary",
                            description="", actor="po", request_id=f"seed-{failure}",
                        )
                    original_call = client.call
                    created = False
                    fault_active = True
                    duplicate_id: int | None = None

                    def fail_proof(method: str, **params: object) -> object:
                        nonlocal created, duplicate_id
                        if created and fault_active:
                            if failure == "transport" and method == "getProjectByName":
                                raise TaskError("backend_unavailable", "proof transport lost", 1)
                            if failure == "json_rpc" and method == "getProjectByName":
                                raise TaskError("backend_error", "proof RPC failed", 1)
                            if failure == "malformed_list" and method == "getAllTasks":
                                return {"not": "a task list"}
                        result = original_call(method, **params)
                        if method == "createTask" and not created:
                            created = True
                            row = client.tasks[-1]
                            row["reference"] = ""
                            if failure == "ambiguous_marker":
                                duplicate_id = max(int(task["id"]) for task in client.tasks) + 1
                                duplicate = dict(row, id=duplicate_id)
                                client.tasks.append(duplicate)
                                client.metadata[duplicate_id] = {}
                                client.comments[duplicate_id] = []
                            return None
                        return result

                    client.call = fail_proof  # type: ignore[method-assign]
                    request_id = f"proof-{entity_kind}-{failure}"
                    reference = (
                        "product:secretary" if entity_kind == "product"
                        else "issue:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]
                    )
                    before = len([call for call in client.calls if call[0] == "createTask"])
                    with self.assertRaises(TaskError) as raised:
                        if entity_kind == "product":
                            store.create_product(
                                product_id="secretary", projects=["secretary"], title="Secretary",
                                description="", actor="po", request_id=request_id,
                            )
                        else:
                            store.create_issue(
                                product="secretary", issue_kind="bug", priority="P2", title="Crash",
                                description="", actor="po", request_id=request_id,
                            )
                    self.assertEqual(raised.exception.code, "audit_pending")
                    self.assertEqual(store.list_transactions(), [{
                        "request_id": request_id, "kind": "entity.created", "ref": reference,
                        "progress": ["typed_event"],
                    }])

                    fault_active = False
                    if duplicate_id is not None:
                        client.tasks[:] = [task for task in client.tasks if task["id"] != duplicate_id]
                        client.metadata.pop(duplicate_id, None)
                        client.comments.pop(duplicate_id, None)
                    repaired = store.retry_transaction(request_id)
                    self.assertEqual(repaired["ref"], reference)
                    self.assertEqual(len([call for call in client.calls if call[0] == "createTask"]), before + 1)
                    self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})

    def test_failing_swimlane_lookup_discards_product_and_issue_create(self) -> None:
        """A pre-create lane lookup failure has no uncertain backend write to retain."""
        for entity_kind in ("product", "issue"):
            for failure in ("transport", "json_rpc", "malformed_list", "semantic"):
                with self.subTest(entity_kind=entity_kind, failure=failure), tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    (root / "projects").mkdir()
                    (root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
                    client = ProductBoard()
                    store = ProductIssueStore(client, data_dir=root / "data", instance=root)
                    if entity_kind == "issue":
                        store.create_product(
                            product_id="secretary", projects=["secretary"], title="Secretary",
                            description="", actor="po", request_id=f"seed-lane-{failure}",
                        )
                    original_call = client.call
                    fault_active = True

                    def fail_swimlane(method: str, **params: object) -> object:
                        if fault_active and method == "getActiveSwimlanes":
                            if failure == "transport":
                                raise TaskError("backend_unavailable", "swimlane transport lost", 1)
                            if failure == "json_rpc":
                                raise TaskError("backend_error", "swimlane RPC failed", 1)
                            if failure == "malformed_list":
                                return {"not": "a swimlane list"}
                        return original_call(method, **params)

                    client.call = fail_swimlane  # type: ignore[method-assign]
                    request_id = f"swimlane-{entity_kind}-{failure}"
                    reference = (
                        "product:secretary" if entity_kind == "product"
                        else "issue:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]
                    )

                    def create() -> dict:
                        if entity_kind == "product":
                            return store.create_product(
                                product_id="secretary", projects=["secretary"], title="Secretary",
                                description="", actor="po", request_id=request_id,
                            )
                        return store.create_issue(
                            product="secretary", issue_kind="bug", priority="P2", title="Crash",
                            description="", actor="po", request_id=request_id,
                        )

                    semantic_failure = mock.patch.object(
                        KanboardBoardHost, "_issues_swimlane",
                        side_effect=BoardProtocolError("Pipeline swimlane lookup is invalid"),
                    ) if failure == "semantic" else contextlib.nullcontext()
                    before = len([call for call in client.calls if call[0] == "createTask"])
                    with semantic_failure, self.assertRaises(TaskError):
                        create()
                    self.assertEqual(len([call for call in client.calls if call[0] == "createTask"]), before)
                    self.assertEqual(store.list_transactions(), [])
                    self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})

                    fault_active = False
                    created = create()
                    self.assertEqual(created["ref"], reference)
                    self.assertEqual(len([call for call in client.calls if call[0] == "createTask"]), before + 1)
                    self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})

    def test_staged_transaction_without_a_backend_row_is_discarded_by_the_operator(self) -> None:
        client = LiveSwimlaneBoard()
        store = self._store(client)
        intent = {
            "record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]',
            "title": "Secretary", "description": "", "actor": "po",
        }
        event = store._transaction_event(
            kind="product_created", actor="po", reference="product:secretary",
            request_id="stuck", intent=intent,
        )
        document, _ = store.transactions.begin("stuck", kind="product_created", intent=intent, event=event)
        document["progress"] = {"create_started": True}
        store.transactions.save(document)
        self.assertEqual(store.transactions.status(), {"ok": False, "pending": 1})
        self.assertEqual(
            store.list_transactions(),
            [{"request_id": "stuck", "kind": "product_created", "ref": "product:secretary",
              "progress": ["create_started"]}],
        )

        self.assertEqual(store.discard_transaction("stuck"), {"request_id": "stuck", "discarded": True})

        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(store.audit.events(), [])

    def test_discard_refuses_a_transaction_that_already_wrote_to_the_board(self) -> None:
        client = LiveSwimlaneBoard()
        store = self._store(client)
        original_call = client.call
        rejected = False

        def reject_metadata_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "saveTaskMetadata" and not rejected:
                rejected = True
                client.calls.append((method, params))
                return False
            return original_call(method, **params)

        client.call = reject_metadata_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="half-written",
            )
        self.assertEqual(raised.exception.code, "audit_pending")

        with self.assertRaises(TaskError) as refused:
            store.discard_transaction("half-written")
        self.assertEqual(refused.exception.code, "live_write")
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(store.audit.status(), {"ok": False, "pending": 1})

        product = store.retry_transaction("half-written")

        self.assertEqual(product["id"], "secretary")
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual([entry["kind"] for entry in store.audit.events()], ["entity.created"])

    def test_quarantined_document_returns_through_adopt_and_retry(self) -> None:
        client = LiveSwimlaneBoard()
        store = self._store(client)
        intent = {
            "record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]',
            "title": "Secretary", "description": "", "actor": "po",
        }
        event = store._transaction_event(
            kind="product_created", actor="po", reference="product:secretary",
            request_id="quarantined", intent=intent,
        )
        store.transactions.begin("quarantined", kind="product_created", intent=intent, event=event)
        staged = next((self.root / "data" / "board" / "product-issue-transactions").glob("v1-*.json"))
        quarantine = self.root / "quarantine"
        quarantine.mkdir()
        carried = quarantine / staged.name
        carried.write_text(staged.read_text(encoding="utf-8"), encoding="utf-8")
        staged.unlink()

        with mock.patch("secretary.product_issue_commands.KanboardClient.for_instance", return_value=client):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                adopted = main([
                    "product", "transaction", "adopt", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"), "--path", str(carried),
                ])
            self.assertEqual(adopted, 0)
            self.assertEqual(json.loads(output.getvalue())["request_id"], "quarantined")
            self.assertFalse(carried.exists())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                retried = main([
                    "product", "transaction", "retry", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"), "--request-id", "quarantined",
                ])
            self.assertEqual(retried, 0)
            self.assertEqual(json.loads(output.getvalue())["id"], "secretary")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([
                    "product", "transaction", "list", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"),
                ]), 0)
            self.assertEqual(json.loads(output.getvalue()), [])
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual([entry["kind"] for entry in store.audit.events()], ["product_created"])


class ProductIssueStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
        self.client = ProductBoard()
        self.store = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_released_writes_publish_complete_typed_product_issue_events(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
            request_id="typed-product",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
            request_id="typed-issue",
        )
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="typed-priority",
        )
        self.store.close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="typed-close",
        )
        events = self.store.audit.events()
        self.assertEqual([event["kind"] for event in events], ["entity.created", "entity.created", "entity.updated", "issue.closed"])
        self.assertTrue(all(event["record_type"] == "board.protocol_event" for event in events))
        self.assertEqual(events[1]["related_refs"], ["product:secretary"])
        self.assertEqual(events[-1]["transition"], {"source": "open", "target": "closed"})
        self.assertEqual(events[-1]["data"]["close_reason"], "resolved")

    def test_product_and_issue_lists_use_complete_set_and_show_audit_history(self) -> None:
        product = self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="product-create",
        )
        self.assertEqual(product["id"], "secretary")
        self.assertEqual([item["id"] for item in self.store.list_products()], ["secretary"])

        issue = self.store.create_issue(
            product="secretary", issue_kind="feature", priority="P2", title="Foundation",
            description="", actor="po", request_id="issue-create",
        )
        self.store.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="priority",
        )
        self.store.close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="close",
        )

        self.assertEqual(self.store.list_issues(), [])
        self.assertEqual([item["ref"] for item in self.store.list_issues(include_closed=True)], [issue["ref"]])
        shown = self.store.show_issue(issue["ref"])
        self.assertTrue(shown["closed"])
        self.assertEqual(shown["close_reason"], "resolved")
        self.assertIn("[issue:priority]\nurgent\n[request-id:priority]", [entry["text"] for entry in shown["history"]["comments"]])
        self.assertEqual(
            [entry["kind"] for entry in shown["history"]["audit"]],
            ["entity.created", "entity.updated", "issue.closed"],
        )
        status_ids = [params.get("status_id") for method, params in self.client.calls if method == "getAllTasks"]
        self.assertIn(1, status_ids)
        self.assertIn(0, status_ids)
        self.assertNotIn(2, status_ids)

    def test_issue_needs_all_required_values_and_archive_cannot_bypass_close(self) -> None:
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="", issue_kind="feature", priority="P2", title="x", description="", actor="po",
            )
        self.assertEqual(raised.exception.code, "validation")

        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P0", title="Crash", description="", actor="po",
        )
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        with self.assertRaises(TaskError) as raised:
            writer.archive(role="po", actor="po", reference=issue["ref"], reason="bypass")
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(method == "closeTask" for method, _ in self.client.calls))

    def test_issue_close_has_one_terminal_reason_and_audit_event(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P0", title="Crash", description="", actor="po",
        )
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")

        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="invalid", actor="po", request_id="retry")

        self.assertEqual(raised.exception.code, "closed")
        shown = self.store.show_issue(issue["ref"])
        self.assertEqual(shown["close_reason"], "resolved")
        self.assertEqual([event["kind"] for event in shown["history"]["audit"]], ["entity.created", "issue.closed"])

    def test_issue_and_task_column_guards_are_fail_closed(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="question", priority="P3", title="Question", description="", actor="po",
        )
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        # The observer is a bound head here: what is being tested is the column guard, and an
        # observer nobody bound never reaches it.
        with as_observer("sprint:issues"):
            for role in ("po", "dispatcher", "worker", "reviewer", "steward", "retro", "observer"):
                for target in ("ready", "in_progress", "validate", "blocked", "done"):
                    with self.subTest(role=role, target=target), self.assertRaises(TaskError) as raised:
                        writer.move(role=role, actor=role, reference=issue["ref"], target=target, reason="")
                    self.assertEqual(raised.exception.code, "transition_forbidden")
        with self.assertRaises(TaskError) as raised:
            writer.create(
                role="steward", actor="steward", project="secretary", task_type="research",
                title="Wrong column", target="issues",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")

    def test_claim_rejects_a_product_or_issue_record_without_any_write(self) -> None:
        # A record dragged into Ready by hand still is not an execution task: claim has to reject
        # it the way move does, before the claim metadata, the move and the audit row.
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        for record_type in ("product", "issue"):
            with self.subTest(record_type=record_type):
                self.client.tasks[0]["column_id"] = 2
                self.client.metadata[12] = {
                    "record_type": record_type, "project": "secretary", "task_type": "code", "claim": "",
                }
                self.client.calls.clear()

                with self.assertRaises(TaskError) as raised:
                    writer.claim(
                        role="dispatcher", actor="d", reference="secretary-468",
                        worker="secretary-468-runtime", request_id=f"claim-{record_type}",
                    )

                self.assertEqual(raised.exception.code, "transition_forbidden")
                self.assertIn("cannot enter execution task columns", str(raised.exception))
                self.assertEqual(self.client.metadata[12]["claim"], "")
                self.assertFalse(any(
                    method in {"saveTaskMetadata", "moveTaskPosition"} for method, _ in self.client.calls
                ))
                self.assertEqual(writer.audit.status(), {"ok": True, "pending": 0})
                self.assertFalse(Path(writer.audit.events_path).exists())

    def test_pending_claim_replay_and_reconcile_refuse_a_product_or_issue(self) -> None:
        # The supported partial-write state of a pre-migration generic claim: the claim metadata
        # committed and the column move was lost. New claims use a typed event and discard it on a
        # failed move, so build the released generic pending row explicitly. Neither the retry
        # nor reconcile may finish that old move once the card is a Product or an Issue.
        for record_type in ("product", "issue"):
            with self.subTest(record_type=record_type):
                writer = TaskWriter(self.client, data_dir=self.root / f"data-{record_type}")
                self.client.tasks[0]["column_id"] = 2
                self.client.metadata[12] = {"project": "secretary", "task_type": "code", "claim": ""}
                request_id = f"pending-claim-{record_type}"
                claim = dict(
                    role="dispatcher", actor="d", reference="secretary-468",
                    worker="replayed-worker", request_id=request_id,
                )
                writer.audit.stage(request_id, {
                    "request_id": request_id,
                    "event_id": f"legacy-{request_id}",
                    "kind": "claimed",
                    "ref": "secretary-468",
                    "payload": {
                        "worker": "replayed-worker", "resolved_head": None,
                        "resolved_review_head": None, "slug": None, "base_branch": None,
                        "cap": 3,
                    },
                })
                self.client.metadata[12]["claim"] = "replayed-worker"
                self.client.fail_move = True
                with self.assertRaisesRegex(TaskError, "audit repair"):
                    writer.claim(**claim)
                self.assertEqual(writer.audit.status(), {"ok": False, "pending": 1})

                self.client.fail_move = False
                self.client.metadata[12]["record_type"] = record_type
                self.client.calls.clear()

                with self.assertRaises(TaskError) as raised:
                    writer.claim(**claim)

                self.assertEqual(raised.exception.code, "transition_forbidden")
                self.assertEqual(writer.reader.show("secretary-468")["state"], "ready")
                self.assertFalse(any(method == "moveTaskPosition" for method, _ in self.client.calls))
                self.assertEqual(writer.reconcile(), (0, 1))
                self.assertEqual(writer.reader.show("secretary-468")["state"], "ready")
                self.assertEqual(writer.audit.status(), {"ok": False, "pending": 1})
        self.client.fail_move = False

    def test_a_card_in_issues_reaches_ready_only_through_the_po(self) -> None:
        """Issues is the untriaged backlog: the steward matrix has no exit from it."""
        self.client.tasks[0]["column_id"] = 1
        self.client.metadata[12] = {"record_type": "task"}
        writer = TaskWriter(self.client, data_dir=self.root / "data")

        with self.assertRaises(TaskError) as raised:
            writer.move(
                role="steward", actor="steward", reference="secretary-468", target="ready", reason="",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertEqual(writer.reader.show("secretary-468")["state"], "issues")

        writer.move(role="po", actor="po", reference="secretary-468", target="ready", reason="")
        self.assertEqual(writer.reader.show("secretary-468")["state"], "ready")

    def test_missing_issue_arguments_are_structured(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["issue", "create", "--role", "po"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "usage")

    def test_rejected_product_metadata_stays_pending_until_same_request_repairs_it(self) -> None:
        class RejectMetadataOnce(ProductBoard):
            rejected = False

            def call(self, method: str, **params: object) -> object:
                if method == "saveTaskMetadata" and not self.rejected:
                    self.rejected = True
                    self.calls.append((method, params))
                    return False
                return super().call(method, **params)

        store = ProductIssueStore(RejectMetadataOnce(), data_dir=self.root / "data", instance=self.root)
        with self.assertRaises(TaskError) as raised:
            store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(store.audit.events(), [])
        self.assertEqual(store.audit.status(), {"ok": False, "pending": 1})
        # Generic reconciliation sees but cannot publish a typed owner.
        self.assertEqual(TaskWriter(store.client, data_dir=self.root / "data").reconcile(), (0, 1))
        product = store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product",
        )
        self.assertEqual(product["id"], "secretary")
        self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual([event["kind"] for event in store.audit.events()], ["entity.created"])

    def test_typed_pending_is_listed_with_its_repair_identity(self) -> None:
        original_call = self.client.call
        rejected = False

        def reject_metadata_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "saveTaskMetadata" and not rejected:
                rejected = True
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_metadata_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="listed-pending",
            )

        self.assertEqual(self.store.list_transactions(), [{
            "request_id": "listed-pending", "kind": "entity.created", "ref": "product:secretary",
            "progress": ["typed_event"],
        }])
        self.assertEqual(self.store.retry_transaction("listed-pending")["id"], "secretary")

    def test_rejected_issue_metadata_stays_pending_until_same_request_repairs_it(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        original_call = self.client.call
        rejected = False

        def reject_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "saveTaskMetadata" and not rejected:
                rejected = True
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.store.audit.status(), {"ok": False, "pending": 1})
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue",
        )
        self.assertEqual(issue["kind"], "bug")
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})

    def test_rejected_issue_metadata_does_not_claim_a_priority_or_close_change(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call

        def reject_metadata(method: str, **params: object) -> object:
            if method == "saveTaskMetadata":
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_metadata  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P2")
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po")
        self.assertEqual(raised.exception.code, "audit_pending")
        shown = self.store.show_issue(issue["ref"])
        self.assertFalse(shown["closed"])
        self.assertIsNone(shown["close_reason"])
        self.assertEqual([event["kind"] for event in shown["history"]["audit"]], ["entity.created"])

    def test_refused_close_comment_leaves_no_typed_occurrence(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call
        rejected = False

        def reject_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "createComment" and not rejected:
                rejected = True
                self.client.calls.append((method, params))
                return 0
            return original_call(method, **params)

        self.client.call = reject_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertEqual(raised.exception.code, "backend_rejected")
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})
        self.assertFalse(self.store.show_issue(issue["ref"])["closed"])
        closed = self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["close_reason"], "resolved")
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})

    def test_refused_priority_comment_leaves_no_typed_occurrence(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call

        def reject_comment(method: str, **params: object) -> object:
            if method == "createComment":
                self.client.calls.append((method, params))
                return 0
            return original_call(method, **params)

        self.client.call = reject_comment  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po")
        self.assertEqual(raised.exception.code, "backend_rejected")
        shown = self.store.show_issue(issue["ref"])
        self.assertEqual(shown["priority"], "P2")
        self.assertEqual([event["kind"] for event in shown["history"]["audit"]], ["entity.created"])

    def test_all_operations_restart_without_duplicate_backend_writes(self) -> None:
        class LoseReplyOnce(ProductBoard):
            lost: set[str] = set()

            def call(self, method: str, **params: object) -> object:
                result = super().call(method, **params)
                if method in {"saveTaskMetadata", "createComment", "closeTask"} and method not in self.lost:
                    self.lost.add(method)
                    raise TaskError("backend_unavailable", "reply lost", 1)
                return result

        client = LoseReplyOnce()
        store = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root)
        with self.assertRaises(TaskError):
            store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product-restart")
        product = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product-restart"
        )
        self.assertEqual(product["id"], "secretary")
        client.lost.clear()
        with self.assertRaises(TaskError):
            store.create_issue(product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue-restart")
        issue = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue-restart"
        )
        store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority-restart")
        ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority-restart"
        )
        with self.assertRaises(TaskError):
            store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close-restart")
        closed = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="close-restart"
        )
        self.assertTrue(closed["closed"])
        self.assertEqual(len([call for call in client.calls if call[0] == "createTask"]), 2)
        self.assertEqual(len(client.comments[int(next(task for task in client.tasks if task["reference"] == issue["ref"])["id"])]), 2)
        self.assertEqual([event["kind"] for event in store.audit.events()], ["entity.created", "entity.created", "entity.updated", "issue.closed"])

    def test_request_id_conflicts_are_rejected_before_a_second_write(self) -> None:
        self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product")
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(product_id="secretary", projects=["secretary"], title="Changed", description="", actor="po", request_id="product")
        self.assertEqual(raised.exception.code, "validation")
        issue = self.store.create_issue(product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue")
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(product="secretary", issue_kind="bug", priority="P2", title="Changed", description="", actor="po", request_id="issue")
        self.assertEqual(raised.exception.code, "validation")
        self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority")
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="changed", actor="po", request_id="priority")
        self.assertEqual(raised.exception.code, "validation")
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="invalid", actor="po", request_id="close")
        self.assertEqual(raised.exception.code, "validation")

    def test_committed_priority_replay_survives_a_later_close(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority",
        )
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")

        replayed = self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority",
        )

        self.assertTrue(replayed["closed"])
        self.assertEqual(replayed["priority"], "P0")

    def test_priority_update_on_a_closed_issue_preserves_closed_refusal(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po")

        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po")

        self.assertEqual(raised.exception.code, "closed")
        self.assertEqual(raised.exception.exit_code, 3)

    def test_generic_reconcile_leaves_product_issue_transaction_for_its_owner(self) -> None:
        original_call = self.client.call
        failed = False

        def fail_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "saveTaskMetadata" and not failed:
                failed = True
                return False
            return original_call(method, **params)

        self.client.call = fail_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="isolated")
        self.assertEqual(TaskWriter(self.client, data_dir=self.root / "data").reconcile(), (0, 1))
        self.assertEqual(self.store.audit.events(), [])
        self.assertEqual(self.store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(self.store.audit.status(), {"ok": False, "pending": 1})

    def test_existing_product_retry_uses_its_staged_projects_before_the_registry(self) -> None:
        original_call = self.client.call
        failed = False

        def fail_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "saveTaskMetadata" and not failed:
                failed = True
                return False
            return original_call(method, **params)

        self.client.call = fail_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="registry")
        (self.root / "projects" / "secretary.yaml").unlink()
        product = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="registry"
        )
        self.assertEqual(product["id"], "secretary")

    def test_committed_request_replay_does_not_repeat_the_completed_operation(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="cleanup",
        )
        self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="cleanup")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)
        self.assertEqual([event["kind"] for event in self.store.audit.events()], ["entity.created"])

    def test_request_id_never_becomes_a_pending_filename_and_generic_upgrade_is_fail_closed(self) -> None:
        self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="../../outside")
        names = [path.name for path in (self.root / "data" / "board").rglob("*.json")]
        self.assertTrue(all("outside" not in name for name in names))
        pending = self.root / "data" / "board" / "pending-audit"
        pending.mkdir(exist_ok=True)
        (pending / "old-generic.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(TaskError) as raised:
            self.store.audit.status()
        self.assertEqual(raised.exception.code, "upgrade_required")

    def test_generic_pending_request_id_blocks_product_before_backend_write(self) -> None:
        generic = {
            "event_id": "generic-shared", "request_id": "shared", "kind": "commented",
            "payload": {"body_sha256": "x"}, "ref": "secretary-468",
        }
        TaskAudit(self.root / "data").stage("shared", generic)
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="shared",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(TaskAudit(self.root / "data").pending_event("shared"), generic)
        self.assertFalse(any(method == "createTask" for method, _ in self.client.calls))

    def test_create_reply_loss_is_correlated_and_repaired_without_a_duplicate_row(self) -> None:
        original_call = self.client.call
        failed = False

        def lose_create_reply(method: str, **params: object) -> object:
            nonlocal failed
            result = original_call(method, **params)
            if method == "createTask" and not failed:
                failed = True
                raise TaskError("backend_error", "lost create reply", 1)
            return result

        self.client.call = lose_create_reply  # type: ignore[method-assign]
        # The confirming read proves the uniquely referenced row, so a lost
        # reply does not force a second invocation or a synthetic failure.
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="lost-create",
        )
        product = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="lost-create",
        )
        self.assertEqual(product["id"], "secretary")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)

    def test_new_issue_operation_rejects_until_an_older_pending_priority_is_repaired(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call
        failed = False

        def reject_priority_metadata_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "saveTaskMetadata" and not failed:
                failed = True
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_priority_metadata_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.update_priority(
                reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority",
            )
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority",
        )
        closed = self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["priority"], "P0")
        self.assertEqual(
            [event["kind"] for event in self.store.audit.events() if event.get("ref") == issue["ref"]],
            ["entity.created", "entity.updated", "issue.closed"],
        )

    def test_reference_repair_after_create_reply_persists_the_backend_id_first(self) -> None:
        original_call = self.client.call
        rejected: set[str] = set()

        def reject_reference_once(method: str, **params: object) -> object:
            reference = params.get("reference")
            if method == "updateTask" and isinstance(reference, str) and reference not in rejected:
                rejected.add(reference)
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_reference_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
                request_id="product-reference-repair",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        product = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
            request_id="product-reference-repair",
        )
        self.assertEqual(product["id"], "secretary")
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="secretary", issue_kind="bug", priority="P2", title="Crash", description="",
                actor="po", request_id="reference-repair",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        issue = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="",
            actor="po", request_id="reference-repair",
        )
        self.assertEqual(issue["kind"], "bug")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 2)

    def test_pending_product_identity_blocks_a_second_request_before_create(self) -> None:
        request_id = "first-product"
        intent = {
            "record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]',
            "title": "Secretary", "description": "", "actor": "po",
        }
        event = self.store._transaction_event(
            kind="product_created", actor="po", reference="product:secretary", request_id=request_id, intent=intent,
        )
        self.store.transactions.begin(request_id, kind="product_created", intent=intent, event=event)
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Other", description="", actor="po",
                request_id="second-product",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertFalse(any(method == "createTask" for method, _ in self.client.calls))

    def test_pending_priority_blocks_a_second_priority_before_backend_mutation(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call
        failed = False

        def reject_metadata_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "saveTaskMetadata" and not failed:
                failed = True
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_metadata_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.update_priority(
                reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="first-priority",
            )
        comments = len([call for call in self.client.calls if call[0] == "createComment"])
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(
                reference=issue["ref"], priority="P3", reason="later", actor="po", request_id="second-priority",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createComment"]), comments)
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="first-priority",
        )
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P0")

    def test_generic_upgrade_gate_runs_before_product_mutation(self) -> None:
        pending = self.root / "data" / "board" / "pending-audit"
        pending.mkdir(parents=True)
        (pending / "old-generic.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="upgrade",
            )
        self.assertEqual(raised.exception.code, "upgrade_required")
        self.assertFalse(any(method == "createTask" for method, _ in self.client.calls))


if __name__ == "__main__":
    unittest.main()
